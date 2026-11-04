# Centinela Vial — asistencia y vigilancia v6

Aplicación de escritorio en Python/PyQt6 para reconocer alumnos mediante una
o varias cámaras, confirmar la identidad durante un tiempo configurable y
registrar la asistencia del día. El paquete documentado corresponde al commit
`ca32ef98` (`feat: add stable v6 attendance package`).

## 1. Requisitos

- Windows con Python 3.12 disponible como `py -3.12`.
- Cámara local o ESP32-CAM accesible por red.
- El modelo `models/ocular_v2/detector_seg2/best.pth` incluido en el proyecto.
- Las dependencias de `requirements-stable-v6.txt`.

La primera ejecución puede descargar los pesos de FaceNet (`vggface2`) si no
están disponibles localmente. El reconocimiento puede funcionar con CPU, pero
GPU/CUDA reduce el tiempo de inferencia.

## 2. Instalación y ejecución

Desde la raíz del repositorio, en PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements-stable-v6.txt
python app/asistencia_v6/main.py
```

La aplicación inicia con las cámaras detenidas. Primero se configura una
cámara en `Configuración` y luego se pulsa `Iniciar cámaras`.

## 3. Configuración de cámaras

Para una ESP32-CAM, el firmware expone el stream MJPEG en:

```text
http://IP_DEL_ESP32:81/stream
```

En la configuración de la aplicación se selecciona `Stream HTTP (ESP32)` y se
introduce esa URL. El sketch está en
`app/firmware/camara/CentinelaCamWifi/CentinelaCamWifi.ino`.

La cámara local usa un índice numérico de OpenCV, normalmente `0` o `1`. El
botón de prueba de cámara comprueba la conexión sin cargar los modelos de
reconocimiento.

## 4. Flujo principal de la aplicación

1. `app/asistencia_v6/main.py` crea el `QApplication` y llama a `main()`.
2. `ventana_principal.VentanaPrincipal` carga la configuración, inicializa
   SQLite, sincroniza los alumnos y carga las referencias con embedding.
3. Cada cámara activa se ejecuta en un `camara_hilo.CamaraHilo`, que lee frames
   desde webcam o MJPEG HTTP sin bloquear la interfaz.
4. `CamaraHilo._procesar()` llama a `rostros.pipeline_embeddings()`.
5. `rostros.py` detecta caras con `detector_seg2`, recorta cada cara, calcula
   el embedding FaceNet de 512 dimensiones y devuelve una lista de resultados.
6. El hilo compara cada embedding con las referencias activas mediante producto
   punto/similitud coseno, crea tracks temporales y emite el progreso de
   confirmación.
7. `VentanaPrincipal._on_progreso()` actualiza la barra visual. Al completar
   el tiempo de confirmación, `_on_confirmada()` evita duplicados y llama a
   `db.registrar_entrada()`.
8. La pestaña Historial consulta `db.obtener_estado_fecha()` y muestra entrada,
   salida y permanencia.

## 5. Módulos y funciones importantes

### `app/asistencia_v6/ventana_principal.py`

- `main()`: punto de entrada gráfico.
- `VentanaPrincipal._cargar_referencias()`: carga alumnos activos con
  embedding y prepara las referencias para las cámaras.
- `VentanaPrincipal._recargar_alumnos()`: lanza el trabajo de embeddings
  faltantes sin congelar la interfaz.
- `VentanaPrincipal._iniciar_camaras()` / `_detener_camaras()`: controla los
  hilos de cámara.
- `VentanaPrincipal._on_progreso()`: refleja la confirmación de una identidad.
- `VentanaPrincipal._on_confirmada()`: registra la entrada con protección
  contra duplicados.

### `app/asistencia_v6/camara_hilo.py`

- `CamaraHilo.run()`: bucle de captura, reconexión y emisión de frames.
- `CamaraHilo._leer_frame()`: selecciona webcam local o stream HTTP.
- `CamaraHilo._leer_frame_mjpeg()`: extrae imágenes JPEG del stream MJPEG.
- `CamaraHilo._procesar()`: reconocimiento, matching, tracks y confirmación.
- `probar_fuente()`: prueba una fuente de cámara sin inferencia.

### `app/asistencia_v6/rostros.py`

- `cargar_detector()`: carga una sola vez el detector propio.
- `detectar_caras()`: obtiene todas las cajas de caras del frame.
- `cargar_embedding_modelo()`: carga InceptionResnetV1/FaceNet.
- `extraer_embedding()`: genera el vector normalizado de una cara.
- `pipeline_embeddings()`: encadena detección, recorte y embedding.

### `app/asistencia_v6/gestor_alumnos.py`

Es la puerta de entrada para los datos de alumnos y embeddings.

- `listar()` / `obtener()`: consultas desde `alumnos.json`.
- `agregar()`: guarda alumno, foto y embedding.
- `editar()`: actualiza datos y, si cambia la foto, regenera el embedding.
- `cambiar_estado()`: baja lógica o reactivación.
- `regenerar_embedding()`: recalcula un embedding desde la foto guardada.
- `completar_embeddings()`: procesa solo alumnos activos sin embedding.
- `sincronizar_db()`: refleja el JSON en SQLite.

El JSON se escribe de forma atómica y las operaciones que lo modifican usan un
lock para evitar lecturas/escrituras simultáneas durante la vigilancia.

### `app/asistencia_v6/db.py`

SQLite se guarda en `app/data/centinela/centinela.sqlite`.

- `init_db()`: crea las tablas `alumnos`, `asistencia` y `permanencia`.
- `sincronizar_alumnos()`: inserta o actualiza alumnos desde JSON.
- `registrar_entrada()` / `registrar_salida()`: gestiona marcas y permanencia.
- `obtener_estado_fecha()`: devuelve el resumen de asistencia para la UI.
- `ventana_abierta()`: evita registrar una entrada duplicada reciente.

### `app/asistencia_v6/hilo_trabajo.py`

`Trabajo` ejecuta guardado, regeneración de embeddings y pruebas de cámara en
un `QThread`, emitiendo `(ok, mensaje)` al terminar.

## 6. Datos y archivos generados

```text
app/data/asistencia/alumnos.json             fuente de verdad de alumnos
app/data/asistencia/referencia/              fotos de referencia
app/data/centinela/centinela.sqlite          asistencia e historial
models/ocular_v2/detector_seg2/best.pth      detector facial propio
```

Los IDs internos tienen formato `E001`, `E002`, etc. No se muestran en la
interfaz. Una baja es lógica: el alumno deja de reconocerse y de aparecer en
listas activas, pero su historial se conserva.

## 7. Estado y funciones para LCD 16x2

La integración aislada está en `app/asistencia_v6/estado_lcd.py`. No controla
hardware, no recibe frames y no modifica el firmware de la ESP32-CAM. Mantiene
el último estado publicado por la aplicación y permite recuperarlo mediante
getters sin argumentos.

### Publicar estados desde la aplicación

Estas funciones actualizan el estado compartido. Las llamará la integración de
la aplicación cuando reciba sus eventos normales de progreso y confirmación:

- `actualizar_esperando(mensaje="ESPERANDO...")`
- `actualizar_procesamiento(nombre="", apellidos="", progreso=0.0)`
- `actualizar_confirmado(nombre="", apellidos="")`
- `actualizar_incompleto(nombre="", apellidos="", motivo="INCOMPLETO")`
- `actualizar_desconocido(mensaje="INTENTE DE NUEVO")`

El progreso acepta tanto `0..1` como `0..100` y se limita automáticamente a
`0..100`.

### Recuperar datos sin enviar parámetros

La integración de la otra persona puede llamar directamente:

```python
from estado_lcd import obtener_datos_lcd, obtener_filas_lcd

