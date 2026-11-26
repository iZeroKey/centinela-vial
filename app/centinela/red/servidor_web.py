#!/usr/bin/env python3
"""
Centinela Control — Servidor web + mDNS.

Publica el sistema en la red local para consultarlo desde cualquier
dispositivo (teléfono, laptop) con el navegador:

  https://centinela.local:8443/               visor (video anotado + asistencia)
  https://centinela.local:8443/stream         MJPEG del último frame anotado
  https://centinela.local:8443/foto           frame anotado actual (JPEG suelto)
  https://centinela.local:8443/api/asistencia JSON asistencia de hoy (lectura)
  https://centinela.local:8443/api/status     salud de nodos/cámaras
  https://centinela.local:8443/ping           "OK"

- HTTPS con certificado propio (se genera una sola vez en data/certs/):
  getUserMedia en el teléfono exige contexto seguro.
- mDNS con zeroconf: anuncia centinela.local en la red, sin IP fija.
- La fuente de frames y el estado se inyectan como callables, para no
  acoplar este módulo a la GUI. Solo lectura: nunca toca la IA.

Hilo propio (daemon). start()/detener() son seguros para llamar desde la GUI.
"""

import base64
import collections
import hashlib
import io
import json
import os
import select
import socket
import ssl
import struct
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

from centinela.core import rutas

PUERTO_HTTPS = 8443
NOMBRE_MDNS = "centinela"
_CALIDAD_JPEG = 80

_DIR_CERTS = rutas.DIR_CERTS
_RUTA_CRT = os.path.join(_DIR_CERTS, "centinela.crt")
_RUTA_KEY = os.path.join(_DIR_CERTS, "centinela.key")
_RUTA_FAVICON = rutas.RUTA_FAVICON


# ---------------------------------------------------------------------------
# Certificado propio (getUserMedia exige HTTPS; el teléfono acepta 1 advertencia)
# ---------------------------------------------------------------------------

def _ips_cert():
    """IPs que deben ir en el SAN: loopback + IP LAN actual."""
    import ipaddress
    ips = ["127.0.0.1"]
    ip = ip_local()
    if ip and ip != "127.0.0.1" and ip not in ips:
        try:
            ipaddress.ip_address(ip)
            ips.append(ip)
        except ValueError:
            pass
    return ips


