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
    try:
        con.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
    return con


_NUEVAS_COLUMNAS_ALUMNOS = [
    ("apellidos", "TEXT DEFAULT ''"),
    ("dni", "TEXT DEFAULT ''"),
    ("fecha_nacimiento", "TEXT DEFAULT ''"),
    ("sexo", "TEXT DEFAULT ''"),
    ("telefono", "TEXT DEFAULT ''"),
    ("email", "TEXT DEFAULT ''"),
    ("tutor_nombre", "TEXT DEFAULT ''"),
    ("tutor_telefono", "TEXT DEFAULT ''"),
    ("direccion", "TEXT DEFAULT ''"),
    ("seccion", "TEXT DEFAULT ''"),
    ("condicion", "TEXT DEFAULT 'regular'"),
    ("observaciones", "TEXT DEFAULT ''"),
    ("actualizado_en", "TEXT DEFAULT ''"),
]


def _migrar_alumnos(cur):
    """Añade columnas nuevas a alumnos si no existen (idempotente)."""
    try:
        cur.execute("PRAGMA table_info(alumnos)")
        existentes = {row[1] for row in cur.fetchall()}
    except Exception:
        return
    for col, tipo in _NUEVAS_COLUMNAS_ALUMNOS:
        if col not in existentes:
            try:
                cur.execute(f"ALTER TABLE alumnos ADD COLUMN {col} {tipo}")
            except Exception:
                pass
    # índice único para DNI (solo si no existe)
    try:
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_alumnos_dni ON alumnos(dni) WHERE dni <> ''")
    except Exception:
        pass
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alumnos_curso ON alumnos(curso)")
    except Exception:
        pass
    try:
        cur.execute("CREATE INDEX IF NOT EXISTS idx_alumnos_estado ON alumnos(estado)")
    except Exception:
        pass


_db_inicializada = False


def init_db():
    global _db_inicializada
    if _db_inicializada:
        return
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS alumnos (
        alumno_id   TEXT PRIMARY KEY,
        nombre      TEXT,
        apellidos   TEXT DEFAULT '',
        dni         TEXT DEFAULT '',
        fecha_nacimiento TEXT DEFAULT '',
        sexo        TEXT DEFAULT '',
        telefono    TEXT DEFAULT '',
        email       TEXT DEFAULT '',
        tutor_nombre TEXT DEFAULT '',
        tutor_telefono TEXT DEFAULT '',
        direccion   TEXT DEFAULT '',
        curso       TEXT DEFAULT '',
        grupo       TEXT DEFAULT '',
        seccion     TEXT DEFAULT '',
        condicion   TEXT DEFAULT 'regular',
        foto_referencia TEXT,
        observaciones TEXT DEFAULT '',
        estado      TEXT DEFAULT 'activo',
        fecha_registro TEXT,
        actualizado_en TEXT DEFAULT ''
    )""")
    # Migracion idempotente para BDs antiguas
    _migrar_alumnos(cur)
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
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_asistencia_alumno_entrada
    ON asistencia(alumno_id, entrada)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_permanencia_alumno_fecha
    ON permanencia(alumno_id, fecha)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS asistencia_manual (
        alumno_id      TEXT NOT NULL,
        curso          TEXT NOT NULL DEFAULT '',
        seccion        TEXT NOT NULL DEFAULT '',
        fecha          TEXT NOT NULL,
        estado         TEXT NOT NULL,
        hora           TEXT DEFAULT '',
        actualizado_en TEXT DEFAULT '',
        PRIMARY KEY (alumno_id, curso, seccion, fecha)
    )""")
    con.commit()
    con.close()
    _db_inicializada = True


