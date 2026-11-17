// CentinelaCamWifi - servidor de camara para la app Python (asistencia_v5).
// Hardware: ESP32-CAM AI-Thinker conectado a un ESP32-CAM-MB, sin PSRAM.
//
// Arquitectura basada en el sketch de Freenove CameraWebServer (esp_http_server,
// dos instancias): el stream va aislado en su propio servidor, de modo que si un
// stream queda colgado, el control en el puerto 80 sigue vivo y el stream puede
// reintentarse en el 81 sin reiniciar la placa.
//
//   Puerto 80:  /              info de estado (res, fb=dram)
//               /jpg (o /foto) captura instantanea
//               /res?val=QVGA  mantener/cambiar resolucion (unica sin PSRAM)
//   Puerto 81:  /stream        video MJPEG continuo (multipart/x-mixed-replace)
//
// Para la app / el navegador:  http://<ip>:81/stream
#include "esp_camera.h"
#include <WiFi.h>
#include "esp_http_server.h"
#include <ESPmDNS.h>

// ==== red ====
const char *WIFI_SSID = "wifiprueba";
const char *WIFI_PASS = "prueba123";
#define JPEG_QUALITY 25
#define FPS_MAX 2
#define PUERTO_HTTP 80
#define PUERTO_STREAM 81

#define PWDN_GPIO_NUM 32
#define RESET_GPIO_NUM -1
#define XCLK_GPIO_NUM 0
#define SIOD_GPIO_NUM 26
#define SIOC_GPIO_NUM 27
#define Y2_GPIO_NUM 5
#define Y3_GPIO_NUM 18
#define Y4_GPIO_NUM 19
#define Y5_GPIO_NUM 21
#define Y6_GPIO_NUM 36
#define Y7_GPIO_NUM 39
#define Y8_GPIO_NUM 34
#define Y9_GPIO_NUM 35
#define VSYNC_GPIO_NUM 25
#define HREF_GPIO_NUM 23
#define PCLK_GPIO_NUM 22

#define LIMITE "centinela123"
static const char *STREAM_TIPO = "multipart/x-mixed-replace;boundary=" LIMITE;
static const char *STREAM_FRONT = "\r\n--" LIMITE "\r\n";
static const char *STREAM_PARTE = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

framesize_t res_actual = FRAMESIZE_QVGA;
bool fb_en_dram = false;
httpd_handle_t servidor_ctrl = NULL;
WiFiServer servidor_tcp(PUERTO_STREAM);
WiFiClient cliente_tcp;

static const char *nombre_res(framesize_t r) {
  switch (r) {
  case FRAMESIZE_QVGA: return "QVGA";
  case FRAMESIZE_VGA: return "VGA";
  case FRAMESIZE_SVGA: return "SVGA";
  default: return "?";
  }
}

static bool res_desde_nombre(const String &n, framesize_t &out) {
  String u = n;
  u.toUpperCase();
  if (u == "QVGA") { out = FRAMESIZE_QVGA; return true; }
  if (u == "VGA") { out = FRAMESIZE_VGA; return true; }
  if (u == "SVGA") { out = FRAMESIZE_SVGA; return true; }
  return false;
}

static void aplica_resolucion(framesize_t r) {
  // Con PSRAM se permiten resoluciones mayores; sin PSRAM solo QVGA es estable.
  if (ESP.getPsramSize() == 0 && r != FRAMESIZE_QVGA) return;
  sensor_t *s = esp_camera_sensor_get();
  if (s) s->set_framesize(s, r);
  res_actual = r;
}

