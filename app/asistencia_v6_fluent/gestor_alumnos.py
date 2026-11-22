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
_cache_json = None
_cache_firma = None


# ---------------- Lectura/escritura JSON ----------------

def _leer_json_disco():
    try:
        with open(JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, ValueError):
        return {"alumnos": []}
    if not isinstance(data, dict) or not isinstance(data.get("alumnos", []), list):
        return {"alumnos": []}
    return data


def _leer_json():
    """Lee alumnos.json con caché en memoria validada por (mtime_ns, size)."""
    global _cache_json, _cache_firma
    try:
        st = os.stat(JSON_PATH)
        firma = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache_json = None
        _cache_firma = None
        return {"alumnos": []}
    if _cache_json is not None and _cache_firma == firma:
        return _cache_json
    data = _leer_json_disco()
    _cache_json = data
    _cache_firma = firma
    return data


def _escribir_json(data):
    global _cache_json, _cache_firma
    os.makedirs(BASE_DATOS, exist_ok=True)
    tmp = JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, JSON_PATH)
    _cache_json = data
    try:
        st = os.stat(JSON_PATH)
        _cache_firma = (st.st_mtime_ns, st.st_size)
    except OSError:
        _cache_firma = None


def _sincronizar():
    try:
        db_centinela.sincronizar_alumnos(
            JSON_PATH, alumnos=_leer_json().get("alumnos", []))
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

_CAMPOS_ALUMNO = (
    "nombre", "apellidos", "dni", "fecha_nacimiento", "sexo", "telefono", "email",
    "tutor_nombre", "tutor_telefono", "direccion", "curso", "seccion",
    "condicion", "observaciones",
)


def _normalizar_inscripciones(alumno):
    """
    Normaliza las inscripciones del alumno: lista de {"curso", "seccion"},
    un curso como maximo una vez (solo una seccion por curso).

    - inscripciones ausentes (clave ausente o None) -> se construyen desde
      curso/seccion planos (migración legacy).
    - inscripciones presentes como lista (incluso []) -> fuente de verdad.
    - curso/seccion planos quedan como espejo de la primera inscripcion
      (o "" si no tiene inscripciones) para compatibilidad con SQLite.
    - la clave historica 'grupo' se elimina (el C1 es la seccion).
    """
    brutas = alumno.get("inscripciones")
    norm = []
    if isinstance(brutas, list):
        for i in brutas:
            if not isinstance(i, dict):
                continue
            curso = str(i.get("curso", "") or "").strip()
            if not curso:
                continue
            seccion = str(i.get("seccion", "") or "").strip()
            if any(x["curso"] == curso for x in norm):
                continue   # una sola seccion por curso
            norm.append({"curso": curso, "seccion": seccion})
    else:
        curso = str(alumno.get("curso", "") or "").strip()
        if curso:
            norm.append({"curso": curso,
                         "seccion": str(alumno.get("seccion", "") or "").strip()})
    alumno["inscripciones"] = norm
    if norm:
        alumno["curso"] = norm[0]["curso"]
        alumno["seccion"] = norm[0]["seccion"]
    else:
        alumno["curso"] = ""
        alumno["seccion"] = ""
    alumno.pop("grupo", None)
    return alumno


def _normalizar_alumno(alumno):
    """Asegura que el dict del alumno tenga todas las claves esperadas."""
    for c in _CAMPOS_ALUMNO:
        alumno.setdefault(c, "")
    alumno.setdefault("alumno_id", "")
    alumno.setdefault("foto_referencia", None)
    alumno.setdefault("embedding", None)
    alumno.setdefault("estado", "activo")
    alumno.setdefault("fecha_registro", datetime.now().isoformat())
    alumno.setdefault("actualizado_en", "")
    if not alumno.get("condicion"):
        alumno["condicion"] = "regular"
    _normalizar_inscripciones(alumno)
    return alumno


def mapa_inscripciones():
    """{alumno_id: [{"curso", "seccion"}, ...]} para todo alumnos.json."""
    return {a.get("alumno_id"): _inscripciones_de_alumno(a)
            for a in _leer_json().get("alumnos", []) if a.get("alumno_id")}


