#!/usr/bin/env python3
"""
Centinela Control — Gestión de alumnos (v6)

Única puerta de entrada para altas, ediciones, bajas y embeddings.
Mantiene alumnos.json como fuente de verdad (escritura atómica) y
sincroniza la tabla alumnos de SQLite.

Los alumno_id son internos: se autogeneran (E001, E002, ...) y la UI
nunca los muestra (más adelante se agregará un identificador visible
aparte, sin tocar el ID interno).

Todas las funciones son seguras para llamar desde un QThread mientras
la cámara corre: el JSON se protege con lock y los modelos de rostros
se cargan bajo lock (ver rostros.py).
"""

import os, sys, json, re, glob, threading
from datetime import datetime

import numpy as np
import cv2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rostros import pipeline_embeddings
import db as db_centinela

# .../asistencia_v6/gestor_alumnos.py -> .../app
_ROOT_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_DATOS = os.path.join(_ROOT_APP, "data", "asistencia")
JSON_PATH = os.path.join(BASE_DATOS, "alumnos.json")
REF_DIR = os.path.join(BASE_DATOS, "referencia")

_lock_json = threading.Lock()


# ---------------- Lectura/escritura JSON ----------------

def _leer_json():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {"alumnos": []}
    if not isinstance(data, dict) or not isinstance(data.get("alumnos", []), list):
        return {"alumnos": []}
    return data


def _escribir_json(data):
    os.makedirs(BASE_DATOS, exist_ok=True)
    tmp = JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, JSON_PATH)


def _sincronizar():
    try:
        db_centinela.sincronizar_alumnos(JSON_PATH)
    except Exception as e:
        print(f"[GESTOR] sincronizar con SQLite fallo: {e}")


def sincronizar_db():
    _sincronizar()


# ---------------- Consultas ----------------

def listar(incluir_inactivos=False):
    """Lista de alumnos como dicts. Por defecto solo activos."""
    alumnos = _leer_json().get("alumnos", [])
    if not incluir_inactivos:
        alumnos = [a for a in alumnos if a.get("estado", "activo") == "activo"]
    return alumnos


def obtener(alumno_id):
    for a in _leer_json().get("alumnos", []):
        if a.get("alumno_id") == alumno_id:
            return a
    return None


def siguiente_id(data):
    """Siguiente ID interno E### (los IDs no se muestran en la UI)."""
    nums = [int(m.group(1)) for a in data.get("alumnos", [])
            if (m := re.match(r"^E(\d+)$", str(a.get("alumno_id", ""))))]
    return f"E{max(nums) + 1 if nums else 1:03d}"


def ruta_foto_absoluta(foto_rel):
    """
    Ruta absoluta de una foto de referencia.
    1. Ruta exacta relativa a BASE_DATOS.
    2. Fallback: <id>.* dentro de referencia/ (por deriva de extensión).
    """
    if not foto_rel:
        return None
    ruta = os.path.join(BASE_DATOS, foto_rel)
    if os.path.isfile(ruta):
        return ruta
    aid = os.path.splitext(os.path.basename(foto_rel))[0]
    candidatos = glob.glob(os.path.join(REF_DIR, aid + ".*"))
    return candidatos[0] if candidatos else None


# ---------------- Imágenes ----------------

def leer_imagen(ruta):
    """Lee una imagen tolerando rutas con caracteres no ASCII (Windows)."""
    try:
        datos = np.fromfile(ruta, dtype=np.uint8)
        return cv2.imdecode(datos, cv2.IMREAD_COLOR)
    except (OSError, ValueError):
        return None


def _guardar_imagen_jpg(img_bgr, alumno_id):
    """Normaliza la foto de referencia a <id>.jpg (evita deriva de extensión)."""
    os.makedirs(REF_DIR, exist_ok=True)
    destino = os.path.join(REF_DIR, f"{alumno_id}.jpg")
    if not cv2.imwrite(destino, img_bgr):
        raise RuntimeError(f"No se pudo guardar la imagen en {destino}")
    return destino


def _area_caja(res):
    x1, y1, x2, y2 = res["caja"]
    return (x2 - x1) * (y2 - y1)


def _embedding_de(img_bgr):
    """
    Devuelve (emb, aviso).
      - Sin caras:        (None, mensaje de error).
      - Una cara:         (emb, None).
      - Varias caras:     (emb de la mayor, aviso para el usuario).
    """
    try:
        resultados = pipeline_embeddings(img_bgr, thr=0.5)
    except Exception as e:
        return None, f"Error al procesar la imagen: {e}"
    if not resultados:
        return None, "No se detectó ninguna cara en la imagen."
    if len(resultados) > 1:
        res = max(resultados, key=_area_caja)
        return res["emb"], "Se detectaron varias caras; se usó la más grande."
    return resultados[0]["emb"], None


# ---------------- Operaciones ----------------

