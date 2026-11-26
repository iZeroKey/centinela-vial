# Centinela Control

Aplicación de escritorio en Python y PyQt6 (Fluent UI) para control de asistencia y vigilancia en aula mediante reconocimiento facial en tiempo real desde múltiples cámaras.

## Características principales

- **Múltiples fuentes de cámara**:
  - **Webcam local** (índice OpenCV).
  - **ESP32-CAM**: construcción automática de URL `http://<host>:81/stream?res=...&fps=...&calidad=...` y prueba directa de conectividad.
  - **Stream HTTP MJPEG** genérico.
  - **Teléfono como cámara**: servidor HTTPS integrado con mDNS (`https://centinela.local:8443/telefono`) y streaming binario por WebSocket a baja latencia.
- **Reconocimiento facial**:
  - Detector facial propio (`models/detector_facial.pth`) + embeddings de 512 dimensiones (`InceptionResnetV1` / FaceNet).
  - Confirmación temporal configurable, seguimiento multi-rostro y HUD en vivo.
- **Gestión académica y asistencia**:
  - Alta/edición de alumnos con captura por cámara o imagen, cursos/secciones, franjas horarias, tolerancia de tardanzas e historial diario en SQLite.
- **Integración LCD WiFi**:
  - Envío de estado en tiempo real a un módulo ESP32 con pantalla LCD 16×2 y simulador integrado en la interfaz.

## Estructura del proyecto

```text
app/
  centinela/
    main.py                 Punto de entrada de la aplicación
    core/                   Rutas, configuración, SQLite y gestión de alumnos
    vision/                 Detector facial, embeddings e hilos de cámara
    red/                    Servidor HTTPS/WebSocket y enlace WiFi con ESP32 LCD
    lcd/                    Estado y animaciones para pantalla LCD 16×2
    ui/                     Ventana principal, paneles, diálogos y widgets
  data/                     Datos persistentes (SQLite, config.json, alumnos, fotos, certificados)
assets/                     Iconos y recursos gráficos SVG
firmware/
  CentinelaCamWifi/         Sketch Arduino para ESP32-CAM (carpeta homónima requerida por Arduino IDE)
  CentinelaLcd/             Sketch Arduino para ESP32 receptor LCD 16×2 + LEDs
models/
  detector_facial.pth       Pesos del detector facial
scripts/
  construir_exe.py          Script para generar el ejecutable de Windows (.exe)
tests/
  test_lcd_animacion.py     Pruebas unitarias de animaciones LCD
```

## Requisitos e instalación

- Windows con Python 3.12 (`py -3.12`).
- Opcional: GPU NVIDIA con CUDA para inferencia acelerada.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Para ejecutar la aplicación en modo desarrollo:

```powershell
python app/centinela/main.py
```

## Generar ejecutable para Windows (`.exe`)

Instala PyInstaller si aún no está en `.venv`:

```powershell
python -m pip install pyinstaller
```

Luego ejecuta el script de compilación:

```powershell
python scripts/construir_exe.py --limpiar
```

El ejecutable se generará en `dist/CentinelaControl/CentinelaControl.exe`, con su carpeta `data/` editable junto al binario.

Opciones útiles de `scripts/construir_exe.py`:

- `--onefile`: empaqueta todo en un único `.exe` (arranque más lento que el modo carpeta por defecto).
- `--consola`: mantiene la consola visible para depuración.
- `--con-datos`: copia la carpeta `app/data/` actual dentro de `dist/CentinelaControl/data/`.
- `--incluir-facenet`: incluye el checkpoint local de FaceNet (`vggface2`) para funcionar sin conexión en el primer arranque.
- `--dry-run`: genera el icono `.ico`, valida dependencias y muestra el comando de PyInstaller sin compilar.

## Verificación rápida

```powershell
python app/centinela/main.py --check
python tests/test_lcd_animacion.py
```