def agregar(nombre, curso="", seccion="", imagen=None, ruta_imagen=None,
            inscripciones=None, **extra):
    """
    Alta de alumno con foto (ndarray BGR o ruta). Genera el embedding;
    si no se detecta cara, no guarda nada.
    inscripciones: lista [{"curso","seccion"}] (varios cursos, una seccion
    por curso); si no viene, se usa (curso, seccion).
    Acepta campos extra (apellidos, dni, ...) via kwargs.
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
        data = _leer_json_disco()
        alumno_id = siguiente_id(data)
        try:
            _guardar_imagen_jpg(imagen, alumno_id)
        except RuntimeError as e:
            return None, str(e)
        if inscripciones is not None:
            insc_init = list(inscripciones)
        elif str(curso or "").strip():
            insc_init = [{"curso": str(curso).strip(),
                          "seccion": str(seccion or "").strip()}]
        else:
            insc_init = []
        alumno = {
            "alumno_id": alumno_id,
            "nombre": nombre,
            "curso": curso or "",
            "seccion": seccion or "",
            "inscripciones": insc_init,
            "foto_referencia": f"referencia/{alumno_id}.jpg",
            "embedding": emb.tolist(),
            "estado": "activo",
            "fecha_registro": datetime.now().isoformat(),
            "actualizado_en": datetime.now().isoformat(),
        }
        for c in _CAMPOS_ALUMNO:
            if c in extra and extra[c] is not None:
                alumno[c] = extra[c]
        _normalizar_alumno(alumno)
        data["alumnos"].append(alumno)
        _escribir_json(data)
    _sincronizar()

    mensaje = f"Alumno '{nombre}' agregado con embedding nuevo."
    if aviso:
        mensaje += " " + aviso
    return alumno, mensaje


def editar(alumno_id, nombre=None, curso=None, seccion=None,
           imagen=None, ruta_imagen=None, inscripciones=None, **extra):
    """
    Edicion de datos y/o foto. Con imagen nueva valida la cara y regenera
    el embedding; si falla, no modifica nada.
    inscripciones: lista [{"curso","seccion"}] para reemplazar la vigente.
    Acepta campos extra (apellidos, dni, ...) via kwargs.
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
        data = _leer_json_disco()
        alumno = next((a for a in data.get("alumnos", [])
                       if a.get("alumno_id") == alumno_id), None)
        if alumno is None:
            return False, "No se encontro el alumno."
        if nombre is not None and str(nombre).strip():
            alumno["nombre"] = str(nombre).strip()
        if curso is not None:
            alumno["curso"] = str(curso).strip()
        if seccion is not None:
            alumno["seccion"] = str(seccion).strip()
        if inscripciones is not None and isinstance(inscripciones, list):
            alumno["inscripciones"] = inscripciones
        for c in _CAMPOS_ALUMNO:
            if c in extra and extra[c] is not None:
                val = extra[c]
                if isinstance(val, str):
                    val = val.strip()
                alumno[c] = val
        if imagen is not None:
            try:
                _guardar_imagen_jpg(imagen, alumno_id)
            except RuntimeError as e:
                return False, str(e)
            alumno["foto_referencia"] = f"referencia/{alumno_id}.jpg"
            alumno["embedding"] = emb.tolist()
        alumno["actualizado_en"] = datetime.now().isoformat()
        _normalizar_alumno(alumno)
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
        data = _leer_json_disco()
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
        data = _leer_json_disco()
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


def _inscripciones_de_alumno(a):
    """Lista normalizada de inscripciones [{"curso","seccion"}] de un dict alumno."""
    raw = a.get("inscripciones")
    if isinstance(raw, list):
        return [
            {"curso": str(i.get("curso", "") or "").strip(),
             "seccion": str(i.get("seccion", "") or "").strip()}
            for i in raw
            if isinstance(i, dict) and str(i.get("curso", "") or "").strip()
        ]
    curso = str(a.get("curso", "") or "").strip()
    if curso:
        return [{"curso": curso, "seccion": str(a.get("seccion", "") or "").strip()}]
    return []


def contar_por_curso_seccion():
    """Devuelve ({curso: n}, {(curso, seccion): n}) de alumnos activos."""
    por_curso = {}
    por_seccion = {}
    for a in listar():
        insc = _inscripciones_de_alumno(a)
        vistos_curso = set()
        for i in insc:
            c = i["curso"]
            s = i["seccion"]
            if c and c not in vistos_curso:
                por_curso[c] = por_curso.get(c, 0) + 1
                vistos_curso.add(c)
            if c and s:
                por_seccion[(c, s)] = por_seccion.get((c, s), 0) + 1
    return por_curso, por_seccion


def mapa_secciones_en_curso(curso):
    """Devuelve {alumno_id: seccion} de alumnos activos inscritos en `curso`."""
    curso = str(curso or "").strip()
    if not curso:
        return {}
    res = {}
    for a in listar():
        aid = a.get("alumno_id")
        if not aid:
            continue
        for i in _inscripciones_de_alumno(a):
            if i["curso"] == curso:
                res[aid] = i["seccion"]
                break
    return res