def agregar(nombre, curso="", grupo="", imagen=None, ruta_imagen=None):
    """
    Alta de alumno con foto (ndarray BGR o ruta). Genera el embedding;
    si no se detecta cara, no guarda nada.
    Devuelve (alumno|None, mensaje).
    """
    if imagen is None and ruta_imagen:
        imagen = leer_imagen(ruta_imagen)
    if imagen is None:
        return None, "No se pudo leer la imagen."

    emb, aviso = _embedding_de(imagen)
    if emb is None:
        return None, aviso

    with _lock_json:
        data = _leer_json()
        alumno_id = siguiente_id(data)
        try:
            _guardar_imagen_jpg(imagen, alumno_id)
        except RuntimeError as e:
            return None, str(e)
        alumno = {
            "alumno_id": alumno_id,
            "nombre": nombre,
            "curso": curso or "",
            "grupo": grupo or "",
            "foto_referencia": f"referencia/{alumno_id}.jpg",
            "embedding": emb.tolist(),
            "estado": "activo",
            "fecha_registro": datetime.now().isoformat(),
        }
        data["alumnos"].append(alumno)
        _escribir_json(data)
    _sincronizar()

    mensaje = f"Alumno '{nombre}' agregado con embedding nuevo."
    if aviso:
        mensaje += " " + aviso
    return alumno, mensaje


def editar(alumno_id, nombre=None, curso=None, grupo=None,
           imagen=None, ruta_imagen=None):
    """
    Edición de datos y/o foto. Con imagen nueva valida la cara y regenera
    el embedding; si falla, no modifica nada.
    Devuelve (ok, mensaje).
    """
    if imagen is None and ruta_imagen:
        imagen = leer_imagen(ruta_imagen)

    emb = None
    aviso = None
    if imagen is not None:
        emb, aviso = _embedding_de(imagen)
        if emb is None:
            return False, aviso

    with _lock_json:
        data = _leer_json()
        alumno = next((a for a in data.get("alumnos", [])
                       if a.get("alumno_id") == alumno_id), None)
        if alumno is None:
            return False, "No se encontró el alumno."
        if nombre is not None and nombre.strip():
            alumno["nombre"] = nombre.strip()
        if curso is not None:
            alumno["curso"] = curso.strip()
        if grupo is not None:
            alumno["grupo"] = grupo.strip()
        if imagen is not None:
            try:
                _guardar_imagen_jpg(imagen, alumno_id)
            except RuntimeError as e:
                return False, str(e)
            alumno["foto_referencia"] = f"referencia/{alumno_id}.jpg"
            alumno["embedding"] = emb.tolist()
        _escribir_json(data)
    _sincronizar()

    mensaje = "Cambios guardados."
    if imagen is not None:
        mensaje += " Embedding actualizado."
    if aviso:
        mensaje += " " + aviso
    return True, mensaje


def cambiar_estado(alumno_id, estado):
    """
    Baja lógica ('inactivo': deja de reconocerse y de listarse, conserva
    historial) o reactivación ('activo').
    Devuelve (ok, mensaje).
    """
    with _lock_json:
        data = _leer_json()
        alumno = next((a for a in data.get("alumnos", [])
                       if a.get("alumno_id") == alumno_id), None)
        if alumno is None:
            return False, "No se encontró el alumno."
        alumno["estado"] = "activo" if estado == "activo" else "inactivo"
        _escribir_json(data)
    _sincronizar()
    return True, ("Alumno reactivado." if alumno["estado"] == "activo"
                  else "Alumno dado de baja (su historial se conserva).")


def regenerar_embedding(alumno_id):
    """
    Recalcula el embedding desde la foto de referencia guardada.
    Devuelve (ok, mensaje).
    """
    alumno = obtener(alumno_id)
    if alumno is None:
        return False, "No se encontró el alumno."
    ruta = ruta_foto_absoluta(alumno.get("foto_referencia"))
    if ruta is None:
        return False, f"Sin foto de referencia ({alumno.get('foto_referencia') or 'no definida'})."
    img = leer_imagen(ruta)
    if img is None:
        return False, "No se pudo leer la foto de referencia."

    emb, aviso = _embedding_de(img)
    if emb is None:
        return False, aviso

    with _lock_json:
        data = _leer_json()
        alumno = next((a for a in data.get("alumnos", [])
                       if a.get("alumno_id") == alumno_id), None)
        if alumno is None:
            return False, "No se encontró el alumno."
        alumno["embedding"] = emb.tolist()
        _escribir_json(data)
    _sincronizar()
    return True, "Embedding actualizado." + ((" " + aviso) if aviso else "")


def completar_embeddings():
    """
    Genera embeddings faltantes de todos los alumnos activos.
    Pensado para ejecutarse en un QThread ("Recargar alumnos").
    Devuelve (ok, mensaje_resumen).
    """
    procesados = 0
    fallos = 0
    alumnos = listar()
    for a in alumnos:
        if a.get("embedding") is not None:
            continue
        ok, _ = regenerar_embedding(a["alumno_id"])
        if ok:
            procesados += 1
        else:
            fallos += 1
    mensaje = (f"{len(alumnos)} alumnos activos: {procesados} embeddings nuevos, "
               f"{fallos} sin poder procesar (sin foto o sin cara detectable).")
    return True, mensaje