static void configura_camara() {
  camera_config_t c;
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode = CAMERA_GRAB_LATEST;   // envia el frame mas reciente si el envio se atrasa

  // Intentar primero con PSRAM (si el chip lo reporta). Si el init falla
  // (PSRAM no real o no disponible), caer a DRAM + QVGA automaticamente.
  bool usar_psram = (ESP.getPsramSize() > 0);
  if (usar_psram) {
    c.frame_size = FRAMESIZE_VGA;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.fb_count = 2;
  } else {
    c.frame_size = FRAMESIZE_QVGA;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.fb_count = 1;
  }
  c.jpeg_quality = JPEG_QUALITY;

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK && usar_psram) {
    // PSRAM reportado pero no utilizable: reintentar en DRAM con QVGA
    Serial.println("PSRAM no utilizable, reintentando en DRAM (QVGA)...");
    c.frame_size = FRAMESIZE_QVGA;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.fb_count = 1;
    c.jpeg_quality = JPEG_QUALITY;
    err = esp_camera_init(&c);
  }
  fb_en_dram = (c.fb_location == CAMERA_FB_IN_DRAM);
  if (err == ESP_OK) {
    res_actual = (framesize_t)c.frame_size;
  }
  if (err != ESP_OK) {
    Serial.printf("Camara fallo, error 0x%x\n", err);
    while (true) delay(1000);
  }
  res_actual = (framesize_t)c.frame_size;

  // Ajustes del sensor segun modelo (igual que CameraWebServer)
  sensor_t *s = esp_camera_sensor_get();
  if (s) {
    uint16_t pid = s->id.PID;
    if (pid == OV2640_PID) {
      s->set_hmirror(s, 1);
      s->set_vflip(s, 1);
    } else if (pid == OV3660_PID) {
      s->set_hmirror(s, 1);
      s->set_vflip(s, 0);
    } else if (pid == GC2145_PID || pid == GC0308_PID) {
      s->set_hmirror(s, 0);
      delay(500);
      s->set_vflip(s, 0);
    } else {
      s->set_hmirror(s, 1);
      s->set_vflip(s, 0);
    }
    s->set_brightness(s, 1);
    s->set_saturation(s, 0);
    s->set_ae_level(s, -3);
    s->set_aec2(s, 1);   // AE mejorado: reacciona mejor con movimiento
    // Mejoras de imagen para OV3660 (sin PSRAM, QVGA)
    s->set_contrast(s, 1);
    s->set_sharpness(s, 2);
    s->set_whitebal(s, 1);
    s->set_awb_gain(s, 1);
    s->set_wb_mode(s, 0);
  }
}

static void responde_texto(httpd_req_t *req, const String &cuerpo) {
  httpd_resp_set_type(req, "text/plain");
  httpd_resp_send(req, cuerpo.c_str(), cuerpo.length());
}

static esp_err_t atiende_raiz(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  String ip = WiFi.localIP().toString();
  String b = "CentinelaCam OK\nres=" + String(nombre_res(res_actual)) +
             " fb=" + String(fb_en_dram ? "dram" : "psram") +
             " fps_max=" + String(FPS_MAX) + "\nstream: tcp://" + ip + ":81\n";
  b += "foto:  http://" + ip + "/jpg\nres soportada: QVGA\n";
  responde_texto(req, b);
  return ESP_OK;
}

static esp_err_t atiende_jpg(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb) {
    httpd_resp_set_type(req, "text/plain");
    httpd_resp_send_500(req);
    return ESP_FAIL;
  }
  httpd_resp_set_type(req, "image/jpeg");
  esp_err_t res = httpd_resp_send(req, (const char *)fb->buf, fb->len);
  esp_camera_fb_return(fb);   // SIEMPRE, haya salido o no
  return res;
}

static esp_err_t atiende_res(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  size_t lon = httpd_req_get_url_query_len(req) + 1;
  if (lon > 1) {
    char *buf = (char *)malloc(lon);
    char valor[16];
    if (buf && httpd_req_get_url_query_str(req, buf, lon) == ESP_OK &&
        httpd_query_key_value(buf, "val", valor, sizeof(valor)) == ESP_OK) {
      framesize_t nr;
      if (res_desde_nombre(String(valor), nr) && nr != res_actual && nr == FRAMESIZE_QVGA) {
        aplica_resolucion(nr);
        Serial.print("Resolucion -> ");
        Serial.println(nombre_res(nr));
      }
    }
    if (buf) free(buf);
  }
  return atiende_raiz(req);
}