def asignar_alumnos_seccion(curso, seccion, alumno_ids):
    """Asigna de forma atómica los `alumno_ids` a (curso, seccion).

    - Si un alumno en `alumno_ids` ya estaba en otra sección del mismo curso,
      se mueve a `seccion` (regla: una sección por curso).
    - Si un alumno NO está en `alumno_ids` y estaba en (curso, seccion), se
      quita de esa sección (conservando sus demás cursos y su registro en la
      base de alumnos).
    - No modifica alumnos asignados a otras secciones del mismo curso.
    Devuelve (agregados, quitados, movidos).
    """
    curso = str(curso or "").strip()
    seccion = str(seccion or "").strip()
    if not curso or not seccion:
        return 0, 0, 0
    objetivos = {str(aid).strip() for aid in (alumno_ids or []) if str(aid).strip()}
    agregados = quitados = movidos = 0
    ahora = datetime.now().isoformat()

    with _lock_json:
        data = _leer_json_disco()
        for a in data.get("alumnos", []):
            aid = str(a.get("alumno_id", "") or "").strip()
            if not aid:
                continue
            insc = _inscripciones_de_alumno(a)
            prev = next((i for i in insc if i["curso"] == curso), None)
            otras = [i for i in insc if i["curso"] != curso]
            cambio = False
            if aid in objetivos:
                if prev is None:
                    a["inscripciones"] = otras + [{"curso": curso, "seccion": seccion}]
                    agregados += 1
                    cambio = True
                elif prev["seccion"] != seccion:
                    a["inscripciones"] = otras + [{"curso": curso, "seccion": seccion}]
                    movidos += 1
                    cambio = True
            else:
                if prev is not None and prev["seccion"] == seccion:
                    a["inscripciones"] = otras
                    quitados += 1
                    cambio = True
            if cambio:
                a["actualizado_en"] = ahora
                _normalizar_inscripciones(a)
        if agregados or quitados or movidos:
            _escribir_json(data)
    if agregados or quitados or movidos:
        _sincronizar()
    return agregados, quitados, movidos


def quitar_inscripciones_seccion(curso, seccion):
    """Quita la asignación (curso, seccion) de todos los alumnos."""
    curso = str(curso or "").strip()
    seccion = str(seccion or "").strip()
    if not curso or not seccion:
        return 0
    modificados = 0
    ahora = datetime.now().isoformat()
    with _lock_json:
        data = _leer_json_disco()
        for a in data.get("alumnos", []):
            insc = _inscripciones_de_alumno(a)
            nuevas = [
                i for i in insc
                if not (i["curso"] == curso and i["seccion"] == seccion)
            ]
            if len(nuevas) != len(insc):
                a["inscripciones"] = nuevas
                a["actualizado_en"] = ahora
                _normalizar_inscripciones(a)
                modificados += 1
        if modificados:
            _escribir_json(data)
    if modificados:
        _sincronizar()
    return modificados


def quitar_inscripciones_curso(curso):
    """Quita todas las asignaciones de `curso` en alumnos.json."""
    curso = str(curso or "").strip()
    if not curso:
        return 0
    modificados = 0
    ahora = datetime.now().isoformat()
    with _lock_json:
        data = _leer_json_disco()
        for a in data.get("alumnos", []):
            insc = _inscripciones_de_alumno(a)
            nuevas = [i for i in insc if i["curso"] != curso]
            if len(nuevas) != len(insc):
                a["inscripciones"] = nuevas
                a["actualizado_en"] = ahora
                _normalizar_inscripciones(a)
                modificados += 1
        if modificados:
            _escribir_json(data)
    if modificados:
        _sincronizar()
    return modificados


def renombrar_curso(viejo, nuevo):
    """Actualiza el nombre de un curso en las inscripciones de todos los alumnos."""
    viejo = str(viejo or "").strip()
    nuevo = str(nuevo or "").strip()
    if not viejo or not nuevo or viejo == nuevo:
        return 0
    modificados = 0
    with _lock_json:
        data = _leer_json_disco()
        for a in data.get("alumnos", []):
            cambio = False
            for i in (a.get("inscripciones") or []):
                if isinstance(i, dict) and str(i.get("curso", "") or "").strip() == viejo:
                    i["curso"] = nuevo
                    cambio = True
            if str(a.get("curso", "") or "").strip() == viejo:
                a["curso"] = nuevo
                cambio = True
            if cambio:
                _normalizar_inscripciones(a)
                modificados += 1
        if modificados:
            _escribir_json(data)
    if modificados:
        _sincronizar()
    return modificados


def renombrar_seccion(curso, vieja, nueva):
    """Actualiza el nombre de una sección para un curso en alumnos.json."""
    curso = str(curso or "").strip()
    vieja = str(vieja or "").strip()
    nueva = str(nueva or "").strip()
    if not curso or not vieja or not nueva or vieja == nueva:
        return 0
    modificados = 0
    with _lock_json:
        data = _leer_json_disco()
        for a in data.get("alumnos", []):
            cambio = False
            for i in (a.get("inscripciones") or []):
                if (isinstance(i, dict)
                        and str(i.get("curso", "") or "").strip() == curso
                        and str(i.get("seccion", "") or "").strip() == vieja):
                    i["seccion"] = nueva
                    cambio = True
            if (str(a.get("curso", "") or "").strip() == curso
                    and str(a.get("seccion", "") or "").strip() == vieja):
                a["seccion"] = nueva
                cambio = True
            if cambio:
                _normalizar_inscripciones(a)
                modificados += 1
        if modificados:
            _escribir_json(data)
    if modificados:
        _sincronizar()
    return modificados
