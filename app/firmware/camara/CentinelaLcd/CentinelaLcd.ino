/*
 * Centinela Control - Receptor LCD + LEDs por WiFi (ESP32 Dev Module)
 *
 * El ESP32 se conecta a la misma red WiFi que la PC y la ESP32-CAM y recibe
 * el estado de la app por HTTP:
 *
 *   GET http://IP_DEL_ESP32/estado?e=<estado>&f1=<fila_1>&f2=<fila_2>
 *
 *   estado: esperando | procesando | confirmado | incompleto | desconocido
 *   f1, f2: texto ASCII de hasta 16 caracteres
 *
 * Otras rutas:
 *   GET /        -> página con el estado actual (para probar desde el navegador)
 *   GET /ping    -> "OK"
 *
 * Hardware:
 *   LCD 16x2 I2C (PCF8574)  SDA -> GPIO21, SCL -> GPIO22, VCC -> 5V, GND
 *   LED verde    -> GPIO25 -> resistencia 220 ohm -> GND
 *   LED amarillo -> GPIO26 -> resistencia 220 ohm -> GND
 *   LED rojo     -> GPIO27 -> resistencia 220 ohm -> GND
 *
 * Librería: "LiquidCrystal I2C" de Frank de Brabander.
 * WiFi.h, WebServer.h y ESPmDNS.h vienen con el core ESP32.
 */

#include <WiFi.h>
#include <WebServer.h>
#include <ESPmDNS.h>
#include <Wire.h>
#include <LiquidCrystal_I2C.h>

// ---------------- Configuración WiFi ----------------
const char *WIFI_SSID = "wifiprueba";
const char *WIFI_PASS = "erick12345";
const char *NOMBRE_MDNS   = "centinela-lcd";   // http://centinela-lcd.local

// IP fija opcional: pon USAR_IP_FIJA en true y ajusta los valores a tu red.
#define USAR_IP_FIJA false
IPAddress IP_FIJA(192, 168, 1, 60);
IPAddress PUERTA_ENLACE(192, 168, 1, 1);
IPAddress MASCARA(255, 255, 255, 0);

// ---------------- Configuración hardware ----------------
#define LCD_DIRECCION   0x27      // prueba 0x3F si la pantalla no muestra nada
#define PIN_SDA         21
#define PIN_SCL         22

#define LED_VERDE       25
#define LED_AMARILLO    26
#define LED_ROJO        27

#define TIMEOUT_PC_MS       3000  // sin peticiones en este tiempo -> "SIN CONEXION PC"
#define RESULTADO_MIN_MS    2000  // tiempo mínimo que se mantiene verde/rojo

LiquidCrystal_I2C lcd(LCD_DIRECCION, 16, 2);
WebServer servidor(80);

// ---------------- Estado ----------------
enum Estado { ESPERANDO, PROCESANDO, CONFIRMADO, INVALIDO, SIN_CONEXION };

Estado estadoActual = SIN_CONEXION;
String txtEstadoActual = "sin_conexion";
String fila1Actual = "";
String fila2Actual = "";

unsigned long ultimaPeticionMs = 0;
unsigned long resultadoDesdeMs = 0;
unsigned long ultimoIntentoWifiMs = 0;

// ---------------- Utilidades LCD / LEDs ----------------
String ajustar16(const String &s) {
  String r = s.substring(0, 16);
  while (r.length() < 16) r += ' ';
  return r;
}

void escribirLCD(const String &f1, const String &f2) {
  String a = ajustar16(f1);
  String b = ajustar16(f2);
  if (a != fila1Actual) { lcd.setCursor(0, 0); lcd.print(a); fila1Actual = a; }
  if (b != fila2Actual) { lcd.setCursor(0, 1); lcd.print(b); fila2Actual = b; }
}

void aplicarLeds(Estado e) {
  digitalWrite(LED_VERDE,    e == CONFIRMADO ? HIGH : LOW);
  digitalWrite(LED_AMARILLO, e == PROCESANDO ? HIGH : LOW);
  digitalWrite(LED_ROJO,     e == INVALIDO   ? HIGH : LOW);
}

Estado convertirEstado(const String &txt) {
  if (txt == "procesando")  return PROCESANDO;
  if (txt == "confirmado")  return CONFIRMADO;
  if (txt == "incompleto")  return INVALIDO;
  if (txt == "desconocido") return INVALIDO;
  return ESPERANDO;
}

bool esResultado(Estado e) { return e == CONFIRMADO || e == INVALIDO; }

void mostrarSinConexion() {
  estadoActual = SIN_CONEXION;
  txtEstadoActual = "sin_conexion";
  aplicarLeds(SIN_CONEXION);
  escribirLCD("CENTINELA CTRL", "SIN CONEXION PC");
}

