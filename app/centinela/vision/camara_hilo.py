#!/usr/bin/env python3
"""
Centinela Control — Hilo de cámara y reconocimiento (multi-cámara, múltiples rostros)

Detecta todas las caras con el detector facial, extrae embeddings y compara contra
referencias. Cada cara identificada debe mantenerse reconocida durante
CONFIRM_SEG segundos seguidos sin bajar del umbral antes de marcar asistencia.

Soporta:
  - Cámara local (índice numérico): usa cv2.VideoCapture
  - Stream MJPEG por HTTP (ESP32): lee el stream manualmente y decodifica los frames JPEG.
  - Cámara de teléfono vía WebSocket binario HTTPS.
  - Modo solo preview (`reconocer=False`) y actualización de referencias en caliente.

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

import os, sys, time, math, socket, http.client, urllib.error, urllib.request
import threading
import queue
import cv2
import numpy as np

from PyQt6.QtCore import QThread, pyqtSignal

try:
    from PIL import Image as _PilImage, ImageDraw as _PilDraw, ImageFont as _PilFont
    _HAS_PIL = True
except Exception:
    _HAS_PIL = False

_PIL_FONT_CACHE = {}
_MASK_ESQ_CACHE = {}
_TAG_STATIC_CACHE = {}
_HUD_MEASURE_DRAW = _PilDraw.Draw(_PilImage.new("RGBA", (1, 1))) if _HAS_PIL else None
_MDNS_IP_CACHE = {}
_LOCK_MDNS_CACHE = threading.Lock()
_MDNS_CACHE_TTL = 300.0


def _cargar_fuente_hud(px, negrita=True):
    """Carga y cachea una fuente TrueType (Segoe UI) para el HUD de escaneo."""
    if not _HAS_PIL:
        return None
    clave = (int(px), bool(negrita))
    if clave in _PIL_FONT_CACHE:
        return _PIL_FONT_CACHE[clave]
    rutas = (
        [r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeuib.ttf",
         r"C:\Windows\Fonts\arialbd.ttf"]
        if negrita else
        [r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\arial.ttf"]
    )
    fuente = None
    for r in rutas:
        if os.path.isfile(r):
            try:
                fuente = _PilFont.truetype(r, max(10, int(px)))
                break
            except Exception:
                pass
    _PIL_FONT_CACHE[clave] = fuente
    return fuente

# Opener HTTP sin proxy: el stream va por LAN directa y un proxy del sistema
# (tipico en red de campus) romperia la lectura del MJPEG.
_OPENER_SIN_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _resolver_url_http(url, forzar_refresco=False):
    """Resuelve un host (.local/DNS) a IPv4 con caché de 300 s.

    Devuelve (url_resuelta, netloc_original_o_None) para evitar los 1-2.5 s de
    espera mDNS de Windows al abrir, reconectar o probar la cámara.
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(str(url or "").strip())
    host = parsed.hostname
    if not host:
        return url, None
    try:
        socket.inet_aton(host)
        return url, None  # Ya es una dirección IPv4 directa
    except OSError:
        pass

    ahora = time.monotonic()
    ip = None
    if not forzar_refresco:
        with _LOCK_MDNS_CACHE:
            cached = _MDNS_IP_CACHE.get(host)
            if cached is not None and (ahora - cached[1]) < _MDNS_CACHE_TTL:
                ip = cached[0]
    if ip is None:
        try:
            puerto = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError:
            puerto = 80
        try:
            infos = socket.getaddrinfo(host, puerto, socket.AF_INET, socket.SOCK_STREAM)
            if infos:
                ip = infos[0][4][0]
                with _LOCK_MDNS_CACHE:
                    _MDNS_IP_CACHE[host] = (ip, ahora)
        except OSError:
            ip = None
    if not ip:
        return url, None
    try:
        puerto_url = parsed.port
    except ValueError:
        puerto_url = None
    netloc_ip = f"{ip}:{puerto_url}" if puerto_url else ip
    url_ip = urlunparse((parsed.scheme, netloc_ip, parsed.path,
                         parsed.params, parsed.query, parsed.fragment))
    return url_ip, parsed.netloc


def _crear_request_http(url, forzar_refresco=False):
    """Construye Request HTTP preservando cabecera Host si resolvió mDNS/DNS a IP."""
    url_res, host_orig = _resolver_url_http(url, forzar_refresco=forzar_refresco)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    if host_orig:
        headers["Host"] = host_orig
    return urllib.request.Request(url_res, headers=headers)

from centinela.vision.rostros import pipeline_embeddings

CONFIRM_SEG = 3.0          # segundos seguidos necesarios para confirmar
CENTRO_DIST_MAX = 100.0
GRACE_SEG = 0.8     # px máximo entre frames para considerar misma cara

# --- Paleta del overlay (BGR) ---
# teal Fluent al verificar, esmeralda al confirmar, coral suave para desconocidos.
COLOR_IDENT = (191, 212, 45)    # #2dd4bf
COLOR_CONFIRM = (153, 211, 52)  # #34d399
COLOR_DESC = (113, 113, 248)    # #f87171
COLOR_SIN_DATOS = (140, 140, 140)
CHIP_FONDO = (26, 18, 13)       # #0d121a (glass oscuro)
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


class _FiltroOneEuro:
    """Filtro adaptativo 1-Euro: firme cuando el rostro está quieto, ágil al moverse."""

    def __init__(self, min_cutoff=1.4, beta=6.5, d_cutoff=1.0):
        self.min_cutoff = float(min_cutoff)
        self.beta = float(beta)
        self.d_cutoff = float(d_cutoff)
        self._x = None
        self._dx = 0.0

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2.0 * math.pi * max(1e-4, cutoff))
        return 1.0 / (1.0 + tau / dt)

    def filtrar(self, x_obj, dt):
        x_obj = float(x_obj)
        if self._x is None:
            self._x = x_obj
            self._dx = 0.0
            return x_obj
        dt = max(1e-3, min(0.25, float(dt)))
        a_d = self._alpha(self.d_cutoff, dt)
        dx_raw = (x_obj - self._x) / dt
        self._dx = a_d * dx_raw + (1.0 - a_d) * self._dx
        cutoff = self.min_cutoff + self.beta * abs(self._dx)
        a = self._alpha(cutoff, dt)
        self._x = a * x_obj + (1.0 - a) * self._x
        return self._x


