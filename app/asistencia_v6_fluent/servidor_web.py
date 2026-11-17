#!/usr/bin/env python3
"""
Centinela Control — Servidor web + mDNS (Fase 1a)

Publica el sistema en la red local para consultarlo desde cualquier
dispositivo (teléfono, laptop) con el navegador:

  https://centinela.local:8443/               visor (video anotado + asistencia)
  https://centinela.local:8443/stream         MJPEG del último frame anotado
  https://centinela.local:8443/foto           frame anotado actual (JPEG suelto)
  https://centinela.local:8443/api/asistencia JSON asistencia de hoy (lectura)
  https://centinela.local:8443/api/status     salud de nodos/cámaras
  https://centinela.local:8443/ping           "OK"

- HTTPS con certificado propio (se genera una sola vez en data/certs/):
  getUserMedia en el teléfono exige contexto seguro (Fase 1b lo usa).
- mDNS con zeroconf: anuncia centinela.local en la red, sin IP fija.
- La fuente de frames y el estado se inyectan como callables, para no
  acoplar este módulo a la GUI. Solo lectura: nunca toca la IA.

Hilo propio (daemon). start()/detener() son seguros para llamar desde la GUI.
"""

import io
import json
import os
import socket
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PUERTO_HTTPS = 8443
NOMBRE_MDNS = "centinela"
_CALIDAD_JPEG = 80