def generar_certificado(ruta_crt=_RUTA_CRT, ruta_key=_RUTA_KEY,
                        hostname="centinela.local"):
    """Genera un certificado autofirmado (10 años).

    Se regenera si existe pero NO incluye la IP LAN actual: al entrar por
    https://<ip>:8443/telefono (centinela.local no resuelve en todas las
    redes) el SAN debe cubrir esa IP o el navegador trata la página como
    contexto NO seguro y bloquea getUserMedia (la cámara del teléfono).
    """
    import ipaddress
    ips = _ips_cert()
    if os.path.exists(ruta_crt) and os.path.exists(ruta_key):
        try:
            from cryptography import x509
            with open(ruta_crt, "rb") as f:
                cert = x509.load_pem_x509_certificates(f.read())[0]
            san = cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName).value
            san_ips = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            if all(ip in san_ips for ip in ips):
                return
            print(f"[WEB] certificado no cubre la IP actual ({ips[-1]}); "
                  "regenerando (acepta de nuevo el aviso en el teléfono)")
        except Exception:
            pass  # cert ilegible/corrupto: regenerar
    os.makedirs(os.path.dirname(ruta_crt), exist_ok=True)
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import ipaddress

    desde = datetime.now(tz=timezone.utc) - timedelta(days=1)
    hasta = desde + timedelta(days=3650)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    san = x509.SubjectAlternativeName(
        [x509.DNSName(hostname),
         x509.DNSName("localhost")]
        + [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips])
    cert = (x509.CertificateBuilder()
            .subject_name(nombre)
            .issuer_name(nombre)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(desde)
            .not_valid_after(hasta)
            .add_extension(san, critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .sign(key, hashes.SHA256()))
    with open(ruta_key, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    with open(ruta_crt, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def ip_local():
    """IP LAN de esta máquina. Evita salir a Internet (8.8.8.8)."""
    # 1) nombre del host: normalmente el DNS local devuelve la IP del adaptador
    try:
        hostname = socket.gethostname()
        ip = socket.gethostbyname(hostname)
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass
    # 2) getaddrinfo: filtra el primer IPv4 no-loopback
    try:
        infos = socket.getaddrinfo(socket.gethostname(), 80,
                                   socket.AF_INET, socket.SOCK_DGRAM)
        for fam, _, _, _, sockaddr in infos:
            ip = sockaddr[0]
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
    # 3) último intento: la IP de la ruta LAN
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.3)
        s.connect(("192.0.2.1", 80))  # TEST-NET-1, sin tráfico real
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        if s is not None:
            s.close()


# ---------------------------------------------------------------------------
# WebSocket RFC 6455 ligero (stdlib + numpy, mismo puerto HTTPS 8443)
# ---------------------------------------------------------------------------

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _ws_accept_key(key):
    digest = hashlib.sha1((key.strip() + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def _desenmascarar_ws(payload, mask4):
    """Desenmascara un payload WebSocket usando XOR vectorizado con NumPy."""
    n = len(payload)
    if n == 0:
        return b""
    arr = np.frombuffer(payload, dtype=np.uint8)
    m = np.frombuffer(mask4, dtype=np.uint8)
    rem = n & 3
    if rem == 0:
        return np.bitwise_xor(arr.reshape(-1, 4), m).tobytes()
    buf = np.empty(n + (4 - rem), dtype=np.uint8)
    buf[:n] = arr
    return np.bitwise_xor(buf.reshape(-1, 4), m).reshape(-1)[:n].tobytes()


class _SocketBuffer:
    """Lector por chunks compatible con SSLSocket.pending() y select()."""

    def __init__(self, conn):
        self.conn = conn
        self.buf = bytearray()

    def tiene_datos_listos(self, timeout=0.05):
        if self.buf:
            return True
        if hasattr(self.conn, "pending") and self.conn.pending() > 0:
            return True
        r, _, _ = select.select([self.conn], [], [], timeout)
        return bool(r)

    def leer_exacto(self, n):
        if n <= 0:
            return b""
        while len(self.buf) < n:
            chunk = self.conn.recv(max(16384, n - len(self.buf)))
            if not chunk:
                return None
            self.buf.extend(chunk)
        if len(self.buf) == n:
            out = bytes(self.buf)
            self.buf.clear()
            return out
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out


def _ws_leer_frame(lector, max_bytes=8_000_000):
    """Lee un mensaje WebSocket RFC 6455 (soporta fragmentación). Devuelve (opcode, bytes) o (None, None)."""
    partes = []
    op_principal = None
    total = 0
    while True:
        hdr = lector.leer_exacto(2)
        if hdr is None:
            return None, None
        b0, b1 = hdr[0], hdr[1]
        fin = bool(b0 & 0x80)
        op = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = lector.leer_exacto(2)
            if ext is None:
                return None, None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = lector.leer_exacto(8)
            if ext is None:
                return None, None
            length = struct.unpack("!Q", ext)[0]
        if length > max_bytes or total + length > max_bytes:
            return 0x8, b""
        mask_key = None
        if masked:
            mask_key = lector.leer_exacto(4)
            if mask_key is None:
                return None, None
        payload = lector.leer_exacto(length) if length > 0 else b""
        if payload is None:
            return None, None
        if masked and payload:
            payload = _desenmascarar_ws(payload, mask_key)
        if op >= 0x8:
            # Frame de control (ping/pong/close): devolver de inmediato
            return op, payload
        if op != 0x0:
            op_principal = op
            partes = [payload]
        else:
            partes.append(payload)
        total += length
        if fin:
            return (op_principal if op_principal is not None else 0x2), b"".join(partes)


def _ws_codificar_frame(opcode, payload_bytes):
    """Codifica un frame WebSocket servidor -> cliente (sin máscara)."""
    n = len(payload_bytes)
    b0 = 0x80 | (opcode & 0x0F)
    if n < 126:
        cab = bytes((b0, n))
    elif n < 65536:
        cab = struct.pack("!BBH", b0, 126, n)
    else:
        cab = struct.pack("!BBQ", b0, 127, n)
    return cab + payload_bytes


class _ConexionWS:
    """Canal de salida thread-safe para una conexión WebSocket de teléfono.

    Las escrituras reales sobre TLS ocurren únicamente dentro del bucle de esa
    conexión (`drenar_envios`), evitando lecturas/escrituras TLS simultáneas.
    """

    def __init__(self, conn, tel_id):
        self.conn = conn
        self.tel_id = tel_id
        self._lock = threading.Lock()
        self._cola = collections.deque()
        self.cerrado = False

    def encolar_frame(self, opcode, payload_bytes):
        trama = _ws_codificar_frame(opcode, payload_bytes)
        with self._lock:
            if self.cerrado:
                return
            self._cola.append(trama)

    def encolar_json(self, obj):
        datos = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.encolar_frame(0x1, datos)

    def enviar_inmediato_json(self, obj):
        """Solo llamar desde el hilo dueño del socket."""
        datos = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.conn.sendall(_ws_codificar_frame(0x1, datos))

    def enviar_inmediato_frame(self, opcode, payload_bytes=b""):
        """Solo llamar desde el hilo dueño del socket."""
        self.conn.sendall(_ws_codificar_frame(opcode, payload_bytes))

    def drenar_envios(self):
        """Envía todos los mensajes encolados desde el hilo dueño del socket."""
        with self._lock:
            if not self._cola:
                return
            pendientes = list(self._cola)
            self._cola.clear()
        for trama in pendientes:
            self.conn.sendall(trama)

    def marcar_cerrado(self):
        with self._lock:
            self.cerrado = True
            self._cola.clear()


# ---------------------------------------------------------------------------
# Servidor HTTPS
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Inyectado por ServidorWeb (callables), compartidos por todos los hilos:
    provedor_frame = None      # () -> np.ndarray | None (frame anotado BGR)
    provedor_estado = None     # () -> dict (salud de nodos/cámaras)
    servidor = None            # ServidorWeb (registro de telefonos)

    def setup(self):
        super().setup()
        try:
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    def log_message(self, fmt, *args):  # silencioso: la consola ya tiene log
        pass

    # -- utilidades ---------------------------------------------------------
    def _json(self, obj, codigo=200):
        cuerpo = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(cuerpo)

    def _html(self, texto):
        cuerpo = texto.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _jpeg(self):
        """Encode del frame anotado actual, o None si no hay cámara."""
        frame = self.provedor_frame() if self.provedor_frame else None
        if frame is None:
            return None
        import cv2
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), _CALIDAD_JPEG])
        return buf.tobytes() if ok else None

    # -- rutas ---------------------------------------------------------------
    def do_GET(self):
        ruta = self.path.split("?", 1)[0]
        params = self._params()
        try:
            if ruta == "/ping":
                self._json({"ok": True, "ts": datetime.now().isoformat()})
            elif ruta == "/api/ip":
                self._json({"ip": ip_local(), "puerto": PUERTO_HTTPS,
                            "url": f"https://{ip_local()}:{PUERTO_HTTPS}/"})
            elif ruta == "/favicon.svg" or ruta == "/favicon.ico":
                try:
                    with open(_RUTA_FAVICON, "rb") as f:
                        cuerpo = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/svg+xml")
                    self.send_header("Content-Length", str(len(cuerpo)))
                    self.send_header("Cache-Control", "max-age=86400")
                    self.end_headers()
                    self.wfile.write(cuerpo)
                except OSError:
                    self._json({"error": "favicon no encontrado"}, 404)
            elif ruta == "/api/asistencia":
                from centinela.core import db as db_centinela
                filas = db_centinela.obtener_estado_hoy()
                self._json({"fecha": datetime.now().strftime("%Y-%m-%d"),
                            "alumnos": filas})
            elif ruta == "/api/status":
                estado = self.provedor_estado() if self.provedor_estado else {}
                estado["telefonos"] = self._telefonos_para_api()
                self._json(estado)
            elif ruta == "/api/telefonos":
                self._json({"telefonos": self._telefonos_para_api()})
            elif ruta == "/api/telefono/estado":
                self._json(self._estado_telefono(params.get("tel", "")))
            elif ruta == "/api/telefono/heartbeat":
                # GET y POST: la pagina manda fetch sin method (GET)
                self._telefono_heartbeat(params)
            elif ruta == "/ws/telefono":
                self._ws_telefono(params)
            elif ruta == "/stream":
                self._stream_mjpeg()
            elif ruta == "/foto":
                jpg = self._jpeg()
                if jpg is None:
                    self._json({"error": "sin cámara activa"}, 503)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(jpg)))
                self.end_headers()
                self.wfile.write(jpg)
            elif ruta == "/telefono":
                self._html(_PAGINA_TELEFONO)
            elif ruta == "/":
                self._html(_PAGINA_VISOR)
            else:
                self._json({"error": "ruta desconocida"}, 404)
        except (ConnectionError, BrokenPipeError, ssl.SSLError):
            pass  # el navegador cortó la conexión (normal en /stream)
        except Exception:
            import traceback
            print(f"[WEB] error en {ruta}:\n{traceback.format_exc()}")
            try:
                self._json({"error": "error interno"}, 500)
            except Exception:
                pass

    def do_POST(self):
        ruta = self.path.split("?", 1)[0]
        params = self._params()
        try:
            if ruta == "/api/telefono/frame":
                self._telefono_frame(params)
            elif ruta == "/api/telefono/heartbeat":
                self._telefono_heartbeat(params)
            elif ruta == "/api/telefono/politica":
                tel_id = (params.get("tel") or "").strip()
                ok = self.servidor.fijar_estado(tel_id,
                                                params.get("estado", ""))
                self._json({"ok": ok}, 200 if ok else 400)
            else:
                self._json({"error": "ruta desconocida"}, 404)
        except (ConnectionError, BrokenPipeError, ssl.SSLError):
            pass
        except Exception:
            import traceback
            print(f"[WEB] error en POST {ruta}:\n{traceback.format_exc()}")
            try:
                self._json({"error": "error interno"}, 500)
            except Exception:
                pass

    # -- telefonos ------------------------------------------------------------
    def _params(self):
        from urllib.parse import parse_qs
        return {k: v[0] for k, v in
                parse_qs(self.path.split("?", 1)[1] if "?" in self.path
                         else "").items()}

    def _telefono_tocar_y_estado(self, tel_id, nombre, camara=""):
        """Ruta rápida de un solo lock y sin copias de dict para frames WS."""
        ahora = time.time()
        ip_cli = self.client_address[0]
        with self.servidor._lock_tel:
            reg = self.servidor.telefonos.get(tel_id)
            if reg is None:
                reg = {"nombre": "", "ip": ip_cli,
                       "visto": ahora, "camara": camara, "estado": "libre"}
                self.servidor.telefonos[tel_id] = reg
            reg["visto"] = ahora
            reg["ip"] = ip_cli
            if nombre:
                reg["nombre"] = nombre
            if camara:
                reg["camara"] = camara
            return reg["estado"]

    def _telefono_registrar(self, tel_id, nombre, camara=""):
        """Alta/actualizacion del telefono que se reporta (hilo servidor)."""
        self._telefono_tocar_y_estado(tel_id, nombre, camara)
        with self.servidor._lock_tel:
            return dict(self.servidor.telefonos[tel_id])

    def _estado_telefono(self, tel_id):
        with self.servidor._lock_tel:
            reg = self.servidor.telefonos.get(tel_id)
            if reg is None:
                return {"estado": "libre", "conocido": False}
            caido = (time.time() - reg["visto"]) > self.servidor.TIEMPO_TELEFONO_CAIDO
            return {"estado": reg["estado"], "conocido": True, "caido": caido,
                    "nombre": reg["nombre"]}

    def _telefonos_para_api(self):
        ahora = time.time()
        with self.servidor._lock_tel:
            return {tel: dict(reg,
                              online=(ahora - reg["visto"])
                              <= self.servidor.TIEMPO_TELEFONO_CAIDO)
                    for tel, reg in self.servidor.telefonos.items()}

    def _telefono_heartbeat(self, params):
        tel_id = (params.get("tel") or "").strip()
        if not tel_id:
            self._json({"error": "falta tel"}, 400)
            return
        self._telefono_registrar(tel_id, params.get("nombre", ""),
                                 params.get("camara", ""))
        self._json(self._estado_telefono(tel_id))

    def _telefono_frame(self, params):
        """Foto subida por el telefono -> pipeline IA (JSON rápido o JPEG legacy)."""
        tel_id = (params.get("tel") or "").strip()
        if not tel_id:
            self._json({"error": "falta tel"}, 400)
            return
        try:
            lon = int(self.headers.get("Content-Length", 0))
        except ValueError:
            lon = 0
        if lon <= 0 or lon > 8_000_000:  # tope 8 MB por frame
            self._json({"error": "cuerpo invalido"}, 400)
            return
        jpg = self.rfile.read(lon)
        nombre = params.get("nombre", "")
        camara = params.get("camara", "")
        self._telefono_registrar(tel_id, nombre, camara)

        if params.get("formato") == "json" and self.servidor.al_frame_telefono_rapido:
            res = self.servidor.al_frame_telefono_rapido(tel_id, nombre, jpg)
            if res is None:
                self._json({"error": "procesamiento no disponible"}, 503)
                return
            seq, dets = res
            est = self._estado_telefono(tel_id)
            self._json({"ok": True, "seq": seq, "dets": dets, "estado": est["estado"]})
            return

        cb = self.servidor.al_frame_telefono
        anotado = cb(tel_id, nombre, jpg) if cb else None
        if anotado is None:
            self._json({"error": "procesamiento no disponible"}, 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(anotado)))
        self.end_headers()
        self.wfile.write(anotado)

    def _ws_telefono(self, params):
        """Endpoint WebSocket RFC 6455 binario para streaming fluido desde celulares."""
        upgrade = str(self.headers.get("Upgrade", "") or "").lower()
        key = str(self.headers.get("Sec-WebSocket-Key", "") or "").strip()
        if "websocket" not in upgrade or not key:
            self._json({"error": "se requiere Upgrade: websocket"}, 400)
            return
        tel_id = (params.get("tel") or "").strip()
        if not tel_id:
            self._json({"error": "falta tel"}, 400)
            return

        nombre = params.get("nombre", "")
        camara = params.get("camara", "")

        accept = _ws_accept_key(key)
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")
        self.wfile.write(resp)
        self.wfile.flush()
        self.close_connection = True

        self._telefono_registrar(tel_id, nombre, camara)
        ws = _ConexionWS(self.connection, tel_id)
        self.servidor._registrar_ws(tel_id, ws)
        lector = _SocketBuffer(self.connection)
        try:
            est_ini = self._estado_telefono(tel_id)
            ws.enviar_inmediato_json({"tipo": "politica", "estado": est_ini["estado"]})
            cb_rapido = self.servidor.al_frame_telefono_rapido

            while not self.servidor._detenido and not ws.cerrado:
                ws.drenar_envios()
                if not lector.tiene_datos_listos(timeout=0.02):
                    continue
                op, data = _ws_leer_frame(lector)
                if op is None:
                    break
                if op == 0x8:
                    try:
                        ws.enviar_inmediato_frame(0x8, b"")
                    except Exception:
                        pass
                    break
                if op == 0x9:
                    ws.enviar_inmediato_frame(0xA, data)
                    continue
                if op == 0xA:
                    self._telefono_tocar_y_estado(tel_id, nombre, camara)
                    continue
                if op == 0x1:
                    try:
                        msg = json.loads(data.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(msg, dict):
                        continue
                    tipo = msg.get("tipo")
                    if tipo == "meta":
                        if "nombre" in msg:
                            nombre = str(msg.get("nombre", "") or "").strip()
                        if "camara" in msg:
                            camara = str(msg.get("camara", "") or "").strip()
                        self._telefono_tocar_y_estado(tel_id, nombre, camara)
                    elif tipo == "ping":
                        estado_act = self._telefono_tocar_y_estado(tel_id, nombre, camara)
                        ws.enviar_inmediato_json({"tipo": "pong", "estado": estado_act})
                elif op == 0x2:
                    estado_act = self._telefono_tocar_y_estado(tel_id, nombre, camara)
                    res = cb_rapido(tel_id, nombre, data) if cb_rapido else None
                    seq, dets = res if res is not None else (0, [])
                    ws.enviar_inmediato_json({
                        "tipo": "ack",
                        "seq": seq,
                        "dets": dets,
                        "estado": estado_act,
                    })
        except (ConnectionError, BrokenPipeError, ssl.SSLError, OSError):
            pass
        finally:
            ws.marcar_cerrado()
            self.servidor._desregistrar_ws(tel_id, ws)

    def _stream_mjpeg(self):
        """MJPEG multipart optimizado con caché compartida (hasta ~24 fps)."""
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        ultimo_id = None
        ultimo_envio = 0.0
        gris_cache = None
        while not self.servidor._detenido:
            ahora = time.monotonic()
            frame_id, jpg = self.servidor.obtener_jpeg_mjpeg()
            if jpg is None:
                if ahora - ultimo_envio < 0.8:
                    time.sleep(0.04)
                    continue
                if gris_cache is None:
                    import cv2
                    ok, buf = cv2.imencode(".jpg", np.full((270, 480, 3), 32, np.uint8))
                    if not ok:
                        return
                    gris_cache = buf.tobytes()
                jpg = gris_cache
                ultimo_id = None
            else:
                if frame_id == ultimo_id and (ahora - ultimo_envio) < 1.0:
                    time.sleep(0.015)
                    continue
                if (ahora - ultimo_envio) < 0.042:
                    time.sleep(0.01)
                    continue
                ultimo_id = frame_id

            try:
                cab = (
                    f"--frame\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpg)}\r\n\r\n"
                ).encode("ascii")
                self.wfile.write(cab)
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
                ultimo_envio = time.monotonic()
            except (ConnectionError, BrokenPipeError, ssl.SSLError, OSError):
                return


class ServidorWeb:
    """Servidor HTTPS + mDNS en un hilo daemon. start() / detener()."""

    TIEMPO_TELEFONO_CAIDO = 8.0  # s sin heartbeat -> aparece como caido

    def __init__(self, provedor_frame=None, provedor_estado=None,
                 al_frame_telefono=None, al_frame_telefono_rapido=None):
        self.provedor_frame = provedor_frame
        self.provedor_estado = provedor_estado or (lambda: {})
        # al_frame_telefono(tel_id, nombre, jpeg_bytes) -> jpeg anotado | None
        self.al_frame_telefono = al_frame_telefono
        # al_frame_telefono_rapido(tel_id, nombre, jpeg_bytes) -> (seq, dets) | None
        self.al_frame_telefono_rapido = al_frame_telefono_rapido
        # registro de telefonos conectados (protegido por _lock_tel):
        #   tel_id -> {"nombre", "ip", "visto": ts, "camara", "estado"}
        self.telefonos = {}
        self._lock_tel = threading.Lock()
        self._ws_clientes = {}
        self._lock_ws = threading.Lock()
        self._lock_mjpeg = threading.Lock()
        self._mjpeg_frame_id = None
        self._mjpeg_jpg = None
        self._server = None
        self._hilo = None
        self._zeroconf = None
        self._info = None
        self._lock = threading.Lock()
        self._mdns_lock = threading.Lock()
        self._detenido = False

    def _registrar_ws(self, tel_id, ws):
        with self._lock_ws:
            self._ws_clientes.setdefault(tel_id, []).append(ws)

    def _desregistrar_ws(self, tel_id, ws):
        with self._lock_ws:
            lista = self._ws_clientes.get(tel_id)
            if lista and ws in lista:
                lista.remove(ws)
                if not lista:
                    self._ws_clientes.pop(tel_id, None)

    def obtener_jpeg_mjpeg(self):
        """Codifica y cachea el último frame compartido entre clientes /stream."""
        frame = self.provedor_frame() if self.provedor_frame else None
        if frame is None:
            return None, None
        fid = id(frame)
        with self._lock_mjpeg:
            if fid == self._mjpeg_frame_id and self._mjpeg_jpg is not None:
                return fid, self._mjpeg_jpg
        import cv2
        ok, buf = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), _CALIDAD_JPEG])
        if not ok:
            return None, None
        jpg = buf.tobytes()
        with self._lock_mjpeg:
            self._mjpeg_frame_id = fid
            self._mjpeg_jpg = jpg
        return fid, jpg

    # -- ciclo de vida -------------------------------------------------------
    def start(self):
        with self._lock:
            if self._server is not None:
                return
            self._detenido = False
            generar_certificado()
            # staticmethod: si se asignara la funcion pelada como atributo de
            # clase, el handler la llamaria con self (metodo enlazado)
            _Handler.provedor_frame = (
                staticmethod(self.provedor_frame) if self.provedor_frame else None)
            _Handler.provedor_estado = (
                staticmethod(self.provedor_estado) if self.provedor_estado else None)
            _Handler.servidor = self
            server = ThreadingHTTPServer(("0.0.0.0", PUERTO_HTTPS), _Handler)
            server.daemon_threads = True
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(_RUTA_CRT, _RUTA_KEY)
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            self._server = server
            self._hilo = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                name="servidor-web", daemon=True)
            self._hilo.start()
            # El registro mDNS sondea la red (~3 s); se lanza en segundo plano
            # para no bloquear el arranque de la ventana.
            threading.Thread(
                target=self._anunciar_mdns,
                name="mdns-anuncio", daemon=True,
            ).start()
            print(f"[WEB] https://{NOMBRE_MDNS}.local:{PUERTO_HTTPS} "
                  f"(o https://{ip_local()}:{PUERTO_HTTPS})")

    def detener(self):
        with self._lock:
            self._detenido = True
            server, hilo = self._server, self._hilo
            self._server, self._hilo = None, None
        with self._lock_ws:
            for lista in self._ws_clientes.values():
                for ws in lista:
                    ws.marcar_cerrado()
            self._ws_clientes.clear()
        if server is not None:
            try:
                server.shutdown()   # sirve_forever sale en <= poll_interval
                server.server_close()
            except Exception:
                pass
        if hilo is not None:
            hilo.join(timeout=2)
        self._retirar_mdns()
        print("[WEB] servidor detenido")

    # -- mDNS ----------------------------------------------------------------
    def _anunciar_mdns(self):
        """Registra centinela.local + _http._tcp en la red local."""
        try:
            from zeroconf import NonUniqueNameException, ServiceInfo, Zeroconf
        except ImportError:
            print("[WEB] zeroconf no instalado: sin centinela.local "
                  "(use la IP o instale zeroconf)")
            return
        try:
            zc = Zeroconf()
            import ipaddress
            addr = ipaddress.ip_address(ip_local())
            info = ServiceInfo(
                "_http._tcp.local.",
                f"{NOMBRE_MDNS}._http._tcp.local.",
                port=PUERTO_HTTPS,
                properties={"path": "/", "https": "true"},
                server=f"{NOMBRE_MDNS}.local.",
                addresses=[addr.packed],
            )
            with self._mdns_lock:
                if self._detenido:
                    zc.close()
                    return
                self._zeroconf = zc
                self._info = info
            try:
                zc.register_service(info)
                print("[MDNS] anunciado centinela.local")
            except NonUniqueNameException:
                print("[MDNS] centinela.local ya anunciado por otro equipo; "
                      "la IP directa sigue funcionando")
        except Exception as e:
            print(f"[MDNS] no se pudo anunciar: {e}")
            self._retirar_mdns()

    def _retirar_mdns(self):
        with self._mdns_lock:
            zc, info = self._zeroconf, self._info
            self._zeroconf, self._info = None, None
        if zc is not None:
            try:
                if info is not None:
                    zc.unregister_service(info)
            except Exception:
                pass
            try:
                zc.close()
            except Exception:
                pass

    # -- politicas de telefonos (Fase 1c) ------------------------------------
    ESTADOS_TELEFONO = ("libre", "controles_ocultos", "bloqueado")

    def fijar_estado(self, tel_id, estado):
        """Fija la politica de un telefono desde el panel del PC y notifica por WS."""
        estado = str(estado).strip()
        if estado not in self.ESTADOS_TELEFONO:
            return False
        with self._lock_tel:
            reg = self.telefonos.get(tel_id)
            if reg is None:
                return False
            reg["estado"] = estado
        with self._lock_ws:
            clientes = list(self._ws_clientes.get(tel_id, ()))
        for ws in clientes:
            ws.encolar_json({"tipo": "politica", "estado": estado})
        return True

    def listar_telefonos(self):
        """Copia del registro para la GUI (con marca online/caido)."""
        ahora = time.time()
        with self._lock_tel:
            return {tel: dict(reg,
                              online=(ahora - reg["visto"])
                              <= self.TIEMPO_TELEFONO_CAIDO)
                    for tel, reg in self.telefonos.items()}


