#!/usr/bin/env python3
"""
Centinela Control — Configuración persistente.

Guarda cámaras y parámetros del módulo de asistencia en data/centinela/config.json.
Si el archivo no existe se autogenera con valores por defecto.

Las cámaras se modelan como una lista para soportar 1..N cámaras.
"""

import os, json, copy
from datetime import datetime, timedelta

from centinela.core import rutas

DATA_DIR = rutas.DIR_DATOS_CENTINELA
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

CAMARA_DEFAULT = {
    "id": "cam_1",
    "nombre": "Aula",
    "tipo": "http",            # "local" (webcam) | "esp32" (ESP32-CAM auto URL) | "http" (stream MJPEG) | "telefono" (celular)
    "fuente": "http://10.245.111.47:81/stream",
    "activa": True,
    "intervalo_proceso": 0.1,  # segundos entre procesados de reconocimiento
}

DEFAULTS = {
    "host_lcd": "",
    "lcd_animacion": {
        "activa": True,
        "velocidad": 0.6,
        "estilo_barra": "bloques",
        "marquesina": True,
        "iconos_ascii": True,
        "destello_confirmado": True,
    },
    "camaras": [dict(CAMARA_DEFAULT)],
    "cursos": [],            # [{"nombre": X, "secciones": ["C1", ...]}]
    "horarios": [],          # franjas horarias por curso+seccion (ver horarios_*)
    "asistencia": {
        "umbral_similitud": 0.70,
        "segundos_confirmacion": 3.0,
        "intervalo_antiduplicado": 30,
        "umbral_lcd_nombre": 0.30,
        "duracion_resultado_lcd": 3.0,
        "modo_escaneo": "uno",   # "uno" = solo el rostro mas grande | "varios" = todos
        "procesar_cada_n": 1,     # procesar 1 de cada N frames (fluidez vs reconocimiento)
        "tolerancia_tardanza_min": 5,   # minutos de gracia tras el inicio
        "aplicar_horarios": False,      # 4B: solo registrar dentro de la franja
    },
}


def cargar():
    """Lee config.json y lo mezcla con los defaults. Nunca falla."""
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                datos = json.load(f)
            for seccion, valor in datos.items():
                if seccion == "camaras" and isinstance(valor, list):
                    cfg["camaras"] = valor
                elif isinstance(valor, dict) and isinstance(cfg.get(seccion), dict):
                    cfg[seccion].update(valor)
                else:
                    cfg[seccion] = valor
        except (OSError, ValueError):
            pass  # config corrupto: se usan los defaults
    cfg.pop("modulos", None)  # sección retirada (solo existe Asistencia)
    return cfg


def guardar(cfg):
    """Escribe el config de forma atómica."""
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, CONFIG_PATH)


def nuevo_id_camara(cfg):
    """Genera un id de cámara único (cam_1, cam_2, ...)."""
    nums = []
    for c in cfg.get("camaras", []):
        cid = str(c.get("id", ""))
        if cid.startswith("cam_"):
            try:
                nums.append(int(cid[4:]))
            except ValueError:
                pass
    return f"cam_{(max(nums) + 1) if nums else 1}"


ESP32_RESOLUCIONES = ("QVGA", "VGA", "SVGA")


def normalizar_params_esp32(resolucion="VGA", fps=25, calidad=16):
    """Normaliza y acota (res, fps, calidad) para cámaras ESP32-CAM."""
    res = str(resolucion or "VGA").strip().upper()
    if res not in ESP32_RESOLUCIONES:
        res = "VGA"
    try:
        fps_i = max(0, min(60, int(25 if fps is None else fps)))
    except (TypeError, ValueError):
        fps_i = 25
    try:
        cal_i = max(10, min(40, int(16 if calidad is None else calidad)))
    except (TypeError, ValueError):
        cal_i = 16
    return res, fps_i, cal_i


