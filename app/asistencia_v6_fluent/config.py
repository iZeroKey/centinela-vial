#!/usr/bin/env python3
"""
Centinela Control — Configuración persistente (v6)

Guarda cámaras, parámetros del módulo de asistencia y los módulos de
vigilancia en app/data/centinela/config.json. Si el archivo no existe se
autogenera con valores por defecto.

Las cámaras se modelan como una lista para soportar 1..N cámaras.
"""

import os, json, copy
from datetime import datetime, timedelta

# .../app/asistencia_v6/config.py -> .../app
_ROOT_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(_ROOT_APP, "data", "centinela")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

CAMARA_DEFAULT = {
    "id": "cam_1",
    "nombre": "Aula",
    "tipo": "http",            # "local" (índice de webcam) | "http" (stream MJPEG)
    "fuente": "http://10.245.111.47:81/stream",
    "activa": True,
    "intervalo_proceso": 0.1,  # segundos entre procesados de reconocimiento
}

DEFAULTS = {
    "host_lcd": "",
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
    cfg.pop("modulos", None)  # sección retirada en v6 (solo existe Asistencia)
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


def fuente_de_camara(cam):
    """Fuente lista para CamaraHilo a partir del dict de cámara."""
    fuente = str(cam.get("fuente", "")).strip()
    if cam.get("tipo") == "http":
        return fuente
    return fuente or "0"


# ---------------- Cursos y secciones ----------------
# cfg["cursos"]: [{"nombre": "Gestion IoT", "secciones": ["C1", "C2"],
#                  "clases": 15}]  (clases = cantidad total de sesiones del
#                  curso; 0 o ausente = sin limite)
# Un alumno puede pertenecer a varios cursos, pero solo a una seccion de
# cada curso (inscripciones en alumnos.json, ver gestor_alumnos).

def lista_cursos(cfg):
    """Cursos configurados como [{nombre, secciones, clases}] (normalizados)."""
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
        res.append({"nombre": nombre, "secciones": secciones, "clases": clases})
    return res


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
        if seccion and fs != seccion:
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
                tolerancia_min=5):
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