class _EstadoVisualRostro:
    """Estado interpolado de un rostro para renderizar el HUD a 30+ fps sin tirones."""

    def __init__(self, det, ahora):
        cx, cy, bw, bh = self._de_caja(det["caja_norm"])
        self.f_cx = _FiltroOneEuro(min_cutoff=1.4, beta=6.5)
        self.f_cy = _FiltroOneEuro(min_cutoff=1.4, beta=6.5)
        self.f_bw = _FiltroOneEuro(min_cutoff=1.1, beta=4.0)
        self.f_bh = _FiltroOneEuro(min_cutoff=1.1, beta=4.0)
        self.cx = self.f_cx.filtrar(cx, 0.033)
        self.cy = self.f_cy.filtrar(cy, 0.033)
        self.bw = self.f_bw.filtrar(bw, 0.033)
        self.bh = self.f_bh.filtrar(bh, 0.033)
        self.obj = (cx, cy, bw, bh)

        self.alumno_id = det.get("alumno_id")
        self.identificado = bool(det.get("identificado"))
        self.nombre = det.get("nombre", "")
        self.sim = float(det.get("sim", 0.0))
        self.confirmado = bool(det.get("confirmado"))
        self.progreso_obj = det.get("progreso")
        self.progreso_vis = float(self.progreso_obj) if self.progreso_obj is not None else 0.0

        c_ini = self._color_objetivo()
        self.color_vis = [float(c_ini[0]), float(c_ini[1]), float(c_ini[2])]

        self.opacidad = 0.0
        self.escala = 1.07
        self.activo = True
        self.t_visto = ahora
        self.t_confirmado = ahora if self.confirmado else None

    @staticmethod
    def _de_caja(caja_norm):
        x1, y1, x2, y2 = caja_norm
        return (x1 + x2) * 0.5, (y1 + y2) * 0.5, max(0.01, x2 - x1), max(0.01, y2 - y1)

    def _color_objetivo(self):
        if self.identificado and self.confirmado:
            return COLOR_CONFIRM
        if self.identificado:
            return COLOR_IDENT
        return COLOR_DESC

    def actualizar_objetivo(self, det, ahora):
        self.obj = self._de_caja(det["caja_norm"])
        estaba_conf = self.confirmado
        self.alumno_id = det.get("alumno_id")
        self.identificado = bool(det.get("identificado"))
        self.nombre = det.get("nombre", "")
        self.sim = float(det.get("sim", 0.0))
        self.confirmado = bool(det.get("confirmado"))
        self.progreso_obj = det.get("progreso")
        if self.confirmado and not estaba_conf:
            self.t_confirmado = ahora
        elif not self.confirmado:
            self.t_confirmado = None
        self.activo = True
        self.t_visto = ahora

    def paso(self, dt, ahora, confirm_seg):
        dt = max(1e-3, min(0.15, float(dt)))
        ocx, ocy, obw, obh = self.obj
        self.cx = self.f_cx.filtrar(ocx, dt)
        self.cy = self.f_cy.filtrar(ocy, dt)
        self.bw = self.f_bw.filtrar(obw, dt)
        self.bh = self.f_bh.filtrar(obh, dt)

        # Opacidad: fade-in rápido, espera corta (evita parpadeo) y fade-out suave
        if self.activo:
            self.opacidad = min(1.0, self.opacidad + dt / 0.16)
        else:
            sin_ver = ahora - self.t_visto
            if sin_ver > 0.22:
                self.opacidad = max(0.0, self.opacidad - dt / 0.22)

        # Escala de encaje inicial (1.07 -> 1.0) + micro-pulso al confirmar
        k_esc = 1.0 - math.exp(-14.0 * dt)
        self.escala += (1.0 - self.escala) * k_esc
        factor_escala = self.escala
        if self.t_confirmado is not None:
            dt_conf = ahora - self.t_confirmado
            if 0.0 <= dt_conf < 0.36:
                factor_escala *= 1.0 - 0.035 * math.sin(math.pi * (dt_conf / 0.36))

        # Transición suave de color (p.ej. teal -> esmeralda)
        c_obj = self._color_objetivo()
        k_col = 1.0 - math.exp(-10.0 * dt)
        for i in range(3):
            self.color_vis[i] += (float(c_obj[i]) - self.color_vis[i]) * k_col

        # Progreso fluido entre inferencias
        if self.identificado and self.progreso_obj is not None:
            if self.confirmado:
                objetivo_p = 1.0
            else:
                avance_extra = min(0.06, max(0.0, (ahora - self.t_visto) / max(0.5, float(confirm_seg))))
                objetivo_p = min(1.0, float(self.progreso_obj) + avance_extra)
            k_p = 1.0 - math.exp(-12.0 * dt)
            self.progreso_vis += (objetivo_p - self.progreso_vis) * k_p
        elif self.confirmado:
            self.progreso_vis = 1.0

        return factor_escala


class _SuavizadorHUD:
    """Administra la transición suave de posición, progreso, color y opacidad de todos los rostros."""

    def __init__(self):
        self._lock = threading.Lock()
        self._estados = []
        self._confirm_seg = CONFIRM_SEG

    def limpiar(self):
        with self._lock:
            self._estados.clear()

    def actualizar_objetivos(self, detecciones_norm, confirm_seg=CONFIRM_SEG):
        ahora = time.monotonic()
        with self._lock:
            self._confirm_seg = max(0.5, float(confirm_seg or CONFIRM_SEG))
            usados = set()
            for det in (detecciones_norm or []):
                aid = det.get("alumno_id") if det.get("identificado") else None
                cx, cy, _w, _h = _EstadoVisualRostro._de_caja(det["caja_norm"])

                # 1. Emparejar por alumno_id
                mejor_idx = None
                if aid:
                    for idx, est in enumerate(self._estados):
                        if idx not in usados and est.alumno_id == aid:
                            mejor_idx = idx
                            break

                # 2. Emparejar por cercanía espacial
                if mejor_idx is None:
                    mejor_d = 0.22
                    for idx, est in enumerate(self._estados):
                        if idx in usados:
                            continue
                        d = math.hypot(cx - est.cx, cy - est.cy)
                        if d < mejor_d:
                            mejor_d = d
                            mejor_idx = idx

                if mejor_idx is not None:
                    self._estados[mejor_idx].actualizar_objetivo(det, ahora)
                    usados.add(mejor_idx)
                else:
                    self._estados.append(_EstadoVisualRostro(det, ahora))
                    usados.add(len(self._estados) - 1)

            for idx, est in enumerate(self._estados):
                if idx not in usados:
                    est.activo = False

    def paso(self, ancho, alto, dt):
        ahora = time.monotonic()
        salida = []
        with self._lock:
            vivos = []
            for est in self._estados:
                factor_esc = est.paso(dt, ahora, self._confirm_seg)
                if not est.activo and est.opacidad <= 0.015:
                    continue
                vivos.append(est)
                hw = est.bw * 0.5 * factor_esc * ancho
                hh = est.bh * 0.5 * factor_esc * alto
                cx_px = est.cx * ancho
                cy_px = est.cy * alto
                prog = (
                    est.progreso_vis
                    if (est.identificado and est.progreso_obj is not None)
                    else None
                )
                salida.append({
                    "nombre": est.nombre,
                    "sim": est.sim,
                    "caja": (cx_px - hw, cy_px - hh, cx_px + hw, cy_px + hh),
                    "identificado": est.identificado,
                    "alumno_id": est.alumno_id,
                    "color": (int(round(est.color_vis[0])),
                              int(round(est.color_vis[1])),
                              int(round(est.color_vis[2]))),
                    "progreso": prog,
                    "confirmado": est.confirmado,
                    "opacidad": est.opacidad,
                })
            self._estados = vivos
        return salida