def construir_url_esp32(host_o_url, resolucion="VGA", fps=25, calidad=16):
    """Construye la URL HTTP MJPEG del stream ESP32-CAM con res/fps/calidad."""
    from urllib.parse import urlparse, urlunparse

    raw = str(host_o_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname
    if not host:
        return ""
    scheme = "http" if parsed.scheme.lower() in ("", "tcp") else parsed.scheme.lower()
    try:
        port = parsed.port
    except ValueError:
        port = None
    puerto = 81 if port in (None, 80) else port
    host_net = f"[{host}]" if ":" in host else host
    netloc = f"{host_net}:{puerto}"
    path = parsed.path if parsed.path not in ("", "/") else "/stream"
    res, fps_i, cal_i = normalizar_params_esp32(resolucion, fps, calidad)
    query = f"res={res}&fps={fps_i}&calidad={cal_i}"
    return urlunparse((scheme, netloc, path, "", query, ""))


def extraer_host_esp32(host_o_url):
    """Extrae el host (y puerto si es distinto de 80/81) de una URL o dirección."""
    from urllib.parse import urlparse

    raw = str(host_o_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    host = parsed.hostname
    if not host:
        return ""
    host_net = f"[{host}]" if ":" in host else host
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port and port not in (80, 81):
        return f"{host_net}:{port}"
    return host_net


def fuente_de_camara(cam):
    """Fuente lista para CamaraHilo a partir del dict de cámara."""
    fuente = str(cam.get("fuente", "")).strip()
    tipo = cam.get("tipo")
    if tipo == "telefono":
        # CamaraHilo detecta el prefijo "telefono:" y consume los frames
        # que el servidor web sube desde el celular (modo teléfono).
        return f"telefono:{fuente}"
    if tipo == "esp32":
        return construir_url_esp32(
            cam.get("esp32_host") or fuente,
            cam.get("esp32_res", "VGA"),
            cam.get("esp32_fps", 25),
            cam.get("esp32_calidad", 16),
        ) or fuente
    if tipo == "http":
        return fuente
    return fuente or "0"


# ---------------- Cursos y secciones ----------------
# cfg["cursos"]: [{"nombre": "Gestion IoT", "secciones": ["C1", "C2"],
#                  "clases": 15}]  (clases = cantidad total de sesiones del
#                  curso; 0 o ausente = sin limite)
# Un alumno puede pertenecer a varios cursos, pero solo a una seccion de
# cada curso (inscripciones en alumnos.json, ver gestor_alumnos).

def lista_cursos(cfg):
    """Cursos configurados como [{nombre, secciones, clases, tolerancia_tardanza_min, tolerancia_secciones}]."""
    tol_default = _tol_global_fallback(cfg)
    res = []
    for c in cfg.get("cursos", []) or []:
        nombre = str(c.get("nombre", "") or "").strip()
        if not nombre:
            continue
        secciones = [str(s).strip() for s in c.get("secciones", []) or []
                     if str(s).strip()]
        try:
            clases = max(0, int(c.get("clases", 0) or 0))
        except (TypeError, ValueError):
            clases = 0
        raw_tol = c.get("tolerancia_tardanza_min")
        try:
            tol_curso = max(0, int(raw_tol)) if raw_tol is not None else tol_default
        except (TypeError, ValueError):
            tol_curso = tol_default
        tol_secs = {}
        raw_map = c.get("tolerancia_secciones")
        if isinstance(raw_map, dict):
            for sec, val in raw_map.items():
                sec_k = str(sec or "").strip()
                if not sec_k or val is None:
                    continue
                try:
                    tol_secs[sec_k] = max(0, int(val))
                except (TypeError, ValueError):
                    pass
        res.append({
            "nombre": nombre,
            "secciones": secciones,
            "clases": clases,
            "tolerancia_tardanza_min": tol_curso,
            "tolerancia_secciones": tol_secs,
        })
    return res


def _tol_global_fallback(cfg):
    try:
        return max(0, int((cfg or {}).get("asistencia", {}).get("tolerancia_tardanza_min", 5)))
    except (TypeError, ValueError):
        return 5


def tolerancia_de(cfg, curso, seccion=""):
    """Minutos de tolerancia de tardanza para (curso, seccion).

    Si la sección tiene un margen propio activado en `tolerancia_secciones`,
    usa ese valor; si no, hereda `tolerancia_tardanza_min` del curso.
    """
    curso = str(curso or "").strip()
    seccion = str(seccion or "").strip()
    for c in lista_cursos(cfg):
        if c["nombre"] == curso:
            if seccion and seccion in c["tolerancia_secciones"]:
                return c["tolerancia_secciones"][seccion]
            return c["tolerancia_tardanza_min"]
    return _tol_global_fallback(cfg)


def nombres_cursos(cfg):
    """Nombres de los cursos configurados."""
    return [c["nombre"] for c in lista_cursos(cfg)]


def clases_de_curso(cfg, curso):
    """Cantidad de clases configurada del curso (0 = sin limite)."""
    curso = str(curso or "").strip()
    for c in lista_cursos(cfg):
        if c["nombre"] == curso:
            return c["clases"]
    return 0


def fechas_sesiones(cfg, curso, primer_dia, hoy, maximo=366):
    """
    Fechas de sesion de un curso (historial completo sin elegir rango).

    Sesion = dia con franja horaria del curso (respeta dias de la semana y
    vigencia desde/hasta). Arranca en la vigencia 'desde' de la franja si
    existe, o en la primera actividad del curso, y avanza hasta HOY,
    cortando al alcanzar la cantidad de clases del curso (0 = sin limite).

    Devuelve lista 'YYYY-MM-DD'; None si el curso no tiene franjas (el
    llamador decide el fallback).
    """
    franjas = franjas_de(cfg, curso)
    if not franjas:
        return None
    desde = [str(f["desde"])[:10] for f in franjas if f.get("desde")]
    inicio = min(desde) if desde else str(primer_dia or hoy)[:10]
    hoy = str(hoy)[:10]
    limite = clases_de_curso(cfg, curso) or maximo
    res = []
    try:
        d = datetime.strptime(inicio, "%Y-%m-%d").date()
        fin = datetime.strptime(hoy, "%Y-%m-%d").date()
    except ValueError:
        return res
    while d <= fin and len(res) < limite:
        iso = d.isoformat()
        if franjas_del_dia(cfg, iso, curso):
            res.append(iso)
        d += timedelta(days=1)
    return res


def secciones_de_curso(cfg, curso):
    """Secciones configuradas de un curso (lista de str)."""
    curso = str(curso or "").strip()
    for c in lista_cursos(cfg):
        if c["nombre"] == curso:
            return list(c["secciones"])
    return []


def renombrar_curso(cfg, viejo, nuevo):
    """Renombra un curso en cfg['cursos'], cfg['horarios'] y cfg['camaras']."""
    viejo = str(viejo or "").strip()
    nuevo = str(nuevo or "").strip()
    if not viejo or not nuevo or viejo == nuevo:
        return
    for c in cfg.get("cursos", []) or []:
        if str(c.get("nombre", "") or "").strip() == viejo:
            c["nombre"] = nuevo
    for h in cfg.get("horarios", []) or []:
        if str(h.get("curso", "") or "").strip() == viejo:
            h["curso"] = nuevo
    for cam in cfg.get("camaras", []) or []:
        if str(cam.get("curso", "") or "").strip() == viejo:
            cam["curso"] = nuevo


def renombrar_seccion(cfg, curso, vieja, nueva):
    """Renombra una sección dentro de un curso y actualiza sus horarios."""
    curso = str(curso or "").strip()
    vieja = str(vieja or "").strip()
    nueva = str(nueva or "").strip()
    if not curso or not vieja or not nueva or vieja == nueva:
        return
    for c in cfg.get("cursos", []) or []:
        if str(c.get("nombre", "") or "").strip() == curso:
            secs = [str(s).strip() for s in (c.get("secciones") or []) if str(s).strip()]
            reemplazo = []
            for s in secs:
                val = nueva if s == vieja else s
                if val not in reemplazo:
                    reemplazo.append(val)
            c["secciones"] = reemplazo
            tol_secs = c.get("tolerancia_secciones")
            if isinstance(tol_secs, dict) and vieja in tol_secs:
                tol_secs[nueva] = tol_secs.pop(vieja)
    for h in cfg.get("horarios", []) or []:
        if str(h.get("curso", "") or "").strip() == curso:
            sec = str(h.get("seccion") or h.get("grupo") or "").strip()
            if sec == vieja:
                h["seccion"] = nueva
                h.pop("grupo", None)


# ---------------- Horarios de clase ----------------
# Cada franja: {"id": "h1", "nombre": "Clase manana", "curso": "6A",
#               "seccion": "" (todas) | "C1", "dias": [0..6] (0=lunes),
#               "inicio": "08:00", "fin": "12:00",
#               "desde": "YYYY-MM-DD" (opcional), "hasta": "YYYY-MM-DD" (opcional)}
# desde/hasta acotan la vigencia (cursos de duracion limitada); sin ellos
# la franja aplica siempre.
# Un alumno pertenece a una franja cuando franja.curso == inscripcion.curso
# del alumno y (franja.seccion == "" o franja.seccion == inscripcion.seccion).

DIAS_SEMANA = ["Lunes", "Martes", "Miercoles", "Jueves", "Viernes", "Sabado", "Domingo"]


def _dias_de(franja):
    """Dias de la semana de una franja como conjunto 0..6 (tolerante)."""
    dias = franja.get("dias", [])
    if isinstance(dias, int):
        dias = [dias]
    out = set()
    for d in dias if isinstance(dias, (list, tuple, set)) else []:
        try:
            d = int(d)
        except (TypeError, ValueError):
            continue
        if 0 <= d <= 6:
            out.add(d)
    return out


def _minutos(hhmm):
    """'08:30' -> 510. Devuelve None si el formato no es valido."""
    try:
        h, m = str(hhmm).split(":")[:2]
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError, TypeError):
        return None


def _normalizar(curso, seccion):
    return str(curso or "").strip(), str(seccion or "").strip()


def _seccion_de_franja(franja):
    """Seccion de la franja ('seccion'; tolera 'grupo' de configs anteriores)."""
    return str(franja.get("seccion") or franja.get("grupo") or "").strip()


def franjas_de(cfg, curso="", seccion=""):
    """Franjas que aplican a (curso, seccion). Seccion '' coincide con todas."""
    curso, seccion = _normalizar(curso, seccion)
    res = []
    for f in cfg.get("horarios", []) or []:
        if str(f.get("curso", "")).strip() != curso:
            continue
        fs = _seccion_de_franja(f)
        if seccion and fs and fs != seccion:
            continue
        res.append(f)
    return res


def _en_vigencia(franja, fecha_iso):
    """True si la fecha cae dentro del rango opcional desde/hasta de la franja."""
    fecha = str(fecha_iso)[:10]
    desde = str(franja.get("desde", "") or "").strip()
    hasta = str(franja.get("hasta", "") or "").strip()
    if desde and fecha < desde:
        return False
    if hasta and fecha > hasta:
        return False
    return True


def franjas_del_dia(cfg, fecha_iso, curso="", seccion=""):
    """Franjas de (curso, seccion) para la fecha dada ('YYYY-MM-DD')."""
    try:
        dia = datetime.strptime(str(fecha_iso)[:10], "%Y-%m-%d").weekday()
    except (ValueError, TypeError):
        return []
    return [f for f in franjas_de(cfg, curso, seccion)
            if dia in _dias_de(f) and _en_vigencia(f, fecha_iso)]


def en_franja(cfg, curso, seccion, fecha_iso, hora_hhmm):
    """Franja que contiene la hora dada, o None si esta fuera de horario."""
    minutos = _minutos(hora_hhmm)
    if minutos is None:
        return None
    for f in franjas_del_dia(cfg, fecha_iso, curso, seccion):
        ini, fin = _minutos(f.get("inicio")), _minutos(f.get("fin"))
        if ini is not None and fin is not None and ini <= minutos <= fin:
            return f
    return None


def es_tardanza(cfg, curso, seccion, fecha_iso, hora_entrada_hhmm,
                tolerancia_min=None):
    """
    Clasifica una entrada contra el horario del curso ese dia.
      True  -> tardanza (llegada posterior a inicio + tolerancia)
      False -> puntual
      None  -> el curso/seccion no tiene franjas ese dia (o datos invalidos)
    """
    franjas = franjas_del_dia(cfg, fecha_iso, curso, seccion)
    if not franjas:
        return None
    inicios = [m for m in (_minutos(f.get("inicio")) for f in franjas)
               if m is not None]
    minutos = _minutos(hora_entrada_hhmm)
    if not inicios or minutos is None:
        return None
    if tolerancia_min is None:
        tolerancia_min = tolerancia_de(cfg, curso, seccion)
    return minutos > min(inicios) + int(tolerancia_min)


def nuevo_id_horario(cfg):
    """Siguiente id de franja (h1, h2, ...) sobre el config dado."""
    nums = []
    for h in cfg.get("horarios", []) or []:
        hid = str(h.get("id", ""))
        if hid.startswith("h"):
            try:
                nums.append(int(hid[1:]))
            except ValueError:
                pass
    return f"h{(max(nums) + 1) if nums else 1}"