def sincronizar_alumnos(json_path, alumnos=None):
    """Lee alumnos.json (o usa `alumnos` si ya se cargó) y lo refleja en SQLite."""
    init_db()
    if alumnos is None:
        with open(json_path, encoding="utf-8") as f:
            alumnos = json.load(f).get("alumnos", [])
    con = _conectar()
    cur = con.cursor()
    for a in alumnos:
        cur.execute("""
        INSERT INTO alumnos (alumno_id, nombre, apellidos, dni, fecha_nacimiento, sexo,
            telefono, email, tutor_nombre, tutor_telefono, direccion, curso, grupo, seccion,
            condicion, foto_referencia, observaciones, estado, fecha_registro, actualizado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alumno_id) DO UPDATE SET
            nombre=excluded.nombre,
            apellidos=excluded.apellidos,
            dni=excluded.dni,
            fecha_nacimiento=excluded.fecha_nacimiento,
            sexo=excluded.sexo,
            telefono=excluded.telefono,
            email=excluded.email,
            tutor_nombre=excluded.tutor_nombre,
            tutor_telefono=excluded.tutor_telefono,
            direccion=excluded.direccion,
            curso=excluded.curso,
            grupo=excluded.grupo,
            seccion=excluded.seccion,
            condicion=excluded.condicion,
            foto_referencia=excluded.foto_referencia,
            observaciones=excluded.observaciones,
            estado=excluded.estado,
            actualizado_en=excluded.actualizado_en
        """, (
            a.get("alumno_id"),
            a.get("nombre"),
            a.get("apellidos", ""),
            a.get("dni", ""),
            a.get("fecha_nacimiento", ""),
            a.get("sexo", ""),
            a.get("telefono", ""),
            a.get("email", ""),
            a.get("tutor_nombre", ""),
            a.get("tutor_telefono", ""),
            a.get("direccion", ""),
            a.get("curso", ""),
            "",   # 'grupo' retirado del modelo; la seccion vive en 'seccion'
            a.get("seccion", ""),
            a.get("condicion", "regular"),
            a.get("foto_referencia"),
            a.get("observaciones", ""),
            a.get("estado", "activo"),
            a.get("fecha_registro"),
            a.get("actualizado_en", ""),
        ))
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


def obtener_estado_alumno_hoy(alumno_id):
    """Estado de asistencia de hoy para un solo alumno (consulta indexada)."""
    if not alumno_id:
        return None
    init_db()
    hoy = datetime.now().strftime("%Y-%m-%d")
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        SELECT COUNT(*) AS marcas,
               MIN(entrada) AS primera_entrada,
               MAX(entrada) AS ultima_entrada,
               MAX(salida) AS ultima_salida
        FROM asistencia
        WHERE alumno_id=? AND entrada >= ? AND entrada < ?
    """, (alumno_id, hoy, hoy + "\uffff"))
    row = cur.fetchone()
    con.close()
    if row is None:
        return {
            "marcas": 0,
            "primera_entrada": None,
            "ultima_entrada": None,
            "ultima_salida": None,
        }
    return dict(row)


def obtener_estado_fecha(fecha):
    """
    Devuelve lista de alumnos activos con su estado en una fecha dada (YYYY-MM-DD).
    Incluye curso, seccion, entrada, salida y minutos de permanencia.
    """
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        SELECT a.alumno_id, a.nombre, a.curso, a.seccion,
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
        ORDER BY a.apellidos, a.nombre
    """, (fecha, fecha, fecha, fecha))
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return rows


def primer_dia_actividad():
    """Primera fecha (YYYY-MM-DD) con asistencia registrada, o None."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("SELECT MIN(date(entrada)) AS fecha FROM asistencia")
    fila = cur.fetchone()
    con.close()
    return fila["fecha"] if fila else None


def fechas_con_actividad(fecha_ini, fecha_fin):
    """Fechas (YYYY-MM-DD) del rango con al menos una asistencia registrada."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        SELECT DISTINCT date(entrada) AS fecha
        FROM asistencia
        WHERE date(entrada) BETWEEN ? AND ?
        ORDER BY fecha
    """, (fecha_ini, fecha_fin))
    filas = [r["fecha"] for r in cur.fetchall()]
    con.close()
    return filas


def obtener_historial_rango(fecha_ini, fecha_fin):
    """
    Historial de asistencias por alumno y dia dentro del rango (incluidos).

    Devuelve dicts con: alumno_id, nombre, fecha, entrada
    (primera del dia), salida (ultima no nula), minutos, confianza y marcas.
    El curso/seccion de la vista los aporta el gestor (inscripciones), no
    esta consulta: un alumno puede pertenecer a varios cursos.
    Solo cubre dias con actividad registrada (si el sistema no corrio,
    no hay datos que mostrar de ese dia).
    """
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        SELECT a.alumno_id, a.nombre, a.apellidos,
               date(x.entrada) AS fecha,
               MIN(x.entrada) AS entrada,
               (SELECT MAX(x2.salida) FROM asistencia x2
                 WHERE x2.alumno_id = a.alumno_id
                   AND date(x2.entrada) = date(x.entrada)) AS salida,
               (SELECT p.minutos FROM permanencia p
                 WHERE p.alumno_id = a.alumno_id
                   AND p.fecha = date(x.entrada)) AS minutos,
               (SELECT x3.confianza FROM asistencia x3
                 WHERE x3.alumno_id = a.alumno_id
                   AND date(x3.entrada) = date(x.entrada)
                 ORDER BY x3.entrada LIMIT 1) AS confianza,
               COUNT(x.id) AS marcas
        FROM alumnos a
        JOIN asistencia x ON x.alumno_id = a.alumno_id
        WHERE a.estado = 'activo' AND date(x.entrada) BETWEEN ? AND ?
        GROUP BY a.alumno_id, date(x.entrada)
    """, (fecha_ini, fecha_fin))
    filas = [dict(r) for r in cur.fetchall()]
    con.close()
    return filas


