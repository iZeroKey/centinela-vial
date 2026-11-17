#!/usr/bin/env python3
"""Prueba de humo Fase 1b con la VentanaPrincipal real (via completo)."""
import os, sys, time, json, ssl, threading, urllib.request

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app", "asistencia_v6_fluent")
sys.path.insert(0, APP)
os.chdir(APP)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import QTimer
app_qt = QApplication([])

import numpy as np
import cv2
from ventana_principal import VentanaPrincipal

ventana = VentanaPrincipal()
ventana.show()
ventana._iniciar_servidor_web()   # servidor con el callback real

resultado = {"ok": False}

def pruebas_http():
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx),
            urllib.request.ProxyHandler({}))
        TEL = "tel-prueba-1234"

        r = opener.open(f"https://localhost:8443/api/telefono/heartbeat"
                        f"?tel={TEL}&nombre=Prueba&camara=environment", timeout=5)
        print("heartbeat:", json.loads(r.read().decode()), flush=True)

        r = opener.open(f"https://localhost:8443/api/telefono/estado?tel={TEL}",
                        timeout=5)
        print("estado:", json.loads(r.read().decode()), flush=True)

        frame = np.full((480, 640, 3), (40, 90, 60), np.uint8)
        cv2.circle(frame, (320, 240), 60, (90, 200, 120), -1)
        _, buf = cv2.imencode(".jpg", frame)
        exitos = 0
        intentos = 6
        for i in range(intentos):
            try:
                req = urllib.request.Request(
                    f"https://localhost:8443/api/telefono/frame?tel={TEL}&nombre=Prueba",
                    data=buf.tobytes(), method="POST")
                r = opener.open(req, timeout=15)
                cuerpo = r.read()
                if r.status == 200 and cuerpo[:2] == b"\xff\xd8":
                    exitos += 1
            except Exception:
                pass  # igual que la pagina: reintenta en el proximo ciclo
            time.sleep(0.2)
        print(f"frames anotados: {exitos}/{intentos}", flush=True)
        resultado["ok"] = exitos >= intentos // 2

        r = opener.open("https://localhost:8443/api/telefonos", timeout=5)
        print("telefonos:", json.loads(r.read().decode()), flush=True)

        r = opener.open("https://localhost:8443/telefono", timeout=5)
        html = r.read().decode()
        print("pagina /telefono:", r.status, "| getUserMedia =",
              "getUserMedia" in html, flush=True)

        hilo_tel = ventana.hilos_telefono.get(TEL)
        print("hilo telefono creado:", hilo_tel is not None
              and hilo_tel.isRunning(), flush=True)
        resultado["ok"] = (resultado["ok"] and hilo_tel is not None
                           and hilo_tel.isRunning())
    except Exception as e:
        print("FALLO HTTP:", repr(e), flush=True)
    finally:
        QTimer.singleShot(0, app_qt.quit)

threading.Thread(target=pruebas_http, daemon=True).start()
app_qt.exec()

ventana._servidor_web.detener() if ventana._servidor_web else None
for h in list(ventana.hilos_telefono.values()):
    h.detener()

if not resultado["ok"]:
    sys.exit(1)
print("TODO OK")
