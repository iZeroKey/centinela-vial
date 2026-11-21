"""Animacion de texto para el LCD fisico (app-side, sin cambios en firmware).

El firmware solo imprime lo que recibe. Esta capa transforma el estado actual
en frames que cambian con el tiempo, siguiendo las reglas estrictas del
HD44780: 16 chars por linea, solo ASCII seguro, sin ghosting.
"""

import os
import threading
import time
import unicodedata

import config as config_app
from estado_lcd import obtener_datos_lcd


# ---------------- Estado interno de animacion ----------------
_lock = threading.RLock()
_estado_prev = None
_t_inicio_estado = None
_marq_state = {}   # texto -> timestamp de inicio de su ciclo

_config_cache = {"mtime": None, "cfg": {}}


def _cargar_config():
    try:
        mtime = os.path.getmtime(config_app.CONFIG_PATH)
    except OSError:
        mtime = None
    with _lock:
        if _config_cache["mtime"] != mtime:
            try:
                _config_cache["cfg"] = config_app.cargar()
            except Exception:
                _config_cache["cfg"] = {}
            _config_cache["mtime"] = mtime
        return _config_cache["cfg"]


def _ascii(texto):
    # Normaliza a ASCII y elimina caracteres inseguros para HD44780:
    # controles, backslash (se imprime como yen en ROM A00) y no imprimibles.
    out = []
    for ch in unicodedata.normalize("NFKD", str(texto or "")):
        if ch == "\\":
            continue
        if 32 <= ord(ch) <= 126:
            out.append(ch)
    return "".join(out)


def _pad(texto):
    return str(texto or "")[:16].ljust(16)


def _num_camara(camara):
    s = _ascii(camara).strip()
    if not s:
        return "1"
    # Extraer digitos; si no hay, usar primer caracter.
    digs = "".join(ch for ch in s if ch.isdigit())
    if digs:
        return digs[:2]
    return s[:1]


def _centrar(texto):
    t = _ascii(texto).strip()[:16]
    if len(t) >= 16:
        return t
    left = (16 - len(t)) // 2
    right = 16 - len(t) - left
    return " " * left + t + " " * right


def _config(cfg):
    base = {
        "activa": True,
        "velocidad": 1.0,
        "marquesina": True,
    }
    if isinstance(cfg, dict):
        base.update(cfg.get("lcd_animacion", {}) or {})
    return base


def _velocidad_segura(cfg):
    try:
        v = float(cfg.get("velocidad", 1.0))
    except (TypeError, ValueError):
        v = 1.0
    return min(2.0, max(0.5, v))


def _intervalo(base, velocidad):
    # Limita a >= 0.35 s para evitar ghosting y saturacion del enlace.
    return max(0.35, base / velocidad)


def _idx_frame(t, intervalo, n):
    # Cadencia determinista por piso (no redondeo, para no saltar frames).
    return int((t + 1e-9) / intervalo) % n


def _reset_si_cambio_estado(estado, ahora):
    global _estado_prev, _t_inicio_estado, _marq_state
    with _lock:
        if estado != _estado_prev:
            _estado_prev = estado
            _t_inicio_estado = ahora
            _marq_state.clear()


def _t_estado(ahora):
    with _lock:
        if _t_inicio_estado is None:
            return 0.0
        return max(0.0, ahora - _t_inicio_estado)


def _marquesina_pausada(texto, ahora, activa):
    t = _ascii(texto).strip()
    if len(t) <= 16:
        return _pad(t)
    if not activa:
        if len(t) > 16:
            return _pad(t[:13] + "...")
        return _pad(t)
    with _lock:
        inicio = _marq_state.get(t)
        if inicio is None:
            inicio = ahora
            _marq_state[t] = inicio
    elapsed = ahora - inicio
    posiciones = len(t) - 16 + 1
    tiempo_mov = max(0.0, (posiciones - 1) * 0.4)
    ciclo = 1.2 + tiempo_mov + 1.2
    e = elapsed % ciclo
    if e < 1.2:
        idx = 0
    elif e < 1.2 + tiempo_mov:
        idx = int((e - 1.2 + 1e-9) / 0.4)
        if idx > posiciones - 1:
            idx = posiciones - 1
    else:
        idx = posiciones - 1
    return _pad(t[idx:idx + 16])


