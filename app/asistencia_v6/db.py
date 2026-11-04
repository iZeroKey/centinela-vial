#!/usr/bin/env python3
"""
Centinela Control — Base de datos SQLite para asistencia y vigilancia de aula (v6).

Tablas:
  alumnos       : registro de alumnos (sincronizado con app/data/asistencia/alumnos.json)
  asistencia    : marcas de presencia (alumno_id, entrada, salida, confianza, camara)
  permanencia   : resumen diario por alumno (fecha, entrada, salida, minutos)

Cambios respecto a v5:
  - La BD vive en app/data/centinela/centinela.sqlite (en v5 caía en
    app/app/asistencia/db por un doble dirname).
  - alumnos tiene columnas curso y grupo.
  - La baja de alumnos es lógica (estado='inactivo'): conserva historial.
"""

import os, sqlite3, json
from datetime import datetime, timedelta

# .../asistencia_v6/db.py -> .../app
_ROOT_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DB_DIR = os.path.join(_ROOT_APP, "data", "centinela")
os.makedirs(_DB_DIR, exist_ok=True)
DB_PATH = os.path.join(_DB_DIR, "centinela.sqlite")


def _conectar():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alumnos (
        alumno_id   TEXT PRIMARY KEY,
        nombre      TEXT,
        curso       TEXT DEFAULT '',
        grupo       TEXT DEFAULT '',
        foto_referencia TEXT,
        estado      TEXT DEFAULT 'activo',
        fecha_registro TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS asistencia (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id   TEXT NOT NULL,
        entrada     TEXT NOT NULL,
        salida      TEXT,
        confianza   REAL,
        camara      TEXT,
        FOREIGN KEY(alumno_id) REFERENCES alumnos(alumno_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS permanencia (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id   TEXT NOT NULL,
        fecha       TEXT NOT NULL,
        entrada     TEXT NOT NULL,
        salida      TEXT,
        minutos     INTEGER DEFAULT 0,
        UNIQUE(alumno_id, fecha)
    )""")
    con.commit()
    con.close()


def sincronizar_alumnos(json_path):
    """Lee alumnos.json y lo refleja en la tabla alumnos (insert o update)."""
    init_db()
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    con = _conectar()
    cur = con.cursor()
    for a in data.get("alumnos", []):
        cur.execute("""
        INSERT INTO alumnos (alumno_id, nombre, curso, grupo, foto_referencia, estado, fecha_registro)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alumno_id) DO UPDATE SET
            nombre=excluded.nombre,
            curso=excluded.curso,
            grupo=excluded.grupo,
            foto_referencia=excluded.foto_referencia,
            estado=excluded.estado,
            fecha_registro=excluded.fecha_registro
        """, (a["alumno_id"], a.get("nombre"), a.get("curso", ""), a.get("grupo", ""),
              a.get("foto_referencia"), a.get("estado", "activo"), a.get("fecha_registro")))
    con.commit()
    con.close()


def obtener_alumnos():
    """Devuelve todos los alumnos activos como lista de dict."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("SELECT * FROM alumnos WHERE estado='activo'")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def ventana_abierta(alumno_id, segundos=300):
    """
    True si el alumno tiene una asistencia abierta (sin salida) en los últimos
    `segundos` segundos. Se usa para evitar duplicados.
    """
    init_db()
    con = _conectar()
    cur = con.cursor()
    corte = (datetime.now() - timedelta(seconds=segundos)).isoformat()
    cur.execute("""
        SELECT id FROM asistencia
        WHERE alumno_id=? AND salida IS NULL AND entrada > ?
        ORDER BY entrada DESC LIMIT 1
    """, (alumno_id, corte))
    row = cur.fetchone()
    con.close()
    return row is not None


def registrar_entrada(alumno_id, confianza, camara="cam_1"):
    """Marca entrada de asistencia si no hay una abierta recientemente."""
    if ventana_abierta(alumno_id):
        return None  # duplicado ignorado
    init_db()
    con = _conectar()
    cur = con.cursor()
    ahora = datetime.now().isoformat()
    cur.execute("""
        INSERT INTO asistencia (alumno_id, entrada, confianza, camara)
        VALUES (?, ?, ?, ?)
    """, (alumno_id, ahora, confianza, camara))
    asistencia_id = cur.lastrowid
    # permanencia del día
    hoy = ahora[:10]
    cur.execute("""
        INSERT INTO permanencia (alumno_id, fecha, entrada, salida, minutos)
        VALUES (?, ?, ?, NULL, 0)
        ON CONFLICT(alumno_id, fecha) DO NOTHING
    """, (alumno_id, hoy, ahora))
    con.commit()
    con.close()
    return asistencia_id


def registrar_salida(alumno_id, camara="cam_1"):
    """Cierra la asistencia abierta y actualiza permanencia."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    ahora = datetime.now().isoformat()
    hoy = ahora[:10]
    cur.execute("""
        UPDATE asistencia SET salida=? WHERE alumno_id=? AND salida IS NULL
        ORDER BY entrada DESC LIMIT 1
    """, (ahora, alumno_id))
    # actualizar permanencia
    cur.execute("SELECT entrada FROM permanencia WHERE alumno_id=? AND fecha=?", (alumno_id, hoy))
    row = cur.fetchone()
    if row:
        entrada = datetime.fromisoformat(row["entrada"])
        minutos = int((datetime.now() - entrada).total_seconds() // 60)
        cur.execute("""
            UPDATE permanencia SET salida=?, minutos=? WHERE alumno_id=? AND fecha=?
        """, (ahora, minutos, alumno_id, hoy))
    con.commit()
    con.close()


def obtener_estado_hoy():
    """Devuelve lista de alumnos con su estado de asistencia hoy."""
    return obtener_estado_fecha(datetime.now().strftime("%Y-%m-%d"))


def obtener_estado_fecha(fecha):
    """
    Devuelve lista de alumnos activos con su estado en una fecha dada (YYYY-MM-DD).
    Incluye curso, grupo, entrada, salida y minutos de permanencia.
    """
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        SELECT a.alumno_id, a.nombre, a.curso, a.grupo,
               (SELECT COUNT(*) FROM asistencia as2
                WHERE as2.alumno_id=a.alumno_id AND date(as2.entrada)=date(?)) as marcas,
               (SELECT entrada FROM asistencia as2
                WHERE as2.alumno_id=a.alumno_id AND as2.salida IS NULL
                  AND date(as2.entrada)=date(?) ORDER BY entrada DESC LIMIT 1) as entrada_abierta,
               (SELECT salida FROM asistencia as2
                WHERE as2.alumno_id=a.alumno_id AND date(as2.entrada)=date(?)
                ORDER BY entrada DESC LIMIT 1) as ultima_salida,
               (SELECT minutos FROM permanencia p
                WHERE p.alumno_id=a.alumno_id AND p.fecha=?) as minutos
        FROM alumnos a
        WHERE a.estado='activo'
        ORDER BY a.nombre
    """, (fecha, fecha, fecha, fecha))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows
