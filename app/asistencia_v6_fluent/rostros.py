#!/usr/bin/env python3
"""
Centinela Control — Rostros v6 (asistencia y vigilancia de aula)

Detección y reconocimiento de MÚLTIPLE rostro al mismo tiempo.

Detecta caras con detector_seg2 (modelo propio, WIDER FACE) y extrae
embeddings 512-d con InceptionResnetV1 (FaceNet, preentrenado en VGGFace2,
nunca entrenado con caras de estudiantes).

Flujo por cada cara detectada:
  recorte con margen -> FaceNet -> embedding 512-d
  embedding vs foto de referencia -> similitud coseno -> identidad

Cambios respecto a v5:
  - Las cargas de modelos están protegidas con un lock: en v6 por primera
    vez puede haber dos hilos (cámara + regeneración de embeddings)
    tocando los singletons a la vez.

Requisitos:
  - models/ocular_v2/detector_seg2/best.pth (detector facial propio)
  - facenet-pytorch (InceptionResnetV1 con pesos vggface2)
  - torch, torchvision, opencv, numpy
"""

import os, sys, threading, cv2, numpy as np, torch

# --- Detector alternativo para pruebas ---
# USAR_MEDIAPIPE = True  -> usa MediaPipe Face Detection (mas rapido, mas robusto)
# USAR_MEDIAPIPE = False -> usa detector_seg2 propio (modelo original, comentado abajo)
USAR_MEDIAPIPE = False
try:
    import mediapipe as _mp
    _MEDIAPIPE_OK = True
except Exception:
    _MEDIAPIPE_OK = False
    USAR_MEDIAPIPE = False

# ---- rutas del proyecto (relativas a este archivo) ----
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DETECTOR_PTH = os.path.join(_ROOT, "models", "ocular_v2", "detector_seg2", "best.pth")

# ---- config del detector (misma que el entrenador) ----
IN_W, IN_H = 640, 480
STRIDES = [8, 16, 32]
ANCHORS = {8: [16, 32, 64], 16: [32, 64, 128], 32: [64, 128, 256]}
MEAN = torch.tensor([0.485, 0.456, 0.406]).view(3, 1, 1)
STD = torch.tensor([0.229, 0.224, 0.225]).view(3, 1, 1)

# cara mínima aceptable (px en imagen original)
MIN_CARA_PX = 60

_device = None
_detw = None
_modelo_det = None
_modelo_emb = None
_lock_modelos = threading.Lock()


def _cargar_detw():
    """Carga el módulo del entrenador (mismo patrón que app/demo/probar_detector_webcam.py)."""
    global _detw
    if _detw is not None:
        return _detw
    ent_dir = os.path.join(_ROOT, "app", "entrenamiento")
    sys.path.insert(0, ent_dir)
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("detw",
                                         os.path.join(ent_dir, "entrenar_detector_facial_wider.py"))
    _detw = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_detw)
    return _detw


def _device_actual():
    global _device
    if _device is None:
        _device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return _device


def cargar_detector():
    """Carga detector_seg2 en dispositivo disponible. Devuelve (detw, modelo, device)."""
    global _modelo_det
    if _modelo_det is not None:
        return _detw, _modelo_det, _device_actual()
    with _lock_modelos:
        if _modelo_det is not None:  # doble chequeo: otro hilo pudo cargar
            return _detw, _modelo_det, _device_actual()
        detw = _cargar_detw()
        modelo = detw.Detector().to(_device_actual())
        saved = torch.load(_DETECTOR_PTH, map_location="cpu")
        modelo.load_state_dict(saved["model_state_dict"])
        modelo.eval()
        _modelo_det = modelo
    return _detw, _modelo_det, _device_actual()


def preprocess_det(frame_bgr):
    """BGR -> tensor normalizado listo para el detector. Devuelve (tensor, w0, h0)."""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h0, w0 = rgb.shape[:2]
    im = cv2.resize(rgb, (IN_W, IN_H), interpolation=cv2.INTER_LINEAR)
    t = torch.from_numpy(im.astype(np.float32) / 255.0).permute(2, 0, 1)
    t = (t - MEAN) / STD
    return t, w0, h0



_mp_face_detector = None

def _cargar_mediapipe_detector():
    """Carga (lazy) el detector de caras de MediaPipe."""
    global _mp_face_detector
    if _mp_face_detector is not None:
        return _mp_face_detector
    if not _MEDIAPIPE_OK:
        return None
    _mp_face_detector = _mp.solutions.face_detection.FaceDetection(
        model_selection=0, min_detection_confidence=0.5)
    return _mp_face_detector

