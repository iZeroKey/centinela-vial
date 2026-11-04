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

// ==== red ====
const char *WIFI_SSID = "wifiprueba";
const char *WIFI_PASS = "erick12345";
#define RES_INICIAL FRAMESIZE_QVGA
#define JPEG_QUALITY 12
#define FPS_MAX 10
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

framesize_t res_actual = RES_INICIAL;
bool fb_en_dram = false;
httpd_handle_t servidor_ctrl = NULL;
httpd_handle_t servidor_stream = NULL;

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
  // Sin PSRAM, QVGA es el limite estable para un framebuffer JPEG en DRAM.
  if (r != FRAMESIZE_QVGA) return;
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
  c.frame_size = RES_INICIAL;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  c.fb_location = CAMERA_FB_IN_DRAM;
  c.jpeg_quality = JPEG_QUALITY;
  c.fb_count = 1;

  esp_err_t err = esp_camera_init(&c);
  fb_en_dram = true;
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
             " fb=dram (sin PSRAM)" +
             " fps_max=" + String(FPS_MAX) + "\nvideo: http://" + ip + ":81/stream\n";
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

static esp_err_t atiende_stream(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  esp_err_t res = httpd_resp_set_type(req, STREAM_TIPO);
  if (res != ESP_OK) return res;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "X-Framerate", "10");

  const uint32_t intervalo = 1000 / FPS_MAX;
  uint8_t fallos = 0;
  while (true) {
    if (WiFi.status() != WL_CONNECTED) break;   // enlace caido: salir y no quedar zombie
    uint32_t t0 = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb || fb->format != PIXFORMAT_JPEG) {
      if (fb) esp_camera_fb_return(fb);
      if (++fallos > 10) break;
      delay(50);
      continue;
    }
    fallos = 0;
    char parte[96];
    res = httpd_resp_send_chunk(req, STREAM_FRONT, strlen(STREAM_FRONT));
    if (res == ESP_OK) {
      int hlon = snprintf(parte, sizeof(parte), STREAM_PARTE, (unsigned int)fb->len);
      res = httpd_resp_send_chunk(req, parte, hlon);
    }
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);   // SIEMPRE, tras los envios
    if (res != ESP_OK) break;   // cliente corto la conexion
    uint32_t tardo = millis() - t0;
    if (tardo < intervalo) delay(intervalo - tardo);
  }
  return ESP_OK;
}

static void arranca_servidores() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  // El consumidor (app de reconocimiento) procesa cada frame pesadamente y deja
  // de leer durante varios segundos; con send_wait_timeout=5 (default del core)
  // IDF aborta el envio y cierra la conexion. Se amplia la ventana para que un
  // consumidor lento no rompa el flujo (un cliente muerto sigue acotado: aborta
  // a los ~120 s en vez de bloquear para siempre).
  cfg.send_wait_timeout = 120;
  cfg.recv_wait_timeout = 60;

  httpd_uri_t uri_raiz = { "/", HTTP_GET, atiende_raiz, NULL };
  httpd_uri_t uri_jpg = { "/jpg", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_foto = { "/foto", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_res = { "/res", HTTP_GET, atiende_res, NULL };
  httpd_uri_t uri_stream = { "/stream", HTTP_GET, atiende_stream, NULL };

  if (httpd_start(&servidor_ctrl, &cfg) == ESP_OK) {
    httpd_register_uri_handler(servidor_ctrl, &uri_raiz);
    httpd_register_uri_handler(servidor_ctrl, &uri_jpg);
    httpd_register_uri_handler(servidor_ctrl, &uri_foto);
    httpd_register_uri_handler(servidor_ctrl, &uri_res);
    Serial.printf("Control en http://%s:%d/\n", WiFi.localIP().toString().c_str(), cfg.server_port);
  } else {
    Serial.println("No pude arrancar el servidor de control");
  }

  cfg.server_port = PUERTO_STREAM;
  cfg.ctrl_port += 1;
  if (httpd_start(&servidor_stream, &cfg) == ESP_OK) {
    httpd_register_uri_handler(servidor_stream, &uri_stream);
    Serial.printf("Stream  en http://%s:%d/stream\n", WiFi.localIP().toString().c_str(), cfg.server_port);
  } else {
    Serial.println("No pude arrancar el servidor de stream");
  }
}

void setup() {
  Serial.begin(115200);
  Serial.setDebugOutput(true);   // logs del driver de camara (como CameraWebServer)
  delay(500);
  Serial.println("\nCentinelaCam iniciando...");
  Serial.printf("PSRAM: %u\n", (uint32_t)ESP.getPsramSize());

  configura_camara();

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  WiFi.setSleep(false);
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

  arranca_servidores();
  Serial.print("\nWiFi conectado. Video: http://");
  Serial.print(WiFi.localIP());
  Serial.println(":81/stream");
  Serial.print("Foto: http://");
  Serial.print(WiFi.localIP());
  Serial.println("/jpg");
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("WiFi perdido, reconectando...");
    WiFi.reconnect();
    delay(2000);
    return;
  }
  delay(200);
}
