#!/usr/bin/env python3
"""
Centinela Control — Hilo de cámara y reconocimiento v6 (multi-cámara, múltiples rostros)

Detecta todas las caras con detector_seg2, extrae embeddings y compara contra
referencias. Cada cara identificada debe mantenerse reconocida durante
CONFIRM_SEG segundos seguidos sin bajar del umbral antes de marcar asistencia.

Soporta:
  - Cámara local (índice numérico): usa cv2.VideoCapture
  - Stream MJPEG por HTTP (ESP32): lee el stream manualmente con urllib
    y decodifica los frames JPEG del boundary multipart.

Cambios respecto a v5:
  - reconocer=False: modo "solo preview" (sin inferencia). Lo usa el diálogo
    de captura de fotos de alumnos y el modo sin módulo de asistencia.
  - set_referencias(): actualización de referencias en caliente, thread-safe.
    En v5 recargar alumnos no llegaba a una cámara ya en marcha.
  - probar_fuente(): prueba de conectividad para la pestaña Configuración.

Señales:
  frame_listo (np.ndarray, list)
    frame ya anotado, lista de detecciones con progreso individual.
  progreso_cara (int, float, str, str, tuple)
    track_id, progreso 0.0-1.0, nombre, alumno_id, caja
  cara_confirmada (int, str, str, float, tuple)
    track_id, nombre, alumno_id, similitud, caja
  estado_camara (bool)
  error (str)
"""

import os, sys, time, socket, urllib.request
import threading
import cv2
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rostros import pipeline_embeddings

CONFIRM_SEG = 3.0          # segundos seguidos necesarios para confirmar
CENTRO_DIST_MAX = 100.0
GRACE_SEG = 0.8     # px máximo entre frames para considerar misma cara


class _Track:
    """Estado de seguimiento de una cara en pantalla."""
    def __init__(self, track_id, alumno_id, nombre, caja, t0):
        self.track_id = track_id
        self.alumno_id = alumno_id
        self.nombre = nombre
        self.t_acumulado = 0.0
        self.t_ultimo = t0
        self.caja = caja
        self.confirmado = False


