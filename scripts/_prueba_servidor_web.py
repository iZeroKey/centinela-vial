#!/usr/bin/env python3
"""Prueba de humo del servidor web (Fase 1a): cert + HTTPS + endpoints."""
import os, sys, threading, time, json, ssl, urllib.request

os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app", "asistencia_v6_fluent"))
os.chdir(os.path.join(ROOT, "app", "asistencia_v6_fluent"))

import numpy as np
from servidor_web import ServidorWeb, generar_certificado, ip_local

generar_certificado()
print("cert OK:", os.path.exists(os.path.join("..", "data", "certs", "centinela.crt")))

frame = np.full((270, 480, 3), (40, 90, 60), np.uint8)
estado = {"nodos": {"cam_1": {"online": True, "tipo": "camara"}}}
srv = ServidorWeb(lambda: frame, lambda: estado)
srv.start()
time.sleep(0.5)

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx),
                                     urllib.request.ProxyHandler({}))

r = opener.open("https://localhost:8443/ping", timeout=5)
print("ping:", r.status, r.read().decode())

r = opener.open("https://localhost:8443/api/asistencia", timeout=5)
datos = json.loads(r.read().decode())
print("asistencia: alumnos =", len(datos.get("alumnos", [])),
      "| fecha =", datos.get("fecha"))

r = opener.open("https://localhost:8443/api/status", timeout=5)
print("status:", r.read().decode())

r = opener.open("https://localhost:8443/foto", timeout=5)
cuerpo = r.read()
print("foto:", r.status, "bytes =", len(cuerpo), "jpeg =", cuerpo[:2] == b"\xff\xd8")

r = opener.open("https://localhost:8443/stream", timeout=5)
trozo = r.read(120)
print("stream:", r.status, "multipart =",
      b"multipart" in r.headers.get("Content-Type", "").encode(),
      "| primer bloque =",
      trozo.split(b"\r\n")[0].decode() if trozo else "?")

r = opener.open("https://localhost:8443/", timeout=5)
html = r.read().decode()
print("visor:", r.status, "html =", len(html), "chars | tiene stream =",
      "/stream" in html and "/api/asistencia" in html)

srv.detener()
print("servidor detenido OK")