# .../asistencia_v6_fluent/servidor_web.py -> .../app
_ROOT_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DIR_CERTS = os.path.join(_ROOT_APP, "data", "certs")
_RUTA_CRT = os.path.join(_DIR_CERTS, "centinela.crt")
_RUTA_KEY = os.path.join(_DIR_CERTS, "centinela.key")
# favicon.svg vive en la raiz del repo (un nivel por encima de app/)
_RUTA_FAVICON = os.path.join(os.path.dirname(_ROOT_APP), "favicon.svg")


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
# Servidor HTTPS
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    # Inyectado por ServidorWeb (callables), compartidos por todos los hilos:
    provedor_frame = None      # () -> np.ndarray | None (frame anotado BGR)
    provedor_estado = None     # () -> dict (salud de nodos/cámaras)
    servidor = None            # ServidorWeb (registro de telefonos)

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
                import db as db_centinela
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

    def _telefono_registrar(self, tel_id, nombre, camara=""):
        """Alta/actualizacion del telefono que se reporta (hilo servidor)."""
        ahora = time.time()
        with self.servidor._lock_tel:
            reg = self.servidor.telefonos.get(tel_id)
            if reg is None:
                reg = {"nombre": "", "ip": self.client_address[0],
                       "visto": ahora, "camara": camara, "estado": "libre"}
                self.servidor.telefonos[tel_id] = reg
            reg["visto"] = ahora
            reg["ip"] = self.client_address[0]
            if nombre:
                reg["nombre"] = nombre
            if camara:
                reg["camara"] = camara
            return dict(reg)

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
        """Foto subida por el telefono -> pipeline IA -> JPEG anotado."""
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
        self._telefono_registrar(tel_id, params.get("nombre", ""),
                                 params.get("camara", ""))
        cb = self.servidor.al_frame_telefono
        anotado = cb(tel_id, params.get("nombre", ""), jpg) if cb else None
        if anotado is None:
            self._json({"error": "procesamiento no disponible"}, 503)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(anotado)))
        self.end_headers()
        self.wfile.write(anotado)

    def _stream_mjpeg(self):
        """MJPEG multipart: mismo formato que sirve la ESP32 en :81."""
        self.send_response(200)
        self.send_header("Content-Type",
                         "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        while True:
            jpg = self._jpeg()
            if jpg is None:
                # sin cámara: frame gris para que el cliente no corte
                import numpy as np
                import cv2
                ok, buf = cv2.imencode(".jpg", np.full((270, 480, 3), 32, np.uint8))
                if not ok:
                    return
                jpg = buf.tobytes()
            try:
                self.wfile.write(b"--frame\r\n"
                                 b"Content-Type: image/jpeg\r\n\r\n")
                self.wfile.write(jpg)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            except (ConnectionError, BrokenPipeError, ssl.SSLError):
                return
            # ~8 fps de publicación (el ancho de banda es el límite real)
            threading.Event().wait(0.125)


class ServidorWeb:
    """Servidor HTTPS + mDNS en un hilo daemon. start() / detener()."""

    TIEMPO_TELEFONO_CAIDO = 8.0  # s sin heartbeat -> aparece como caido

    def __init__(self, provedor_frame=None, provedor_estado=None,
                 al_frame_telefono=None):
        self.provedor_frame = provedor_frame
        self.provedor_estado = provedor_estado or (lambda: {})
        # al_frame_telefono(tel_id, nombre, jpeg_bytes) -> jpeg anotado | None
        self.al_frame_telefono = al_frame_telefono
        # registro de telefonos conectados (protegido por _lock_tel):
        #   tel_id -> {"nombre", "ip", "visto": ts, "camara", "estado"}
        self.telefonos = {}
        self._lock_tel = threading.Lock()
        self._server = None
        self._hilo = None
        self._zeroconf = None
        self._info = None
        self._lock = threading.Lock()

    # -- ciclo de vida -------------------------------------------------------
    def start(self):
        with self._lock:
            if self._server is not None:
                return
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
            self._anunciar_mdns()
            print(f"[WEB] https://{NOMBRE_MDNS}.local:{PUERTO_HTTPS} "
                  f"(o https://{ip_local()}:{PUERTO_HTTPS})")

    def detener(self):
        with self._lock:
            server, hilo = self._server, self._hilo
            self._server, self._hilo = None, None
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
            self._zeroconf = zc = Zeroconf()
            import ipaddress
            addr = ipaddress.ip_address(ip_local())
            self._info = ServiceInfo(
                "_http._tcp.local.",
                f"{NOMBRE_MDNS}._http._tcp.local.",
                port=PUERTO_HTTPS,
                properties={"path": "/", "https": "true"},
                server=f"{NOMBRE_MDNS}.local.",
                addresses=[addr.packed],
            )
            try:
                zc.register_service(self._info)
                print("[MDNS] anunciado centinela.local")
            except NonUniqueNameException:
                print("[MDNS] centinela.local ya anunciado por otro equipo; "
                      "la IP directa sigue funcionando")
        except Exception as e:
            print(f"[MDNS] no se pudo anunciar: {e}")
            self._retirar_mdns()

    def _retirar_mdns(self):
        if self._zeroconf is not None:
            try:
                if self._info is not None:
                    self._zeroconf.unregister_service(self._info)
            except Exception:
                pass
            try:
                self._zeroconf.close()
            except Exception:
                pass
            self._zeroconf, self._info = None, None

    # -- politicas de telefonos (Fase 1c) ------------------------------------
    ESTADOS_TELEFONO = ("libre", "controles_ocultos", "bloqueado")

    def fijar_estado(self, tel_id, estado):
        """Fija la politica de un telefono desde el panel del PC."""
        estado = str(estado).strip()
        if estado not in self.ESTADOS_TELEFONO:
            return False
        with self._lock_tel:
            reg = self.telefonos.get(tel_id)
            if reg is None:
                return False
            reg["estado"] = estado
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
         display: flex; flex-direction: column; height: 100dvh; }
  video { display: none; }
  #vista { flex: 1; min-height: 0; display: flex; align-items: center;
           justify-content: center; background: #0a0a0e; }
  #vista img { max-width: 100%; max-height: 100%; }
  #controles { padding: 10px 12px; display: flex; gap: 8px;
               align-items: center; flex-wrap: wrap; }
  button { padding: 10px 16px; border-radius: 10px; border: 1px solid #3f3f46;
           background: #27272a; color: #e4e4e7; font-size: 14px; }
  button.primario { background: #0e7490; border-color: #0e7490; }
  input { padding: 9px 12px; border-radius: 10px; border: 1px solid #3f3f46;
          background: #18181b; color: #e4e4e7; font-size: 14px; flex: 1;
          min-width: 120px; }
  #estado { font-size: 12px; color: #8b97a8; width: 100%; }
  #ip { font-size: 11px; color: #8b97a8; width: 100%; }
  #ip code { color: #e4e4e7; user-select: all; }
  .oculto { display: none !important; }
  body.bloqueado #controles { display: none; }
  body.bloqueado { cursor: none; }
</style></head><body>
<div id="vista"><img id="foto" alt="Esperando cámara…"></div>
<div id="controles">
  <input id="nombre" placeholder="Tu nombre (opcional)" autocomplete="off">
  <button id="btn_cam" class="primario">📷 Cámara</button>
  <div id="estado"></div>
  <div id="ip">Si centinela.local no resuelve: <code id="ip-url">cargando…</code></div>
</div>
<video id="video" autoplay playsinline muted></video>
<script>
fetch("/api/ip").then(r => r.json()).then(d => {
  document.getElementById("ip-url").textContent = d.url + "telefono";
}).catch(() => {
  document.getElementById("ip-url").textContent = "https://" + location.hostname + ":8443/telefono";
});

const $ = id => document.getElementById(id);
const video = $("video"), img = $("foto");
let stream = null, camara = "environment";
let tel = localStorage.getItem("centinela_tel");
if (!tel) {
  tel = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()));
  localStorage.setItem("centinela_tel", tel);
}
$("nombre").value = localStorage.getItem("centinela_tel_nombre") || "";
$("nombre").addEventListener("change", () =>
  localStorage.setItem("centinela_tel_nombre", $("nombre").value.trim()));