# ---------------------------------------------------------------------------
# Visor web (página única, sin dependencias externas)
# ---------------------------------------------------------------------------

_PAGINA_VISOR = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centinela — Visor</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101014; color: #e4e4e7;
         font-family: system-ui, "Segoe UI", sans-serif; }
  header { padding: 14px 20px; background: #16161c; border-bottom: 1px solid #26262e;
           display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; }
  .punto { font-size: 12px; color: #8b97a8; }
  main { max-width: 1100px; margin: 0 auto; padding: 16px; }
  img.stream { width: 100%; border-radius: 12px; background: #0a0a0e;
               border: 1px solid #26262e; }
  h2 { font-size: 15px; margin: 22px 0 8px; color: #a5a5a5; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { padding: 8px 10px; border-bottom: 1px solid #26262e; text-align: left; }
  th { color: #8b97a8; font-weight: 600; font-size: 12px; }
  .b { padding: 2px 10px; border-radius: 999px; font-size: 12px; }
  .Presente  { background: #102f26; color: #34d399; }
  .Tardanza  { background: #332711; color: #fbbf24; }
  .Ausente   { background: #341417; color: #f87171; }
  .otro      { background: #1d1d24; color: #8b97a8; }
  .nodos span { margin-right: 14px; font-size: 13px; }
  .on  { color: #34d399; } .off { color: #f87171; }
  .ip { font-size: 11px; color: #8b97a8; margin-top: 10px;
        padding: 8px 12px; background: #16161c; border: 1px solid #26262e;
        border-radius: 10px; }
  .ip code { color: #e4e4e7; user-select: all; }
</style></head><body>
<header>
  <h1>Centinela — Asistencia de aula</h1>
  <span class="punto" id="reloj"></span>
</header>
<main>
  <img class="stream" src="/stream" alt="Video anotado en vivo">
  <h2>Nodos</h2>
  <div class="nodos" id="nodos">—</div>
  <h2>Asistencia de hoy</h2>
  <table><thead><tr>
    <th>Alumno</th><th>Curso</th><th>Sección</th>
    <th>Estado</th><th>Entrada</th><th>Salida</th>
  </tr></thead><tbody id="filas"><tr><td colspan="6">Cargando…</td></tr></tbody></table>
</main>
<div class="ip" style="position:fixed; bottom:10px; left:10px; max-width:calc(100% - 20px);">
  Si <b>centinela.local</b> no resuelve en tu red, entra directo por IP:
  <code id="ip-url">cargando…</code>
</div>
<script>
fetch("/api/ip").then(r => r.json()).then(d => {
  document.getElementById("ip-url").textContent = d.url;
}).catch(() => {
  document.getElementById("ip-url").textContent = location.origin;
});
</script>
<script>
const f = s => (s || "").replace("T", " ").slice(11, 16);
async function refrescar() {
  document.getElementById("reloj").textContent =
    new Date().toLocaleString("es-PE");
  try {
    const a = await (await fetch("/api/asistencia")).json();
    document.getElementById("filas").innerHTML = a.alumnos.map(x => {
      let estado = "Sin registro";
      if (x.entrada_abierta) estado = "Presente";
      else if (x.marcas > 0) estado = "Ausente";   // marcó y cerró
      const cls = ["Presente","Tardanza","Ausente"].includes(estado) ? estado : "otro";
      return `<tr><td>${x.nombre}</td><td>${x.curso ?? ""}</td>
        <td>${x.seccion ?? ""}</td>
        <td><span class="b ${cls}">${estado}</span></td>
        <td>${f(x.entrada_abierta)}</td><td>${f(x.ultima_salida)}</td></tr>`;
    }).join("");
  } catch (e) { /* servidor ocupado: reintenta en el próximo ciclo */ }
  try {
    const s = await (await fetch("/api/status")).json();
    const nodos = s.nodos || {};
    document.getElementById("nodos").innerHTML = Object.keys(nodos).length
      ? Object.entries(nodos).map(([id, n]) =>
          `<span class="${n.online ? "on" : "off"}">● ${id} — ${n.online ? "ONLINE" : "OFFLINE"}</span>`
        ).join("")
      : `<span class="punto">(sin nodos gestionados aún)</span>`;
  } catch (e) {}
}
setInterval(refrescar, 5000); refrescar();
</script></body></html>
"""

_PAGINA_TELEFONO = """<!DOCTYPE html>
<html lang="es"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, user-scalable=no">
<title>Centinela — Cámara</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; background: #101014; color: #e4e4e7;
         font-family: system-ui, "Segoe UI", sans-serif;
         display: flex; flex-direction: column; height: 100dvh; overflow: hidden; }
  #vista { position: relative; flex: 1; min-height: 0; display: flex;
           align-items: center; justify-content: center; background: #0a0a0e; overflow: hidden; }
  #video { width: 100%; height: 100%; object-fit: contain; display: block; }
  #video.espejo { transform: scaleX(-1); }
  #hud { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  #pill-info { position: absolute; top: 12px; left: 12px; padding: 5px 11px;
               border-radius: 999px; background: rgba(13, 18, 26, 0.78);
               border: 1px solid rgba(255, 255, 255, 0.12);
               font-size: 11px; font-weight: 600; color: #cbd5e1;
               display: flex; align-items: center; gap: 6px; pointer-events: none;
               backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
  #pill-dot { width: 7px; height: 7px; border-radius: 50%; background: #94a3b8; }
  #pill-dot.ws { background: #2dd4bf; }
  #pill-dot.http { background: #fbbf24; }
  #controles { padding: 10px 12px; display: flex; gap: 8px;
               align-items: center; flex-wrap: wrap; background: #12151b;
               border-top: 1px solid rgba(255, 255, 255, 0.08); }
  button { padding: 10px 14px; border-radius: 10px; border: 1px solid #3f3f46;
           background: #27272a; color: #e4e4e7; font-size: 13px; font-weight: 600; }
  button.primario { background: #0d9488; border-color: #14b8a6; color: #ffffff; }
  input, select { padding: 9px 12px; border-radius: 10px; border: 1px solid #3f3f46;
                  background: #18181b; color: #e4e4e7; font-size: 13px; }
  input { flex: 1; min-width: 120px; }
  #estado { font-size: 12px; color: #94a3b8; width: 100%; }
  #ip { font-size: 11px; color: #8b97a8; width: 100%; }
  #ip code { color: #e4e4e7; user-select: all; }
  .oculto { display: none !important; }
  body.bloqueado #controles { display: none; }
  body.bloqueado #pill-info { opacity: 0.45; }
  body.bloqueado { cursor: none; }
</style></head><body>
<div id="vista">
  <video id="video" autoplay playsinline muted></video>
  <canvas id="hud"></canvas>
  <div id="pill-info"><span id="pill-dot"></span><span id="pill-txt">Iniciando…</span></div>
</div>
<div id="controles">
  <input id="nombre" placeholder="Tu nombre (opcional)" autocomplete="off">
  <select id="resolucion">
    <option value="480">480 px</option>
    <option value="720" selected>720 px</option>
    <option value="960">960 px</option>
    <option value="1280">1280 px</option>
    <option value="0">Original</option>
  </select>
  <button id="btn_cam" class="primario">📷 Cambiar cámara</button>
  <button id="btn_full">Pantalla completa</button>
  <div id="estado"></div>
  <div id="ip">Si centinela.local no resuelve: <code id="ip-url">cargando…</code></div>
</div>
<script>
fetch("/api/ip").then(r => r.json()).then(d => {
  document.getElementById("ip-url").textContent = d.url + "telefono";
}).catch(() => {
  document.getElementById("ip-url").textContent = "https://" + location.hostname + ":8443/telefono";
});

const $ = id => document.getElementById(id);
const video = $("video"), hud = $("hud"), ctxHud = hud.getContext("2d");
let stream = null, camara = "environment";
let genStream = 0, rvfcActivo = false, videoFrameSeq = 0, ultimoVideoSeq = -1;
const tieneRVFC = typeof HTMLVideoElement !== "undefined" &&
  "requestVideoFrameCallback" in HTMLVideoElement.prototype;
let ws = null, wsAbierto = false, usarFallbackHttp = false, fallosWsSeguidos = 0;
let enVuelo = false, tEnvio = 0, framesOk = 0, tFps = performance.now(), fpsActual = 0, rttMs = 0;
let capturando = false, tUltSubida = 0, blobListo = null;

let tel = localStorage.getItem("centinela_tel");
if (!tel) {
  tel = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));
  localStorage.setItem("centinela_tel", tel);
}
$("nombre").value = localStorage.getItem("centinela_tel_nombre") || "";
$("nombre").addEventListener("change", () => {
  localStorage.setItem("centinela_tel_nombre", $("nombre").value.trim());
  enviarMetaWS();
});

const qParams = () => "tel=" + encodeURIComponent(tel) +
  "&nombre=" + encodeURIComponent($("nombre").value.trim()) +
  "&camara=" + encodeURIComponent(camara);
const q = extra => "/api/telefono/" + extra + "?" + qParams();

function aplicar_estado(s) {
  document.body.classList.toggle("bloqueado", s === "bloqueado");
  $("controles").classList.toggle("oculto",
    s === "bloqueado" || s === "controles_ocultos");
  if (s === "bloqueado" && !document.fullscreenElement) {
    document.documentElement.requestFullscreen?.().catch(() => {});
  }
}

function registrarRVFC(gen) {
  if (!tieneRVFC || gen !== genStream || !stream) return;
  video.requestVideoFrameCallback(() => {
    if (gen !== genStream || !stream) return;
    rvfcActivo = true;
    videoFrameSeq++;
    intentarCapturar(performance.now());
    registrarRVFC(gen);
  });
}

async function encender(que = camara) {
  genStream++;
  rvfcActivo = false;
  blobListo = null;
  videoFrameSeq = 0;
  ultimoVideoSeq = -1;
  if (stream) stream.getTracks().forEach(t => t.stop());
  video.classList.toggle("espejo", que === "user");
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $("estado").textContent =
      "Cámara bloqueada: el navegador exige HTTPS con certificado de confianza " +
      "(" + location.origin + "/telefono). Vuelve a aceptar el aviso de seguridad.";
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: que,
        width: { ideal: 1280 },
        height: { ideal: 720 },
        frameRate: { ideal: 30, max: 60 },
      },
      audio: false,
    });
    video.srcObject = stream;
    await video.play();
    $("estado").textContent = "Cámara activa · conectando flujo rápido…";
    enviarMetaWS();
    registrarRVFC(genStream);
    intentarCapturar(performance.now());
  } catch (e) {
    $("estado").textContent = "Sin permiso de cámara: " + e.message;
  }
}

$("btn_cam").onclick = () => {
  camara = camara === "environment" ? "user" : "environment";
  $("btn_cam").textContent = (camara === "environment" ? "📷 Trasera" : "📷 Frontal");
  encender(camara);
};

$("btn_full").onclick = () => {
  if (document.fullscreenElement) {
    document.exitFullscreen().catch(() => {});
  } else {
    document.documentElement.requestFullscreen().catch(() => {});
  }
};

// --- Transporte WebSocket binario + respaldo HTTP/1.1 keep-alive ---

function enviarMetaWS() {
  if (ws && wsAbierto && ws.readyState === 1) {
    try {
      ws.send(JSON.stringify({
        tipo: "meta",
        nombre: $("nombre").value.trim(),
        camara: camara,
      }));
    } catch (e) {}
  }
}

function actualizarPill(modo) {
  const dot = $("pill-dot"), txt = $("pill-txt");
  dot.className = modo === "ws" ? "ws" : (modo === "http" ? "http" : "");
  if (modo === "ws") {
    const fpsTxt = fpsActual > 0 ? fpsActual + " fps · " + Math.round(rttMs) + " ms" : "WS listo";
    txt.textContent = fpsTxt;
    $("estado").textContent = "Conectado (WS) · " + fpsTxt;
  } else if (modo === "http") {
    const fpsTxt = fpsActual > 0 ? fpsActual + " fps · " + Math.round(rttMs) + " ms" : "HTTP";
    txt.textContent = "HTTP · " + fpsTxt;
    $("estado").textContent = "Conectado (HTTP respaldo) · " + fpsTxt;
  } else {
    txt.textContent = "Reconectando…";
  }
}

function registrarAck(rtt) {
  rttMs = rttMs > 0 ? rttMs * 0.7 + rtt * 0.3 : rtt;
  framesOk++;
  const ahora = performance.now();
  if (ahora - tFps >= 1000) {
    fpsActual = Math.round((framesOk * 1000) / (ahora - tFps));
    framesOk = 0;
    tFps = ahora;
  }
  actualizarPill(wsAbierto ? "ws" : "http");
}

function conectarWS() {
  if (ws && (ws.readyState === 0 || ws.readyState === 1)) return;
  const proto = location.protocol === "https:" ? "wss://" : "ws://";
  const url = proto + location.host + "/ws/telefono?" + qParams();
  let sock;
  try {
    sock = new WebSocket(url);
  } catch (e) {
    usarFallbackHttp = true;
    actualizarPill("http");
    return;
  }
  ws = sock;
  sock.binaryType = "arraybuffer";
  sock.onopen = () => {
    if (ws !== sock) return;
    wsAbierto = true;
    usarFallbackHttp = false;
    fallosWsSeguidos = 0;
    enVuelo = false;
    blobListo = null;
    actualizarPill("ws");
    intentarCapturar(performance.now());
  };
  sock.onmessage = ev => {
    if (ws !== sock || typeof ev.data !== "string") return;
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.estado) aplicar_estado(msg.estado);
    if (msg.tipo === "ack") {
      enVuelo = false;
      registrarAck(Math.max(1, performance.now() - tEnvio));
      actualizarObjetivosHUD(msg.dets || []);
      despacharPendiente();
    }
  };
  sock.onclose = () => {
    if (ws !== sock) return;
    wsAbierto = false;
    enVuelo = false;
    blobListo = null;
    fallosWsSeguidos++;
    if (fallosWsSeguidos >= 2) usarFallbackHttp = true;
    actualizarPill(usarFallbackHttp ? "http" : "reconectando");
    setTimeout(conectarWS, 1200);
  };
  sock.onerror = () => {
    try { sock.close(); } catch (e) {}
  };
}

// Keepalive ligero solo si WS está abierto sin subida activa
setInterval(() => {
  if (ws && wsAbierto && ws.readyState === 1) {
    if (!stream) {
      try { ws.send(JSON.stringify({ tipo: "ping" })); } catch (e) {}
    }
  } else {
    fetch("/api/telefono/estado?tel=" + encodeURIComponent(tel))
      .then(r => r.json())
      .then(r => { if (r.conocido) aplicar_estado(r.estado); })
      .catch(() => {});
  }
}, 2000);

// --- HUD Canvas 60 FPS en el celular (dibuja sobre el <video> nativo) ---
const COL_IDENT = [45, 212, 191];    // #2dd4bf teal
const COL_CONF  = [52, 211, 153];    // #34d399 esmeralda
const COL_DESC  = [248, 113, 113];   // #f87171 coral
const tracksHUD = [];

function deCaja(b) {
  return [(b[0] + b[2]) * 0.5, (b[1] + b[3]) * 0.5,
          Math.max(0.02, b[2] - b[0]), Math.max(0.02, b[3] - b[1])];
}

function actualizarObjetivosHUD(dets) {
  const ahora = performance.now();
  const usados = new Set();
  for (const det of dets) {
    const bRaw = det.box || [0, 0, 0, 0];
    const bNorm = camara === "user" ? [1 - bRaw[2], bRaw[1], 1 - bRaw[0], bRaw[3]] : bRaw;
    const obj = deCaja(bNorm);
    let idx = -1;
    if (det.ident && det.id) {
      idx = tracksHUD.findIndex((t, i) => !usados.has(i) && t.id === det.id);
    }
    if (idx < 0) {
      let bestD = 0.24;
      tracksHUD.forEach((t, i) => {
        if (usados.has(i)) return;
        const d = Math.hypot(obj[0] - t.cx, obj[1] - t.cy);
        if (d < bestD) { bestD = d; idx = i; }
      });
    }
    if (idx >= 0) {
      const t = tracksHUD[idx];
      t.obj = obj; t.id = det.id || ""; t.nombre = det.nombre || "";
      t.ident = !!det.ident; t.conf = !!det.conf;
      t.progObj = det.prog; t.sim = det.sim || 0;
      t.activo = true; t.tVisto = ahora;
      usados.add(idx);
    } else {
      const colIni = det.ident ? (det.conf ? COL_CONF : COL_IDENT) : COL_DESC;
      tracksHUD.push({
        cx: obj[0], cy: obj[1], bw: obj[2], bh: obj[3], obj: obj,
        id: det.id || "", nombre: det.nombre || "",
        ident: !!det.ident, conf: !!det.conf,
        progObj: det.prog, progVis: det.prog || 0, sim: det.sim || 0,
        col: [colIni[0], colIni[1], colIni[2]],
        opacidad: 0, activo: true, tVisto: ahora,
      });
      usados.add(tracksHUD.length - 1);
    }
  }
  tracksHUD.forEach((t, i) => { if (!usados.has(i)) t.activo = false; });
}

function roundRectPath(c, x, y, w, h, r) {
  const rr = Math.max(0, Math.min(r, w * 0.5, h * 0.5));
  c.beginPath();
  c.moveTo(x + rr, y);
  c.lineTo(x + w - rr, y);
  c.quadraticCurveTo(x + w, y, x + w, y + rr);
  c.lineTo(x + w, y + h - rr);
  c.quadraticCurveTo(x + w, y + h, x + w - rr, y + h);
  c.lineTo(x + rr, y + h);
  c.quadraticCurveTo(x, y + h, x, y + h - rr);
  c.lineTo(x, y + rr);
  c.quadraticCurveTo(x, y, x + rr, y);
  c.closePath();
}

function dibujarEsquinasHUD(c, x1, y1, x2, y2, rgbStr, k) {
  const bw = x2 - x1, bh = y2 - y1;
  if (bw < 16 || bh < 16) return;
  let L = Math.max(12, Math.min(bw, bh) * 0.24);
  L = Math.min(L, bw * 0.5 - 2, bh * 0.5 - 2);
  const r = Math.min(Math.max(6, Math.min(bw, bh) * 0.13), Math.max(3, L - 2));

  const trace = () => {
    c.beginPath();
    c.moveTo(x1, y1 + L); c.lineTo(x1, y1 + r); c.quadraticCurveTo(x1, y1, x1 + r, y1); c.lineTo(x1 + L, y1);
    c.moveTo(x2 - L, y1); c.lineTo(x2 - r, y1); c.quadraticCurveTo(x2, y1, x2, y1 + r); c.lineTo(x2, y1 + L);
    c.moveTo(x2, y2 - L); c.lineTo(x2, y2 - r); c.quadraticCurveTo(x2, y2, x2 - r, y2); c.lineTo(x2 - L, y2);
    c.moveTo(x1 + L, y2); c.lineTo(x1 + r, y2); c.quadraticCurveTo(x1, y2, x1, y2 - r); c.lineTo(x1, y2 - L);
    c.stroke();
  };

  c.save();
  c.lineCap = "round";
  c.lineJoin = "round";
  c.shadowColor = "rgba(6, 10, 16, 0.55)";
  c.shadowBlur = 4 * k;
  c.shadowOffsetY = 1;
  c.strokeStyle = "rgba(" + rgbStr + ", 0.26)";
  c.lineWidth = Math.max(4, 5.5 * k);
  trace();
  c.shadowBlur = 0; c.shadowOffsetY = 0;
  c.strokeStyle = "rgb(" + rgbStr + ")";
  c.lineWidth = Math.max(2.2, 2.7 * k);
  trace();
  c.restore();
}

let tUltHUD = performance.now(), hudLimpio = true;
function dibujarHUD(ahoraMs) {
  const dt = Math.max(0.004, Math.min(0.1, (ahoraMs - tUltHUD) * 0.001));
  tUltHUD = ahoraMs;

  const cw = hud.clientWidth, ch = hud.clientHeight;
  const dpr = Math.min(2.5, window.devicePixelRatio || 1);
  if (cw > 0 && ch > 0) {
    const tw = Math.round(cw * dpr), th = Math.round(ch * dpr);
    if (hud.width !== tw || hud.height !== th) {
      hud.width = tw; hud.height = th;
      hudLimpio = true;
    }
    if (tracksHUD.length === 0) {
      if (!hudLimpio) {
        ctxHud.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctxHud.clearRect(0, 0, cw, ch);
        hudLimpio = true;
      }
      return;
    }
    hudLimpio = false;
    ctxHud.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctxHud.clearRect(0, 0, cw, ch);

    const vw = video.videoWidth || 0, vh = video.videoHeight || 0;
    if (vw > 0 && vh > 0 && tracksHUD.length > 0) {
      const esc = Math.min(cw / vw, ch / vh);
      const rw = vw * esc, rh = vh * esc;
      const rx0 = (cw - rw) * 0.5, ry0 = (ch - rh) * 0.5;
      const k = Math.max(0.75, Math.min(1.6, rh / 480));
      const kPos = 1 - Math.exp(-18 * dt);
      const kCol = 1 - Math.exp(-10 * dt);

      for (let i = tracksHUD.length - 1; i >= 0; i--) {
        const t = tracksHUD[i];
        t.cx += (t.obj[0] - t.cx) * kPos;
        t.cy += (t.obj[1] - t.cy) * kPos;
        t.bw += (t.obj[2] - t.bw) * kPos;
        t.bh += (t.obj[3] - t.bh) * kPos;
        if (t.activo) t.opacidad = Math.min(1, t.opacidad + dt / 0.14);
        else if (ahoraMs - t.tVisto > 250) t.opacidad = Math.max(0, t.opacidad - dt / 0.2);
        if (!t.activo && t.opacidad <= 0.015) { tracksHUD.splice(i, 1); continue; }

        const cObj = t.ident ? (t.conf ? COL_CONF : COL_IDENT) : COL_DESC;
        for (let c = 0; c < 3; c++) t.col[c] += (cObj[c] - t.col[c]) * kCol;
        const rgb = Math.round(t.col[0]) + "," + Math.round(t.col[1]) + "," + Math.round(t.col[2]);

        if (t.ident && t.progObj != null) {
          const objP = t.conf ? 1 : t.progObj;
          t.progVis += (objP - t.progVis) * (1 - Math.exp(-12 * dt));
        }

        const x1 = rx0 + (t.cx - t.bw * 0.5) * rw - 8 * k;
        const y1 = ry0 + (t.cy - t.bh * 0.5) * rh - 8 * k;
        const x2 = rx0 + (t.cx + t.bw * 0.5) * rw + 8 * k;
        const y2 = ry0 + (t.cy + t.bh * 0.5) * rh + 8 * k;

        ctxHud.save();
        ctxHud.globalAlpha = t.opacidad;
        dibujarEsquinasHUD(ctxHud, x1, y1, x2, y2, rgb, k);

        // Haz sutil de escaneo
        if (t.ident && !t.conf && t.progObj != null) {
          const fase = 0.5 - 0.5 * Math.cos(ahoraMs * 0.0028);
          const yb = y1 + 12 * k + (y2 - y1 - 24 * k) * fase;
          const bx1 = x1 + 8 * k, bx2 = x2 - 8 * k;
          if (bx2 > bx1) {
            const gV = ctxHud.createLinearGradient(0, yb - 8 * k, 0, yb + 8 * k);
            gV.addColorStop(0, "rgba(" + rgb + ", 0)");
            gV.addColorStop(0.5, "rgba(" + rgb + ", 0.55)");
            gV.addColorStop(1, "rgba(" + rgb + ", 0)");
            ctxHud.fillStyle = gV;
            ctxHud.fillRect(bx1, yb - 8 * k, bx2 - bx1, 16 * k);
          }
        }

        // Píldora glassmórfica flotante (texto siempre legible)
        const titulo = t.ident ? (t.nombre || "Alumno") : "Rostro no registrado";
        const detTxt = t.ident ? (t.conf ? "Confirmado" : (t.progObj != null ? Math.round(t.progVis * 100) + "%" : Math.round(t.sim * 100) + "%")) : "";
        const tieneBar = t.ident && !t.conf && t.progObj != null;
        const fTit = "600 " + Math.round(13 * k) + "px system-ui, Segoe UI, sans-serif";
        const fDet = "600 " + Math.round(11.5 * k) + "px system-ui, Segoe UI, sans-serif";

        ctxHud.font = fTit;
        const tw = ctxHud.measureText(titulo).width;
        ctxHud.font = fDet;
        const dw = detTxt ? ctxHud.measureText(detTxt).width : 0;

        const padX = 11 * k, padY = 7 * k, indR = (t.conf ? 6 : 4.5) * k;
        const gapDot = 7 * k, gapDet = detTxt ? 10 * k : 0;
        const hBar = tieneBar ? 4.5 * k : 0, gapBar = tieneBar ? 6 * k : 0;
        const rowH = Math.max(16 * k, indR * 2);
        const pw = Math.min(cw - 16, padX * 2 + indR * 2 + gapDot + tw + gapDet + dw);
        const ph = padY * 2 + rowH + gapBar + hBar;
        const px = Math.min(Math.max(8, (x1 + x2) * 0.5 - pw * 0.5), Math.max(8, cw - pw - 8));
        let py = y1 - ph - 9 * k;
        if (py < 8) py = Math.min(ch - ph - 8, y2 + 9 * k);

        roundRectPath(ctxHud, px, py, pw, ph, Math.min(10 * k, ph * 0.5));
        ctxHud.fillStyle = "rgba(13, 18, 26, 0.88)";
        ctxHud.fill();
        ctxHud.strokeStyle = "rgba(" + rgb + ", 0.68)";
        ctxHud.lineWidth = 1.2 * k;
        ctxHud.stroke();

        // Indicador izquierdo
        const icx = px + padX + indR, icy = py + padY + rowH * 0.5;
        if (t.conf) {
          ctxHud.beginPath(); ctxHud.arc(icx, icy, indR, 0, Math.PI * 2);
          ctxHud.fillStyle = "rgb(" + rgb + ")"; ctxHud.fill();
          ctxHud.beginPath();
          ctxHud.moveTo(icx - indR * 0.42, icy + indR * 0.04);
          ctxHud.lineTo(icx - indR * 0.08, icy + indR * 0.36);
          ctxHud.lineTo(icx + indR * 0.44, icy - indR * 0.32);
          ctxHud.strokeStyle = "#0a1410"; ctxHud.lineWidth = Math.max(1.6, 1.8 * k);
          ctxHud.lineCap = "round"; ctxHud.lineJoin = "round"; ctxHud.stroke();
        } else {
          const pFrac = 0.5 + 0.5 * Math.sin(ahoraMs * 0.005);
          ctxHud.beginPath(); ctxHud.arc(icx, icy, indR + (1.5 + 1.5 * pFrac) * k, 0, Math.PI * 2);
          ctxHud.fillStyle = "rgba(" + rgb + ", " + (0.22 + 0.28 * pFrac).toFixed(2) + ")"; ctxHud.fill();
          ctxHud.beginPath(); ctxHud.arc(icx, icy, indR * 0.78, 0, Math.PI * 2);
          ctxHud.fillStyle = "rgb(" + rgb + ")"; ctxHud.fill();
        }

        ctxHud.textBaseline = "middle";
        ctxHud.font = fTit;
        ctxHud.fillStyle = "#f8fafc";
        ctxHud.fillText(titulo, px + padX + indR * 2 + gapDot, icy);
        if (detTxt) {
          ctxHud.font = fDet;
          ctxHud.fillStyle = "rgb(" + rgb + ")";
          ctxHud.fillText(detTxt, px + pw - padX - dw, icy);
        }
        if (tieneBar) {
          const bx = px + padX, by = py + padY + rowH + gapBar, bwBar = pw - padX * 2;
          roundRectPath(ctxHud, bx, by, bwBar, hBar, hBar * 0.5);
          ctxHud.fillStyle = "rgba(48, 59, 74, 0.92)";
          ctxHud.fill();
          const fw = Math.max(hBar, bwBar * Math.max(0, Math.min(1, t.progVis)));
          roundRectPath(ctxHud, bx, by, Math.min(bwBar, fw), hBar, hBar * 0.5);
          ctxHud.fillStyle = "rgb(" + rgb + ")";
          ctxHud.fill();
        }
        ctxHud.restore();
      }
    }
  }
}

// --- Bucle continuo con rVFC + pre-codificación OffscreenCanvas 1-deep (~28-30 fps) ---
let usarOffscreen = Boolean(
  typeof OffscreenCanvas !== "undefined" &&
  typeof OffscreenCanvas.prototype.convertToBlob === "function"
);
const offscreenCap = usarOffscreen ? new OffscreenCanvas(1, 1) : null;
const ctxOff = offscreenCap ? offscreenCap.getContext("2d", { alpha: false }) : null;
if (!ctxOff) usarOffscreen = false;
const lienzo = document.createElement("canvas");
const contexto = lienzo.getContext("2d", { alpha: false });
const CALIDAD_UPLOAD = 0.66;
const MAX_EDAD_BLOB_MS = 90;

function wsListo() {
  return ws && wsAbierto && ws.readyState === 1 && ws.bufferedAmount < 131072;
}

async function codificarFrame(tw, th) {
  if (usarOffscreen && offscreenCap && ctxOff) {
    try {
      if (offscreenCap.width !== tw || offscreenCap.height !== th) {
        offscreenCap.width = tw;
        offscreenCap.height = th;
      }
      ctxOff.drawImage(video, 0, 0, tw, th);
      return await offscreenCap.convertToBlob({ type: "image/jpeg", quality: CALIDAD_UPLOAD });
    } catch (e) {
      usarOffscreen = false;
    }
  }
  if (lienzo.width !== tw || lienzo.height !== th) {
    lienzo.width = tw;
    lienzo.height = th;
  }
  contexto.drawImage(video, 0, 0, tw, th);
  return await new Promise(resolve => lienzo.toBlob(resolve, "image/jpeg", CALIDAD_UPLOAD));
}

function enviarBlobWS(blob) {
  if (!blob || enVuelo || !wsListo()) return false;
  try {
    enVuelo = true;
    tEnvio = performance.now();
    ws.send(blob);
    return true;
  } catch (e) {
    enVuelo = false;
    return false;
  }
}

function despacharPendiente() {
  const ahora = performance.now();
  if (blobListo) {
    const item = blobListo;
    blobListo = null;
    if ((ahora - item.ts) <= MAX_EDAD_BLOB_MS && enviarBlobWS(item.blob)) {
      return;
    }
  }
  intentarCapturar(ahora);
}

async function intentarCapturar(ahoraMs) {
  if (enVuelo && (ahoraMs - tEnvio) > 2500) enVuelo = false;
  if (!stream || capturando) return;
  const minMs = rvfcActivo ? 24 : 28;
  if (rvfcActivo && videoFrameSeq === ultimoVideoSeq) return;
  if ((ahoraMs - tUltSubida) < minMs) return;

  const w = video.videoWidth, h = video.videoHeight;
  if (!w || !h) return;

  const listoWS = wsListo();
  if (!listoWS && !(usarFallbackHttp && !enVuelo)) return;
  if (enVuelo && (!listoWS || blobListo)) return;

  const genInicio = genStream;
  if (rvfcActivo) ultimoVideoSeq = videoFrameSeq;
  capturando = true;
  tUltSubida = ahoraMs;
  let blob = null;
  try {
    const objetivo = parseInt($("resolucion").value, 10) || 0;
    const escala = objetivo > 0 ? Math.min(1, objetivo / w) : 1;
    const tw = Math.max(1, Math.round(w * escala)), th = Math.max(1, Math.round(h * escala));
    blob = await codificarFrame(tw, th);
  } finally {
    capturando = false;
  }
  if (!blob || !stream || genInicio !== genStream) return;

  if (wsListo()) {
    if (!enVuelo) {
      enviarBlobWS(blob);
    } else {
      blobListo = { blob, ts: performance.now() };
    }
    return;
  }
  if (usarFallbackHttp && !enVuelo) {
    enVuelo = true;
    tEnvio = performance.now();
    try {
      const r = await fetch(q("frame") + "&formato=json", { method: "POST", body: blob });
      if (r.ok) {
        const d = await r.json();
        if (d.estado) aplicar_estado(d.estado);
        registrarAck(Math.max(1, performance.now() - tEnvio));
        actualizarObjetivosHUD(d.dets || []);
      }
    } catch (e) {}
    enVuelo = false;
    intentarCapturar(performance.now());
  }
}

function ciclo(ahoraMs) {
  dibujarHUD(ahoraMs);
  if (!rvfcActivo) intentarCapturar(ahoraMs);
  requestAnimationFrame(ciclo);
}

$("estado").textContent = "Conectado. Si no ves imagen, acepta el permiso de cámara.";
conectarWS();
encender();
requestAnimationFrame(ciclo);
</script></body></html>
"""