// ---------------- Lógica de estado (igual que la versión serial) ----------------
void aplicarEstado(const String &txtEstado, const String &f1, const String &f2) {
  ultimaPeticionMs = millis();
  Estado nuevo = convertirEstado(txtEstado);

  // Mantener visible el resultado (verde/rojo) un mínimo de tiempo
  if (esResultado(estadoActual) && nuevo == ESPERANDO &&
      millis() - resultadoDesdeMs < RESULTADO_MIN_MS) {
    return;
  }

  if (esResultado(nuevo) && nuevo != estadoActual) {
    resultadoDesdeMs = millis();
  }

  estadoActual = nuevo;
  txtEstadoActual = txtEstado;
  aplicarLeds(estadoActual);
  escribirLCD(f1, f2);
}

// ---------------- Rutas HTTP ----------------
void rutaEstado() {
  if (!servidor.hasArg("e")) {
    servidor.send(400, "text/plain", "Falta el parametro e");
    return;
  }
  String e  = servidor.arg("e");
  String f1 = servidor.arg("f1");
  String f2 = servidor.arg("f2");
  e.trim();
  aplicarEstado(e, f1, f2);
  servidor.send(200, "text/plain", "OK");
}

void rutaPing() {
  servidor.send(200, "text/plain", "OK");
}

void rutaRaiz() {
  String html =
    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
    "<meta http-equiv='refresh' content='1'>"
    "<title>Centinela LCD</title></head>"
    "<body style='font-family:monospace'>"
    "<h3>Centinela Control - receptor LCD</h3>"
    "<pre style='background:#9cb83a;padding:10px;display:inline-block'>" +
    fila1Actual + "\n" + fila2Actual +
    "</pre><p>Estado: " + txtEstadoActual + "</p>"
    "<p>Prueba: <a href='/estado?e=confirmado&f1=Erick&f2=ASISTENCIA%20OK'>"
    "/estado?e=confirmado&amp;f1=Erick&amp;f2=ASISTENCIA OK</a></p>"
    "</body></html>";
  servidor.send(200, "text/html", html);
}

// ---------------- WiFi ----------------
void conectarWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);  // respuesta más rápida
  if (USAR_IP_FIJA) WiFi.config(IP_FIJA, PUERTA_ENLACE, MASCARA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  escribirLCD("CONECTANDO WIFI", WIFI_SSID);
  unsigned long inicio = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - inicio < 15000) {
    delay(250);
  }

  if (WiFi.status() == WL_CONNECTED) {
    String ip = WiFi.localIP().toString();
    Serial.print("IP: ");
    Serial.println(ip);
    escribirLCD("WIFI OK  IP:", ip);
    MDNS.begin(NOMBRE_MDNS);
    MDNS.addService("http", "tcp", 80);
    delay(3000);  // tiempo para leer la IP en la LCD
  } else {
    escribirLCD("SIN WIFI", "REINTENTANDO...");
  }
  ultimoIntentoWifiMs = millis();
}

// ---------------- Setup / Loop ----------------
void setup() {
  pinMode(LED_VERDE, OUTPUT);
  pinMode(LED_AMARILLO, OUTPUT);
  pinMode(LED_ROJO, OUTPUT);

  Serial.begin(115200);  // solo para ver la IP en el Monitor Serie
  Wire.begin(PIN_SDA, PIN_SCL);
  lcd.init();
  lcd.backlight();

  int leds[] = {LED_VERDE, LED_AMARILLO, LED_ROJO};
  for (int i = 0; i < 3; i++) {
    digitalWrite(leds[i], HIGH); delay(200); digitalWrite(leds[i], LOW);
  }

  conectarWifi();

  servidor.on("/", rutaRaiz);
  servidor.on("/estado", rutaEstado);
  servidor.on("/ping", rutaPing);
  servidor.onNotFound([]() { servidor.send(404, "text/plain", "No encontrado"); });
  servidor.begin();

  mostrarSinConexion();
}

void loop() {
  // Reconexión WiFi si se cae
  if (WiFi.status() != WL_CONNECTED) {
    if (millis() - ultimoIntentoWifiMs > 5000) {
      aplicarLeds(SIN_CONEXION);
      escribirLCD("SIN WIFI", "REINTENTANDO...");
      WiFi.reconnect();
      ultimoIntentoWifiMs = millis();
    }
    return;
  }

  servidor.handleClient();

  // Si la PC deja de enviar (app cerrada, otra red), avisarlo
  if (estadoActual != SIN_CONEXION && millis() - ultimaPeticionMs > TIMEOUT_PC_MS) {
    mostrarSinConexion();
  }
}