class CamaraHilo(QThread):
    frame_listo = pyqtSignal(object, list)
    progreso_cara = pyqtSignal(int, float, str, str, tuple)
    cara_confirmada = pyqtSignal(int, str, str, float, tuple)
    estado_camara = pyqtSignal(bool)
    error = pyqtSignal(str)

    def __init__(self, fuente, referencias, umbral_global=0.70,
                 intervalo_segundos=0.1, confirm_seg=CONFIRM_SEG,
                 reconocer=True, modo_escaneo="uno"):
        super().__init__()
        self.fuente = fuente
        self._lock_ref = threading.Lock()
        self.referencias = list(referencias or [])
        self.umbral_global = umbral_global
        self.intervalo_segundos = intervalo_segundos
        self.confirm_seg = confirm_seg
        self.reconocer = reconocer
        self.modo_escaneo = modo_escaneo  # "uno" | "varios"
        self._corriendo = False
        self._cap = None
        self._stream = None          # respuesta HTTP para streams MJPEG
        self._es_url = False         # True si la fuente es una URL
        self._ultimo_tiempo = 0.0

        self._tracks = {}          # track_id -> _Track
        self._next_track_id = 0

        self._ultimo_frame = None
        self._ultimas_detecciones = []
        self._mjpeg_buf = b""
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0

    # ---------------- Referencias en caliente ----------------
    def set_referencias(self, nuevas):
        """Reemplaza las referencias aunque el hilo esté corriendo."""
        with self._lock_ref:
            self.referencias = list(nuevas or [])

    def _refs_snapshot(self):
        with self._lock_ref:
            return list(self.referencias)

    # ---------------- Bucle principal ----------------
    def run(self):
        self._corriendo = True
        self._tracks = {}
        self._next_track_id = 0
        self._ultimo_frame = None
        self._ultimas_detecciones = []
        self._mjpeg_buf = b""
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0
        try:
            self._abrir()
            t_prev = time.time()
            while self._corriendo:
                ok, frame = self._leer_frame()
                if not ok:
                    # Para streams MJPEG, un frame fallido no es fatal: seguimos
                    # intentando. Ademas del contador de intentos hay un
                    # presupuesto de tiempo: si cada reconexion falla por timeout
                    # (servidor vivo pero sin servicio, p.ej. otro cliente
                    # ocupando el stream) 30 conteos tardaria varios minutos; el
                    # presupuesto acota el aviso a ~45 s.
                    ahora_f = time.time()
                    if self._t_inicio_fallo is None:
                        self._t_inicio_fallo = ahora_f
                    self._frames_consecutivos_fallidos += 1
                    if (self._frames_consecutivos_fallidos > 30
                            or ahora_f - self._t_inicio_fallo > 45.0):
                        self.error.emit("No se reciben frames de la cámara (verificar URL y red)")
                        break
                    self.msleep(50)
                    continue
                self._frames_consecutivos_fallidos = 0
                self._t_inicio_fallo = None

                ahora = time.time()
                dt = ahora - t_prev
                t_prev = ahora

                if self.reconocer and ahora - self._ultimo_tiempo >= self._intervalo_segundos():
                    self._ultimo_tiempo = ahora
                    t_proceso = time.time()
                    self._procesar(frame, dt)
                    t_proceso = time.time() - t_proceso
                    if t_proceso > 1.0:
                        print(f"[CAM] {time.strftime('%H:%M:%S')} _procesar tardo {t_proceso:.1f} s")

                mostrar = self._ultimo_frame if self._ultimo_frame is not None else frame
                self.frame_listo.emit(mostrar, list(self._ultimas_detecciones))
                self.msleep(5)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self._liberar()
            self.estado_camara.emit(False)

    def _intervalo_segundos(self):
        return max(0.05, self.intervalo_segundos)

    def _es_url_http(self, fuente):
        return isinstance(fuente, str) and fuente.lower().startswith(("http://", "https://"))

    def _abrir(self):
        fuente = self.fuente
        self._es_url = self._es_url_http(fuente)

        if self._es_url:
            # Leer stream MJPEG por HTTP manualmente (cv2.VideoCapture no lo hace bien en Windows)
            req = urllib.request.Request(
                fuente,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            # Abrimos la conexión con timeout de conexión; el socket se configurará
            # con un timeout de lectura para no bloquear para siempre.
            self._stream = urllib.request.urlopen(req, timeout=15)
            # NOTA: a proposito NO se acorta el timeout de lectura del socket
            # (el intento original era 0.5 s). Una lectura que vence por timeout
            # sobre un BufferedReader queda "envenenada": TODAS las lecturas
            # posteriores lanzan OSError("cannot read from timed out object"),
            # aunque lleguen datos o incluso el cierre de conexion, y el except
            # de _leer_frame_mjpeg lo enmascararia como reintento eterno sin
            # emitir error. Se conserva el timeout (15 s) que urlopen fija al
            # socket: ante un hipo de red bloquea y despues actua el except de
            # _leer_frame_mjpeg (si el BufferedReader quedara envenenado por el
            # vencimiento, la reconexion de _tras_fallo_lectura lo reemplaza).
            # Leer y descartar los headers HTTP iniciales hasta el primer boundary
            self._leer_hasta_boundary()
        else:
            # Cámara local (índice numérico)
            try:
                fuente = int(fuente)
            except (TypeError, ValueError):
                raise RuntimeError(f"Fuente inválida: {fuente}")
            self._cap = cv2.VideoCapture(fuente)
            if not self._cap.isOpened():
                raise RuntimeError(f"No se pudo abrir la cámara {fuente}")

        self.estado_camara.emit(True)

    def _tras_fallo_lectura(self):
        """Ruta comun de fallo de lectura del stream: intenta reabrir la
        conexion hasta 3 veces seguidas. Devuelve (True, None) si la
        reconexion listo (el caller vuelve a leer de inmediato con el nuevo
        socket), o (False, None) para que run() cuente el fallo y avise."""
        self._intentos_reconexion += 1
        if self._intentos_reconexion > 3:
            print(f"[CAM] {time.strftime('%H:%M:%S')} 3 reconexiones sin exito; se cuenta como fallo")
            self._intentos_reconexion = 0
            return False, None
        # No se resetea el contador al conectar bien: resetear aqui permitiria
        # un bucle eterno con un servidor que acepta la conexion y la cierra
        # de inmediato (reconexiones siempre "exitosas" sin ningun frame). El
        # contador solo baja al decodificar un frame real (en _leer_frame_mjpeg).
        if self._reconectar():
            return True, None
        return False, None

    def _reconectar(self):
        """Reabre el stream MJPEG tras un corte (EOF, RST o abort del servidor).

        El BufferedReader viejo no se recupera (queda envenenado tras un
        timeout), asi que la unica via de reunion es abrir la conexion otra
        vez. Se llama desde la ruta de fallo de _leer_frame_mjpeg; los
        intentos consecutivos quedan acotados por _intentos_reconexion y por
        los 30 fallos de run().
        """
        print(f"[CAM] {time.strftime('%H:%M:%S')} reconectando stream...")
        try:
            if self._stream is not None:
                try:
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            req = urllib.request.Request(
                self.fuente,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            self._stream = urllib.request.urlopen(req, timeout=5)
            self._leer_hasta_boundary()
            self._mjpeg_buf = b""
            print(f"[CAM] {time.strftime('%H:%M:%S')} stream reconectado")
            return True
        except Exception as e:
            print(f"[CAM] {time.strftime('%H:%M:%S')} reconexion fallo: {type(e).__name__}: {e}")
            return False

    def _leer_hasta_boundary(self):
        """Descarta los headers HTTP iniciales hasta encontrar el primer boundary del multipart."""
        datos = b""
        while self._corriendo:
            try:
                chunk = self._stream.read(1024)
                if not chunk:
                    break
                datos += chunk
                if b"\r\n--" in datos or b"--\r\n" in datos:
                    break
            except (socket.timeout, urllib.error.URLError):
                break
        # No importa cuánto leímos; el siguiente frame se buscará por marcadores JPEG
        return

    def _leer_frame(self):
        """
        Devuelve (ok, frame_bgr).
        Para URLs HTTP lee el stream MJPEG frame por frame.
        Para cámaras locales usa cv2.VideoCapture.read().
        """
        if self._es_url:
            return self._leer_frame_mjpeg()
        else:
            ok, frame = self._cap.read()
            return ok, frame

    def _leer_frame_mjpeg(self):
        """Lee un frame JPEG del stream multipart de la cámara ESP32."""
        stream = self._stream
        buf = getattr(self, "_mjpeg_buf", b"")

        # Lector MJPEG. Politica de lecturas:
        #   - datos: se acumulan en buf y se extraen frames SOI..EOI
        #   - b"" (EOF, servidor cerro bien): fallo, run() avisa tras 30 intentos
        #   - excepcion: la conexion esta rota (RST, cierre abrupto, URL mala,
        #     silencio >= 15 s). NO se reintentan: un BufferedReader queda
        #     envenenado tras un timeout y un RST nunca devuelve datos, asi
        #     que reintentar solo producia un ciclo eterno sin aviso. Se
        #     devuelve (False, None) y run() sera quien emita el error.
        while self._corriendo:
            try:
                chunk = stream.read(4096)
            except (TimeoutError, OSError, ValueError, AttributeError) as e:
                # TimeoutError es subclase de OSError; se lista por claridad.
                # Con el socket roto (timeout, RST, cierre abrupto) la conexion
                # actual ya no sirve: intentar reconectar antes de pasar el
                # fallo al contador de run().
                print(f"[CAM] {time.strftime('%H:%M:%S')} lectura fallo: {type(e).__name__}: {e}")
                viva, _ = self._tras_fallo_lectura()
                if not viva:
                    return False, None
                stream = self._stream
                continue
            if not chunk:
                # Servidor cerro la conexion (EOF)
                print(f"[CAM] {time.strftime('%H:%M:%S')} stream cerro (EOF)")
                viva, _ = self._tras_fallo_lectura()
                if not viva:
                    return False, None
                stream = self._stream
                continue
            buf += chunk

            # Buscar inicio y fin de JPEG.
            # El stream puede comenzar a mitad de un frame (el handshake de
            # conexión ya consumió parte de la cabecera HTTP multipart). Todo
            # lo anterior al SOI (cabeceras multipart o restos de un frame
            # roto) se descarta; así el EOI buscado siempre corresponde al
            # frame abierto y nunca queda un EOI sobrante por delante de los
            # SOI posteriores (el bug original: ningún frame se decodificaba).
            a = buf.find(b"\xff\xd8")  # SOI
            if a == -1:
                # Sin SOI visible: basura pura, descartarla y seguir
                buf = b""
                continue
            if a > 0:
                buf = buf[a:]
                a = 0

            b = buf.find(b"\xff\xd9", 2)  # EOI después del SOI
            if b == -1:
                # Frame incompleto: esperar más datos (buffer acotado)
                if len(buf) > 2_000_000:
                    buf = b""
                continue

            jpg = buf[: b + 2]
            buf = buf[b + 2 :]
            self._mjpeg_buf = buf
            arr = np.frombuffer(jpg, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is not None:
                self._intentos_reconexion = 0
                return True, frame
            # Si falla el decode, descartar el buffer para resincronizar
            buf = b""

        return False, None

    def _liberar(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None
        if self._stream is not None:
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    def _centro(self, caja):
        x1, y1, x2, y2 = caja
        return ((x1 + x2) / 2.0, (y1 + y2) / 2.0)

    def _dist_centros(self, c1, c2):
        return ((c1[0] - c2[0]) ** 2 + (c1[1] - c2[1]) ** 2) ** 0.5

    def _procesar(self, frame, dt):
        resultados = pipeline_embeddings(frame, thr=0.5)
        # Modo "uno": solo procesar la cara mas grande (mayor area).
        if self.modo_escaneo == "uno" and len(resultados) > 1:
            resultados = [max(resultados, key=lambda r: (
                (r["caja"][2] - r["caja"][0]) * (r["caja"][3] - r["caja"][1])))]
        anotado = frame.copy()

        refs = self._refs_snapshot()

        # Identificar cada cara contra referencias
        identificadas = []  # lista de (caja, nombre, sim, alumno_id, identificado, color)
        for res in resultados:
            emb = res["emb"]
            caja = res["caja"]
            mejor = None
            mejor_sim = -1.0
            for r in refs:
                s = float(np.dot(emb, r["emb"]))
                if s > mejor_sim:
                    mejor_sim = s
                    mejor = r

            identificado = False
            nombre = "DESCONOCIDO"
            color = (128, 128, 128)
            alumno_id = None
            if mejor is not None and mejor_sim >= self.umbral_global and mejor_sim >= mejor["umbral"]:
                # v6: los IDs son internos y no se muestran en la UI
                nombre = mejor['nombre']
                identificado = True
                alumno_id = mejor["alumno_id"]
                color = (0, 200, 0)
            else:
                color = (0, 0, 200)

            identificadas.append({
                "caja": caja,
                "nombre": nombre,
                "sim": mejor_sim,
                "alumno_id": alumno_id,
                "identificado": identificado,
                "color": color,
            })

        # Log de diagnostico acotado (~1 cada 2 s): distingue sin-datos si el
        # problema es deteccion (detecciones=0), similitud por debajo del
        # umbral (sim=x.xx) o reconocido-sin-confirmar (nombre visible).
        self._t_ultimo_log_rec = getattr(self, "_t_ultimo_log_rec", 0.0)
        if time.time() - self._t_ultimo_log_rec >= 2.0:
            self._t_ultimo_log_rec = time.time()
            if identificadas:
                caras_txt = " | ".join(
                    f"{d['nombre'].split(' (')[0]} sim={d['sim']:.2f}"
                    for d in identificadas
                )
                print(f"[REC] {time.strftime('%H:%M:%S')} detecciones={len(identificadas)} "
                      f"umbral={self.umbral_global:.2f} :: {caras_txt}")
            else:
                print(f"[REC] {time.strftime('%H:%M:%S')} detecciones=0 (sin caras en el frame)")

        # Matching de tracks con detecciones de este frame
        tracks_vistos = set()
        for det in identificadas:
            caja = det["caja"]
            centro = self._centro(caja)

            # Buscar track existente del mismo alumno cercano
            track_match = None
            mejor_dist = CENTRO_DIST_MAX
            for tid, tr in self._tracks.items():
                if tr.alumno_id != det["alumno_id"] or not det["identificado"]:
                    continue
                d = self._dist_centros(centro, self._centro(tr.caja))
                if d < mejor_dist:
                    mejor_dist = d
                    track_match = tid

            if track_match is not None:
                # Mismo alumno, misma zona: acumular tiempo
                tr = self._tracks[track_match]
                # Acumular tiempo REAL (no dt del loop, que es ~ms).
                # Tope por frame para no saltar por pérdidas momentáneas.
                delta = time.time() - tr.t_ultimo
                if delta > 0.5:
                    delta = 0.5
                if delta < 0:
                    delta = 0.0
                tr.t_acumulado += delta
                tr.t_ultimo = time.time()
                tr.caja = caja
                tracks_vistos.add(track_match)

                progreso = min(tr.t_acumulado / self.confirm_seg, 1.0)
                self.progreso_cara.emit(
                    tr.track_id, progreso, tr.nombre, tr.alumno_id, caja
                )

                if tr.t_acumulado >= self.confirm_seg and not tr.confirmado:
                    tr.confirmado = True
                    self.cara_confirmada.emit(
                        tr.track_id, tr.nombre, tr.alumno_id,
                        det["sim"], caja
                    )
            elif det["identificado"]:
                # Nueva cara identificada: crear track
                tid = self._next_track_id
                self._next_track_id += 1
                tr = _Track(tid, det["alumno_id"], det["nombre"], caja, time.time())
                self._tracks[tid] = tr
                tracks_vistos.add(tid)

                self.progreso_cara.emit(
                    tr.track_id, 0.0, tr.nombre, tr.alumno_id, caja
                )

        # Limpiar tracks que ya no se ven (se fueron de pantalla).
        # Grace period: no borrar de inmediato si la cara se pierde por un
        # instante; mantener el track un poco para no resetear el escaneo.
        for tid in list(self._tracks.keys()):
            if tid not in tracks_vistos:
                tr = self._tracks[tid]
                if time.time() - tr.t_ultimo < GRACE_SEG:
                    continue
                del self._tracks[tid]

        # Dibujar anotaciones en el frame y preparar lista de detecciones
        detecciones = []
        for det in identificadas:
            x1, y1, x2, y2 = [int(v) for v in det["caja"]]
            # Overlay moderno: solo esquinas (corner brackets) con margen,
            # en lugar del rectangulo completo. Mas limpio y menos invasivo.
            m = 8
            ax1, ay1, ax2, ay2 = x1 - m, y1 - m, x2 + m, y2 + m
            L = max(18, (ax2 - ax1) // 5, (ay2 - ay1) // 5)
            color = det["color"]
            thick = 3
            # superior izquierda
            cv2.line(anotado, (ax1, ay1), (ax1 + L, ay1), color, thick)
            cv2.line(anotado, (ax1, ay1), (ax1, ay1 + L), color, thick)
            # superior derecha
            cv2.line(anotado, (ax2, ay1), (ax2 - L, ay1), color, thick)
            cv2.line(anotado, (ax2, ay1), (ax2, ay1 + L), color, thick)
            # inferior izquierda
            cv2.line(anotado, (ax1, ay2), (ax1 + L, ay2), color, thick)
            cv2.line(anotado, (ax1, ay2), (ax1, ay2 - L), color, thick)
            # inferior derecha
            cv2.line(anotado, (ax2, ay2), (ax2 - L, ay2), color, thick)
            cv2.line(anotado, (ax2, ay2), (ax2, ay2 - L), color, thick)
            cv2.putText(
                anotado,
                f"{det['nombre']}  {det['sim']:.2f}",
                (ax1, max(ay1 - 12, 24)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                color,
                2,
            )
            detecciones.append({
                "nombre": det["nombre"],
                "sim": det["sim"],
                "caja": det["caja"],
                "identificado": det["identificado"],
                "alumno_id": det["alumno_id"],
            })

        self._ultimo_frame = anotado
        self._ultimas_detecciones = detecciones

    @property
    def frame_actual(self):
        return self._ultimo_frame

    def detener(self):
        self._corriendo = False
        self.wait(3000)


def probar_fuente(tipo, fuente, segundos=6.0):
    """
    Prueba una fuente de cámara para la pestaña Configuración.
    Devuelve (ok, mensaje). No requiere modelos ni inferencia.
    """
    fuente = str(fuente or "").strip()
    if tipo == "http":
        if not fuente.lower().startswith(("http://", "https://")):
            return False, f"URL inválida: {fuente or '(vacía)'}"
        try:
            req = urllib.request.Request(
                fuente,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            t0 = time.time()
            with urllib.request.urlopen(req, timeout=segundos) as r:
                datos = r.read(8192)
            if not datos:
                return False, "Conectó pero no llegó ningún dato."
            return True, f"Conexión OK ({len(datos)} bytes recibidos en {time.time() - t0:.1f} s)."
        except Exception as e:
            return False, f"No se pudo conectar: {type(e).__name__}: {e}"
    try:
        idx = int(fuente)
    except (TypeError, ValueError):
        return False, f"Fuente local inválida: {fuente or '(vacía)'} (usa un índice numérico)."
    cap = cv2.VideoCapture(idx)
    if not cap.isOpened():
        return False, f"No se pudo abrir la webcam {idx} (¿existe o está en uso?)."
    ok_frame, _ = cap.read()
    cap.release()
    if not ok_frame:
        return False, f"La webcam {idx} abrió pero no entrega frames."
    return True, f"Webcam {idx} OK."
