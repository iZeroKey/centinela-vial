// CentinelaCamWifi - servidor de camara para Centinela Control.
// Hardware: ESP32-CAM AI-Thinker conectado a un ESP32-CAM-MB, con o sin PSRAM.
//
// Arquitectura basada en el sketch de Freenove CameraWebServer (esp_http_server,
// dos instancias): el stream va aislado en su propio servidor, de modo que si un
// stream queda colgado, el control en el puerto 80 sigue vivo y el stream puede
// reintentarse en el 81 sin reiniciar la placa.
//
//   Puerto 80:  /              info de estado (res, fb, fps, calidad, rssi)
//               /jpg (o /foto) captura instantanea
//               /res?val=VGA   cambiar resolucion (QVGA|VGA|SVGA)
//               /fps?val=25    cambiar FPS maximo (0=max .. 60)
//               /calidad?val=16 cambiar calidad JPEG (10..40, menor=mejor)
//               /config?res=VGA&fps=25&calidad=16  ajuste combinado
//   Puerto 81:  /stream        video MJPEG continuo (acepta ?res=&fps=&calidad=)
//
// Para la app / el navegador:  http://<ip>:81/stream
#include "esp_camera.h"
#include <WiFi.h>
#include "esp_http_server.h"
#include <ESPmDNS.h>
#include <lwip/sockets.h>

// ==== red ====
const char *WIFI_SSID = "wifiprueba";
const char *WIFI_PASS = "prueba123";
#define JPEG_QUALITY 16
#define FPS_MAX 25
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
static const char *STREAM_CABECERA =
    "\r\n--" LIMITE "\r\nContent-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

framesize_t res_actual = FRAMESIZE_QVGA;
int calidad_actual = JPEG_QUALITY;
volatile int fps_actual = FPS_MAX;
bool fb_en_dram = false;
uint8_t fb_count_actual = 1;
uint8_t *tx_buf = NULL;
size_t tx_buf_cap = 0;
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
  // Con PSRAM se permiten resoluciones mayores; sin PSRAM solo QVGA es estable.
  if (fb_en_dram && r != FRAMESIZE_QVGA) return;
  if (r == res_actual) return;
  sensor_t *s = esp_camera_sensor_get();
  if (s && s->set_framesize) {
    s->set_framesize(s, r);
    res_actual = r;
  }
}

static void aplica_calidad(int q) {
  if (q < 10) q = 10;
  if (q > 40) q = 40;
  if (q == calidad_actual) return;
  sensor_t *s = esp_camera_sensor_get();
  if (s && s->set_quality) {
    s->set_quality(s, q);
    calidad_actual = q;
  }
}

static void aplica_fps(int f) {
  if (f < 0) f = 0;
  if (f > 60) f = 60;
  fps_actual = f;
}