const q = extra => "/api/telefono/" + extra + "?tel=" + encodeURIComponent(tel) +
  "&nombre=" + encodeURIComponent($("nombre").value.trim()) +
  "&camara=" + camara;

async function encender(que = camara) {
  if (stream) stream.getTracks().forEach(t => t.stop());
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    $("estado").textContent =
      "Cámara bloqueada: el navegador exige HTTPS con certificado de confianza " +
      "(" + location.origin + "/telefono). Vuelve a aceptar el aviso de seguridad.";
    return;
  }
  try {
    stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: que, width: { ideal: 1280 } }, audio: false });
    video.srcObject = stream;
    await video.play();
    $("estado").textContent = "Cámara en marcha.";
    subir_frames();
  } catch (e) {
    $("estado").textContent = "Sin permiso de cámara: " + e.message;
  }
}

$("btn_cam").onclick = () => {
  camara = camara === "environment" ? "user" : "environment";
  encender(camara);
};

const lienzo = document.createElement("canvas");
const contexto = lienzo.getContext("2d");
let ocupado = false;

let urlFrameActual = null;

async function subir_frames() {
  if (!stream || ocupado) return;
  ocupado = true;
  try {
    const w = video.videoWidth, h = video.videoHeight;
    if (w && h) {
      const escala = Math.min(1, 640 / w);
      lienzo.width = Math.round(w * escala);
      lienzo.height = Math.round(h * escala);
      contexto.drawImage(video, 0, 0, lienzo.width, lienzo.height);
      const blob = await new Promise(r =>
        lienzo.toBlob(r, "image/jpeg", 0.6));
      if (blob) {
        const r = await fetch(q("frame"), { method: "POST", body: blob });
        if (r.ok) {
          const url = URL.createObjectURL(await r.blob());
          const anterior = urlFrameActual;
          img.src = url;          // asignar ANTES de revocar la anterior
          urlFrameActual = url;
          if (anterior) URL.revokeObjectURL(anterior);
        }
      }
    }
  } catch (e) { /* red lenta: reintenta en el próximo ciclo */ }
  ocupado = false;
}

setInterval(subir_frames, 330);            // ~3 fotos por segundo
setInterval(() => fetch(q("heartbeat"), { method: "POST" }), 2000);

function aplicar_estado(s) {
  document.body.classList.toggle("bloqueado", s === "bloqueado");
  $("controles").classList.toggle("oculto",
    s === "bloqueado" || s === "controles_ocultos");
  if (s === "bloqueado" && !document.fullscreenElement) {
    document.documentElement.requestFullscreen?.().catch(() => {});
  }
}
setInterval(async () => {
  try {
    const r = await (await fetch("/api/telefono/estado?tel=" +
      encodeURIComponent(tel))).json();
    if (r.conocido) aplicar_estado(r.estado);
  } catch (e) {}
}, 1000);

$("estado").textContent = "Conectado. Si no ves imagen, acepta el permiso de cámara.";
encender();
</script></body></html>
"""
