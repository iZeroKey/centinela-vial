#!/usr/bin/env python3
"""
Centinela Control — Resolución centralizada de rutas (desarrollo y ejecutable).
"""

import os
import sys

ES_CONGELADO = bool(getattr(sys, "frozen", False))

if ES_CONGELADO:
    DIR_RECURSOS = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DIR_DATOS = os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "data")
else:
    DIR_CORE = os.path.dirname(os.path.abspath(__file__))      # .../app/centinela/core
    DIR_PAQUETE = os.path.dirname(DIR_CORE)                    # .../app/centinela
    DIR_APP = os.path.dirname(DIR_PAQUETE)                     # .../app
    DIR_RAIZ = os.path.dirname(DIR_APP)                        # .../centinela-vial
    DIR_RECURSOS = DIR_RAIZ
    DIR_DATOS = os.path.join(DIR_APP, "data")

DIR_DATOS_CENTINELA = os.path.join(DIR_DATOS, "centinela")
DIR_DATOS_ASISTENCIA = os.path.join(DIR_DATOS, "asistencia")
DIR_CERTS = os.path.join(DIR_DATOS, "certs")

DIR_MODELOS = os.path.join(DIR_RECURSOS, "models")
RUTA_MODELO_DETECTOR = os.path.join(DIR_MODELOS, "detector_facial.pth")
RUTA_FAVICON = os.path.join(DIR_RECURSOS, "assets", "favicon.svg")