static bool aplica_params_query(httpd_req_t *req, const char *clave_val) {
  bool cambio_res = false;
  size_t lon = httpd_req_get_url_query_len(req) + 1;
  if (lon <= 1 || lon > 256) return false;
  char buf[256];
  if (httpd_req_get_url_query_str(req, buf, lon) != ESP_OK) return false;

  char valor[24];
  bool tiene_res = (httpd_query_key_value(buf, "res", valor, sizeof(valor)) == ESP_OK);
  if (!tiene_res && clave_val && strcmp(clave_val, "res") == 0) {
    tiene_res = (httpd_query_key_value(buf, "val", valor, sizeof(valor)) == ESP_OK);
  }
  if (tiene_res) {
    framesize_t nr;
    if (res_desde_nombre(String(valor), nr) && nr != res_actual &&
        (!fb_en_dram || nr == FRAMESIZE_QVGA)) {
      aplica_resolucion(nr);
      cambio_res = true;
      Serial.printf("Resolucion -> %s\n", nombre_res(res_actual));
    }
  }

  bool tiene_fps = (httpd_query_key_value(buf, "fps", valor, sizeof(valor)) == ESP_OK);
  if (!tiene_fps && clave_val && strcmp(clave_val, "fps") == 0) {
    tiene_fps = (httpd_query_key_value(buf, "val", valor, sizeof(valor)) == ESP_OK);
  }
  if (tiene_fps) {
    aplica_fps(atoi(valor));
    Serial.printf("FPS_MAX -> %d\n", (int)fps_actual);
  }

  bool tiene_cal = (httpd_query_key_value(buf, "calidad", valor, sizeof(valor)) == ESP_OK) ||
                   (httpd_query_key_value(buf, "q", valor, sizeof(valor)) == ESP_OK);
  if (!tiene_cal && clave_val && strcmp(clave_val, "calidad") == 0) {
    tiene_cal = (httpd_query_key_value(buf, "val", valor, sizeof(valor)) == ESP_OK);
  }
  if (tiene_cal) {
    aplica_calidad(atoi(valor));
    Serial.printf("Calidad -> %d\n", calidad_actual);
  }
  return cambio_res;
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

  // Intentar primero con PSRAM (si el chip lo reporta). Si el init falla
  // (PSRAM no real o no disponible), caer a DRAM + QVGA automaticamente.
  bool usar_psram = (ESP.getPsramSize() > 0);
  if (usar_psram) {
    c.frame_size = FRAMESIZE_VGA;
    c.fb_location = CAMERA_FB_IN_PSRAM;
    c.fb_count = 2;
    c.grab_mode = CAMERA_GRAB_LATEST;
  } else {
    c.frame_size = FRAMESIZE_QVGA;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.fb_count = 1;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  }
  c.jpeg_quality = JPEG_QUALITY;

  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK && usar_psram) {
    // PSRAM reportado pero no utilizable: reintentar en DRAM con QVGA
    Serial.println("PSRAM no utilizable, reintentando en DRAM (QVGA)...");
    c.frame_size = FRAMESIZE_QVGA;
    c.fb_location = CAMERA_FB_IN_DRAM;
    c.fb_count = 1;
    c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
    c.jpeg_quality = JPEG_QUALITY;
    err = esp_camera_init(&c);
  }
  fb_en_dram = (c.fb_location == CAMERA_FB_IN_DRAM);
  fb_count_actual = c.fb_count;
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
    if (s->set_brightness) s->set_brightness(s, 1);
    if (s->set_saturation) s->set_saturation(s, 0);
    if (s->set_ae_level) s->set_ae_level(s, 0);     // 0 evita obturador largo en interiores
    if (s->set_aec2) s->set_aec2(s, 1);             // AE mejorado con movimiento
    if (s->set_gain_ctrl) s->set_gain_ctrl(s, 1);
    if (s->set_gainceiling) s->set_gainceiling(s, (gainceiling_t)4);
    if (s->set_contrast) s->set_contrast(s, 1);
    if (s->set_sharpness) s->set_sharpness(s, 2);
    if (s->set_whitebal) s->set_whitebal(s, 1);
    if (s->set_awb_gain) s->set_awb_gain(s, 1);
    if (s->set_wb_mode) s->set_wb_mode(s, 0);
    if (s->set_lenc) s->set_lenc(s, 1);
    if (s->set_bpc) s->set_bpc(s, 1);
    if (s->set_wpc) s->set_wpc(s, 1);
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
             " fb_count=" + String(fb_count_actual) +
             " fps=" + String((int)fps_actual) +
             " calidad=" + String(calidad_actual) +
             " rssi=" + String(WiFi.RSSI()) + " dBm\n";
  b += "video:   http://" + ip + ":81/stream?res=" + String(nombre_res(res_actual)) +
       "&fps=" + String((int)fps_actual) + "&calidad=" + String(calidad_actual) + "\n";
  b += "foto:    http://" + ip + "/jpg\n";
  b += "res:     /res?val=VGA (QVGA|VGA|SVGA)\n";
  b += "fps:     /fps?val=25 (0=max .. 60)\n";
  b += "calidad: /calidad?val=16 (10..40)\n";
  b += "config:  /config?res=VGA&fps=25&calidad=16\n";
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
  aplica_params_query(req, "res");
  return atiende_raiz(req);
}

static esp_err_t atiende_fps(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  aplica_params_query(req, "fps");
  return atiende_raiz(req);
}

static esp_err_t atiende_calidad(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  aplica_params_query(req, "calidad");
  return atiende_raiz(req);
}

static esp_err_t atiende_config(httpd_req_t *req) {
  Serial.printf("> GET %s\n", req->uri);
  aplica_params_query(req, NULL);
  return atiende_raiz(req);
}


