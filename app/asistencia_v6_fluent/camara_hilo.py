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
import queue
import cv2
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

# Opener HTTP sin proxy: el stream va por LAN directa y un proxy del sistema
# (tipico en red de campus) romperia la lectura del MJPEG.
_OPENER_SIN_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rostros import pipeline_embeddings

CONFIRM_SEG = 3.0          # segundos seguidos necesarios para confirmar
CENTRO_DIST_MAX = 100.0
GRACE_SEG = 0.8     # px máximo entre frames para considerar misma cara

# --- Paleta del overlay (BGR) ---
# teal Fluent para caras reconocidas, rojo suave para desconocidas.
COLOR_IDENT = (191, 212, 45)   # ~#2dd4bf
COLOR_DESC = (68, 68, 239)     # ~#ef4444
COLOR_SIN_DATOS = (140, 140, 140)
CHIP_FONDO = (26, 22, 16)      # fondo oscuro translucido del chip
CHIP_TEXTO = (248, 250, 252)


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

    def __init__(self, fuente, referencias, umbral_global=0.75,
                 intervalo_segundos=0.1, confirm_seg=CONFIRM_SEG,
                 reconocer=True, modo_escaneo="uno",
                 procesar_cada_n=1, recorte_aspecto="Original"):
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
        self._tracker = None
        self._tracker_box = None
        self._tracker_ok = False
        self._mjpeg_buf = b""
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0

        # Desacoplar lectura del stream del procesamiento: el reconocimiento
        # corre en un hilo aparte para no bloquear el bucle ni congelar el
        # stream. Se procesa 1 de cada `procesar_cada_n` frames leidos.
        self._procesando = False
        self._worker = None
        self._frame_count = 0
        self.procesar_cada_n = max(1, int(procesar_cada_n or 1))
        self.recorte_aspecto = str(recorte_aspecto or "Original")

        # Productor/consumidor: hilo lector llena la cola, bucle principal consume
        self._frame_queue = queue.Queue(maxsize=2)
        self._reader_thread = None
        self._reader_stop = threading.Event()
        self._ultimo_frame_raw = None  # ultimo frame leido (para preview sin bloqueo)

        # Modo telefono: la "fuente" es telefono:<id> y los frames llegan
        # subidos por HTTP (servidor_web). Mismo pipeline y misma logica de
        # confirmacion que una camara fisica.
        self._es_telefono = isinstance(fuente, str) and fuente.startswith("telefono:")
        self._cola_telefono = queue.Queue(maxsize=2)
        self._ultimo_frame_ts = 0.0

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
        self._ultimo_frame_ts = 0.0
        self._ultimas_detecciones = []
        self._mjpeg_buf = b""
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0
        try:
            self._abrir()
            # Arrancar hilo productor (lector)
            self._reader_stop.clear()
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()

            t_prev = time.time()
            self._t_ultimo_log_med = 0.0
            ultimo_frame_consumido = None
            while self._corriendo:
                # Consumir frame de la cola (sin bloquear)
                frame = None
                try:
                    frame = self._frame_queue.get_nowait()
                    ultimo_frame_consumido = frame
                except queue.Empty:
                    frame = ultimo_frame_consumido

                if frame is None:
                    self.msleep(30)
                    continue

                ahora = time.time()
                dt = ahora - t_prev
                t_prev = ahora

                self._frame_count += 1
                if (self.reconocer and not self._procesando
                        and self._frame_count % self.procesar_cada_n == 0
                        and ahora - self._ultimo_tiempo >= self._intervalo_segundos()):
                    self._ultimo_tiempo = ahora
                    self._procesando = True
                    # Procesar sobre el frame original para no degradar la
                    # calidad del video mostrado (sin redimensionado).
                    frame_copia = frame.copy()
                    self._worker = threading.Thread(
                        target=self._worker_procesar,
                        args=(frame_copia, dt), daemon=True)
                    self._worker.start()

                # Mientras se procesa, dibujar las ultimas anotaciones sobre el
                # frame crudo para mantener el preview fluido sin parpadeo.
                # Mostrar el ultimo frame anotado (congelado durante procesamiento)
                # para que el recuadro no salte a una posicion vieja.
                mostrar = self._ultimo_frame if self._ultimo_frame is not None else frame

                # Emitir preview solo 1 de cada 2 frames para reducir overhead
                # de serializacion de arrays en las señales PyQt.
                mostrar, _d = self._preparar_display(frame, [])
                dets = list(self._ultimas_detecciones)
                # Actualizar tracker para suavizar el recuadro entre detecciones.
                if self._tracker is not None and self._tracker_ok and dets:
                    try:
                        ok, box = self._tracker.update(frame)
                        if ok:
                            x, y, w, h = [int(v) for v in box]
                            fh, fw = frame.shape[:2]
                            mh, mw = mostrar.shape[:2]
                            sx = mw / float(fw)
                            sy = mh / float(fh)
                            rx, ry = int(x * sx), int(y * sy)
                            rw, rh = int(w * sx), int(h * sy)
                            # Reutilizar el estilo original con la caja suavizada.
                            dets = [dict(d) for d in dets]
                            dets[0]["caja"] = (rx, ry, rx + rw, ry + rh)
                        else:
                            self._tracker_ok = False
                    except Exception:
                        self._tracker_ok = False
                if dets:
                    try:
                        self._dibujar(mostrar, dets)
                    except Exception:
                        pass
                self.frame_listo.emit(mostrar, dets)
                self.msleep(1)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self._reader_stop.set()
            if self._reader_thread is not None and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=5.0)
            if self._worker is not None and self._worker.is_alive():
                self._worker.join(timeout=5.0)
            self._liberar()
            self.estado_camara.emit(False)

    def _intervalo_segundos(self):
        return max(0.05, self.intervalo_segundos)

    def _worker_procesar(self, frame, dt):
        """Ejecuta _procesar en un hilo aparte para no bloquear el bucle."""
        try:
            self._procesar(frame, dt)
        except Exception as e:
            print(f"[CAM] error en procesamiento: {type(e).__name__}: {e}")
        finally:
            self._procesando = False

    def _reader(self):
        """Hilo productor: lee frames del stream y los pone en la cola."""
        backoff = 1.0
        while self._corriendo and not self._reader_stop.is_set():
            t_lec = time.time()
            try:
                ok, frame = self._leer_frame()
            except Exception as e:
                print(f"[CAM] reader error: {type(e).__name__}: {e}")
                ok, frame = False, None
            t_lec = time.time() - t_lec
            if ok and time.time() - getattr(self, "_t_ultimo_log_med", 0.0) >= 2.0:
                self._t_ultimo_log_med = time.time()
                print(f"[MED] lectura+decode={t_lec*1000:.0f} ms  procesando={self._procesando}")

            if not ok or frame is None:
                if self._es_telefono:
                    # sin frame nuevo del telefono: normal (nadie subio todavia)
                    continue
                self._frames_consecutivos_fallidos += 1
                ahora_f = time.time()
                if self._t_inicio_fallo is None:
                    self._t_inicio_fallo = ahora_f
                if (self._frames_consecutivos_fallidos > 30
                        or ahora_f - self._t_inicio_fallo > 45.0):
                    self.error.emit("No se reciben frames de la camara (verificar URL y red)")
                    break
                espera = min(backoff, 8.0)
                self._reader_stop.wait(espera)
                backoff = min(backoff * 2, 8.0)
                viva, _ = self._tras_fallo_lectura()
                if not viva:
                    continue
                backoff = 1.0
                continue

            self._frames_consecutivos_fallidos = 0
            self._t_inicio_fallo = None
            backoff = 1.0
            try:
                self._frame_queue.put_nowait(frame)
            except queue.Full:
                try:
                    self._frame_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frame_queue.put_nowait(frame)
                except queue.Full:
                    pass

    def set_tamano_display(self, ancho, alto):
        """Define la caja (px) a la que la UI muestra el video.

        El overlay se dibuja a esa resolucion (con la geometria de las caras
        escalada), de modo que no queda limitado por la resolucion de la
        captura (p.ej. 320x240 del pipeline) y se ve nitido a cualquier
        tamano de ventana.
        """
        try:
            ancho, alto = int(ancho), int(alto)
        except (TypeError, ValueError):
            ancho, alto = 0, 0
        self._tamano_display = (ancho, alto) if ancho > 1 and alto > 1 else None

    def _preparar_display(self, frame, detecciones):
        """Recorta por aspecto (si aplica) y escala a la resolucion de visualizacion."""
        frame = frame.copy()
        dets = [dict(d) for d in (detecciones or [])]

        # Recorte por relacion de aspecto (sin pixeles, solo ratio).
        aspecto = str(getattr(self, "recorte_aspecto", "Original") or "Original")
        if aspecto and aspecto.lower() != "original":
            try:
                aw, ah = aspecto.split(":")
                ratio = float(aw) / float(ah)
            except (ValueError, ZeroDivisionError):
                ratio = None
            if ratio and ratio > 0:
                h, w = frame.shape[:2]
                if w / h > ratio:
                    nuevo_w = int(h * ratio)
                    x0 = (w - nuevo_w) // 2
                    frame = frame[:, x0:x0 + nuevo_w]
                    for d in dets:
                        x1, y1, x2, y2 = d["caja"]
                        d["caja"] = (x1 - x0, y1, x2 - x0, y2)
                else:
                    nuevo_h = int(w / ratio)
                    y0 = (h - nuevo_h) // 2
                    frame = frame[y0:y0 + nuevo_h, :]
                    for d in dets:
                        x1, y1, x2, y2 = d["caja"]
                        d["caja"] = (x1, y1 - y0, x2, y2 - y0)

        tam = getattr(self, "_tamano_display", None)
        if not tam:
            return frame, dets
        ancho_d, alto_d = tam[0], tam[1]
        if ancho_d <= 0 or alto_d <= 0:
            return frame, dets
        alto_f, ancho_f = frame.shape[:2]
        escala = min(ancho_d / ancho_f, alto_d / alto_f)
        if escala <= 0:
            return frame, dets
        tw, th = max(1, round(ancho_f * escala)), max(1, round(alto_f * escala))
        if tw == ancho_f and th == alto_f and not dets:
            return frame, dets
        mostrar = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)
        f = escala
        dets2 = []
        for det in dets:
            x1, y1, x2, y2 = det["caja"]
            d = dict(det)
            d["caja"] = (x1 * f, y1 * f, x2 * f, y2 * f)
            dets2.append(d)
        return mostrar, dets2

    # ---------------- Overlay (dibujado a resolucion de display) ----------
    def _factor_metrica(self, frame):
        """Escala de las metricas del overlay segun el alto del frame."""
        k = frame.shape[0] / 480.0
        return max(0.7, min(2.4, k))

    def _rect_alpha(self, frame, x, y, w, h, color, alpha=1.0):
        """Rectangulo relleno; con alpha<1 se mezcla con el fondo."""
        alto_f, ancho_f = frame.shape[:2]
        xa, ya = max(0, int(x)), max(0, int(y))
        xb, yb = min(ancho_f, int(x + w)), min(alto_f, int(y + h))
        if xb <= xa or yb <= ya:
            return
        if alpha >= 1.0:
            cv2.rectangle(frame, (xa, ya), (xb - 1, yb - 1), color, -1, cv2.LINE_AA)
            return
        roi = frame[ya:yb, xa:xb]
        overlay = roi.copy()
        cv2.rectangle(overlay, (0, 0), (xb - xa - 1, yb - ya - 1), color, -1, cv2.LINE_AA)
        cv2.addWeighted(overlay, alpha, roi, 1.0 - alpha, 0, dst=roi)

    def _tag(self, frame, x, y_borde, texto, color, progreso=None):
        """Etiqueta solida pegada al borde superior del box.

        (x, y_borde) es el borde inferior-izquierdo deseado. El progreso se
        dibuja DENTRO de la etiqueta (franja inferior), de modo que entre
        etiqueta y corchetes no queda ningun elemento que pueda chocar.
        Color de texto por luminancia del fondo.
        """
        k = self._factor_metrica(frame)
        escala = 0.55 * k
        grosor = max(1, round(k))
        hbar = max(3, round(4 * k)) if progreso is not None else 0
        (tw, th), base = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, grosor)
        pad_x = round(9 * k)
        alto = round(th + base + 14 * k) + hbar
        ancho = tw + pad_x * 2
        alto_f, ancho_f = frame.shape[:2]
        b, g, r = color
        lum = 0.114 * b + 0.587 * g + 0.299 * r
        texto_color = CHIP_FONDO if lum > 140 else (255, 255, 255)
        x = min(max(2, int(x)), max(2, ancho_f - ancho - 2))
        y = int(y_borde) - alto
        if y < 2:
            y = int(y_borde)  # sin sitio arriba: dentro del box
        # Fondo solido de la etiqueta
        self._rect_alpha(frame, x, y, ancho, alto, color, 1.0)
        # Texto centrado en la parte de texto (por encima de la franja)
        cv2.putText(frame, texto, (x + pad_x, y + (alto - hbar - th - base) // 2 + th),
                    cv2.FONT_HERSHEY_SIMPLEX, escala, texto_color, grosor, cv2.LINE_AA)
        # Franja de progreso integrada en la base de la etiqueta
        if progreso is not None:
            ystrip = y + alto - hbar
            self._rect_alpha(frame, x, ystrip, ancho, hbar, CHIP_FONDO, 0.55)
            wfill = max(round(2 * k), int(ancho * min(max(progreso, 0.0), 1.0)))
            self._rect_alpha(frame, x, ystrip, wfill, hbar, (255, 255, 255), 1.0)

    def _dibujar(self, frame, detecciones):
        """Dibuja el overlay: esquinas suavizadas + etiqueta de estado.

        Se invoca sobre el frame ya escalado a resolucion de visualizacion,
        de modo que el resultado no depende de la resolucion de captura.
        """
        k = self._factor_metrica(frame)
        thick = max(2, round(3 * k))
        for det in detecciones:
            x1, y1, x2, y2 = [float(v) for v in det["caja"]]
            m = 8 * k
            ax1, ay1, ax2, ay2 = x1 - m, y1 - m, x2 + m, y2 + m
            L = max(18 * k, (ax2 - ax1) / 5.0, (ay2 - ay1) / 5.0)
            ident = det.get("identificado")
            color = det.get("color") or (COLOR_IDENT if ident else COLOR_DESC)
            p1 = (int(round(ax1)), int(round(ay1)))
            p2 = (int(round(ax2)), int(round(ay2)))
            esq_tl, esq_tr = p1, (p2[0], p1[1])
            esq_bl, esq_br = (p1[0], p2[1]), p2
            Lc = int(round(L))
            cv2.line(frame, esq_tl, (esq_tl[0] + Lc, esq_tl[1]), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_tl, (esq_tl[0], esq_tl[1] + Lc), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_tr, (esq_tr[0] - Lc, esq_tr[1]), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_tr, (esq_tr[0], esq_tr[1] + Lc), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_bl, (esq_bl[0] + Lc, esq_bl[1]), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_bl, (esq_bl[0], esq_bl[1] - Lc), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_br, (esq_br[0] - Lc, esq_br[1]), color, thick, cv2.LINE_AA)
            cv2.line(frame, esq_br, (esq_br[0], esq_br[1] - Lc), color, thick, cv2.LINE_AA)

            # Texto de la etiqueta segun el estado de la cara
            nombre_txt = det["nombre"] if ident else "Desconocido"
            if ident:
                prog = det.get("progreso")
                if det.get("confirmado"):
                    texto, progreso = nombre_txt, None
                elif prog is not None:
                    texto = f"{nombre_txt} · {int(prog * 100)}%"
                    progreso = prog
                else:
                    sim = max(0.0, min(1.0, float(det.get("sim", 0.0))))
                    texto, progreso = f"{nombre_txt} · {sim * 100:.0f}%", None
            else:
                texto, progreso = "Desconocido", None

            self._tag(frame, ax1, ay1, texto, color, progreso)

    def _es_url_http(self, fuente):
        return isinstance(fuente, str) and fuente.lower().startswith(("http://", "https://", "tcp://"))

    def _url_http_normalizada(self):
        """Normaliza la fuente a una URL HTTP MJPEG del firmware actual.

        CentinelaCamWifi sirve el stream como HTTP MJPEG en el puerto 81
        (/stream). Configs antiguas podian guardar scheme tcp:// o URL sin
        ruta; aqui se corrigen para abrir SIEMPRE por HTTP. El antiguo
        "TCP crudo" ([4 bytes len][JPEG]) ya no existe en el firmware.
        """
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(str(self.fuente).strip())
        scheme = "http" if parsed.scheme in ("", "tcp") else parsed.scheme
        path = parsed.path if parsed.path not in ("", "/") else "/stream"
        return urlunparse((scheme, parsed.netloc, path, "", "", ""))

    def _abrir(self):
        if self._es_telefono:
            # no hay stream que abrir: los frames llegan por HTTP. Marcar la
            # cámara como activa para la tarjeta de Vigilancia.
            self.estado_camara.emit(True)
            return
        self._es_url = self._es_url_http(self.fuente)

        if self._es_url:
            # Stream MJPEG por HTTP manualmente (el puerto 81 de la camara es HTTP;
            # no se infiere el modo por numero de puerto).
            req = urllib.request.Request(
                self._url_http_normalizada(),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            self._stream = _OPENER_SIN_PROXY.open(req, timeout=5)
            self._leer_hasta_boundary()
        else:
            # Cámara local (índice numérico)
            try:
                fuente = int(self.fuente)
            except (TypeError, ValueError):
                raise RuntimeError(f"Fuente inválida: {self.fuente}")
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
        # Backoff exponencial para no reconectar agresivamente
        espera = min(2 ** (self._intentos_reconexion - 1), 8.0)
        print(f"[CAM] {time.strftime('%H:%M:%S')} esperando {espera:.0f}s antes de reconectar...")
        time.sleep(espera)
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
                self._url_http_normalizada(),
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            )
            self._stream = _OPENER_SIN_PROXY.open(req, timeout=5)
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
        Modo telefono: extrae de la cola de frames subidos por el telefono.
        Para URLs HTTP lee el stream MJPEG frame por frame.
        Para cámaras locales usa cv2.VideoCapture.read().
        """
        if self._es_telefono:
            try:
                return True, self._cola_telefono.get(timeout=0.5)
            except queue.Empty:
                return False, None
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
                chunk = stream.read(1024)
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
        # Redimensionar para procesamiento (mas rapido) sin degradar el video.
        orig_h, orig_w = frame.shape[:2]
        escala = 1.0
        if orig_w > 640:
            escala = 640.0 / orig_w
            frame_proc = cv2.resize(frame, (int(orig_w * escala), int(orig_h * escala)))
        else:
            frame_proc = frame
        resultados = pipeline_embeddings(frame_proc, thr=0.5)
        if getattr(self, "_es_telefono", False):
            print(f"[TEL] frame={frame.shape} detecciones={len(resultados)}")
        # Reescalar cajas a la resolucion original.
        if escala != 1.0:
            for r in resultados:
                c = r["caja"]
                r["caja"] = tuple(int(v / escala) for v in c)
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
            color = COLOR_SIN_DATOS
            alumno_id = None
            if mejor is not None and mejor_sim >= self.umbral_global and mejor_sim >= mejor["umbral"]:
                # v6: los IDs son internos y no se muestran en la UI
                nombre = mejor['nombre']
                identificado = True
                alumno_id = mejor["alumno_id"]
                color = COLOR_IDENT
            else:
                color = COLOR_DESC

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

        # Preparar lista de detecciones (sin dibujar aun), con el progreso
        # de confirmacion del track mas cercano del mismo alumno.
        detecciones = []
        for det in identificadas:
            progreso = None
            confirmado = False
            if det["identificado"]:
                centro = self._centro(det["caja"])
                mejor_tr = None
                mejor_d = CENTRO_DIST_MAX
                for tr in self._tracks.values():
                    if tr.alumno_id != det["alumno_id"]:
                        continue
                    d = self._dist_centros(centro, self._centro(tr.caja))
                    if d < mejor_d:
                        mejor_d = d
                        mejor_tr = tr
                if mejor_tr is not None and self.confirm_seg > 0:
                    progreso = min(mejor_tr.t_acumulado / self.confirm_seg, 1.0)
                    confirmado = mejor_tr.confirmado
            detecciones.append({
                "nombre": det["nombre"],
                "sim": det["sim"],
                "caja": det["caja"],
                "identificado": det["identificado"],
                "alumno_id": det["alumno_id"],
                "color": det["color"],
                "progreso": progreso,
                "confirmado": confirmado,
            })

        # El overlay se dibuja a resolucion de visualizacion: el frame se
        # escala a la caja del QLabel de la UI y las cajas lo acompanan.
        # Inicializar tracker con la primera deteccion (coords originales).
        if detecciones:
            try:
                caja = detecciones[0]["caja"]
                x1, y1, x2, y2 = [int(v) for v in caja]
                self._tracker = cv2.TrackerKCF_create()
                self._tracker.init(frame, (x1, y1, x2 - x1, y2 - y1))
                self._tracker_ok = True
            except Exception:
                self._tracker = None
                self._tracker_ok = False
        else:
            self._tracker = None
            self._tracker_ok = False

        mostrar, detecciones = self._preparar_display(anotado, detecciones)
        self._dibujar(mostrar, detecciones)
        self._ultimo_frame = mostrar
        self._ultimo_frame_ts = time.time()
        self._ultimas_detecciones = detecciones

    @property
    def frame_actual(self):
        return self._ultimo_frame

    # ---------------- Modo telefono (servidor_web) ----------------
    def recibir_frame_telefono(self, frame):
        """Encola un frame subido por el telefono. Hilo del servidor web."""
        # espera breve al arranque del hilo (carrera start() vs primer frame)
        fin = time.time() + 1.0
        while not self._corriendo and time.time() < fin:
            time.sleep(0.02)
        if not self._corriendo:
            return False
        try:
            self._cola_telefono.put_nowait(frame)
        except queue.Full:
            pass  # descarta atrasados: siempre interesa el mas reciente
        return True

    def esperar_frame_anotado(self, ts_enviado, timeout=0.8):
        """Espera hasta que haya un frame anotado posterior a ts_enviado.

        Se llama desde el hilo del servidor web justo despues de encolar el
        frame del telefono; devuelve el frame anotado o None si no hubo
        tiempo de procesarlo (telefono con señal debil, CPU saturada...).
        """
        fin = time.time() + timeout
        while time.time() < fin:
            if (self._ultimo_frame_ts > ts_enviado
                    and self._ultimo_frame is not None):
                return self._ultimo_frame
            time.sleep(0.02)
        return None

    def detener(self):
        self._corriendo = False
        self._reader_stop.set()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=5.0)
        if self._worker is not None and self._worker.is_alive():
            self._worker.join(timeout=5.0)
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
            with _OPENER_SIN_PROXY.open(req, timeout=segundos) as r:
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
