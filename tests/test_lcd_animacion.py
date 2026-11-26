"""Tests rápidos para lcd_animacion.py."""

import os
import sys

_DIR_APP = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "app"))
if _DIR_APP not in sys.path:
    sys.path.insert(0, _DIR_APP)

from centinela.lcd import estado_lcd, lcd_animacion as L


def _assert_ok(a, b):
    assert isinstance(a, str) and isinstance(b, str)
    assert len(a) == 16, (len(a), repr(a))
    assert len(b) == 16, (len(b), repr(b))
    for c in a + b:
        assert 32 <= ord(c) <= 126, repr(c)
        assert c != "\\", repr(c)


def _run(estado_setup, tiempos, cfg=None):
    if estado_setup is not None:
        estado_setup()
    L._estado_prev = None
    L._t_inicio_estado = None
    L._marq_state.clear()
    base = 5000.0
    for dt in tiempos:
        a, b = L.obtener_filas_animadas(cfg=cfg, ahora=base + dt)
        _assert_ok(a, b)


# Esperando
_run(estado_lcd.actualizar_esperando, [0, 0.45, 0.9, 1.35, 1.8, 2.25])

# Procesando (progreso bajo y alto)
estado_lcd.actualizar_procesamiento(nombre="JUAN PEREZ GOMEZ LARGO", progreso=20)
_run(None, [0, 0.35, 0.7])
estado_lcd.actualizar_procesamiento(nombre="JUAN", progreso=45)
_run(None, [0, 0.35])
estado_lcd.actualizar_procesamiento(nombre="JUAN", progreso=100)
_run(None, [0, 0.35])

# Confirmado
estado_lcd.actualizar_confirmado(nombre="ERICK OBED")
_run(None, [0, 0.2, 0.4, 0.8])

# Desconocido
estado_lcd.actualizar_desconocido()
_run(None, [0, 0.45, 0.9])

# Incompleto (motivo corto y largo)
estado_lcd.actualizar_incompleto(nombre="ANA", motivo="FOTO")
_run(None, [0, 0.5, 1.0])
estado_lcd.actualizar_incompleto(nombre="ANA", motivo="FOTO INCOMPLETA LARGA")
_run(None, [0, 0.5, 1.4, 2.8])

# Velocidad (no debe romper cadencia ni longitudes)
estado_lcd.actualizar_esperando()
_run(None, [0, 0.2, 0.4], cfg={"lcd_animacion": {"activa": True, "velocidad": 2.0, "marquesina": True}})

# Marquesina pausada (nombre largo)
estado_lcd.actualizar_procesamiento(nombre="JUAN PEREZ GOMEZ LARGO", progreso=50)
_run(None, [0, 0.5, 1.0, 2.0, 4.0])

# Probar LCD
for paso in range(3):
    a, b = L.obtener_filas_prueba(paso)
    _assert_ok(a, b)

# Caracteres prohibidos / control
estado_lcd.actualizar_incompleto(nombre="A\\B", motivo="C\\D")
_run(None, [0, 0.5])

print("TODOS LOS TESTS OK")