static void arranca_servidores() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.send_wait_timeout = 5;
  cfg.lru_purge_enable = true;
  cfg.max_open_sockets = 7;
  cfg.recv_wait_timeout = 60;

  httpd_uri_t uri_raiz = { "/", HTTP_GET, atiende_raiz, NULL };
  httpd_uri_t uri_jpg = { "/jpg", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_foto = { "/foto", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_res = { "/res", HTTP_GET, atiende_res, NULL };

  if (httpd_start(&servidor_ctrl, &cfg) == ESP_OK) {
    httpd_register_uri_handler(servidor_ctrl, &uri_raiz);
    httpd_register_uri_handler(servidor_ctrl, &uri_jpg);
    httpd_register_uri_handler(servidor_ctrl, &uri_foto);
    httpd_register_uri_handler(servidor_ctrl, &uri_res);
    Serial.printf("Control en http://%s:%d/\n", WiFi.localIP().toString().c_str(), cfg.server_port);
  } else {
    Serial.println("No pude arrancar el servidor de control");
  }

  // Servidor TCP crudo para stream de video (sin overhead HTTP)
  servidor_tcp.begin();
  Serial.printf("Stream TCP en %s:%d\n", WiFi.localIP().toString().c_str(), PUERTO_STREAM);
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(false);  // desactivado: reduce overhead de CPU/UART con mucho movimiento
  delay(500);
  Serial.println("\nCentinelaCam iniciando...");
  Serial.printf("PSRAM: %u\n", (uint32_t)ESP.getPsramSize());

  configura_camara();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  WiFi.setSleep(false);
  WiFi.setTxPower(WIFI_POWER_19_5dBm);   // maxima potencia: mejor señal con mucho movimiento
  uint8_t intentos = 0;
  while (WiFi.status() != WL_CONNECTED && intentos < 40) {
    delay(500);
    Serial.print(".");
    intentos++;
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("\nWiFi no conecta, reintento en 5 s...");
    delay(5000);
    ESP.restart();
  }

  if (MDNS.begin("centinela-cam")) {
    MDNS.addService("http", "tcp", PUERTO_HTTP);
    Serial.println("mDNS: http://centinela-cam.local");
  }

  arranca_servidores();
  Serial.print("\nWiFi conectado. Control: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/");
  Serial.print("Stream TCP: ");
  Serial.print(WiFi.localIP());
  Serial.println(":81");
  Serial.print("Foto: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/jpg");
}

// Envia un frame por TCP crudo: [4 bytes longitud big-endian][datos JPEG]
static bool enviar_frame_tcp(WiFiClient &cli, camera_fb_t *fb) {
  uint32_t len = (uint32_t)fb->len;
  uint8_t hdr[4];
  hdr[0] = (len >> 24) & 0xFF;
  hdr[1] = (len >> 16) & 0xFF;
  hdr[2] = (len >> 8) & 0xFF;
  hdr[3] = len & 0xFF;
  if (cli.write(hdr, 4) != 4) return false;
  size_t sent = cli.write((const uint8_t *)fb->buf, fb->len);
  return sent == fb->len;
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi perdido, reconectando...");
    WiFi.reconnect();
    delay(2000);
    return;
  }

  // Aceptar nuevo cliente TCP si no hay uno conectado
  if (!cliente_tcp || !cliente_tcp.connected()) {
    cliente_tcp = servidor_tcp.accept();
    if (cliente_tcp) {
      Serial.println("Cliente TCP conectado (stream)");
    }
  }

  if (cliente_tcp && cliente_tcp.connected()) {
    uint32_t t0 = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    if (fb && fb->format == PIXFORMAT_JPEG) {
      if (!enviar_frame_tcp(cliente_tcp, fb)) {
        // cliente corto la conexion
        cliente_tcp.stop();
        Serial.println("Cliente TCP desconectado");
      }
    }
    if (fb) esp_camera_fb_return(fb);
    uint32_t tardo = millis() - t0;
    const uint32_t intervalo = 1000 / FPS_MAX;
    if (tardo < intervalo) delay(intervalo - tardo);
  } else {
    delay(100);
  }
}
