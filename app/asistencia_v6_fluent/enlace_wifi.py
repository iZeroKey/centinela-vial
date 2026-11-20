"""
Enlace WiFi hacia el ESP32 receptor (LCD 16x2 + LEDs).

Lee el estado compartido de estado_lcd.py y lo envía por HTTP al ESP32:

    GET http://<ip>/estado?e=<estado>&f1=<fila_1>&f2=<fila_2>

- Envía solo cuando algo cambia, más un heartbeat cada segundo.
- Corre en su propio hilo: si la red falla, no bloquea la UI ni las cámaras.
- Resuelve el nombre (p. ej. "centinela-lcd.local") UNA sola vez y guarda la
  IP: en Windows la resolución mDNS puede tardar ~1 s por petición, lo que
  hacía que el porcentaje avanzara a saltos.
- Ignora el proxy del sistema: el ESP32 está en la red local.
- Solo usa la librería estándar.

Uso:
    from enlace_wifi import EnlaceWifi
    enlace = EnlaceWifi("192.168.1.60")      # o "centinela-lcd.local"
    enlace.start()
    ...
    enlace.detener()
"""
import socket
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from estado_lcd import obtener_estado_lcd, obtener_filas_lcd
import config as config_app
from lcd_animacion import obtener_filas_animadas

# Opener sin proxy: en Windows urllib lee el proxy del sistema y puede
# desviar o retrasar las peticiones a la red local.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _ascii(texto):
    """La LCD HD44780 no tiene tildes ni ñ: se convierten a ASCII."""
    return unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()


def _fila(texto):
    return f"{_ascii(texto)[:16]:<16}"


def construir_parametros():
    """Arma (estado, fila_1, fila_2) a partir del estado actual de estado_lcd."""
    fila_1, fila_2 = obtener_filas_animadas()
    return obtener_estado_lcd(), _fila(fila_1), _fila(fila_2)


class EnlaceWifi(threading.Thread):
    def __init__(self, host, puerto=80, intervalo=0.1, heartbeat=1.0,
                 timeout=0.5, al_enviar=None):
        """
        host: IP del ESP32 receptor ("192.168.1.60") o nombre mDNS
              ("centinela-lcd.local").
        al_enviar: callback opcional que recibe (estado, fila_1, fila_2) tras
                   cada envío correcto. La latencia del último envío queda en
                   self.latencia_ms.
        """
        super().__init__(daemon=True, name="EnlaceWifi")
        self.host = host.strip().removeprefix("http://").rstrip("/")
        self.puerto = puerto
        self.intervalo = intervalo
        self.heartbeat = heartbeat
        self.timeout = timeout
        self.al_enviar = al_enviar

        self.ip = None
        self.conectado = False
        self.ultimo_error = ""
        self.latencia_ms = 0.0
        self._parar = threading.Event()
        self._ultimo = None
        self._t_ultimo = 0.0

    # ---------------- Resolución de nombre ----------------
    def resolver(self):
        """Resuelve el host a una IPv4 y la guarda. Devuelve la IP o None."""
        try:
            info = socket.getaddrinfo(self.host, self.puerto,
                                      socket.AF_INET, socket.SOCK_STREAM)
            self.ip = info[0][4][0]
            return self.ip
        except OSError as exc:
            self.ip = None
            self.ultimo_error = f"No se pudo resolver {self.host}: {exc}"
            return None

    @property
    def url_base(self):
        return f"http://{self.ip or self.host}:{self.puerto}"

    # ---------------- Hilo ----------------
    def run(self):
        while not self._parar.is_set():
            if self.ip is None and self.resolver() is None:
                self.conectado = False
                self._parar.wait(2.0)
                continue

            datos = construir_parametros()
            ahora = time.monotonic()
            if datos != self._ultimo or ahora - self._t_ultimo >= self.heartbeat:
                if self._enviar(datos):
                    self._ultimo, self._t_ultimo = datos, ahora
                else:
                    self._ultimo = None   # reenviar en cuanto vuelva
                    self.ip = None        # por si el ESP32 cambió de IP
                    self._parar.wait(1.0)
                    continue
            self._parar.wait(self.intervalo)

    def _get(self, ruta):
        inicio = time.perf_counter()
        with _OPENER.open(f"{self.url_base}{ruta}", timeout=self.timeout) as resp:
            cuerpo = resp.read()
        self.latencia_ms = (time.perf_counter() - inicio) * 1000
        return cuerpo

    def _enviar(self, datos):
        estado, fila_1, fila_2 = datos
        consulta = urllib.parse.urlencode(
            {"e": estado, "f1": fila_1, "f2": fila_2},
            quote_via=urllib.parse.quote,
        )
        try:
            self._get(f"/estado?{consulta}")
            self.conectado = True
            self.ultimo_error = ""
            if self.al_enviar:
                self.al_enviar(datos)
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.conectado = False
            self.ultimo_error = str(getattr(exc, "reason", exc))
            return False

    def ping(self):
        """Comprueba si el ESP32 responde. Útil para un botón 'Probar LCD'."""
        if self.ip is None and self.resolver() is None:
            return False
        try:
            return self._get("/ping").strip() == b"OK"
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.ultimo_error = str(getattr(exc, "reason", exc))
            return False

    def detener(self, timeout=2.0):
        self._parar.set()
        if self.is_alive():
            self.join(timeout)
