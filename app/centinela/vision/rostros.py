#!/usr/bin/env python3
"""
Centinela Control — Detección facial y embeddings (asistencia y vigilancia de aula).

Detección y reconocimiento de múltiples rostros al mismo tiempo.

Detecta caras con el detector facial propio y extrae embeddings 512-d con
InceptionResnetV1 (FaceNet, preentrenado en VGGFace2).

Flujo por cada cara detectada:
  recorte con margen -> FaceNet -> embedding 512-d
  embedding vs foto de referencia -> similitud coseno -> identidad

Requisitos:
  - models/detector_facial.pth (detector facial propio)
  - facenet-pytorch (InceptionResnetV1 con pesos vggface2)
  - torch, torchvision, opencv, numpy
"""

import os, sys, threading, cv2, numpy as np

from centinela.core import rutas

# --- Detector alternativo para pruebas ---
# USAR_MEDIAPIPE = True  -> usa MediaPipe Face Detection (mas rapido, mas robusto)
# USAR_MEDIAPIPE = False -> usa detector facial propio (modelo por defecto)
USAR_MEDIAPIPE = False
_mp = None
_MEDIAPIPE_OK = False

# ---- rutas del proyecto ----
_DETECTOR_PTH = rutas.RUTA_MODELO_DETECTOR

# ---- config del detector (misma que el entrenador) ----
IN_W, IN_H = 640, 480
STRIDES = [8, 16, 32]
ANCHORS = {8: [16, 32, 64], 16: [32, 64, 128], 32: [64, 128, 256]}

# cara mínima aceptable (px en imagen original)
MIN_CARA_PX = 60

_torch_mod = None
_MEAN = None
_STD = None
_device = None
_detw = None
_modelo_det = None
_modelo_emb = None
_lock_modelos = threading.Lock()


def _torch():
    """Import perezoso de PyTorch para no frenar el arranque de la GUI (~7 s)."""
    global _torch_mod
    if _torch_mod is None:
        import torch as _t
        _torch_mod = _t
    return _torch_mod


def _mean_std():
    global _MEAN, _STD
    if _MEAN is None:
        t = _torch()
        dev = _device_actual()
        _MEAN = t.tensor([0.485, 0.456, 0.406], dtype=t.float32, device=dev).view(3, 1, 1)
        _STD = t.tensor([0.229, 0.224, 0.225], dtype=t.float32, device=dev).view(3, 1, 1)
    return _MEAN, _STD


def _cargar_detw():
    """Import perezoso del módulo de arquitectura e inferencia del detector."""
    global _detw
    if _detw is not None:
        return _detw
    from centinela.vision import detector_facial as _df
    _detw = _df
    return _detw


def _device_actual():
    global _device
    if _device is None:
        torch = _torch()
        if torch.cuda.is_available():
            torch.backends.cudnn.benchmark = True
            _device = torch.device("cuda")
        else:
            _device = torch.device("cpu")
    return _device


def cargar_detector():
    """Carga el detector facial en dispositivo disponible. Devuelve (detw, modelo, device)."""
    global _modelo_det
    if _modelo_det is not None:
        return _detw, _modelo_det, _device_actual()
    with _lock_modelos:
        if _modelo_det is not None:  # doble chequeo: otro hilo pudo cargar
            return _detw, _modelo_det, _device_actual()
        torch = _torch()
        detw = _cargar_detw()
        modelo = detw.Detector().to(_device_actual())
        saved = torch.load(_DETECTOR_PTH, map_location="cpu")
        modelo.load_state_dict(saved["model_state_dict"])
        modelo.eval()
        _modelo_det = modelo
    return _detw, _modelo_det, _device_actual()


def preprocess_det(frame_bgr):
    """BGR -> tensor normalizado en dispositivo listo para el detector. Devuelve (tensor, w0, h0)."""
    torch = _torch()
    mean, std = _mean_std()
    dev = _device_actual()
    h0, w0 = frame_bgr.shape[:2]
    im_bgr = (
        frame_bgr if (w0 == IN_W and h0 == IN_H)
        else cv2.resize(frame_bgr, (IN_W, IN_H), interpolation=cv2.INTER_LINEAR)
    )
    rgb = cv2.cvtColor(im_bgr, cv2.COLOR_BGR2RGB)
    t = torch.from_numpy(np.ascontiguousarray(rgb)).to(dev, non_blocking=True)
    t = t.permute(2, 0, 1).float().div_(255.0).sub_(mean).div_(std)
    return t, w0, h0



_mp_face_detector = None