datos = obtener_datos_lcd()
fila_1, fila_2 = obtener_filas_lcd()
```

`obtener_datos_lcd()` devuelve una copia del estado completo:

```python
{
    "estado": "procesando",
    "nombre": "Erick",
    "apellidos": "Flores",
    "progreso": 75.0,
    "incompleto": False,
    "motivo": "",
    "actualizado_en": "2026-11-04T18:00:00",
    "fila_1": "Erick           ",
    "fila_2": "PROCESANDO  75% "
}
```

Getters separados disponibles, todos sin argumentos:

- `obtener_filas_lcd()`: devuelve las dos filas listas para escribir.
- `obtener_nombre_lcd()`: nombre actual, ajustado a 16 caracteres.
- `obtener_apellidos_lcd()`: apellidos actuales, ajustados a 16 caracteres.
- `obtener_porcentaje_lcd()`: fila `PROCESANDO xx%`.
- `obtener_incompleto_lcd()`: fila `INCOMPLETO` o una fila vacía.
- `obtener_estado_lcd()`: `esperando`, `procesando`, `confirmado`,
  `incompleto` o `desconocido`.

También existe `obtener_log_lcd()`, sin argumentos, que devuelve una copia de
los últimos 100 estados publicados. `limpiar_log_lcd()` vacía ese historial.
El acceso está protegido con un lock y los getters devuelven copias, por lo
que un hilo de integración no modifica accidentalmente el estado compartido.

### Estados mostrados

```text
esperando:    CENTINELA VIAL / ESPERANDO...
procesando:   nombre       / PROCESANDO 75%
confirmado:   nombre       / apellidos
incompleto:   nombre       / INCOMPLETO
desconocido:  NO IDENTIFICADO / INTENTE DE NUEVO
```

El módulo no está conectado todavía al flujo de cámara: esa conexión queda
deliberadamente en la capa de integración. La persona encargada puede llamar
`actualizar_procesamiento()` desde el callback de progreso y
`actualizar_confirmado()` desde el callback de confirmación, sin alterar la
lectura del stream ni el firmware de la cámara.

### Importante sobre `apellidos`

El esquema actual de la app solo guarda `nombre`; no existe todavía un campo
`apellidos` en el formulario, JSON ni tabla SQLite. El módulo acepta apellidos
cuando la integración se los proporcione; si no existen, devuelve una cadena
vacía y el estado `confirmado` muestra `ASISTENCIA OK` en la segunda fila.

## 8. Solución de problemas

- `No se pudo abrir la cámara`: revisar índice local, permisos y que ninguna
  otra aplicación esté usando la webcam.
- Stream ESP32 sin frames: abrir primero `http://IP_DEL_ESP32/` y comprobar la
  URL `:81/stream`, la red y la alimentación de la placa.
- Alumno con `sin embedding`: revisar que exista la foto de referencia y usar
  `Recargar alumnos (embeddings faltantes)`.
- Reconocimiento lento: reducir el número de cámaras, aumentar el intervalo
  de procesamiento o usar un equipo con GPU.
- No borrar manualmente el JSON mientras la app está abierta: es la fuente de
  verdad y se sincroniza con SQLite durante altas, ediciones y recargas.

## 9. Verificación rápida

```powershell
$files = Get-ChildItem app\asistencia_v6 -Filter *.py | ForEach-Object { $_.FullName }
python -m py_compile $files
```

La compilación valida el código actual sin modificarlo. La prueba completa de
reconocimiento requiere cámara, modelo y, para ESP32-CAM, red disponible.