def guardar_asistencia_manual(alumno_id, curso, seccion, fecha, estado, hora=""):
    """Guarda o actualiza un ajuste manual de asistencia para (alumno, curso, seccion, fecha)."""
    init_db()
    curso = str(curso or "").strip()
    seccion = str(seccion or "").strip()
    fecha = str(fecha or "")[:10]
    estado = str(estado or "").strip()
    hora = str(hora or "").strip()[:5]
    ahora = datetime.now().isoformat()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        INSERT INTO asistencia_manual (alumno_id, curso, seccion, fecha, estado, hora, actualizado_en)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(alumno_id, curso, seccion, fecha) DO UPDATE SET
            estado=excluded.estado,
            hora=excluded.hora,
            actualizado_en=excluded.actualizado_en
    """, (alumno_id, curso, seccion, fecha, estado, hora, ahora))
    con.commit()
    con.close()


def eliminar_asistencia_manual(alumno_id, curso, seccion, fecha):
    """Elimina un ajuste manual y restaura el cálculo automático."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("""
        DELETE FROM asistencia_manual
        WHERE alumno_id=? AND curso=? AND seccion=? AND fecha=?
    """, (alumno_id, str(curso or "").strip(), str(seccion or "").strip(), str(fecha or "")[:10]))
    con.commit()
    con.close()


def obtener_contexto_historial(hoy):
    """Obtiene en una sola conexión (primer_dia, marcas_hasta_hoy, alumnos_activos, manuales)."""
    init_db()
    con = _conectar()
    cur = con.cursor()
    cur.execute("SELECT MIN(date(entrada)) AS fecha FROM asistencia")
    fila = cur.fetchone()
    primer = fila["fecha"] if fila else None
    marcas = []
    if primer:
        cur.execute("""
            SELECT a.alumno_id, a.nombre, a.apellidos,
                   date(x.entrada) AS fecha,
                   MIN(x.entrada) AS entrada,
                   (SELECT MAX(x2.salida) FROM asistencia x2
                     WHERE x2.alumno_id = a.alumno_id
                       AND date(x2.entrada) = date(x.entrada)) AS salida,
                   (SELECT p.minutos FROM permanencia p
                     WHERE p.alumno_id = a.alumno_id
                       AND p.fecha = date(x.entrada)) AS minutos,
                   (SELECT x3.confianza FROM asistencia x3
                     WHERE x3.alumno_id = a.alumno_id
                       AND date(x3.entrada) = date(x.entrada)
                     ORDER BY x3.entrada LIMIT 1) AS confianza,
                   COUNT(x.id) AS marcas
            FROM alumnos a
            JOIN asistencia x ON x.alumno_id = a.alumno_id
            WHERE a.estado = 'activo' AND date(x.entrada) BETWEEN ? AND ?
            GROUP BY a.alumno_id, date(x.entrada)
        """, (primer, hoy))
        marcas = [dict(r) for r in cur.fetchall()]
    cur.execute("SELECT * FROM alumnos WHERE estado='activo'")
    activos = [dict(r) for r in cur.fetchall()]
    cur.execute("""
        SELECT alumno_id, curso, seccion, fecha, estado, hora
        FROM asistencia_manual
    """)
    manuales = {
        (r["alumno_id"], r["curso"], r["seccion"], r["fecha"]): {
            "estado": r["estado"],
            "hora": r["hora"] or "",
        }
        for r in cur.fetchall()
    }
    con.close()
    return primer, marcas, activos, manuales