class CamaraHilo(QThread):
    frame_listo = pyqtSignal(object, list)
    progreso_cara = pyqtSignal(int, float, str, str, tuple)
    cara_confirmada = pyqtSignal(int, str, str, float, tuple)
    estado_camara = pyqtSignal(bool)
    metricas_camara = pyqtSignal(float, float)
    error = pyqtSignal(str)

    def __init__(self, fuente, referencias, umbral_global=0.75,
                 intervalo_segundos=0.1, confirm_seg=CONFIRM_SEG,
                 reconocer=True, modo_escaneo="uno",
                 procesar_cada_n=1, recorte_aspecto="Original"):
        super().__init__()
        self.fuente = fuente
        self._lock_ref = threading.Lock()
        self.referencias = list(referencias or [])
        self._refs_mat = self._construir_refs_mat(self.referencias)
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
        self._suavizador = _SuavizadorHUD()
        self._mjpeg_buf = bytearray()
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0

        # Desacoplar lectura del stream del procesamiento mediante hilo trabajador persistente.
        self._procesando = False
        self._ai_queue = queue.Queue(maxsize=1)
        self._ai_stop = threading.Event()
        self._ai_thread = None
        self._frame_count = 0
        self.procesar_cada_n = max(1, int(procesar_cada_n or 1))
        self.recorte_aspecto = str(recorte_aspecto or "Original")

        # Productor/consumidor: hilo lector llena la cola, bucle principal consume
        self._frame_queue = queue.Queue(maxsize=2)
        self._reader_thread = None
        self._reader_stop = threading.Event()
        self._ultimo_frame_raw = None  # ultimo frame leido (para preview sin bloqueo)
        self._ultimo_decode_ms = 0.0

        # Modo telefono: la "fuente" es telefono:<id> y los frames llegan
        # subidos por HTTP (servidor_web). Mismo pipeline y misma logica de
        # confirmacion que una camara fisica.
        self._es_telefono = isinstance(fuente, str) and fuente.startswith("telefono:")
        self._cola_telefono = queue.Queue(maxsize=1)
        self._ultimo_frame_ts = 0.0
        self._ultimo_frame_display = None
        self._lock_dets_tel = threading.Lock()
        self._dets_tel_norm = []
        self._dets_tel_seq = 0

    # ---------------- Referencias en caliente ----------------
    @staticmethod
    def _construir_refs_mat(refs):
        if not refs:
            return None
        try:
            mats = [np.asarray(r["emb"], dtype=np.float32) for r in refs]
            dim0 = mats[0].shape
            if len(dim0) == 1 and all(m.shape == dim0 for m in mats):
                return np.ascontiguousarray(np.stack(mats, axis=0))
        except Exception:
            pass
        return None

    def set_referencias(self, nuevas):
        """Reemplaza las referencias aunque el hilo esté corriendo."""
        lista = list(nuevas or [])
        mat = self._construir_refs_mat(lista)
        with self._lock_ref:
            self.referencias = lista
            self._refs_mat = mat

    def _refs_snapshot(self):
        with self._lock_ref:
            return list(self.referencias), self._refs_mat

    # ---------------- Bucle principal ----------------
    def run(self):
        self._corriendo = True
        self._tracks = {}
        self._next_track_id = 0
        self._ultimo_frame = None
        self._ultimo_frame_display = None
        self._ultimo_frame_ts = 0.0
        self._ultimas_detecciones = []
        with self._lock_dets_tel:
            self._dets_tel_norm = []
            self._dets_tel_seq = 0
        self._suavizador.limpiar()
        self._mjpeg_buf = bytearray()
        self._intentos_reconexion = 0
        self._t_inicio_fallo = None
        self._frames_consecutivos_fallidos = 0
        try:
            self._abrir()
            # Arrancar hilo trabajador persistente de IA y hilo lector
            while not self._ai_queue.empty():
                try:
                    self._ai_queue.get_nowait()
                except queue.Empty:
                    break
            self._ai_stop.clear()
            if self.reconocer:
                self._ai_thread = threading.Thread(target=self._ai_loop, daemon=True)
                self._ai_thread.start()
            self._reader_stop.clear()
            self._reader_thread = threading.Thread(target=self._reader, daemon=True)
            self._reader_thread.start()

            t_prev = time.time()
            t_ultimo_emit = 0.0
            self._t_ultimo_log_med = 0.0
            ultimo_frame_consumido = None
            while self._corriendo:
                # Consumir frame de la cola con espera acotada para despertar sin polling
                # ni jitter, manteniendo ~33 fps de HUD si la cámara emite más lento.
                frame = None
                frame_nuevo = False
                ahora_m = time.monotonic()
                if ultimo_frame_consumido is None:
                    espera = 0.030
                elif t_ultimo_emit > 0.0:
                    espera = max(0.002, 0.030 - (ahora_m - t_ultimo_emit))
                else:
                    espera = 0.002
                try:
                    frame = self._frame_queue.get(timeout=espera)
                    while True:
                        try:
                            frame = self._frame_queue.get_nowait()
                        except queue.Empty:
                            break
                    ultimo_frame_consumido = frame
                    frame_nuevo = True
                except queue.Empty:
                    frame = ultimo_frame_consumido

                if frame is None:
                    continue

                ahora_m = time.monotonic()
                dt_emit = (
                    0.033 if t_ultimo_emit <= 0.0
                    else max(0.005, min(0.15, ahora_m - t_ultimo_emit))
                )
                t_ultimo_emit = ahora_m

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
                    try:
                        self._ai_queue.put_nowait((frame_copia, dt))
                    except queue.Full:
                        self._procesando = False

                mostrar, _d = self._preparar_display(frame, [])
                mh, mw = mostrar.shape[:2]
                dets_vis = self._suavizador.paso(mw, mh, dt_emit)
                if dets_vis:
                    try:
                        self._dibujar(mostrar, dets_vis)
                    except Exception:
                        pass
                self._ultimas_detecciones = dets_vis
                self._ultimo_frame_display = mostrar
                self.frame_listo.emit(mostrar, dets_vis)
                if not frame_nuevo:
                    self.msleep(1)
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self._reader_stop.set()
            self._ai_stop.set()
            if self._reader_thread is not None and self._reader_thread.is_alive():
                self._reader_thread.join(timeout=5.0)
            if self._ai_thread is not None and self._ai_thread.is_alive():
                self._ai_thread.join(timeout=5.0)
            self._liberar()
            self.estado_camara.emit(False)

    def _intervalo_segundos(self):
        return max(0.05, self.intervalo_segundos)

    def _ai_loop(self):
        """Hilo trabajador persistente para inferencia facial."""
        while self._corriendo and not self._ai_stop.is_set():
            try:
                frame, dt = self._ai_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if not self._corriendo or self._ai_stop.is_set():
                break
            self._worker_procesar(frame, dt)

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
        t_vent = time.monotonic()
        frames_vent = 0
        ms_ema = 0.0
        while self._corriendo and not self._reader_stop.is_set():
            t_lec = time.perf_counter()
            try:
                ok, frame = self._leer_frame()
            except Exception as e:
                print(f"[CAM] reader error: {type(e).__name__}: {e}")
                ok, frame = False, None
            t_lec = time.perf_counter() - t_lec
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
            ms_frame = self._ultimo_decode_ms if self._es_telefono else (t_lec * 1000.0)
            ms_ema = ms_frame if ms_ema <= 0.0 else (ms_ema * 0.75 + ms_frame * 0.25)
            frames_vent += 1
            ahora_r = time.monotonic()
            dt_vent = ahora_r - t_vent
            if dt_vent >= 1.0:
                self.metricas_camara.emit(frames_vent / dt_vent, ms_ema)
                frames_vent = 0
                t_vent = ahora_r
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
            return frame.copy(), dets
        ancho_d, alto_d = tam[0], tam[1]
        if ancho_d <= 0 or alto_d <= 0:
            return frame.copy(), dets
        alto_f, ancho_f = frame.shape[:2]
        escala = min(ancho_d / ancho_f, alto_d / alto_f)
        if escala <= 0:
            return frame.copy(), dets
        tw, th = max(1, round(ancho_f * escala)), max(1, round(alto_f * escala))
        if tw == ancho_f and th == alto_f and not dets:
            return frame.copy(), dets
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

    def _componer_rgba(self, frame, x, y, rgba_arr):
        """Compone un parche RGBA sobre el frame BGR (alpha blending rápido)."""
        alto_f, ancho_f = frame.shape[:2]
        ph, pw = rgba_arr.shape[:2]
        x1, y1 = max(0, int(x)), max(0, int(y))
        x2, y2 = min(ancho_f, int(x) + pw), min(alto_f, int(y) + ph)
        if x2 <= x1 or y2 <= y1:
            return
        sx1, sy1 = x1 - int(x), y1 - int(y)
        sx2, sy2 = sx1 + (x2 - x1), sy1 + (y2 - y1)
        patch = rgba_arr[sy1:sy2, sx1:sx2]
        alpha = patch[:, :, 3:4].astype(np.float32) / 255.0
        # PIL es RGB; OpenCV es BGR
        bgr_patch = patch[:, :, 2::-1].astype(np.float32)
        roi = frame[y1:y2, x1:x2].astype(np.float32)
        frame[y1:y2, x1:x2] = np.clip(
            bgr_patch * alpha + roi * (1.0 - alpha), 0, 255).astype(np.uint8)

    def _dibujar_esquinas_redondeadas(self, frame, ax1, ay1, ax2, ay2, color, k, opacidad=1.0):
        """Esquinas curvas antialias estilo FaceID con halo translúcido y sombra suave."""
        opacidad = max(0.0, min(1.0, float(opacidad)))
        if opacidad <= 0.01:
            return
        x1, y1 = int(round(ax1)), int(round(ay1))
        x2, y2 = int(round(ax2)), int(round(ay2))
        bw, bh = max(1, x2 - x1), max(1, y2 - y1)
        if bw < 16 or bh < 16:
            return
        r = max(6, min(22, int(round(min(bw, bh) * 0.13))))
        L = max(r + 6, int(round(min(bw, bh) * 0.24)))
        L = min(L, bw // 2 - 2, bh // 2 - 2)
        r = min(r, max(3, L - 2))
        thick = max(2, round(2.5 * k))

        pad = thick + 5
        q = pad + L + 2
        S = 2 * q
        clave_mask = (r, L, thick)
        cache_item = _MASK_ESQ_CACHE.get(clave_mask)
        if cache_item is None:
            sx1, sy1 = pad, pad
            sx2, sy2 = S - pad, S - pad

            def _trazar_mask(mask, dx, dy, grosor):
                cx1, cy1 = sx1 + dx, sy1 + dy
                cx2, cy2 = sx2 + dx, sy2 + dy
                cv2.ellipse(mask, (cx1 + r, cy1 + r), (r, r), 0, 180, 270, 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx1 + r, cy1), (cx1 + L, cy1), 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx1, cy1 + r), (cx1, cy1 + L), 255, grosor, cv2.LINE_AA)

                cv2.ellipse(mask, (cx2 - r, cy1 + r), (r, r), 0, 270, 360, 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx2 - L, cy1), (cx2 - r, cy1), 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx2, cy1 + r), (cx2, cy1 + L), 255, grosor, cv2.LINE_AA)

                cv2.ellipse(mask, (cx2 - r, cy2 - r), (r, r), 0, 0, 90, 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx2 - L, cy2), (cx2 - r, cy2), 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx2, cy2 - L), (cx2, cy2 - r), 255, grosor, cv2.LINE_AA)

                cv2.ellipse(mask, (cx1 + r, cy2 - r), (r, r), 0, 90, 180, 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx1 + r, cy2), (cx1 + L, cy2), 255, grosor, cv2.LINE_AA)
                cv2.line(mask, (cx1, cy2 - L), (cx1, cy2 - r), 255, grosor, cv2.LINE_AA)

            m_shadow = np.zeros((S, S), dtype=np.uint8)
            m_glow = np.zeros((S, S), dtype=np.uint8)
            m_main = np.zeros((S, S), dtype=np.uint8)
            _trazar_mask(m_shadow, 0, 1, thick + 1)
            _trazar_mask(m_glow, 0, 0, thick + 4)
            _trazar_mask(m_main, 0, 0, thick)

            a_s = (m_shadow.astype(np.float32) * (115.0 / 65025.0))[:, :, None]
            a_g = (m_glow.astype(np.float32) * (44.0 / 65025.0))[:, :, None]
            a_m = (m_main.astype(np.float32) * (248.0 / 65025.0))[:, :, None]

            w_accent = a_m + a_g * (1.0 - a_m)
            w_shad = a_s * (1.0 - a_g) * (1.0 - a_m)
            shad_layer = np.array([16.0, 10.0, 6.0], dtype=np.float32) * w_shad
            cov_base = w_accent + w_shad
            if len(_MASK_ESQ_CACHE) >= 32:
                _MASK_ESQ_CACHE.clear()
            cache_item = (q, w_accent, shad_layer, cov_base)
            _MASK_ESQ_CACHE[clave_mask] = cache_item

        q, w_accent, shad_layer, cov_base = cache_item
        col_bgr = np.array([color[0], color[1], color[2]], dtype=np.float32)
        premul = col_bgr * w_accent + shad_layer
        cov = cov_base
        if opacidad < 0.999:
            premul = premul * opacidad
            cov = cov_base * opacidad

        # Volcar los 4 cuadrantes directamente sobre las esquinas de frame
        alto_f, ancho_f = frame.shape[:2]
        cuadrantes = [
            (x1 - pad,     y1 - pad,     0, 0),
            (x2 - L - 2,   y1 - pad,     q, 0),
            (x1 - pad,     y2 - L - 2,   0, q),
            (x2 - L - 2,   y2 - L - 2,   q, q),
        ]
        for fx, fy, qx, qy in cuadrantes:
            fx1, fy1 = max(0, fx), max(0, fy)
            fx2, fy2 = min(ancho_f, fx + q), min(alto_f, fy + q)
            if fx2 <= fx1 or fy2 <= fy1:
                continue
            sx_a = qx + (fx1 - fx)
            sy_a = qy + (fy1 - fy)
            sx_b = sx_a + (fx2 - fx1)
            sy_b = sy_a + (fy2 - fy1)
            p_sub = premul[sy_a:sy_b, sx_a:sx_b]
            a_sub = cov[sy_a:sy_b, sx_a:sx_b]
            roi = frame[fy1:fy2, fx1:fx2].astype(np.float32)
            frame[fy1:fy2, fx1:fx2] = np.clip(
                p_sub + roi * (1.0 - a_sub), 0, 255).astype(np.uint8)

    def _dibujar_linea_escaneo(self, frame, ax1, ay1, ax2, ay2, color, k, opacidad=1.0):
        """Haz horizontal degradado con caída gaussiana y bordes suaves."""
        opacidad = max(0.0, min(1.0, float(opacidad)))
        if opacidad <= 0.01:
            return
        x1 = max(0, int(round(ax1)) + max(6, round(8 * k)))
        x2 = min(frame.shape[1], int(round(ax2)) - max(6, round(8 * k)))
        y1 = int(round(ay1)) + max(10, round(12 * k))
        y2 = int(round(ay2)) - max(10, round(12 * k))
        if x2 <= x1 + 8 or y2 <= y1 + 12:
            return
        fase = 0.5 - 0.5 * math.cos(time.monotonic() * 2.8)
        y_beam = y1 + (y2 - y1) * fase
        sigma_halo = max(3.5, 5.0 * k)
        sigma_core = max(1.0, 1.3 * k)
        half_h = max(8, int(round(10 * k)))
        ya = max(0, int(math.floor(y_beam - half_h)))
        yb = min(frame.shape[0], int(math.ceil(y_beam + half_h)) + 1)
        if yb <= ya:
            return
        ys = np.arange(ya, yb, dtype=np.float32)[:, None]
        dy = ys - float(y_beam)
        prof_v = (
            0.20 * np.exp(-((dy / sigma_halo) ** 2))
            + 0.48 * np.exp(-((dy / sigma_core) ** 2))
        )
        w_beam = x2 - x1
        taper_h = (np.maximum(0.0, np.sin(np.pi * np.linspace(0.0, 1.0, w_beam, dtype=np.float32))) ** 0.65)[None, :]
        alpha = np.clip(prof_v * taper_h * opacidad, 0.0, 0.85)[:, :, None]
        col_bgr = np.array(color, dtype=np.float32).reshape(1, 1, 3)
        roi = frame[ya:yb, x1:x2].astype(np.float32)
        frame[ya:yb, x1:x2] = np.clip(
            col_bgr * alpha + roi * (1.0 - alpha), 0, 255).astype(np.uint8)

    def _tag(self, frame, cx, y_top, y_bot, titulo, detalle, color,
             progreso=None, confirmado=False, opacidad=1.0):
        """Píldora glassmórfica flotante centrada sobre el rostro con tipografía nítida."""
        opacidad = max(0.0, min(1.0, float(opacidad)))
        if opacidad <= 0.01:
            return
        k = self._factor_metrica(frame)
        alto_f, ancho_f = frame.shape[:2]
        b, g, r = [int(v) for v in color]
        accent_rgb = (r, g, b)

        if _HAS_PIL:
            clave_tag = (
                round(k, 2), str(titulo), str(detalle), accent_rgb, bool(confirmado), ancho_f
            ) if progreso is None else None
            cached_tag = _TAG_STATIC_CACHE.get(clave_tag) if clave_tag is not None else None
            if cached_tag is not None:
                arr_base, pw, ph, ox, oy = cached_tag
                dibujado_pil = True
            else:
                dibujado_pil = False
                fuente_tit = _cargar_fuente_hud(round(14 * k), negrita=True)
                fuente_det = _cargar_fuente_hud(round(12.5 * k), negrita=True)
                if fuente_tit is not None:
                    pad_x = max(10, round(12 * k))
                    pad_y = max(7, round(8 * k))
                    badge_r = max(6, round(6.5 * k)) if confirmado else max(4, round(4.2 * k))
                    ring_r = badge_r if confirmado else badge_r + max(2, round(2.5 * k))
                    gap_dot = max(7, round(8 * k))
                    gap_det = max(10, round(12 * k)) if detalle else 0
                    hbar = max(4, round(4.5 * k)) if progreso is not None else 0
                    gap_bar = max(6, round(6 * k)) if progreso is not None else 0

                    dummy = _HUD_MEASURE_DRAW
                    bb_t = dummy.textbbox((0, 0), titulo, font=fuente_tit)
                    tw, th = bb_t[2] - bb_t[0], bb_t[3] - bb_t[1]
                    dw = dh = 0
                    if detalle:
                        bb_d = dummy.textbbox((0, 0), detalle, font=fuente_det)
                        dw, dh = bb_d[2] - bb_d[0], bb_d[3] - bb_d[1]

                    row_h = max(th, dh, ring_r * 2 + 2)
                    pw = pad_x + ring_r * 2 + gap_dot + tw + gap_det + dw + pad_x
                    ph = pad_y + row_h + gap_bar + hbar + pad_y
                    pw = min(pw, max(80, ancho_f - 12))

                    pad_s = max(3, round(3.5 * k))
                    cw = pw + pad_s * 2
                    ch = ph + pad_s * 2 + 2
                    img = _PilImage.new("RGBA", (cw, ch), (0, 0, 0, 0))
                    draw = _PilDraw.Draw(img, "RGBA")
                    rad = min(round(10 * k), ph // 2)
                    ox, oy = pad_s, pad_s

                    # Sombra difusa suave bajo la píldora
                    draw.rounded_rectangle(
                        [ox, oy + 2, ox + pw - 1, oy + 2 + ph - 1],
                        radius=rad, fill=(0, 0, 0, 90),
                    )

                    # Fondo glass oscuro + borde fino en color de acento
                    draw.rounded_rectangle(
                        [ox, oy, ox + pw - 1, oy + ph - 1], radius=rad,
                        fill=(13, 18, 26, 226),
                        outline=(*accent_rgb, 170),
                        width=max(1, round(1.3 * k)),
                    )
                    # Reflejo sutil en el borde interior superior
                    if ox + pw - rad - 1 > ox + rad:
                        draw.line(
                            [(ox + rad, oy + 1), (ox + pw - rad - 1, oy + 1)],
                            fill=(255, 255, 255, 22), width=1,
                        )

                    # Indicador izquierdo antialias (2x supersampling)
                    ind_cx = ox + pad_x + ring_r
                    ind_cy = oy + pad_y + row_h // 2
                    slot_sz = ring_r * 2 + 4
                    ind_img = _PilImage.new("RGBA", (slot_sz * 2, slot_sz * 2), (0, 0, 0, 0))
                    ind_draw = _PilDraw.Draw(ind_img, "RGBA")
                    ic = float(slot_sz)
                    if confirmado:
                        br2 = badge_r * 2.0
                        ind_draw.ellipse(
                            [ic - br2, ic - br2, ic + br2, ic + br2],
                            fill=(*accent_rgb, 255),
                        )
                        chk_pts = [
                            (ic - br2 * 0.42, ic + br2 * 0.04),
                            (ic - br2 * 0.08, ic + br2 * 0.36),
                            (ic + br2 * 0.44, ic - br2 * 0.32),
                        ]
                        ind_draw.line(
                            chk_pts, fill=(10, 20, 16, 255),
                            width=max(3, round(3.4 * k)), joint="curve",
                        )
                    elif progreso is not None:
                        pulse = 0.5 + 0.5 * math.sin(time.monotonic() * 5.0)
                        pr2 = (badge_r + max(2, round((1.8 + 1.2 * pulse) * k))) * 2.0
                        br2 = badge_r * 2.0
                        ind_draw.ellipse(
                            [ic - pr2, ic - pr2, ic + pr2, ic + pr2],
                            fill=(*accent_rgb, int(55 + 90 * pulse)),
                        )
                        ind_draw.ellipse(
                            [ic - br2, ic - br2, ic + br2, ic + br2],
                            fill=(*accent_rgb, 255),
                        )
                    else:
                        rr2 = ring_r * 2.0
                        br2 = badge_r * 2.0
                        ind_draw.ellipse(
                            [ic - rr2, ic - rr2, ic + rr2, ic + rr2],
                            fill=(*accent_rgb, 65),
                        )
                        ind_draw.ellipse(
                            [ic - br2, ic - br2, ic + br2, ic + br2],
                            fill=(*accent_rgb, 255),
                        )
                    ind_aa = ind_img.resize((slot_sz, slot_sz), _PilImage.Resampling.BILINEAR)
                    img.paste(ind_aa, (int(round(ind_cx - slot_sz / 2)), int(round(ind_cy - slot_sz / 2))), ind_aa)

                    # Nombre del alumno
                    tx = ox + pad_x + ring_r * 2 + gap_dot
                    ty = oy + pad_y + (row_h - th) // 2 - bb_t[1]
                    draw.text((tx, ty), titulo, font=fuente_tit, fill=(248, 250, 252, 255))

                    # Porcentaje o estado a la derecha
                    if detalle:
                        dx = ox + pw - pad_x - dw
                        dy = oy + pad_y + (row_h - dh) // 2 - bb_d[1]
                        draw.text((dx, dy), detalle, font=fuente_det, fill=(*accent_rgb, 255))

                    # Barra de progreso redondeada sólida con degradado teal -> menta
                    if progreso is not None:
                        bx1 = ox + pad_x
                        bx2 = ox + pw - pad_x
                        by1 = oy + pad_y + row_h + gap_bar
                        by2 = by1 + hbar
                        br = hbar // 2
                        draw.rounded_rectangle(
                            [bx1, by1, bx2, by2], radius=br, fill=(48, 59, 74, 235))
                        frac = min(max(float(progreso), 0.0), 1.0)
                        if frac > 0.005:
                            fill_w = max(hbar, int(round((bx2 - bx1) * frac)))
                            fx2 = min(bx2, bx1 + fill_w)
                            fw = max(1, fx2 - bx1 + 1)
                            fh = max(1, by2 - by1 + 1)
                            light_rgb = tuple(min(255, int(c + (255 - c) * 0.36)) for c in accent_rgb)
                            grad_row = np.zeros((1, fw, 4), dtype=np.uint8)
                            t_grad = np.linspace(0.0, 1.0, fw, dtype=np.float32)
                            for ch_i in range(3):
                                grad_row[0, :, ch_i] = np.clip(
                                    accent_rgb[ch_i] * (1.0 - t_grad) + light_rgb[ch_i] * t_grad,
                                    0, 255,
                                ).astype(np.uint8)
                            grad_row[0, :, 3] = 255
                            grad_img = _PilImage.fromarray(np.repeat(grad_row, fh, axis=0), "RGBA")
                            mask = _PilImage.new("L", (fw, fh), 0)
                            _PilDraw.Draw(mask).rounded_rectangle(
                                [0, 0, fw - 1, fh - 1], radius=br, fill=255)
                            img.paste(grad_img, (bx1, by1), mask)

                    arr_base = np.array(img)
                    if clave_tag is not None:
                        if len(_TAG_STATIC_CACHE) >= 64:
                            _TAG_STATIC_CACHE.clear()
                        _TAG_STATIC_CACHE[clave_tag] = (arr_base, pw, ph, ox, oy)
                    dibujado_pil = True

            if dibujado_pil:
                if opacidad < 0.995:
                    arr = arr_base.copy()
                    arr[:, :, 3] = np.clip(
                        arr[:, :, 3].astype(np.float32) * opacidad, 0, 255).astype(np.uint8)
                else:
                    arr = arr_base

                gap_box = max(8, round(10 * k))
                x = int(round(cx - pw / 2.0)) - ox
                x = min(max(6 - ox, x), max(6 - ox, ancho_f - pw - 6 - ox))
                y = int(round(y_top)) - ph - gap_box - oy
                if y + oy < 6:
                    y = int(round(y_bot)) + gap_box - oy
                    if y + oy + ph > alto_f - 6:
                        y = max(6 - oy, alto_f - ph - 6 - oy)
                self._componer_rgba(frame, x, y, arr)
                return

        # Fallback OpenCV si Pillow/fuente no estuviera disponible
        texto = f"{titulo} {detalle}".strip()
        escala = 0.52 * k
        grosor = max(1, round(k))
        (tw, th), base = cv2.getTextSize(texto, cv2.FONT_HERSHEY_SIMPLEX, escala, grosor)
        pad_x = round(10 * k)
        hbar = max(3, round(4 * k)) if progreso is not None else 0
        alto = round(th + base + 14 * k) + hbar
        ancho = tw + pad_x * 2
        x = min(max(6, int(round(cx - ancho / 2.0))), max(6, ancho_f - ancho - 6))
        y = max(6, int(round(y_top)) - alto - round(8 * k))
        self._rect_alpha(frame, x, y, ancho, alto, CHIP_FONDO, 0.84 * opacidad)
        cv2.putText(frame, texto, (x + pad_x, y + (alto - hbar - th - base) // 2 + th),
                    cv2.FONT_HERSHEY_SIMPLEX, escala, CHIP_TEXTO, grosor, cv2.LINE_AA)
        if progreso is not None:
            ystrip = y + alto - hbar
            wfill = max(round(2 * k), int(ancho * min(max(progreso, 0.0), 1.0)))
            self._rect_alpha(frame, x, ystrip, wfill, hbar, color, opacidad)

    def _dibujar(self, frame, detecciones):
        """Dibuja el overlay moderno: esquinas curvas, línea de escaneo y píldora glass."""
        k = self._factor_metrica(frame)
        for det in detecciones:
            opacidad = max(0.0, min(1.0, float(det.get("opacidad", 1.0))))
            if opacidad <= 0.01:
                continue
            x1, y1, x2, y2 = [float(v) for v in det["caja"]]
            m = 10 * k
            ax1, ay1, ax2, ay2 = x1 - m, y1 - m, x2 + m, y2 + m
            ident = det.get("identificado")
            confirmado = bool(det.get("confirmado"))
            prog = det.get("progreso")

            color = det.get("color")
            if color is None:
                if ident and confirmado:
                    color = COLOR_CONFIRM
                elif ident:
                    color = COLOR_IDENT
                else:
                    color = COLOR_DESC

            # Esquinas curvas estilo FaceID
            self._dibujar_esquinas_redondeadas(
                frame, ax1, ay1, ax2, ay2, color, k, opacidad=opacidad)

            # Haz de escaneo sutil mientras se verifica
            if ident and not confirmado and prog is not None:
                self._dibujar_linea_escaneo(
                    frame, ax1, ay1, ax2, ay2, color, k, opacidad=opacidad)

            # Texto de la píldora flotante
            nombre_raw = str(det.get("nombre") or "").strip() if ident else "Rostro no registrado"
            if ident and nombre_raw.isupper():
                nombre_txt = nombre_raw.title()
            else:
                nombre_txt = nombre_raw or "Alumno"

            if ident:
                if confirmado:
                    detalle, progreso_bar = "Confirmado", None
                elif prog is not None:
                    detalle = f"{int(round(prog * 100))}%"
                    progreso_bar = prog
                else:
                    sim = max(0.0, min(1.0, float(det.get("sim", 0.0))))
                    detalle, progreso_bar = f"{sim * 100:.0f}%", None
            else:
                detalle, progreso_bar = "", None

            cx = (ax1 + ax2) / 2.0
            self._tag(
                frame, cx, ay1, ay2, nombre_txt, detalle, color,
                progreso_bar, confirmado=confirmado, opacidad=opacidad)

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
        return urlunparse((scheme, parsed.netloc, path, "", parsed.query, ""))

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
            req = _crear_request_http(self._url_http_normalizada())
            self._stream = _OPENER_SIN_PROXY.open(req, timeout=5)
            self._mjpeg_buf = bytearray()
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
            req = _crear_request_http(
                self._url_http_normalizada(),
                forzar_refresco=(self._intentos_reconexion > 1),
            )
            self._stream = _OPENER_SIN_PROXY.open(req, timeout=5)
            self._mjpeg_buf = bytearray()
            self._leer_hasta_boundary()
            print(f"[CAM] {time.strftime('%H:%M:%S')} stream reconectado")
            return True
        except Exception as e:
            print(f"[CAM] {time.strftime('%H:%M:%S')} reconexion fallo: {type(e).__name__}: {e}")
            return False

    def _leer_hasta_boundary(self):
        """Descarta los headers HTTP iniciales hasta encontrar el primer boundary del multipart."""
        datos = bytearray()
        read_fn = getattr(self._stream, "read1", self._stream.read)
        while self._corriendo:
            try:
                chunk = read_fn(4096)
                if not chunk:
                    break
                datos.extend(chunk)
                idx = datos.find(b"\r\n--")
                if idx == -1 and datos.startswith(b"--"):
                    idx = 0
                if idx == -1:
                    idx = datos.find(b"--\r\n")
                if idx != -1:
                    soi = datos.find(b"\xff\xd8", idx)
                    if soi != -1:
                        self._mjpeg_buf = bytearray(datos[soi:])
                    else:
                        self._mjpeg_buf = bytearray(datos[idx:])
                    return
            except (TimeoutError, OSError, urllib.error.URLError, http.client.HTTPException):
                break
        return

    def _leer_frame(self):
        """
        Devuelve (ok, frame_bgr).
        Modo telefono: extrae de la cola de frames (o JPEG crudo) subidos por el telefono.
        Para URLs HTTP lee el stream MJPEG frame por frame.
        Para cámaras locales usa cv2.VideoCapture.read().
        """
        if self._es_telefono:
            try:
                item = self._cola_telefono.get(timeout=0.5)
            except queue.Empty:
                return False, None
            if isinstance(item, (bytes, bytearray, memoryview)):
                t_dec = time.perf_counter()
                frame = cv2.imdecode(np.frombuffer(item, np.uint8), cv2.IMREAD_COLOR)
                self._ultimo_decode_ms = (time.perf_counter() - t_dec) * 1000.0
                if frame is None:
                    return False, None
                return True, frame
            self._ultimo_decode_ms = 0.0
            return True, item
        if self._es_url:
            return self._leer_frame_mjpeg()
        else:
            ok, frame = self._cap.read()
            return ok, frame

    def _preparar_estado_mjpeg(self):
        """Sincroniza lector, buffer mutable y offset tras abrir o reconectar."""
        stream = self._stream
        read_fn = getattr(stream, "read1", stream.read)
        buf = self._mjpeg_buf
        if not isinstance(buf, bytearray):
            buf = bytearray(buf)
            self._mjpeg_buf = buf
        return stream, read_fn, buf, 2

    def _leer_frame_mjpeg(self):
        """Lee un frame JPEG del stream multipart usando read1 para no esperar al siguiente chunk."""
        stream, read_fn, buf, buscar_desde = self._preparar_estado_mjpeg()

        # Lector MJPEG con read1() + bytearray:
        #   - HTTPResponse.read(1024) en streams chunked espera completar 1024 B y se bloquea
        #     hasta el siguiente frame entero (~80-100 ms extra). read1(16384) entrega de inmediato.
        #   - Se conserva cualquier SOI/bytes ya presentes en buf y se avanza buscar_desde.
        while self._corriendo:
            a = buf.find(b"\xff\xd8")  # SOI
            if a != -1:
                if a > 0:
                    del buf[:a]
                    buscar_desde = 2
                b = buf.find(b"\xff\xd9", max(2, buscar_desde))  # EOI después del SOI
                if b != -1:
                    jpg = bytes(buf[: b + 2])
                    del buf[: b + 2]
                    arr = np.frombuffer(jpg, dtype=np.uint8)
                    frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        self._intentos_reconexion = 0
                        return True, frame
                    # Frame corrupto: resincronizar buscando el siguiente SOI
                    buscar_desde = 2
                    continue
                buscar_desde = max(2, len(buf) - 1)
                if len(buf) > 2_000_000:
                    buf.clear()
                    buscar_desde = 2
            else:
                # Conservar el último byte por si \xff y \xd8 quedaron divididos entre chunks
                if len(buf) > 1:
                    del buf[:-1]
                buscar_desde = 2

            try:
                chunk = read_fn(16384)
                if not chunk:
                    if not self._corriendo:
                        return False, None
                    raise EOFError("stream cerro (EOF)")
            except (TimeoutError, OSError, ValueError, AttributeError,
                    http.client.HTTPException, EOFError) as e:
                if not self._corriendo:
                    return False, None
                print(f"[CAM] {time.strftime('%H:%M:%S')} lectura fallo: {type(e).__name__}: {e}")
                viva, _ = self._tras_fallo_lectura()
                if not viva:
                    return False, None
                stream, read_fn, buf, buscar_desde = self._preparar_estado_mjpeg()
                continue
            buf.extend(chunk)

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

        refs, refs_mat = self._refs_snapshot()

        # Identificar cada cara contra referencias
        identificadas = []  # lista de (caja, nombre, sim, alumno_id, identificado, color)
        for res in resultados:
            emb = res["emb"]
            caja = res["caja"]
            mejor = None
            mejor_sim = -1.0
            if refs_mat is not None and emb.shape[0] == refs_mat.shape[1]:
                sims = refs_mat @ np.asarray(emb, dtype=np.float32)
                idx_mejor = int(np.argmax(sims))
                mejor_sim = float(sims[idx_mejor])
                mejor = refs[idx_mejor]
            elif refs:
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
                # Los IDs internos no se muestran en la UI
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

        fw_s, fh_s = max(1, orig_w), max(1, orig_h)
        raw_norm = []
        for det in detecciones:
            x1, y1, x2, y2 = det["caja"]
            ident = bool(det["identificado"])
            nombre_raw = str(det.get("nombre") or "").strip() if ident else "Rostro no registrado"
            nombre_txt = nombre_raw.title() if (ident and nombre_raw.isupper()) else (nombre_raw or "Alumno")
            raw_norm.append({
                "id": det["alumno_id"] or "",
                "nombre": nombre_txt,
                "ident": ident,
                "conf": bool(det["confirmado"]),
                "prog": round(float(det["progreso"]), 3) if det["progreso"] is not None else None,
                "sim": round(float(det.get("sim", 0.0)), 3),
                "box": [
                    round(float(x1) / fw_s, 4),
                    round(float(y1) / fh_s, 4),
                    round(float(x2) / fw_s, 4),
                    round(float(y2) / fh_s, 4),
                ],
            })
        with self._lock_dets_tel:
            self._dets_tel_norm = raw_norm
            self._dets_tel_seq += 1

        dets_norm = self._cajas_norm_display(orig_w, orig_h, detecciones)
        self._suavizador.actualizar_objetivos(dets_norm, self.confirm_seg)
        self._ultimo_frame_ts = time.time()
        self._ultimas_detecciones = detecciones

    def _cajas_norm_display(self, orig_w, orig_h, detecciones):
        """Calcula caja_norm acorde a recorte_aspecto sin copiar ni escalar píxeles."""
        w = max(1, int(orig_w))
        h = max(1, int(orig_h))
        x0, y0 = 0, 0
        cw, ch = w, h
        aspecto = str(getattr(self, "recorte_aspecto", "Original") or "Original")
        if aspecto and aspecto.lower() != "original":
            try:
                aw, ah = aspecto.split(":")
                ratio = float(aw) / float(ah)
            except (ValueError, ZeroDivisionError):
                ratio = None
            if ratio and ratio > 0:
                if w / h > ratio:
                    cw = max(1, int(h * ratio))
                    x0 = (w - cw) // 2
                else:
                    ch = max(1, int(w / ratio))
                    y0 = (h - ch) // 2
        fw, fh = float(cw), float(ch)
        dets_norm = []
        for det in detecciones:
            x1, y1, x2, y2 = det["caja"]
            d = dict(det)
            d["caja_norm"] = (
                (x1 - x0) / fw, (y1 - y0) / fh,
                (x2 - x0) / fw, (y2 - y0) / fh,
            )
            dets_norm.append(d)
        return dets_norm

    @property
    def frame_actual(self):
        return self._ultimo_frame_display if self._ultimo_frame_display is not None else self._ultimo_frame

    def ultimas_detecciones_tel(self):
        """Últimas detecciones normalizadas al frame crudo del teléfono (thread-safe)."""
        with self._lock_dets_tel:
            return self._dets_tel_seq, self._dets_tel_norm

    # ---------------- Modo telefono (servidor_web) ----------------
    def _encolar_item_telefono(self, item):
        """Encola un frame o JPEG crudo reemplazando cualquier elemento previo."""
        # espera breve al arranque del hilo (carrera start() vs primer frame)
        fin = time.time() + 1.0
        while not self._corriendo and time.time() < fin:
            time.sleep(0.02)
        if not self._corriendo:
            return False
        try:
            self._cola_telefono.put_nowait(item)
        except queue.Full:
            try:
                self._cola_telefono.get_nowait()
            except queue.Empty:
                pass
            try:
                self._cola_telefono.put_nowait(item)
            except queue.Full:
                pass
        return True

    def recibir_frame_telefono(self, frame):
        """Encola un frame decodificado subido por el telefono (reemplaza el anterior si está llena)."""
        return self._encolar_item_telefono(frame)

    def recibir_jpeg_telefono(self, jpg_bytes):
        """Encola bytes JPEG crudos del telefono para decodificarlos en _reader."""
        if not jpg_bytes or len(jpg_bytes) < 4 or jpg_bytes[:2] != b"\xff\xd8":
            return False
        return self._encolar_item_telefono(bytes(jpg_bytes))

    def esperar_frame_anotado(self, ts_enviado, timeout=0.8):
        """Espera hasta que haya un frame anotado posterior a ts_enviado.

        Se llama desde el hilo del servidor web justo despues de encolar el
        frame del telefono; devuelve el frame anotado o None si no hubo
        tiempo de procesarlo (telefono con señal debil, CPU saturada...).
        """
        fin = time.time() + timeout
        while time.time() < fin:
            frame_disp = self.frame_actual
            if (self._ultimo_frame_ts > ts_enviado
                    and frame_disp is not None):
                return frame_disp
            time.sleep(0.02)
        return None

    def detener(self):
        self._corriendo = False
        self._reader_stop.set()
        self._ai_stop.set()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=5.0)
        if self._ai_thread is not None and self._ai_thread.is_alive():
            self._ai_thread.join(timeout=5.0)
        self.wait(3000)


def probar_fuente(tipo, fuente, segundos=6.0):
    """
    Prueba una fuente de cámara para la pestaña Configuración.
    Devuelve (ok, mensaje). No requiere modelos ni inferencia.
    """
    fuente = str(fuente or "").strip()
    if tipo in ("http", "esp32"):
        if not fuente.lower().startswith(("http://", "https://")):
            return False, f"URL inválida: {fuente or '(vacía)'}"
        try:
            req = _crear_request_http(fuente, forzar_refresco=True)
            t0 = time.time()
            with _OPENER_SIN_PROXY.open(req, timeout=segundos) as r:
                read_fn = getattr(r, "read1", r.read)
                datos = read_fn(8192)
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