def _cargar_mediapipe_detector():
    """Carga (lazy) el detector de caras de MediaPipe."""
    global _mp_face_detector, _mp, _MEDIAPIPE_OK
    if _mp_face_detector is not None:
        return _mp_face_detector
    if not USAR_MEDIAPIPE:
        return None
    if _mp is None:
        try:
            import mediapipe as mp_mod
            _mp = mp_mod
            _MEDIAPIPE_OK = True
        except Exception:
            _MEDIAPIPE_OK = False
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
    if USAR_MEDIAPIPE:
        caras = detectar_caras_mediapipe(frame_bgr, thr=thr)
        if _MEDIAPIPE_OK:
            return caras

    # --- Modelo propio (detector facial) ---
    torch = _torch()
    detw, modelo, dev = cargar_detector()
    t, w0, h0 = preprocess_det(frame_bgr)
    with torch.inference_mode():
        if dev.type == "cuda":
            with torch.amp.autocast("cuda", dtype=torch.float16):
                boxes, scores = detw.infer(modelo, t, thr=thr)
        else:
            boxes, scores = detw.infer(modelo, t, thr=thr)
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
        peso_local = os.path.join(
            rutas.DIR_MODELOS, "checkpoints", "20180402-114759-vggface2.pt"
        )
        if os.path.isfile(peso_local):
            os.environ.setdefault("TORCH_HOME", rutas.DIR_MODELOS)
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
    torch = _torch()
    rgb = cv2.cvtColor(cara_bgr, cv2.COLOR_BGR2RGB)
    rgb = cv2.resize(rgb, (size, size), interpolation=cv2.INTER_LINEAR)
    t = torch.from_numpy(rgb.astype(np.float32) / 255.0).permute(2, 0, 1)
    # FaceNet espera rango [-1, 1]
    t = (t - 0.5) / 0.5
    return t


def extraer_embeddings_lote(caras_bgr, size=160):
    """Extrae embeddings 512-d normalizados L2 para N caras en un solo batch."""
    if not caras_bgr:
        return []
    torch = _torch()
    modelo = cargar_embedding_modelo()
    dev = _device_actual()
    lote_rgb = np.empty((len(caras_bgr), size, size, 3), dtype=np.uint8)
    for idx, cara in enumerate(caras_bgr):
        ch, cw = cara.shape[:2]
        rec = (
            cara if (cw == size and ch == size)
            else cv2.resize(cara, (size, size), interpolation=cv2.INTER_LINEAR)
        )
        cv2.cvtColor(rec, cv2.COLOR_BGR2RGB, dst=lote_rgb[idx])
    batch = torch.from_numpy(lote_rgb).to(dev, non_blocking=True)
    batch = batch.permute(0, 3, 1, 2).float().div_(127.5).sub_(1.0)
    with torch.inference_mode():
        if dev.type == "cuda":
            with torch.amp.autocast("cuda", dtype=torch.float16):
                embs = modelo(batch)
        else:
            embs = modelo(batch)
    embs_np = embs.float().cpu().numpy()
    normas = np.linalg.norm(embs_np, axis=1, keepdims=True)
    salida = []
    for i in range(embs_np.shape[0]):
        n = float(normas[i, 0])
        salida.append((embs_np[i] / n) if n > 0.0 else None)
    return salida


def extraer_embedding(cara_bgr):
    """
    Devuelve vector 512-d (numpy) normalizado L2, o None si falla.
    """
    if cara_bgr is None:
        return None
    res = extraer_embeddings_lote([cara_bgr])
    return res[0] if res else None


def cos_sim(a, b):
    """Similitud coseno entre dos vectores (ya normalizados -> producto punto)."""
    return float(np.dot(a, b))


def pipeline_embeddings(frame_bgr, thr=0.5):
    """
    Detector + embeddings en batch para TODAS las caras en un paso.
    Devuelve lista de diccionarios:
      [{"caja": (x1,y1,x2,y2), "emb": np.array(512,), "score_det": float}, ...]
    """
    caras = detectar_caras(frame_bgr, thr=thr)
    candidatas = []
    recortes = []
    for x1, y1, x2, y2, score in caras:
        cara = recorte_cara(frame_bgr, (x1, y1, x2, y2))
        if cara is None:
            continue
        candidatas.append(((x1, y1, x2, y2), score))
        recortes.append(cara)
    if not recortes:
        return []
    embs = extraer_embeddings_lote(recortes)
    resultados = []
    for (caja, score), emb in zip(candidatas, embs):
        if emb is None:
            continue
        resultados.append({
            "caja": caja,
            "emb": emb,
            "score_det": score,
        })
    return resultados
