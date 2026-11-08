#!/usr/bin/env python3
"""
Centinela Control — Configuración persistente (v6)

Guarda cámaras, parámetros del módulo de asistencia y los módulos de
vigilancia en app/data/centinela/config.json. Si el archivo no existe se
autogenera con valores por defecto.

Las cámaras se modelan como una lista para soportar 1..N cámaras.
"""

import os, json, copy

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
    "asistencia": {
        "umbral_similitud": 0.70,
        "segundos_confirmacion": 3.0,
        "intervalo_antiduplicado": 30,
        "umbral_lcd_nombre": 0.30,
        "duracion_resultado_lcd": 3.0,
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
