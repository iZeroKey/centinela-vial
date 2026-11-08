"""Estado compartido y funciones de lectura para una LCD 16x2.

Este módulo no controla hardware ni conoce el stream de la cámara. Mantiene
el último estado publicado por la aplicación y expone getters sin argumentos
para que otra integración pueda recuperar los datos fácilmente.
"""

from copy import deepcopy
from datetime import datetime
from threading import RLock


LCD_ANCHO = 16
MAX_LOG_LCD = 100

# Umbral (0..1) de progreso a partir del cual se muestra el nombre en el LCD
# al detectar un rostro. Evita falsos positivos (nombres con 1% de avance).
UMBRAL_NOMBRE_LCD = 0.30


def configurar_umbral_nombre_lcd(valor):
    """Ajusta el umbral global de progreso para mostrar el nombre en el LCD."""
    global UMBRAL_NOMBRE_LCD
    try:
        v = float(valor)
    except (TypeError, ValueError):
        return
    UMBRAL_NOMBRE_LCD = max(0.0, min(1.0, v))

_lock = RLock()
_log = []
_estado = {
    "estado": "esperando",
    "nombre": "",
    "apellidos": "",
    "progreso": 0.0,
    "incompleto": False,
    "motivo": "",
    "actualizado_en": "",
    "fila_1": "".ljust(LCD_ANCHO),
    "fila_2": "".ljust(LCD_ANCHO),
}


def _linea(texto):
    """Recorta y rellena una cadena para la LCD."""
    return str(texto or "").strip()[:LCD_ANCHO].ljust(LCD_ANCHO)


def _progreso_0_100(progreso):
    """Normaliza progreso decimal (0..1) o porcentual (0..100)."""
    try:
        valor = float(progreso)
    except (TypeError, ValueError):
        valor = 0.0
    if 0.0 <= valor <= 1.0:
        valor *= 100.0
    return max(0.0, min(100.0, valor))


def _filas_para(estado, nombre, apellidos, progreso, motivo):
    if estado == "procesando":
        if progreso < UMBRAL_NOMBRE_LCD * 100.0:
            fila_1 = "PROCESANDO"
        else:
            fila_1 = nombre or "PROCESANDO"
        fila_2 = f"PROCESANDO {progreso:3.0f}%"
    elif estado == "confirmado":
        fila_1 = nombre or "IDENTIFICADO"
        fila_2 = apellidos or "ASISTENCIA OK"
    elif estado == "incompleto":
        fila_1 = nombre or "PROCESANDO"
        fila_2 = motivo or "INCOMPLETO"
    elif estado == "desconocido":
        fila_1 = "NO IDENTIFICADO"
        fila_2 = motivo or "INTENTE DE NUEVO"
    else:
        fila_1 = "CENTINELA CTRL"
        fila_2 = motivo or "ESPERANDO..."
    return _linea(fila_1), _linea(fila_2)


def _publicar(estado, nombre="", apellidos="", progreso=0.0, motivo=""):
    progreso = _progreso_0_100(progreso)
    fila_1, fila_2 = _filas_para(estado, nombre, apellidos, progreso, motivo)
    registro = {
        "estado": estado,
        "nombre": str(nombre or "").strip(),
        "apellidos": str(apellidos or "").strip(),
        "progreso": progreso,
        "incompleto": estado == "incompleto",
        "motivo": str(motivo or "").strip(),
        "actualizado_en": datetime.now().isoformat(timespec="seconds"),
        "fila_1": fila_1,
        "fila_2": fila_2,
    }
    with _lock:
        _estado.clear()
        _estado.update(registro)
        _log.append(deepcopy(registro))
        if len(_log) > MAX_LOG_LCD:
            del _log[:-MAX_LOG_LCD]


# Publicadores: los usa la integración de la app, no el firmware de la cámara.
def actualizar_esperando(mensaje="ESPERANDO..."):
    """Publica que no hay un reconocimiento activo."""
    _publicar("esperando", motivo=mensaje)


def actualizar_procesamiento(nombre="", apellidos="", progreso=0.0):
    """Publica el progreso actual de un estudiante identificado parcialmente."""
    _publicar("procesando", nombre, apellidos, progreso)


def actualizar_confirmado(nombre="", apellidos=""):
    """Publica una identificación confirmada."""
    _publicar("confirmado", nombre, apellidos, 100.0)


def actualizar_incompleto(nombre="", apellidos="", motivo="INCOMPLETO"):
    """Publica un reconocimiento que no alcanzó la confirmación."""
    _publicar("incompleto", nombre, apellidos, 0.0, motivo)


def actualizar_desconocido(mensaje="INTENTE DE NUEVO"):
    """Publica que el rostro no pudo asociarse a un estudiante."""
    _publicar("desconocido", motivo=mensaje)


# Getters sin argumentos: esta es la interfaz que consumirá la integración LCD.
def obtener_datos_lcd():
    """Devuelve una copia del estado completo actual, sin parámetros."""
    with _lock:
        return deepcopy(_estado)


def obtener_filas_lcd():
    """Devuelve ``(fila_1, fila_2)`` listas para escribir en la LCD."""
    with _lock:
        return _estado["fila_1"], _estado["fila_2"]


def obtener_nombre_lcd():
    """Devuelve el nombre actual, ajustado a 16 caracteres."""
    with _lock:
        return _linea(_estado["nombre"])


def obtener_apellidos_lcd():
    """Devuelve los apellidos actuales, ajustados a 16 caracteres."""
    with _lock:
        return _linea(_estado["apellidos"])


def obtener_porcentaje_lcd():
    """Devuelve la línea de porcentaje del estado actual."""
    with _lock:
        return _linea(f"PROCESANDO {_estado['progreso']:3.0f}%")


def obtener_incompleto_lcd():
    """Devuelve ``INCOMPLETO`` si ese es el estado actual; si no, vacío."""
    with _lock:
        return _linea("INCOMPLETO") if _estado["incompleto"] else _linea("")


def obtener_estado_lcd():
    """Devuelve solo el nombre del estado actual."""
    with _lock:
        return _estado["estado"]


def obtener_log_lcd():
    """Devuelve una copia de los últimos estados publicados, sin parámetros."""
    with _lock:
        return deepcopy(_log)


def limpiar_log_lcd():
    """Vacía el historial interno de estados LCD."""
    with _lock:
        _log.clear()


actualizar_esperando()