static esp_err_t atiende_stream(httpd_req_t *req) {
  Serial.printf("> GET %s (stream inicio)\n", req->uri);
  bool cambio_res = aplica_params_query(req, NULL);
  if (cambio_res) {
    // Descarta 1 frame en cola para emitir ya la resolucion recien aplicada.
    camera_fb_t *tmp = esp_camera_fb_get();
    if (tmp) esp_camera_fb_return(tmp);
  }

#ifdef TCP_NODELAY
  int fd = httpd_req_to_sockfd(req);
  if (fd >= 0) {
    int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  }
#endif

  esp_err_t r = httpd_resp_set_type(req, STREAM_TIPO);
  if (r != ESP_OK) return r;
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Cache-Control", "no-store");

  uint8_t fallos = 0;
  while (true) {
    if (WiFi.status() != WL_CONNECTED) break;
    uint32_t t0 = millis();
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb || fb->format != PIXFORMAT_JPEG) {
      if (fb) esp_camera_fb_return(fb);
      if (++fallos > 10) break;
      delay(25);
      continue;
    }
    fallos = 0;

    const char *payload_ptr = (const char *)fb->buf;
    size_t payload_len = fb->len;
    if (fb_count_actual == 1) {
      // Sin PSRAM (1 buffer DMA): copiar a tx_buf y devolver fb antes del envio
      // TCP para que el sensor capture el siguiente frame mientras transmite el Wi-Fi.
      if (!tx_buf) {
        tx_buf = (uint8_t *)malloc(32768);
        if (tx_buf) tx_buf_cap = 32768;
      }
      if (tx_buf && fb->len <= tx_buf_cap) {
        memcpy(tx_buf, fb->buf, fb->len);
        payload_ptr = (const char *)tx_buf;
        esp_camera_fb_return(fb);
        fb = NULL;
      }
    }

    char cab[112];
    int n_cab = snprintf(cab, sizeof(cab), STREAM_CABECERA, (unsigned int)payload_len);
    esp_err_t err = ESP_OK;
    if (n_cab > 0) {
      err = httpd_resp_send_chunk(req, cab, n_cab);
    }
    if (err == ESP_OK) {
      err = httpd_resp_send_chunk(req, payload_ptr, payload_len);
    }
    if (fb) {
      esp_camera_fb_return(fb);
      fb = NULL;
    }
    if (err != ESP_OK) {
      Serial.println("Stream desconectado por cliente");
      return ESP_FAIL;
    }

    int fps_obj = fps_actual;
    if (fps_obj > 0) {
      uint32_t intervalo = 1000 / (uint32_t)fps_obj;
      uint32_t tardo = millis() - t0;
      if (tardo < intervalo) delay(intervalo - tardo);
      else delay(1);
    } else {
      delay(1);
    }
  }
  httpd_resp_send_chunk(req, NULL, 0);
  return ESP_OK;
}

static void arranca_servidores() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.core_id = 1;
  cfg.send_wait_timeout = 5;
  cfg.recv_wait_timeout = 10;
  cfg.lru_purge_enable = true;
  cfg.max_open_sockets = 3;
  cfg.max_uri_handlers = 10;

  httpd_uri_t uri_raiz = { "/", HTTP_GET, atiende_raiz, NULL };
  httpd_uri_t uri_jpg = { "/jpg", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_foto = { "/foto", HTTP_GET, atiende_jpg, NULL };
  httpd_uri_t uri_res = { "/res", HTTP_GET, atiende_res, NULL };
  httpd_uri_t uri_fps = { "/fps", HTTP_GET, atiende_fps, NULL };
  httpd_uri_t uri_calidad = { "/calidad", HTTP_GET, atiende_calidad, NULL };
  httpd_uri_t uri_config = { "/config", HTTP_GET, atiende_config, NULL };
  httpd_uri_t uri_stream = { "/stream", HTTP_GET, atiende_stream, NULL };

  if (httpd_start(&servidor_ctrl, &cfg) == ESP_OK) {
    httpd_register_uri_handler(servidor_ctrl, &uri_raiz);
    httpd_register_uri_handler(servidor_ctrl, &uri_jpg);
    httpd_register_uri_handler(servidor_ctrl, &uri_foto);
    httpd_register_uri_handler(servidor_ctrl, &uri_res);
    httpd_register_uri_handler(servidor_ctrl, &uri_fps);
    httpd_register_uri_handler(servidor_ctrl, &uri_calidad);
    httpd_register_uri_handler(servidor_ctrl, &uri_config);
    Serial.printf("Control en http://%s:%d/\n", WiFi.localIP().toString().c_str(), cfg.server_port);
  } else {
    Serial.println("No pude arrancar el servidor de control");
  }

  // Stream MJPEG aislado en su propio servidor HTTP (puerto 81, Core 1).
  // El handler de stream SOLO se registra aqui: esp_http_server atiende con un
  // solo hilo, y registrarlo tambien en el 80 haria que un visor de video
  // bloqueara /jpg. Con send_wait_timeout=5 y lru_purge, un cliente colgado
  // expira y se purga solo, sin arrastrar a la camara.
  cfg.server_port = PUERTO_STREAM;
  cfg.ctrl_port += 1;
  cfg.core_id = 1;
  cfg.task_priority = 5;
  cfg.max_open_sockets = 2;
  cfg.max_uri_handlers = 4;
  if (httpd_start(&servidor_stream, &cfg) == ESP_OK) {
    httpd_register_uri_handler(servidor_stream, &uri_stream);
    Serial.printf("Stream http://%s:%d/stream\n", WiFi.localIP().toString().c_str(), PUERTO_STREAM);
  } else {
    Serial.println("No pude arrancar el servidor de stream");
  }
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
  Serial.print("Stream: http://");
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

  // Telemetria minima: RSSI y heap cada 5 s para diagnosticar red/memoria
  // desde el Monitor Serie (el stream vive en su propio hilo httpd).
  static unsigned long t_rssi = 0;
  if (millis() - t_rssi >= 5000) {
    t_rssi = millis();
    Serial.printf("RSSI: %d dBm, heap: %u\n", WiFi.RSSI(), (unsigned int)ESP.getFreeHeap());
  }
  delay(25);
}