def detectar_caras_mediapipe(frame_bgr, thr=0.5):
    """Detecta caras con MediaPipe. Devuelve [(x1,y1,x2,y2,score), ...]."""
    fd = _cargar_mediapipe_detector()
    if fd is None:
        return []
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    res = fd.process(rgb)
    if not res.detections:
        return []
    h, w = frame_bgr.shape[:2]
    caras = []
    for d in res.detections:
        bb = d.location_data.relative_bounding_box
        x1 = max(0, int(bb.xmin * w))
        y1 = max(0, int(bb.ymin * h))
        x2 = min(w, int((bb.xmin + bb.width) * w))
        y2 = min(h, int((bb.ymin + bb.height) * h))
        score = float(d.score[0]) if d.score else 0.0
        if score < thr:
            continue
        if x2 - x1 < 20 or y2 - y1 < 20:
            continue
        caras.append((x1, y1, x2, y2, score))
    return caras

def detectar_caras(frame_bgr, thr=0.5):
    """
    Detecta TODAS las caras en el frame.
    - Si USAR_MEDIAPIPE=True: usa MediaPipe Face Detection (prueba).
    - Si USAR_MEDIAPIPE=False: usa detector_seg2 propio (modelo original).
    Devuelve lista de tuplas: [(x1, y1, x2, y2, score), ...]
    en coordenadas de la imagen original.
    """
    if USAR_MEDIAPIPE and _MEDIAPIPE_OK:
        return detectar_caras_mediapipe(frame_bgr, thr=thr)

    # --- Modelo original (detector_seg2) ---
    detw, modelo, dev = cargar_detector()
    t, w0, h0 = preprocess_det(frame_bgr)
    with torch.no_grad():
        boxes, scores = detw.infer(modelo, t.to(dev), thr=thr)
    if boxes.numel() == 0:
        return []
    sx, sy = w0 / IN_W, h0 / IN_H
    caras = []
    for b, s in zip(boxes.tolist(), scores.tolist()):
        x1 = float(b[0] * sx)
        y1 = float(b[1] * sy)
        x2 = float(b[2] * sx)
        y2 = float(b[3] * sy)
        caras.append((x1, y1, x2, y2, float(s)))
    return caras


# ---- FaceNet (embeddings) ----
_emb = None


def cargar_embedding_modelo():
    """Carga InceptionResnetV1 con pesos vggface2. Evalúa en dispositivo disponible."""
    global _emb
    if _emb is not None:
        return _emb
    with _lock_modelos:
        if _emb is not None:
            return _emb
        from facenet_pytorch import InceptionResnetV1
        _emb = InceptionResnetV1(pretrained="vggface2").eval().to(_device_actual())
    return _emb


def recorte_cara(frame_bgr, caja, margen=0.25):
    """
    Recorta la cara con margen relativo y devuelve la imagen recortada (BGR).
    caja: (x1, y1, x2, y2) en coordenadas de imagen original.
    """
    x1, y1, x2, y2 = caja[:4]
    h, w = frame_bgr.shape[:2]
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    tam = max(x2 - x1, y2 - y1) * (1.0 + margen)
    nx1 = int(max(cx - tam / 2, 0))
    ny1 = int(max(cy - tam / 2, 0))
    nx2 = int(min(cx + tam / 2, w))
    ny2 = int(min(cy + tam / 2, h))
    if nx2 - nx1 < MIN_CARA_PX or ny2 - ny1 < MIN_CARA_PX:
        return None
    return frame_bgr[ny1:ny2, nx1:nx2]


def preparar_para_embedding(cara_bgr, size=160):
    """BGR recorte -> tensor RGB 160x160 normalizado para FaceNet."""
    rgb = cv2.cvtColor(cara_bgr, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)
    t = torch.from_numpy(rgb.astype(np.float32) / 255.0).permute(2, 0, 1)
    # FaceNet espera rango [-1, 1]
    t = (t - 0.5) / 0.5
    return t


def extraer_embedding(cara_bgr):
    """
    Devuelve vector 512-d (numpy) normalizado L2, o None si falla.
    """
    modelo = cargar_embedding_modelo()
    t = preparar_para_embedding(cara_bgr).unsqueeze(0).to(_device_actual())
    with torch.no_grad():
        emb = modelo(t)
    emb = emb[0].cpu().numpy()
    # L2 normalize
    n = np.linalg.norm(emb)
    if n == 0:
        return None
    return emb / n


def cos_sim(a, b):
    """Similitud coseno entre dos vectores (ya normalizados -> producto punto)."""
    return float(np.dot(a, b))


def pipeline_embeddings(frame_bgr, thr=0.5):
    """
    Detector + embeddings para TODAS las caras en un paso.
    Devuelve lista de diccionarios:
      [{"caja": (x1,y1,x2,y2), "emb": np.array(512,), "score": float}, ...]
    """
    caras = detectar_caras(frame_bgr, thr=thr)
    resultados = []
    for x1, y1, x2, y2, score in caras:
        cara = recorte_cara(frame_bgr, (x1, y1, x2, y2))
        if cara is None:
            continue
        emb = extraer_embedding(cara)
        if emb is None:
            continue
        resultados.append({
            "caja": (x1, y1, x2, y2),
            "emb": emb,
            "score_det": score,
        })
    return resultados