def obtener_filas_animadas(cfg=None, ahora=None):
    """Devuelve (fila_1, fila_2) animadas, listas para enviar al LCD."""
    if ahora is None:
        ahora = time.monotonic()
    if cfg is None:
        cfg = _cargar_config()
    cfg = _config(cfg)
    if not cfg.get("activa", True):
        from estado_lcd import obtener_filas_lcd
        f1, f2 = obtener_filas_lcd()
        return _pad(_ascii(f1)), _pad(_ascii(f2))

    datos = obtener_datos_lcd()
    estado = (datos.get("estado") or "esperando").strip().lower()
    _reset_si_cambio_estado(estado, ahora)
    t = _t_estado(ahora)

    velocidad = _velocidad_segura(cfg)
    progreso = float(datos.get("progreso") or 0.0)
    progreso = max(0.0, min(100.0, progreso))
    nombre = _ascii(datos.get("nombre") or "").strip() or "IDENTIFICADO"
    motivo = _ascii(datos.get("motivo") or "").strip() or "INCOMPLETO"
    marquesina_on = bool(cfg.get("marquesina", True))

    # ---------------- Esperando ----------------
    if estado == "esperando":
        l1 = _centrar("CENTINELA CTRL")
        num = _num_camara(datos.get("camara"))
        frames_l2 = [
            f"ESPERANDO [{num}>  ]",
            f"ESPERANDO [ {num}> ]",
            f"ESPERANDO [  {num}>]",
            f"ESPERANDO [   {num}]",
            f"ESPERANDO [  {num}<]",
            f"ESPERANDO [ {num}< ]",
            f"ESPERANDO [{num}<  ]",
            f"ESPERANDO [{num}   ]",
        ]
        iv = _intervalo(0.45, velocidad)
        idx = _idx_frame(t, iv, len(frames_l2))
        l2 = _pad(frames_l2[idx])
        return l1, l2

    # ---------------- Procesando ----------------
    if estado == "procesando":
        iv = _intervalo(0.35, velocidad)
        if progreso < 30.0:
            if _idx_frame(t, iv, 2) == 0:
                l1 = _pad("ESCANEANDO  [= ]")
            else:
                l1 = _pad("ESCANEANDO  [ =]")
        else:
            if len(nombre) <= 16:
                l1 = _centrar(nombre)
            else:
                l1 = _marquesina_pausada(nombre, ahora, marquesina_on)
        # barra de 10 bloques
        bloques = int(progreso // 10)
        if bloques > 10:
            bloques = 10
        if progreso >= 100.0:
            chars = ["="] * 10
        else:
            chars = ["="] * bloques + ["-"] * (10 - bloques)
            tip = min(bloques, 9)
            if _idx_frame(t, iv, 2) == 0:
                chars[tip] = ">"
            else:
                chars[tip] = "-"
        barra = "".join(chars)
        l2 = _pad(f"[{barra}]{int(progreso):3d}%")
        return l1, l2

    # ---------------- Confirmado ----------------
    if estado == "confirmado":
        iv = _intervalo(0.4, velocidad)
        if len(nombre) <= 16:
            l1 = _centrar(nombre)
        else:
            l1 = _marquesina_pausada(nombre, ahora, marquesina_on)
        frames_l2 = [
            "---   [OK]   ---",
            " ---  [OK]  --- ",
            "  --- [OK] ---  ",
            "   ---[OK]---   ",
            "  --- [OK] ---  ",
            " ---  [OK]  --- ",
        ]
        idx = _idx_frame(t, iv, len(frames_l2))
        l2 = _pad(frames_l2[idx])
        return l1, l2

    # ---------------- Desconocido ----------------
    if estado == "desconocido":
        iv = _intervalo(0.45, velocidad)
        if _idx_frame(t, iv, 2) == 0:
            l1 = _pad("[X] DESCONOCIDO")
            l2 = _pad(">> REINTENTAR <<")
        else:
            l1 = _pad(" X  DESCONOCIDO ")
            l2 = _pad("   REINTENTAR   ")
        return l1, l2

    # ---------------- Incompleto ----------------
    if estado == "incompleto":
        iv_corto = _intervalo(0.5, velocidad)
        iv_largo = _intervalo(1.4, velocidad)
        if len(nombre) <= 16:
            l1 = _centrar(nombre)
        else:
            l1 = _marquesina_pausada(nombre, ahora, marquesina_on)
        if len(motivo) <= 12:
            if _idx_frame(t, iv_corto, 2) == 0:
                l2 = _pad(f"[!] {motivo}")
            else:
                l2 = _pad(f" !  {motivo}")
        else:
            if _idx_frame(t, iv_largo, 2) == 0:
                l2 = _pad("[!] INCOMPLETO  ")
            else:
                l2 = _pad(motivo[:16])
        return l1, l2

    # ---------------- Fallback ----------------
    l1 = _centrar("CENTINELA CTRL")
    l2 = _pad(motivo or "ESPERANDO...")
    return l1, l2


def obtener_filas_prueba(paso=0):
    """Secuencia opcional 'Probar LCD' (3 frames)."""
    paso = int(paso) % 3
    if paso == 0:
        return _pad("#" * 16), _pad("#" * 16)
    if paso == 1:
        return _pad(">>>> TEST LCD >>"), _pad("<< TEST LCD <<<<")
    return _centrar("CENTINELA CTRL"), _pad("[OK] LCD LISTO  ")