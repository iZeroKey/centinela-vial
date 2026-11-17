#!/usr/bin/env python3
"""
Centinela Control — Ventana principal v6

Interfaz de escritorio en Python + PyQt6.
- Vigilancia: paneles dinámicos según las cámaras activas configuradas
  (1..N), reconocimiento de múltiples rostros con confirmación temporal.
- Historial: asistencias por día (sin IDs visibles).
- Alumnos: galería con alta, edición (datos + foto por cámara o fichero,
  con regeneración automática de embeddings) y baja lógica.
- Configuración: cámaras, parámetros de asistencia y módulos de vigilancia.

Los alumno_id son internos y nunca se muestran en la interfaz.
"""

import os, sys, time, csv
import threading
from datetime import datetime, date

import cv2
import numpy as np

from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QRect, QRectF, QEvent
from PyQt6.QtGui import (QImage, QPixmap, QFont, QColor, QIcon, QShortcut, QKeySequence,
                         QPainter, QPen, QBrush, QPainterPath)
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QBoxLayout,
    QLabel, QFrame, QGroupBox, QHeaderView, QSplitter,
    QDateEdit, QGridLayout, QGraphicsOpacityEffect, QTableWidgetItem,
    QFileDialog,
)
from qfluentwidgets import (
    FluentWindow, setTheme, setThemeColor, Theme, isDarkTheme, FluentIcon as FIcon,
    PushButton, PrimaryPushButton, LineEdit, ComboBox, SpinBox, DoubleSpinBox,
    CheckBox, TableWidget, CardWidget, ElevatedCardWidget, HeaderCardWidget, TitleLabel, SubtitleLabel, BodyLabel,
    CaptionLabel, ScrollArea, SmoothScrollArea, ProgressBar, FlowLayout,
    InfoBar, InfoBarPosition, SwitchButton,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camara_hilo import CamaraHilo
from hilo_trabajo import Trabajo
from ui_util import ndarray_a_pixmap
from dialogo_alumno import DialogoAlumno
from panel_configuracion import PanelConfiguracion
from panel_clases import PanelClases
from panel_dispositivos import PanelDispositivos
from simulador_lcd import SimuladorLCD
from widgets_historial import (
    TarjetaGrupoCurso, TablaSeccion, EstadoVacioHistorial, hhmm,
)
import config as config_app
import gestor_alumnos as gestor
import db as db_centinela
import estado_lcd
from enlace_wifi import EnlaceWifi
from servidor_web import ServidorWeb

# Hueco de video integrado a la tarjeta Fluent (transparente, sin borde).
ESTILO_VIDEO = "background: transparent; border: none; color: #a5a5a5; font-size: 14px;"
RADIO_VIDEO = 10


class _EventoCrearHiloTel(QEvent):
    """Crea el CamaraHilo de un telefono en el hilo GUI (via postEvent).

    El servidor web recibe los frames en SU hilo, pero los QThread de
    inferencia deben vivir en el hilo GUI (afinidad de sus senales).
    """

    TIPO = QEvent.Type(QEvent.registerEventType())

    def __init__(self, tel_id, caja, listo):
        super().__init__(self.TIPO)
        self.tel_id = tel_id
        self.caja = caja      # dict de salida: {"hilo": CamaraHilo}
        self.listo = listo    # threading.Event


def _color_acento_windows(fallback="#2dd4bf"):
    """Lee el color de acento del sistema (registro DWM) para integrar la app a Windows."""
    try:
        import winreg
        clave = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
        valor, _ = winreg.QueryValueEx(clave, "AccentColor")
        winreg.CloseKey(clave)
        # El DWORD llega en formato AABBGGRR
        r, g, b = valor & 0xFF, (valor >> 8) & 0xFF, (valor >> 16) & 0xFF
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return fallback


ACENTO = _color_acento_windows()


class FilaProgreso(ElevatedCardWidget):
    """Fila de verificacion de un alumno: foto de referencia, datos, barra y asistencia."""

    def __init__(self, alumno_id, nombre, parent=None):
        super().__init__(parent)
        self.alumno_id = alumno_id
        self.track_keys = set()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)

        # Foto de referencia
        self.lbl_foto = QLabel()
        self.lbl_foto.setFixedSize(56, 56)
        self.lbl_foto.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_foto.setStyleSheet(
            "background: #0b0f14; border: 1px solid #232d3a; border-radius: 10px; color: #5a6573; font-size: 11px;")
        self._cargar_foto(alumno_id, 56)
        layout.addWidget(self.lbl_foto)

        # Columna de datos (compacta)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.lbl_nombre = QLabel(nombre)
        self.lbl_nombre.setStyleSheet(
            "color: #e6edf3; font-weight: 600; background: transparent; border: none; font-size: 14px;")
        col.addWidget(self.lbl_nombre)

        self.lbl_curso = QLabel("")
        self.lbl_curso.setStyleSheet(
            "color: #8b97a8; background: transparent; border: none; font-size: 12px;")
        col.addWidget(self.lbl_curso)

        fila_barra = QHBoxLayout()
        fila_barra.setSpacing(8)
        self.barra = ProgressBar()
        self.barra.setMaximum(100)
        self.barra.setTextVisible(False)
        self.barra.setFixedHeight(8)
        fila_barra.addWidget(self.barra, 1)
        self.lbl_estado = QLabel("Verificando...")
        self.lbl_estado.setStyleSheet(
            "color: #cbd5e1; background: transparent; border: none; font-size: 12px; font-weight: 500;")
        self.lbl_estado.setMinimumWidth(130)
        fila_barra.addWidget(self.lbl_estado)
        col.addLayout(fila_barra)

        self.lbl_asistencia = QLabel("")
        self.lbl_asistencia.setStyleSheet(
            "color: #34d399; background: transparent; border: none; font-size: 12px; font-weight: 500;")
        col.addWidget(self.lbl_asistencia)

        layout.addLayout(col, 1)

        self._cargar_datos(alumno_id)

    def _cargar_foto(self, alumno_id, size=48):
        if not alumno_id:
            self.lbl_foto.setText("?")
            return
        try:
            import gestor_alumnos as gestor
            ruta = gestor.ruta_foto_absoluta(
                (gestor.obtener(alumno_id) or {}).get("foto_referencia"))
            if ruta and os.path.isfile(ruta):
                pix = QPixmap(ruta).scaled(
                    size, size, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                self.lbl_foto.setPixmap(pix)
                return
        except Exception:
            pass
        self.lbl_foto.setText("?")

    def _cargar_datos(self, alumno_id):
        if not alumno_id:
            self.lbl_curso.setText("")
            self.lbl_asistencia.setText("")
            return
        try:
            import gestor_alumnos as gestor
            import db as db_centinela
            alum = gestor.obtener(alumno_id)
            if alum:
                nombre = str(alum.get("nombre", "") or "").strip()
                apellidos = str(alum.get("apellidos", "") or "").strip()
                completo = " ".join(x for x in (nombre, apellidos) if x)
                if completo:
                    self.lbl_nombre.setText(completo)
                # inscripciones del alumno: "curso·seccion" de cada una
                insc = [dict(i) for i in (alum.get("inscripciones") or [])
                        if isinstance(i, dict) and str(i.get("curso", "") or "").strip()]
                if not insc:
                    cs = str(alum.get("curso", "") or "").strip()
                    ss = str(alum.get("seccion", "") or "").strip()
                    if cs or ss:
                        insc = [{"curso": cs, "seccion": ss}]
                self.lbl_curso.setText(" · ".join(
                    "·".join(x for x in (i["curso"], i.get("seccion", "")) if x)
                    for i in insc))
            # asistencia del dia
            try:
                hoy = db_centinela.obtener_estado_hoy()
                for row in hoy:
                    if row.get("alumno_id") == alumno_id:
                        marcas = row.get("marcas", 0)
                        entrada = row.get("entrada_abierta") or row.get("ultima_salida")
                        if marcas and marcas > 0:
                            texto = "Presente"
                            if entrada:
                                texto += f"  ({str(entrada)[11:16]})"
                            self.lbl_asistencia.setText(texto)
                        else:
                            self.lbl_asistencia.setText("Sin registro hoy")
                        break
                else:
                    self.lbl_asistencia.setText("Sin registro hoy")
            except Exception:
                self.lbl_asistencia.setText("")
        except Exception:
            pass

    def actualizar(self, progreso, texto_estado):
        self.barra.setValue(int(progreso * 100))
        self.lbl_estado.setText(texto_estado)

    def confirmado(self):
        self.barra.setValue(100)
        self.lbl_estado.setText("Confirmado")
        self.lbl_estado.setStyleSheet(
            "color: #34d399; background: transparent; border: none; font-size: 12px; font-weight: 600;")


class TarjetaAlumno(CardWidget):
    """Tarjeta clickable de alumno para la galeria (avatar + nombre).

    Usa el pintado nativo de CardWidget (fondo, hover y pressed del tema
    Fluent); no aplica QSS manual. El ancho es adaptativo: las filas de la
    galeria llenan el espacio disponible del panel.
    """

    clickeado = pyqtSignal(dict)

    ESPACIO = 16      # separacion horizontal de FlowLayout en la galeria
    MIN_ANCHO = 176   # ancho minimo/objetivo de cada tarjeta
    MAX_AVATAR = 124

    def __init__(self, alumno, ancho=MIN_ANCHO, parent=None):
        super().__init__(parent)
        self.alumno = alumno
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        nombre = str(alumno.get("nombre", "") or "").strip()
        apellidos = str(alumno.get("apellidos", "") or "").strip()
        self.nombre_completo = " ".join(x for x in (nombre, apellidos) if x) or "Sin nombre"

        insc = [i for i in (alumno.get("inscripciones") or [])
                if isinstance(i, dict) and str(i.get("curso", "") or "").strip()]
        meta = " · ".join(
            "·".join(x for x in (str(i.get("curso", "") or "").strip(),
                                 str(i.get("seccion", "") or "").strip()) if x)
            for i in insc)
        if alumno.get("estado") == "inactivo":
            meta = "Inactivo" + (f" · {meta}" if meta else "")
        self.texto_meta = meta or "Sin datos"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)
        layout.addStretch(1)

        self.lbl_foto = QLabel()
        self.lbl_foto.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_foto, 0, Qt.AlignmentFlag.AlignHCenter)

        self.lbl_nombre = BodyLabel()
        self.lbl_nombre.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_nombre.setWordWrap(False)
        layout.addWidget(self.lbl_nombre)

        self.lbl_meta = CaptionLabel()
        self.lbl_meta.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_meta.setWordWrap(False)
        layout.addWidget(self.lbl_meta)
        layout.addStretch(1)

        self.set_ancho(int(ancho), inicial=True)

    @classmethod
    def tam_avatar(cls, ancho):
        return max(96, min(cls.MAX_AVATAR, ancho - 44))

    @classmethod
    def tam_alto(cls, avatar):
        return avatar + 70

    def set_ancho(self, ancho, inicial=False):
        """Redimensiona la tarjeta y regenera avatar/texto elidido."""
        ancho = max(self.MIN_ANCHO, int(ancho))
        if not inicial and ancho == self.ancho:
            return
        self.ancho = ancho
        avatar = self.tam_avatar(ancho)
        self.alto = self.tam_alto(avatar)
        self.setFixedSize(ancho, self.alto)
        self.lbl_foto.setFixedSize(avatar, avatar)
        self.lbl_foto.setPixmap(self._avatar_pixmap(self.alumno, avatar))
        fm = self.lbl_nombre.fontMetrics()
        self.lbl_nombre.setText(fm.elidedText(
            self.nombre_completo, Qt.TextElideMode.ElideRight, ancho - 28))
        self.lbl_nombre.setToolTip(self.nombre_completo)
        self.lbl_meta.setText(self.texto_meta)
        self.lbl_meta.setToolTip(self.texto_meta)

    def _avatar_pixmap(self, alumno, size):
        """Avatar con esquinas redondeadas dibujado con QPainter (foto o icono)."""
        out = QPixmap(size, size)
        out.fill(Qt.GlobalColor.transparent)
        pintor = QPainter(out)
        pintor.setRenderHint(QPainter.RenderHint.Antialiasing)

        borde = 1.0
        lado = size - borde * 2
        radio = size * 0.24  # esquinas redondeadas estilo Fluent
        forma = QPainterPath()
        forma.addRoundedRect(QRectF(borde, borde, lado, lado), radio, radio)

        ruta = gestor.ruta_foto_absoluta(alumno.get("foto_referencia"))
        pix = QPixmap(ruta) if ruta and os.path.isfile(ruta) else QPixmap()
        if not pix.isNull():
            lado_src = min(pix.width(), pix.height())
            x = (pix.width() - lado_src) // 2
            y = (pix.height() - lado_src) // 2
            pintor.setClipPath(forma)
            pintor.drawPixmap(
                QRectF(borde, borde, lado, lado), pix,
                QRectF(x, y, lado_src, lado_src))
            pintor.setClipping(False)
        else:
            fondo = QColor(255, 255, 255, 20) if isDarkTheme() else QColor(0, 0, 0, 12)
            pintor.fillPath(forma, fondo)
            FIcon.PEOPLE.icon().paint(
                pintor,
                QRect(int(size * 0.30), int(size * 0.30),
                      int(size * 0.40), int(size * 0.40)),
                Qt.AlignmentFlag.AlignCenter)

        pintor.setPen(QPen(
            QColor(255, 255, 255, 30) if isDarkTheme() else QColor(0, 0, 0, 35), borde))
        pintor.drawPath(forma)
        pintor.end()
        return out

    def mousePressEvent(self, event):
        self.clickeado.emit(self.alumno)
        super().mousePressEvent(event)


class VentanaPrincipal(FluentWindow):
    TIEMPO_SIN_PROGRESO = 2.5

    def __init__(self):
        super().__init__()
        setTheme(Theme.DARK)
        setThemeColor(ACENTO)  # integra el acento del sistema (Windows)
        self.setWindowTitle("Centinela Control — Asistencia y vigilancia de aula")
        self.resize(1560, 960)

        self.cfg = config_app.cargar()
        estado_lcd.configurar_umbral_nombre_lcd(
            self.cfg.get("asistencia", {}).get("umbral_lcd_nombre", 0.30))
        estado_lcd.configurar_duracion_resultado_lcd(
            self.cfg.get("asistencia", {}).get("duracion_resultado_lcd", 3.0))

        self.referencias = []
        self.hilos = {}
        self.labels_video = {}
        self.labels_estado_cam = {}
        self.filas_progreso = {}
        self._ultimo_progreso_track = {}
        self.layout_rostros = None
        self._ultimo_registro = {}
        self._intervalo_registro = float(self.cfg["asistencia"]["intervalo_antiduplicado"])
        self._camaras_pausadas = []
        self._enlace_wifi = None
        self._lcd_track_prioritario = None
        self._lcd_area_prioritaria = 0.0
        self._track_a_alumno = {}
        self._alumno_tracks = {}
        self._trabajo_recarga = None
        self._info_alumnos = {}          # alumno_id -> nombre/inscripciones
        self._avisos_fuera_horario = {}  # alumno_id -> ts del ultimo aviso
        self._servidor_web = None
        self.hilos_telefono = {}         # tel_id -> CamaraHilo en modo telefono
        self._lock_tel = threading.Lock()

        db_centinela.init_db()
        gestor.sincronizar_db()
        self._cargar_referencias()

        self._construir_ui()
        self._temporizador_tracks = QTimer(self)
        self._temporizador_tracks.setInterval(250)
        self._temporizador_tracks.timeout.connect(self._revisar_tracks_perdidos)
        self._temporizador_tracks.start()
        self._actualizar_galeria()
        self._actualizar_historial()
        self._configurar_atajos()
        self.installEventFilter(self)

    _MAPA_TOAST = {
        "exito": InfoBar.success,
        "success": InfoBar.success,
        "warning": InfoBar.warning,
        "advertencia": InfoBar.warning,
        "error": InfoBar.error,
    }

    def mostrar_toast(self, mensaje, tipo="info", duracion_ms=3000):
        """Notificación Fluent (InfoBar) en la esquina superior derecha."""
        nueva = self._MAPA_TOAST.get(tipo, InfoBar.info)
        nueva(title="", content=mensaje,
              orient=Qt.Orientation.Horizontal, isClosable=True,
              duration=duracion_ms, position=InfoBarPosition.TOP_RIGHT,
              parent=self.window())

    def _configurar_atajos(self):
        QShortcut(QKeySequence("F5"), self, activated=self._atajo_iniciar)
        QShortcut(QKeySequence("F6"), self, activated=self._atajo_detener)
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self._agregar_alumno)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.close)

    def _atajo_iniciar(self):
        if self.btn_iniciar.isEnabled():
            self._iniciar_camaras()

    def _atajo_detener(self):
        if self.btn_detener.isEnabled():
            self._detener_camaras()

    # ---------------- UI base ----------------
    def _construir_ui(self):
        self.tab_vigilancia = self._construir_tab_vigilancia()
        self.tab_historial = self._construir_tab_historial()
        self.tab_alumnos = self._construir_tab_alumnos()
        self.panel_config = PanelConfiguracion(callbacks={
            "camaras_cambiadas": self._on_camaras_cambiadas,
            "parametros_cambiados": self._on_parametros_cambiados,
        })
        self.panel_clases = PanelClases(callbacks={
            "horarios_cambiados": self._on_horarios_cambiados,
            "cursos_cambiadas": self._on_cursos_cambiados,
        })
        # un solo estado de config compartido por ambos paneles: asi ninguna
        # edicion puede sobreescribir la del otro panel con datos viejos
        self.panel_config.cfg = self.panel_clases.cfg

        self.tab_vigilancia.setObjectName("vigilanciaInterface")
        self.tab_historial.setObjectName("historialInterface")
        self.tab_alumnos.setObjectName("alumnosInterface")
        self.panel_clases.setObjectName("clasesInterface")
        self.panel_config.setObjectName("configInterface")

        self.tab_dispositivos = PanelDispositivos(
            obtener_servidor=lambda: self._servidor_web)
        self.tab_dispositivos.setObjectName("dispositivosInterface")

        self.addSubInterface(self.tab_vigilancia, FIcon.VIDEO, "Vigilancia")
        self.addSubInterface(self.tab_historial, FIcon.HISTORY, "Historial")
        self.addSubInterface(self.panel_clases, FIcon.CALENDAR, "Clases")
        self.addSubInterface(self.tab_alumnos, FIcon.PEOPLE, "Alumnos")
        self.addSubInterface(self.tab_dispositivos, FIcon.PHONE, "Dispositivos")
        self.addSubInterface(self.panel_config, FIcon.SETTING, "Configuración")

        self.stackedWidget.currentChanged.connect(self._on_page_changed)

        self.mostrar_toast(
            "Listo. Configure las cámaras en Configuración e inicie la vigilancia.")

    def closeEvent(self, event):
        self._detener_camaras()
        event.accept()

    def event(self, ev):
        """Atiende _EventoCrearHiloTel (marshal del hilo del servidor web)."""
        if isinstance(ev, _EventoCrearHiloTel):
            try:
                self._crear_hilo_telefono_en_gui(ev.tel_id, ev.caja)
            finally:
                ev.listo.set()
            return True
        return super().event(ev)

    def eventFilter(self, obj, event):
        from PyQt6.QtCore import QEvent
        galeria = getattr(self, "scroll_galeria", None)
        temporizador = getattr(self, "_timer_reflow", None)
        if (galeria is not None and temporizador is not None
                and obj is galeria.viewport()
                and event.type() == QEvent.Type.Resize):
            temporizador.start()
        return super().eventFilter(obj, event)

    def _on_page_changed(self, _index):
        try:
            widget = self.stackedWidget.currentWidget()
            if widget is self.tab_historial:
                self._actualizar_historial()
            elif widget is self.tab_alumnos:
                self._actualizar_galeria()
        except Exception as e:
            print("page changed error", repr(e), flush=True)

    def _on_tab_changed(self, idx):
        # FluentWindow notifica cambios de página; mantenemos actualización lazy.
        pass

    def _on_tab_changed(self, idx):
        # FluentWindow notifica cambios de página; mantenemos actualización lazy.
        pass

    # ---------------- Pestaña Vigilancia ----------------
    def _construir_tab_vigilancia(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Izquierda: paneles de cámaras (dinámicos, dentro de scroll)
        self.scroll_paneles = ScrollArea()
        self.scroll_paneles.setWidgetResizable(True)
        self.scroll_paneles.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_paneles.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scroll_paneles.enableTransparentBackground()
        self._reconstruir_paneles()
        splitter.addWidget(self.scroll_paneles)

        # Derecha: controles y rostros en verificación
        panel_derecho = QWidget()
        layout_derecho = QVBoxLayout(panel_derecho)
        layout_derecho.setContentsMargins(8, 0, 0, 0)
        layout_derecho.setSpacing(12)

        grupo_controles = HeaderCardWidget()
        grupo_controles.setTitle("Control")
        grupo_controles.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout_controles = grupo_controles.viewLayout
        layout_controles.setSpacing(10)
        layout_controles.setContentsMargins(14, 6, 14, 12)

        self.btn_iniciar = PushButton("Iniciar cámaras")
        self.btn_iniciar.setObjectName("exito")
        self.btn_iniciar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_iniciar.clicked.connect(self._iniciar_camaras)

        self.btn_detener = PushButton("Detener todas")
        self.btn_detener.setObjectName("peligro")
        self.btn_detener.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_detener.setEnabled(False)
        self.btn_detener.clicked.connect(self._detener_camaras)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(10)
        fila_botones.addWidget(self.btn_iniciar, 1)
        fila_botones.addWidget(self.btn_detener, 1)
        layout_controles.addLayout(fila_botones)

        self.lbl_estado = QLabel("Cámaras: detenidas")
        self._estilo_estado(self.lbl_estado, False)
        layout_controles.addWidget(self.lbl_estado)

        self.btn_recargar = PushButton("Recargar alumnos (embeddings faltantes)")
        self.btn_recargar.setObjectName("secundario")
        self.btn_recargar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recargar.clicked.connect(self._recargar_alumnos)
        layout_controles.addWidget(self.btn_recargar)

        layout_derecho.addWidget(grupo_controles)

        self.simulador_lcd = SimuladorLCD()
        layout_derecho.addWidget(self.simulador_lcd)

        grupo_progreso = HeaderCardWidget()
        grupo_progreso.setTitle("Verificando rostros")
        grupo_progreso.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout_progreso = grupo_progreso.viewLayout
        layout_progreso.setContentsMargins(8, 12, 8, 8)
        layout_progreso.setSpacing(8)

        self.lbl_sin_rostros = QLabel("Sin rostros detectados")
        self.lbl_sin_rostros.setStyleSheet("color:#a5a5a5; font-size: 13px; background: transparent; border: none;")
        layout_progreso.addWidget(self.lbl_sin_rostros)

        self.scroll_rostros_area = ScrollArea()
        self.scroll_rostros_area.setWidgetResizable(True)
        self.scroll_rostros_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_rostros_area.setStyleSheet("background: transparent; border: none;")
        self.scroll_rostros = QWidget()
        self.layout_rostros = QVBoxLayout(self.scroll_rostros)
        self.layout_rostros.setContentsMargins(0, 0, 0, 0)
        self.layout_rostros.setSpacing(8)
        self.layout_rostros.addStretch()
        self.scroll_rostros_area.setWidget(self.scroll_rostros)
        self.scroll_rostros_area.enableTransparentBackground()
        layout_progreso.addWidget(self.scroll_rostros_area)

        grupo_progreso.setMinimumHeight(300)
        layout_derecho.addWidget(grupo_progreso, stretch=1)

        splitter.addWidget(panel_derecho)
        splitter.setSizes([950, 550])
        layout.addWidget(splitter)

        return tab

    def _reconstruir_paneles(self):
        """Crea un panel de video por cada cámara activa del config."""
        contenedor = QWidget()
        grid = QGridLayout(contenedor)
        grid.setContentsMargins(2, 2, 2, 2)
        grid.setSpacing(12)

        activas = [c for c in self.cfg.get("camaras", []) if c.get("activa")]
        self.labels_video.clear()
        self.labels_estado_cam.clear()

        if not activas:
            lbl = QLabel("No hay cámaras activas.\nAgrégalas en la pestaña Configuración.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setMinimumSize(700, 520)
            lbl.setStyleSheet(
                "background: transparent; border: none; color: #a5a5a5; font-size: 15px;"
            )
            grid.addWidget(lbl, 0, 0)
        else:
            cols = 2 if len(activas) > 1 else 1
            for i, cam in enumerate(activas):
                cam_id = cam.get("id")
                grupo = HeaderCardWidget()
                grupo.setTitle(cam.get("nombre") or str(cam_id))
                grupo.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
                lbl_video = QLabel("Sin señal")
                lbl_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_video.setMinimumSize(460, 360)
                lbl_video.setStyleSheet(ESTILO_VIDEO)
                lbl_estado = QLabel("Cámara: detenida")
                self._estilo_estado(lbl_estado, False)
                lbl_estado.setAlignment(Qt.AlignmentFlag.AlignCenter)
                grupo.viewLayout.addWidget(lbl_video, 1)
                grupo.viewLayout.addWidget(lbl_estado, 0)
                # curso individual de la camara (informativo, config)
                curso_cam = str(cam.get("curso", "") or "").strip()
                if curso_cam:
                    lbl_curso = CaptionLabel(f"Curso: {curso_cam}")
                    lbl_curso.setAlignment(Qt.AlignmentFlag.AlignCenter)
                    grupo.viewLayout.addWidget(lbl_curso, 0)
                self.labels_video[cam_id] = lbl_video
                self.labels_estado_cam[cam_id] = lbl_estado
                grid.addWidget(grupo, i // cols, i % cols)
            for c in range(cols):
                grid.setColumnStretch(c, 1)

        # setWidget() borra por sí mismo el contenedor anterior (y sus hijos);
        # los dicts labels_video/labels_estado_cam ya se vaciaron arriba.
        self.scroll_paneles.setWidget(contenedor)
        # el contenedor montado en el scroll vuelve a ser opaco: transparentar
        self.scroll_paneles.enableTransparentBackground()

    def _modulo_asistencia_activo(self):
        return True

    # ---------------- Pestaña Historial ----------------
    def _construir_tab_historial(self):
        """Historial completo: sesiones derivadas de las franjas del curso
        (hasta hoy), con selectores de curso/sección. Sin presets ni
        calendarios: el rango lo define la pestaña Clases."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        grupo = HeaderCardWidget()
        grupo.setTitle("Historial de clases")
        lg = grupo.viewLayout
        # viewLayout de HeaderCardWidget es horizontal por defecto: sin esto,
        # filtros/resumen/matriz quedan LADO A LADO (matriz exprimida a la derecha)
        lg.setDirection(QBoxLayout.Direction.TopToBottom)
        lg.setSpacing(10)

        # fila 1: selectores de curso/sección + detalles + búsqueda + acciones
        fila1 = QHBoxLayout()
        fila1.setSpacing(8)
        self.lbl_curso_hist = CaptionLabel("Curso:")
        self.combo_curso_hist = ComboBox()
        self.combo_curso_hist.setMinimumWidth(200)
        self.lbl_seccion_hist = CaptionLabel("Sección:")
        self.combo_seccion_hist = ComboBox()
        self.combo_seccion_hist.setMinimumWidth(120)
        fila1.addWidget(self.lbl_curso_hist)
        fila1.addWidget(self.combo_curso_hist)
        fila1.addWidget(self.lbl_seccion_hist)
        fila1.addWidget(self.combo_seccion_hist)
        # switch de detalles: ON = hora en las celdas, OFF = solo icono
        self._hist_detalles = True
        self.sw_detalles_hist = SwitchButton("Detalles")
        self.sw_detalles_hist.setOnText("Detalles")
        self.sw_detalles_hist.setOffText("Detalles")
        self.sw_detalles_hist.setChecked(True)
        self.sw_detalles_hist.checkedChanged.connect(self._on_detalles_hist)
        fila1.addSpacing(10)
        fila1.addWidget(self.sw_detalles_hist)
        fila1.addStretch(1)
        self.edit_buscar_hist = LineEdit()
        self.edit_buscar_hist.setPlaceholderText("Buscar alumno…")
        self.btn_exportar = PushButton("Exportar CSV")
        self.btn_exportar.setObjectName("secundario")
        self.btn_exportar.clicked.connect(self._exportar_csv)
        btn_refresh = PushButton("Actualizar")
        btn_refresh.setObjectName("secundario")
        btn_refresh.clicked.connect(self._actualizar_historial)
        fila1.addWidget(self.edit_buscar_hist, 1)
        fila1.addWidget(self.btn_exportar)
        fila1.addWidget(btn_refresh)
        lg.addLayout(fila1)

        self.lbl_resumen_hist = CaptionLabel("")
        lg.addWidget(self.lbl_resumen_hist)

        # resultados: matriz por curso dentro de un scroll de página
        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }")
        self.scroll_historial = scroll
        cont_historial = QWidget()
        cont_historial.setStyleSheet("background: transparent;")
        self.v_cuerpo_historial = QVBoxLayout(cont_historial)
        self.v_cuerpo_historial.setContentsMargins(4, 4, 4, 4)
        self.v_cuerpo_historial.setSpacing(12)
        self._hist_grupos = []
        self._hist_filas = []
        self.estado_vacio = EstadoVacioHistorial(cont_historial)
        self.estado_vacio.setVisible(False)
        self.v_cuerpo_historial.addWidget(self.estado_vacio)
        self.v_cuerpo_historial.addStretch(1)
        scroll.setWidget(cont_historial)
        scroll.enableTransparentBackground()
        scroll.setMinimumHeight(320)
        lg.addWidget(scroll, 1)

        layout.addWidget(grupo, stretch=1)

        # ---- Señales ----
        self.combo_curso_hist.currentIndexChanged.connect(
            lambda _i: self._actualizar_historial())
        self.combo_seccion_hist.currentIndexChanged.connect(
            lambda _i: self._actualizar_historial())
        self.edit_buscar_hist.textChanged.connect(self._filtrar_historial)

        self._actualizar_historial()
        return tab

    def _on_detalles_hist(self, checked):
        """Switch de detalles: reconstruye la matriz con/ sin horas."""
        self._hist_detalles = bool(checked)
        self._actualizar_historial()

    # ---------------- Pestaña Alumnos ----------------
    def _construir_tab_alumnos(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        barra = QHBoxLayout()
        barra.setSpacing(10)
        self.btn_agregar_alumno = PushButton("Agregar alumno")
        self.btn_agregar_alumno.setObjectName("exito")
        self.btn_agregar_alumno.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_agregar_alumno.clicked.connect(self._agregar_alumno)
        self.chk_inactivos = CheckBox()
        self.chk_inactivos.setText("Mostrar dados de baja")
        self.chk_inactivos.toggled.connect(lambda _v: self._actualizar_galeria())
        self.edit_buscar_alum = LineEdit()
        self.edit_buscar_alum.setPlaceholderText("Buscar alumno…")
        self.edit_buscar_alum.textChanged.connect(lambda _: self._actualizar_galeria())
        barra.addWidget(self.btn_agregar_alumno)
        barra.addWidget(self.chk_inactivos)
        barra.addWidget(self.edit_buscar_alum, 1)
        barra.addStretch(1)
        layout.addLayout(barra)

        grupo_galeria = HeaderCardWidget()
        grupo_galeria.setTitle("Alumnos registrados")
        grupo_galeria.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout_galeria = grupo_galeria.viewLayout
        layout_galeria.setContentsMargins(10, 6, 10, 10)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.scroll_galeria = scroll
        scroll.viewport().installEventFilter(self)
        self.galeria_alumnos = QWidget()
        self.galeria_alumnos.setStyleSheet("background: transparent;")
        self.grid_alumnos = FlowLayout(self.galeria_alumnos)
        self.grid_alumnos.setContentsMargins(12, 12, 12, 12)
        self.grid_alumnos.setSpacing(TarjetaAlumno.ESPACIO)
        scroll.setWidget(self.galeria_alumnos)
        layout_galeria.addWidget(scroll)
        self._timer_reflow = QTimer(self)
        self._timer_reflow.setSingleShot(True)
        self._timer_reflow.setInterval(120)
        self._timer_reflow.timeout.connect(self._reflow_galeria)
        layout.addWidget(grupo_galeria, 1)

        return tab

    def _actualizar_galeria(self):
        while self.grid_alumnos.count():
            item = self.grid_alumnos.takeAt(0)
            w = item if isinstance(item, QWidget) else getattr(item, "widget", lambda: None)()
            if w is not None:
                w.deleteLater()
        alumnos = gestor.listar(incluir_inactivos=self.chk_inactivos.isChecked())
        texto = self.edit_buscar_alum.text().strip().lower() if hasattr(self, "edit_buscar_alum") else ""
        if texto:
            def _texto_busqueda(a):
                partes = [str(a.get(k, "") or "")
                          for k in ("nombre", "apellidos", "curso", "seccion")]
                partes += [f"{i.get('curso', '')} {i.get('seccion', '')}"
                           for i in (a.get("inscripciones") or [])
                           if isinstance(i, dict)]
                return " ".join(partes).lower()
            alumnos = [a for a in alumnos if texto in _texto_busqueda(a)]
        for alumno in alumnos:
            tarjeta = TarjetaAlumno(alumno, ancho=self._ancho_tarjeta_galeria())
            tarjeta.clickeado.connect(self._editar_alumno)
            self.grid_alumnos.addWidget(tarjeta)
        if not alumnos:
            lbl = BodyLabel()
            lbl.setText('No hay alumnos registrados. Use "Agregar alumno".')
            self.grid_alumnos.addWidget(lbl)

    def _ancho_tarjeta_galeria(self):
        """Ancho de tarjeta para que cada fila de la galeria llene el panel."""
        try:
            disp = self.scroll_galeria.viewport().width() - 24  # margenes del FlowLayout
        except Exception:
            disp = 0
        espacio = TarjetaAlumno.ESPACIO
        if disp < TarjetaAlumno.MIN_ANCHO:
            return TarjetaAlumno.MIN_ANCHO
        columnas = max(1, (disp + espacio) // (TarjetaAlumno.MIN_ANCHO + espacio))
        ancho = (disp - (columnas - 1) * espacio) // columnas
        return int(max(TarjetaAlumno.MIN_ANCHO, ancho))

    def _reflow_galeria(self):
        """Reajusta el ancho de las tarjetas existentes al espacio actual."""
        ancho = self._ancho_tarjeta_galeria()
        for i in range(self.grid_alumnos.count()):
            item = self.grid_alumnos.itemAt(i)
            w = item if isinstance(item, QWidget) else getattr(item, "widget", lambda: None)()
            if isinstance(w, TarjetaAlumno):
                w.set_ancho(ancho)

    def _agregar_alumno(self):
        dialogo = DialogoAlumno(
            callbacks_captura=(self.pausar_camara_para_captura, self.reanudar_tras_captura),
            parent=self)
        if dialogo.exec():
            self._tras_cambio_alumnos("Alumno agregado.")

    def _editar_alumno(self, alumno):
        dialogo = DialogoAlumno(
            alumno=alumno,
            callbacks_captura=(self.pausar_camara_para_captura, self.reanudar_tras_captura),
            parent=self)
        if dialogo.exec():
            self._tras_cambio_alumnos("Cambios guardados.")

    def _tras_cambio_alumnos(self, mensaje):
        self._cargar_referencias()
        self._push_referencias()
        self._actualizar_galeria()
        self._actualizar_historial()
        self.mostrar_toast(mensaje)

    # ---------------- Referencias / reconocimiento ----------------
    def _cargar_referencias(self):
        self.referencias = []
        self._info_alumnos = {}
        for a in gestor.listar():
            insc = [dict(i) for i in (a.get("inscripciones") or [])
                    if isinstance(i, dict)]
            if not insc:
                cs = str(a.get("curso", "") or "").strip()
                ss = str(a.get("seccion", "") or "").strip()
                if cs or ss:
                    insc = [{"curso": cs, "seccion": ss}]
            self._info_alumnos[a["alumno_id"]] = {
                "nombre": a.get("nombre", ""),
                "curso": a.get("curso", "") or "",
                "seccion": a.get("seccion", "") or "",
                "inscripciones": insc,
            }
            emb = a.get("embedding")
            if emb is None:
                continue
            self.referencias.append({
                "alumno_id": a["alumno_id"],
                "nombre": a.get("nombre", ""),
                "umbral": a.get("umbral_similitud", 0.70),
                "emb": gestor_np(emb),
            })
        self._push_referencias()

    def _push_referencias(self):
        """Propaga las referencias a los hilos de cámara en marcha."""
        for hilo in self.hilos.values():
            hilo.set_referencias(self.referencias)

    def _recargar_alumnos(self):
        if self._trabajo_recarga is not None and self._trabajo_recarga.isRunning():
            return
        self.btn_recargar.setEnabled(False)
        self.mostrar_toast("Recalculando embeddings faltantes…")
        self._trabajo_recarga = Trabajo(gestor.completar_embeddings, self)
        self._trabajo_recarga.listo.connect(self._on_recarga_lista)
        self._trabajo_recarga.start()

    def _on_recarga_lista(self, _ok, mensaje):
        self.btn_recargar.setEnabled(True)
        self._cargar_referencias()
        self._actualizar_galeria()
        self.mostrar_toast(mensaje)

    # ---------------- Cámaras ----------------
    def _iniciar_camaras(self):
        if self.hilos:
            return
        activas = [c for c in self.cfg.get("camaras", []) if c.get("activa")]
        if not activas:
            self.mostrar_toast(
                "No hay cámaras activas. Configúrelas en la pestaña Configuración.",
                "warning")
            return
        self._limpiar_filas()
        self._ultimo_registro = {}
        for cam in activas:
            self._iniciar_hilo(cam)
        host_lcd = str(self.cfg.get("host_lcd", "")).strip()
        if host_lcd and self._enlace_wifi is None:
            self._enlace_wifi = EnlaceWifi(host_lcd)
            self._enlace_wifi.start()
        self._iniciar_servidor_web()
        self.btn_iniciar.setEnabled(False)
        self.btn_detener.setEnabled(True)
        self.lbl_estado.setText(f"Cámaras: iniciando ({len(self.hilos)})…")
        self._estilo_estado(self.lbl_estado, True)

    def _iniciar_servidor_web(self):
        """Arranca el visor web + mDNS (https://centinela.local:8443)."""
        if self._servidor_web is not None:
            return

        def provedor_frame():
            for hilo in self.hilos.values():
                frame = hilo.frame_actual
                if frame is not None:
                    return frame
            for hilo in self.hilos_telefono.values():
                frame = hilo.frame_actual
                if frame is not None:
                    return frame
            return None

        def provedor_estado():
            return {"nodos": {
                f"cam_{cid}": {"online": bool(hilo.isRunning()),
                               "tipo": "camara"}
                for cid, hilo in self.hilos.items()
            }}

        try:
            self._servidor_web = ServidorWeb(
                provedor_frame, provedor_estado,
                al_frame_telefono=self._al_frame_telefono)
            self._servidor_web.start()
        except Exception as e:
            import traceback
            print("[WEB] servidor no disponible:\n"
                  + traceback.format_exc(), flush=True)
            self.mostrar_toast(f"Servidor web no disponible: {e}", "warning")
            self._servidor_web = None

    # ---------------- Telefonos como camaras (Fase 1b) ----------------
    def _crear_hilo_telefono(self, tel_id, nombre):
        """Devuelve el CamaraHilo del telefono, creandolo si no existe.

        Se llama desde el hilo del servidor web, pero el QThread debe vivir
        en el hilo GUI (afinidad de sus senales): la creacion se marshaliza
        con QTimer.singleShot y se espera la confirmacion con un Event.
        """
        with self._lock_tel:
            hilo = self.hilos_telefono.get(tel_id)
            if hilo is not None:
                return hilo
            listo = threading.Event()
            caja = {}
            QApplication.postEvent(self, _EventoCrearHiloTel(tel_id, caja, listo))
            if not listo.wait(3.0):
                print(f"[TEL] {tel_id}: timeout creando el hilo de inferencia")
                return None
            return caja.get("hilo")

    def _crear_hilo_telefono_en_gui(self, tel_id, caja):
        """Crea y arranca el hilo de inferencia del telefono (hilo GUI)."""
        try:
            asist = self.cfg.get("asistencia", {})
            nuevo = CamaraHilo(
                f"telefono:{tel_id}",
                list(self.referencias),
                umbral_global=float(asist.get("umbral_similitud", 0.70)),
                intervalo_segundos=0.1,
                confirm_seg=float(asist.get("segundos_confirmacion", 3.0)),
                modo_escaneo=str(asist.get("modo_escaneo", "uno")),
            )
            nuevo.cara_confirmada.connect(
                lambda tid, n, a, s, caja_, cid=tel_id:
                    self._on_confirmada(cid, tid, n, a, s))
            nuevo.error.connect(
                lambda msg, cid=tel_id: self._on_error_telefono(cid, msg))
            nuevo.start()
            caja["hilo"] = nuevo
            self.hilos_telefono[tel_id] = nuevo
        except Exception as e:
            print(f"[TEL] no se pudo crear el hilo de {tel_id}: {e}")

    def _on_error_telefono(self, tel_id, mensaje):
        hilo = self.hilos_telefono.get(tel_id)
        nombre = getattr(hilo, "fuente", tel_id)
        self.mostrar_toast(f"Teléfono {nombre}: {mensaje}", "warning")

    def _al_frame_telefono(self, tel_id, nombre, jpg_bytes):
        """Foto subida por un telefono -> IA -> JPEG anotado.

        Corre en el HILO DEL SERVIDOR WEB (no toca la GUI): decodifica,
        encola el frame en el CamaraHilo virtual del telefono y espera la
        respuesta anotada del worker de inferencia.
        """
        frame = cv2.imdecode(np.frombuffer(jpg_bytes, np.uint8),
                             cv2.IMREAD_COLOR)
        if frame is None:
            return None
        hilo = self._crear_hilo_telefono(tel_id, nombre)
        if hilo is None:
            return None
        hilo.set_referencias(list(self.referencias))
        ts = time.time()
        if not hilo.recibir_frame_telefono(frame):
            return None
        anotado = hilo.esperar_frame_anotado(ts, 0.8)
        if anotado is None:
            return None
        # respuesta legible en la pantalla del telefono
        ancho = 640
        if anotado.shape[1] > ancho:
            escala = ancho / anotado.shape[1]
            anotado = cv2.resize(
                anotado, (ancho, max(1, round(anotado.shape[0] * escala))))
        ok, buf = cv2.imencode(".jpg", anotado,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 82])
        return buf.tobytes() if ok else None

    def _iniciar_hilo(self, cam):
        cam_id = cam.get("id")
        if cam_id is None or cam_id in self.hilos:
            return
        asist = self.cfg.get("asistencia", {})
        try:
            hilo = CamaraHilo(
                config_app.fuente_de_camara(cam),
                self.referencias,
                umbral_global=float(asist.get("umbral_similitud", 0.70)),
                intervalo_segundos=float(cam.get("intervalo_proceso", 0.1)),
                confirm_seg=float(asist.get("segundos_confirmacion", 3.0)),
                modo_escaneo=str(asist.get("modo_escaneo", "uno")),
            )
        except Exception as e:
            self.mostrar_toast(
                f"No se pudo preparar '{cam.get('nombre') or cam_id}': {e}",
                "error")
            return
        cam_id = cam["id"]
        hilo.frame_listo.connect(
            lambda frame, dets, cid=cam_id: self._on_frame(cid, frame, dets))
        hilo.progreso_cara.connect(
            lambda tid, p, n, a, caja, cid=cam_id: self._on_progreso(cid, tid, p, n, a, caja))
        hilo.cara_confirmada.connect(
            lambda tid, n, a, s, caja, cid=cam_id: self._on_confirmada(cid, tid, n, a, s))
        hilo.estado_camara.connect(
            lambda activa, cid=cam_id: self._on_estado_cam(cid, activa))
        hilo.error.connect(
            lambda msg, cid=cam_id: self._on_error_cam(cid, msg))
        self.hilos[cam_id] = hilo
        hilo.start()

    def _detener_camaras(self):
        for hilo in list(self.hilos.values()):
            hilo.detener()
        self.hilos.clear()
        for hilo in list(self.hilos_telefono.values()):
            hilo.detener()
        self.hilos_telefono.clear()
        self._limpiar_filas()
        self._tras_detener()

        if self._enlace_wifi is not None:
            self._enlace_wifi.detener()
            self._enlace_wifi = None

        if self._servidor_web is not None:
            self._servidor_web.detener()
            self._servidor_web = None

    def _tras_detener(self):
        estado_lcd.actualizar_esperando()
        self._actualizar_simulador_lcd()
        self.btn_iniciar.setEnabled(True)
        self.btn_detener.setEnabled(False)
        self.lbl_estado.setText("Cámaras: detenidas")
        self._estilo_estado(self.lbl_estado, False)
        for lbl in self.labels_estado_cam.values():
            lbl.setText("Cámara: detenida")
            self._estilo_estado(lbl, False)
        for lbl in self.labels_video.values():
            # limpiar el pixmap ANTES del texto: un pixmap vacio pisa el texto
            lbl.setPixmap(QPixmap())
            lbl.setText("Sin señal")

    def _limpiar_filas(self):
        for fila in self.filas_progreso.values():
            fila.setParent(None)
        self.filas_progreso.clear()
        self._ultimo_progreso_track.clear()
        self.lbl_sin_rostros.show()

    def _revisar_tracks_perdidos(self):
        """Quita filas sin progreso reciente para no mostrar un estado congelado."""
        ahora = time.monotonic()
        perdidos = [
            (cam_id, track_id)
            for (cam_id, track_id), ultimo in self._ultimo_progreso_track.items()
            if ahora - ultimo > self.TIEMPO_SIN_PROGRESO
        ]
        for cam_id, track_id in perdidos:
            self._quitar_fila(cam_id, track_id)

    @staticmethod
    def _estilo_estado(lbl, activa):
        """Pinta una etiqueta de estado con el color del tema Fluent (teal/gris)."""
        color = ACENTO if activa else "#a5a5a5"
        lbl.setStyleSheet(
            f"background: transparent; color: {color};"
            " font-weight: 600; font-size: 13px;"
        )

    def _nombre_cam(self, cam_id):
        for c in self.cfg.get("camaras", []):
            if c.get("id") == cam_id:
                return c.get("nombre") or str(cam_id)
        return str(cam_id)

    def _actualizar_simulador_lcd(self):
        if hasattr(self, "simulador_lcd"):
            self.simulador_lcd.actualizar()

    # ---------------- Señales de cámaras ----------------
    def _on_frame(self, cam_id, frame, _detecciones):
        lbl = self.labels_video.get(cam_id)
        if lbl is None or frame is None:
            return
        hilo = self.hilos.get(cam_id)
        if hilo is None:
            return  # camara detenida: descartar frames en transito
        # El overlay se dibuja a la resolucion del label (nitidez total)
        hilo.set_tamano_display(lbl.width(), lbl.height())
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        pix = qimg_scaled(qimg, lbl)
        pix = _pixmap_redondeado(pix, RADIO_VIDEO)

        # Overlay moderno (UI-side, no toca la captura): borde + chip "EN VIVO"
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor("#232d3a"))
        pen.setWidth(2)
        painter.setPen(pen)
        painter.drawRoundedRect(pix.rect().adjusted(1, 1, -1, -1), RADIO_VIDEO, RADIO_VIDEO)

        chip_text = "EN VIVO"
        font = QFont("Segoe UI", 9, QFont.Weight.Bold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(chip_text) + 20
        th = fm.height() + 8
        rx = pix.width() - tw - 14
        ry = 14
        painter.setBrush(QBrush(QColor("#12261f")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rx, ry, tw, th, 8, 8)
        # punto de estado
        painter.setBrush(QBrush(QColor("#34d399")))
        painter.drawEllipse(rx + 8, ry + (th - 6) // 2, 6, 6)
        painter.setPen(QColor("#34d399"))
        painter.drawText(rx + 18, ry + fm.ascent() + 4, chip_text)
        painter.end()

        lbl.setPixmap(pix)

    def _on_estado_cam(self, cam_id, activa):
        lbl = self.labels_estado_cam.get(cam_id)
        if lbl is not None:
            lbl.setText("Cámara: activa" if activa else "Cámara: detenida")
            self._estilo_estado(lbl, activa)
        if activa:
            self.lbl_estado.setText(f"Cámaras: activas ({len(self.hilos)})")
            self._estilo_estado(self.lbl_estado, True)

    def _on_error_cam(self, cam_id, mensaje):
        hilo = self.hilos.pop(cam_id, None)
        if hilo is not None:
            hilo.detener()
        self.mostrar_toast(f"{self._nombre_cam(cam_id)}: {mensaje}", "error")
        if not self.hilos:
            self._tras_detener()

    def _on_progreso(self, cam_id, track_id, progreso, nombre, alumno_id, _caja):
        if cam_id not in self.hilos:
            return  # camara detenida: descartar senales en transito
        self._ultimo_progreso_track[(cam_id, track_id)] = time.monotonic()
        estado_lcd.configurar_umbral_nombre_lcd(
            self.cfg.get("asistencia", {}).get("umbral_lcd_nombre", 0.30))
        estado_lcd.configurar_duracion_resultado_lcd(
            self.cfg.get("asistencia", {}).get("duracion_resultado_lcd", 3.0))

        # Área de la cara detectada (para prioridad por tamaño en el LCD)
        area = 0.0
        if _caja is not None:
            try:
                x1, y1, x2, y2 = _caja
                area = max(0.0, float((x2 - x1) * (y2 - y1)))
            except Exception:
                area = 0.0

        # Solo el track prioritario (el de mayor área) actualiza el LCD.
        es_prioritario = (
            self._lcd_track_prioritario is None
            or (cam_id, track_id) == self._lcd_track_prioritario
            or area > self._lcd_area_prioritaria * 1.1
        )
        if es_prioritario:
            estado_lcd.actualizar_procesamiento(nombre=nombre, progreso=progreso)
            self._actualizar_simulador_lcd()
            self._lcd_track_prioritario = (cam_id, track_id)
            self._lcd_area_prioritaria = area

        self.lbl_sin_rostros.hide()

        # Consolidar por alumno: una sola fila por alumno, aunque se detecte
        # en varias camaras a la vez.
        if alumno_id:
            key = ("alumno", alumno_id)
            self._track_a_alumno[(cam_id, track_id)] = alumno_id
            self._alumno_tracks.setdefault(alumno_id, set()).add((cam_id, track_id))
        else:
            key = ("track", cam_id, track_id)

        if key not in self.filas_progreso:
            fila = FilaProgreso(alumno_id, nombre or "Desconocido")
            self.filas_progreso[key] = fila
            self.layout_rostros.insertWidget(self.layout_rostros.count() - 1, fila)

        seg_confirm = float(self.cfg["asistencia"]["segundos_confirmacion"])
        self.filas_progreso[key].actualizar(
            progreso, f"{progreso * seg_confirm:.1f} s / {seg_confirm:.1f} s")

    def _on_confirmada(self, cam_id, track_id, nombre, alumno_id, similitud):
        if cam_id not in self.hilos and cam_id not in self.hilos_telefono:
            return  # fuente detenida: descartar senales en transito
        # Un confirmado tiene prioridad máxima en el LCD.
        estado_lcd.actualizar_confirmado(nombre=nombre)
        self._actualizar_simulador_lcd()
        self._lcd_track_prioritario = (cam_id, track_id)
        self._lcd_area_prioritaria = float("inf")
        if alumno_id:
            key = ("alumno", alumno_id)
        else:
            key = ("track", cam_id, track_id)
        if key in self.filas_progreso:
            self.filas_progreso[key].confirmado()
            self.filas_progreso[key]._cargar_datos(alumno_id)

        ahora_ts = time.time()
        ultimo = self._ultimo_registro.get(alumno_id, 0)
        if ahora_ts - ultimo >= self._intervalo_registro:
            self._ultimo_registro[alumno_id] = ahora_ts
            # Opt-in (Configuración): respetar el horario de clase del curso.
            if not self._marcado_permitido(alumno_id, ahora_ts):
                QTimer.singleShot(2000, lambda c=cam_id, t=track_id: self._quitar_fila(c, t))
                return
            asistencia_id = db_centinela.registrar_entrada(
                alumno_id, float(similitud), self._nombre_cam(cam_id))
            if asistencia_id is not None:
                self.mostrar_toast(
                    f"Entrada confirmada: {nombre} ({similitud:.2f})")

        QTimer.singleShot(2000, lambda c=cam_id, t=track_id: self._quitar_fila(c, t))

    def _marcado_permitido(self, alumno_id, ahora_ts):
        """
        True si el marcado procede. Con 'aplicar_horarios' apagado (default)
        siempre es True: el comportamiento queda intacto. Encendido, bloquea
        el registro fuera de la franja de TODAS las inscripciones del alumno
        (curso+seccion) y avisa con throttling de 5 min por alumno.
        """
        if not self.cfg.get("asistencia", {}).get("aplicar_horarios"):
            return True
        if not alumno_id:
            return True
        info = self._info_alumnos.get(alumno_id) or {}
        ahora = datetime.now()
        hoy = ahora.strftime("%Y-%m-%d")
        hora = ahora.strftime("%H:%M")
        insc = info.get("inscripciones") or []
        if not insc:
            cs = str(info.get("curso", "") or "").strip()
            ss = str(info.get("seccion", "") or "").strip()
            if cs or ss:
                insc = [{"curso": cs, "seccion": ss}]
        # alguna inscripcion dentro de su franja horaria ahora mismo
        for i in insc:
            if config_app.en_franja(self.cfg, i.get("curso", ""),
                                    i.get("seccion", ""), hoy, hora) is not None:
                return True
        nombre = info.get("nombre", "") or alumno_id
        if ahora_ts - self._avisos_fuera_horario.get(alumno_id, 0) >= 300:
            self._avisos_fuera_horario[alumno_id] = ahora_ts
            self.mostrar_toast(
                f"{nombre} fuera de horario de clase (no se registró entrada).",
                "warning")
        return False

    def _quitar_fila(self, cam_id, track_id):
        # Determinar alumno asociado al track
        alumno_id = self._track_a_alumno.pop((cam_id, track_id), None)
        if alumno_id is not None:
            tracks = self._alumno_tracks.get(alumno_id, set())
            tracks.discard((cam_id, track_id))
            if tracks:
                self._alumno_tracks[alumno_id] = tracks
                # Aun hay tracks activos del alumno: no quitar la fila.
                if self._lcd_track_prioritario == (cam_id, track_id):
                    self._lcd_track_prioritario = None
                    self._lcd_area_prioritaria = 0.0
                return
            self._alumno_tracks.pop(alumno_id, None)
            key = ("alumno", alumno_id)
        else:
            key = ("track", cam_id, track_id)

        if self._lcd_track_prioritario == (cam_id, track_id):
            self._lcd_track_prioritario = None
            self._lcd_area_prioritaria = 0.0

        if key in self.filas_progreso:
            fila = self.filas_progreso.pop(key)
            self._ultimo_progreso_track.pop((cam_id, track_id), None)
            fila.setParent(None)
            for i in range(self.layout_rostros.count()):
                item = self.layout_rostros.itemAt(i)
                if item and item.widget() == fila:
                    self.layout_rostros.takeAt(i)
                    break
        if not self.filas_progreso:
            estado_lcd.actualizar_esperando()
            self._actualizar_simulador_lcd()
            self.lbl_sin_rostros.show()

    # ---------------- Captura pausa/reanudación ----------------
    def pausar_camara_para_captura(self, fuente):
        """Detiene temporalmente el hilo de esa fuente para que el diálogo
        de captura pueda abrirla (Windows no permite abrir la webcam 2 veces)."""
        objetivo = str(fuente).strip()
        self._camaras_pausadas = []
        for cam_id, hilo in list(self.hilos.items()):
            if str(hilo.fuente).strip() == objetivo:
                hilo.detener()
                self.hilos.pop(cam_id, None)
                self._camaras_pausadas.append(cam_id)
        return bool(self._camaras_pausadas)

    def reanudar_tras_captura(self):
        if not self._camaras_pausadas:
            return
        for cam_id in self._camaras_pausadas:
            cam = next((c for c in self.cfg.get("camaras", []) if c.get("id") == cam_id), None)
            if cam is not None and cam.get("activa"):
                self._iniciar_hilo(cam)
        self._camaras_pausadas = []

    # ---------------- Historial ----------------
    def _actualizar_historial(self):
        """Arma la matriz del historial completo (sin elegir rango).

        Cada curso muestra sus SESIONES: días con franja horaria desde la
        vigencia 'desde' (o la primera actividad) hasta hoy, limitadas por
        la cantidad de clases configurada en la pestaña Clases.
        """
        if not hasattr(self, "combo_curso_hist"):
            return
        hoy = date.today().isoformat()
        try:
            primer = db_centinela.primer_dia_actividad()
            marcas = (db_centinela.obtener_historial_rango(primer, hoy)
                      if primer else [])
            activos = db_centinela.obtener_alumnos()
            inscripciones = gestor.mapa_inscripciones()
        except Exception as e:
            print("historial error", repr(e), flush=True)
            return

        marcas_dia = {(m["alumno_id"], m["fecha"]): m for m in marcas}
        tolerancia = int(self.cfg.get("asistencia", {}).get(
            "tolerancia_tardanza_min", 5))

        # actividad por curso: un día está activo si algún inscrito marcó
        primer_dia_curso = {}
        dias_act_curso = {}
        for m in marcas:
            fecha = m["fecha"]
            for elemento in (inscripciones.get(m["alumno_id"]) or []):
                c = str(elemento.get("curso", "") or "").strip()
                if not c:
                    continue
                dias_act_curso.setdefault(c, set()).add(fecha)
                if fecha < primer_dia_curso.get(c, "9999"):
                    primer_dia_curso[c] = fecha

        filas = []
        fechas_curso = {}
        for a in activos:
            aid = a["alumno_id"]
            nombre = str(a.get("nombre", "") or "")
            insc = inscripciones.get(aid)
            if not insc:
                cs = str(a.get("curso", "") or "").strip()
                ss = str(a.get("seccion", "") or "").strip()
                insc = [{"curso": cs, "seccion": ss}] if (cs or ss) else []
            for elemento in insc:
                curso = str(elemento.get("curso", "") or "")
                seccion = str(elemento.get("seccion", "") or "")
                dias = fechas_curso.get(curso)
                if dias is None:
                    sesiones = config_app.fechas_sesiones(
                        self.cfg, curso, primer_dia_curso.get(curso), hoy)
                    if sesiones is None:   # sin franjas: actividad + hoy
                        sesiones = sorted(
                            dias_act_curso.get(curso, set()) | {hoy})
                    fechas_curso[curso] = sesiones
                    dias = sesiones
                celdas = {}
                estados = []
                puntos = 0.0
                clases = 0
                for fecha in dias:
                    marca = marcas_dia.get((aid, fecha))
                    estado = self._estado_celda(
                        marca, curso, seccion, fecha, tolerancia,
                        fecha in dias_act_curso.get(curso, ()))
                    celdas[fecha] = {
                        "estado": estado,
                        "entrada": (marca or {}).get("entrada"),
                        "salida": (marca or {}).get("salida"),
                        "minutos": (marca or {}).get("minutos"),
                        "confianza": (marca or {}).get("confianza"),
                        "marcas": (marca or {}).get("marcas", 0),
                    }
                    estados.append(estado)
                    if estado == "Presente":
                        puntos += 1.0
                        clases += 1
                    elif estado == "Tardanza":
                        puntos += 0.5
                        clases += 1
                    elif estado == "Ausente":
                        clases += 1
                filas.append({
                    "alumno_id": aid,
                    "nombre": nombre,
                    "foto": gestor.ruta_foto_absoluta(a.get("foto_referencia")),
                    "porcentaje": (puntos / clases) if clases else None,
                    "curso": curso,
                    "seccion": seccion,
                    "texto_busqueda": " ".join(
                        [nombre, curso, seccion] + estados).lower(),
                    "celdas": celdas,
                })

        filas.sort(key=lambda x: (x["curso"] == "", x["curso"],
                                  x["seccion"] == "", x["seccion"],
                                  x["nombre"]))
        self._hist_filas = filas
        self._hist_fechas = fechas_curso

        # huella: selección de curso/sección + detalles + celdas completas;
        # evita reconstruir matrices idénticas (crash al repintar la pestaña)
        curso_sel = (self.combo_curso_hist.currentText()
                     if self.combo_curso_hist.currentIndex() > 0 else "")
        seccion_sel = (self.combo_seccion_hist.currentText()
                       if curso_sel and
                       self.combo_seccion_hist.currentIndex() > 0 else "")
        huella = (curso_sel, seccion_sel, self._hist_detalles,
                  tuple(sorted((c, config_app.clases_de_curso(self.cfg, c))
                               for c in fechas_curso)),
                  tuple(
            (f["alumno_id"], f["curso"], f["seccion"], tuple(
                (d, c["estado"], c["entrada"], c["salida"], c["minutos"],
                 c["marcas"], c["confianza"])
                for d, c in f["celdas"].items()))
            for f in filas))
        if huella == getattr(self, "_hist_huella", None):
            return
        self._hist_huella = huella

        self._construir_vista_historial()
        self._filtrar_historial(
            self.edit_buscar_hist.text() if hasattr(self, "edit_buscar_hist") else "")

    def _estado_celda(self, marca, curso, seccion, fecha, tolerancia, dia_activo):
        """Estado de una celda: Presente | Tardanza | Ausente | Sin clase | Sin registro."""
        if marca:
            entrada = str(marca.get("entrada") or "")[11:16]
            tardanza = config_app.es_tardanza(
                self.cfg, curso, seccion, fecha, entrada, tolerancia)
            return "Tardanza" if tardanza else "Presente"
        if config_app.franjas_del_dia(self.cfg, fecha, curso, seccion):
            return "Ausente" if dia_activo else "Sin registro"
        return "Sin clase"

    def _construir_vista_historial(self):
        """Pinta las matrices según los selectores de curso y sección."""
        filas = self._hist_filas
        fechas_curso = getattr(self, "_hist_fechas", {})
        hoy = date.today().isoformat()

        # ---- selector de curso (conserva la selección vigente) ----
        cursos = sorted({f["curso"] for f in filas if f["curso"]})
        seleccion_curso = (self.combo_curso_hist.currentText()
                           if self.combo_curso_hist.currentIndex() > 0 else "")
        self.combo_curso_hist.blockSignals(True)
        self.combo_curso_hist.clear()
        self.combo_curso_hist.addItem("Todos los cursos")
        self.combo_curso_hist.addItems(cursos)
        if seleccion_curso and seleccion_curso in cursos:
            self.combo_curso_hist.setCurrentText(seleccion_curso)
        self.combo_curso_hist.blockSignals(False)
        curso_sel = seleccion_curso if seleccion_curso in cursos else ""

        # ---- selector de sección (solo con curso concreto) ----
        secciones = []
        if curso_sel:
            cfg_secs = config_app.secciones_de_curso(self.cfg, curso_sel)
            secciones = sorted(
                {f["seccion"] for f in filas if f["curso"] == curso_sel}
                | set(cfg_secs), key=lambda s: (s == "", s))
        seleccion_sec = (self.combo_seccion_hist.currentText()
                         if self.combo_seccion_hist.currentIndex() > 0 else "")
        self.combo_seccion_hist.blockSignals(True)
        self.combo_seccion_hist.clear()
        self.combo_seccion_hist.addItem("Todas")
        self.combo_seccion_hist.addItems(secciones)
        if seleccion_sec and seleccion_sec in secciones:
            self.combo_seccion_hist.setCurrentText(seleccion_sec)
        self.combo_seccion_hist.setEnabled(bool(curso_sel))
        self.lbl_seccion_hist.setEnabled(bool(curso_sel))
        self.combo_seccion_hist.blockSignals(False)
        seccion_sel = (seleccion_sec
                       if curso_sel and seleccion_sec in secciones else "")

        # limpieza de la vista anterior (el estado vacío se conserva)
        while self.v_cuerpo_historial.count():
            item = self.v_cuerpo_historial.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.estado_vacio:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self.v_cuerpo_historial.insertWidget(0, self.estado_vacio)
        self._hist_grupos = []

        if not filas:
            self.estado_vacio.setVisible(True)
            self.estado_vacio.setTextos(
                "Sin registros todavía",
                "El historial aparece al registrar asistencias en Vigilancia.")
            self.lbl_resumen_hist.setText("")
            self.v_cuerpo_historial.addStretch(1)
            return

        visibles = filas
        if curso_sel:
            visibles = [f for f in visibles if f["curso"] == curso_sel]
            if seccion_sel:
                visibles = [f for f in visibles if f["seccion"] == seccion_sel]
        if not visibles:
            self.estado_vacio.setVisible(True)
            self.estado_vacio.setTextos(
                "Sin alumnos en la selección",
                "Cambia el curso o la sección elegidos.")
            self.lbl_resumen_hist.setText("")
            self.v_cuerpo_historial.addStretch(1)
            return
        self.estado_vacio.setVisible(False)

        # árbol curso -> sección -> filas (una fila por alumno x inscripción)
        arbol = {}
        for f in visibles:
            arbol.setdefault(f["curso"], {}) \
                 .setdefault(f["seccion"], []).append(f)
        desplegar = len(arbol) <= 3 or bool(curso_sel)

        for curso in sorted(arbol, key=lambda c: (c == "", c)):
            filas_curso = [f for fs in arbol[curso].values() for f in fs]
            # badges sobre las CELDAS visibles del curso
            conteos = {}
            for f in filas_curso:
                for celda in f["celdas"].values():
                    e = str(celda.get("estado") or "Sin registro")
                    conteos[e] = conteos.get(e, 0) + 1
            badges = [(e, conteos[e]) for e in
                      ("Presente", "Tardanza", "Ausente", "Sin clase")
                      if conteos.get(e)]
            sesiones = len(fechas_curso.get(curso, []))
            total = config_app.clases_de_curso(self.cfg, curso)
            sub_clases = (f"{sesiones} de {total} clases" if total
                          else f"{sesiones} clases")
            n_sec = len(arbol[curso])
            tarjeta = TarjetaGrupoCurso(
                curso,
                f"{len(filas_curso)} alumno{'s' if len(filas_curso) != 1 else ''}"
                f" · {n_sec} sección{'es' if n_sec != 1 else ''}"
                f" · {sub_clases}",
                badges)
            if not desplegar:
                tarjeta.expandida = False
                tarjeta.cuerpo.setVisible(False)
                tarjeta.btn_chevron.setIcon(FIcon.CARE_DOWN_SOLID)

            for seccion in sorted(arbol[curso], key=lambda s: (s == "", s)):
                filas_seccion = sorted(arbol[curso][seccion],
                                       key=lambda x: str(x.get("nombre") or ""))
                tabla = TablaSeccion(seccion, fechas_curso.get(curso, []),
                                     filas_seccion, self._hist_detalles,
                                     tarjeta)
                tarjeta.v_cuerpo.addWidget(tabla)
            self.v_cuerpo_historial.addWidget(tarjeta)
            self._hist_grupos.append(tarjeta)

        self.v_cuerpo_historial.addStretch(1)
        n_alumnos = len({f["alumno_id"] for f in visibles})
        self.lbl_resumen_hist.setText(
            f"{n_alumnos} alumno{'s' if n_alumnos != 1 else ''} · "
            f"{len(arbol)} curso{'s' if len(arbol) != 1 else ''}"
            f" · hasta el {hoy[8:10]}/{hoy[5:7]}")

    def _filtrar_historial(self, texto):
        """Filtro de busqueda por nombre/curso/seccion/estado sobre la matriz."""
        if not hasattr(self, "v_cuerpo_historial"):
            return
        texto = (texto or "").lower().strip()
        visibles = 0
        for tarjeta in getattr(self, "_hist_grupos", []):
            secciones_visibles = 0
            for i in range(tarjeta.v_cuerpo.count()):
                seccion = tarjeta.v_cuerpo.itemAt(i).widget()
                if not isinstance(seccion, TablaSeccion):
                    continue
                n = 0
                for r, info in enumerate(seccion.filas):
                    ok = not texto or texto in info["texto_busqueda"]
                    seccion.tabla.setRowHidden(r, not ok)
                    n += ok
                seccion.setVisible(n > 0)
                secciones_visibles += n > 0
                visibles += n
            tarjeta.setVisible(secciones_visibles > 0)
        self.estado_vacio.setVisible(visibles == 0)
        if visibles == 0:
            if texto:
                self.estado_vacio.setTextos(
                    "Sin resultados",
                    "Nadie coincide con la búsqueda en el curso y la sección elegidos.")
            else:
                self.estado_vacio.setTextos(
                    "Sin sesiones todavía",
                    "Registra asistencias en Vigilancia o configura las clases.")

    def _exportar_csv(self):
        """Exporta las sesiones mostradas (sin aplicar la busqueda) a CSV.

        Una linea por alumno x inscripcion x sesion: cada celda de la matriz
        con su estado real (Presente/Tardanza/Ausente/Sin clase/Sin registro).
        """
        filas = getattr(self, "_hist_filas", [])
        if not filas:
            self.mostrar_toast("No hay datos para exportar en el rango.", "warning")
            return
        nombre = ("historial_" + date.today().strftime("%Y%m%d") + ".csv")
        ruta, _ = QFileDialog.getSaveFileName(
            self, "Exportar historial a CSV", nombre, "CSV (*.csv)")
        if not ruta:
            return
        try:
            with open(ruta, "w", encoding="utf-8-sig", newline="") as fh:
                wr = csv.writer(fh, delimiter=";")
                wr.writerow(["Fecha", "Curso", "Seccion", "Alumno", "Estado",
                             "Entrada", "Salida", "Minutos", "Marcas"])
                for f in sorted(filas, key=lambda x: (
                        str(x.get("curso") or ""), str(x.get("nombre") or ""))):
                    for fecha in sorted(f["celdas"]):
                        c = f["celdas"][fecha]
                        wr.writerow([
                            fecha, f.get("curso") or "", f.get("seccion") or "",
                            f.get("nombre") or "",
                            str(c.get("estado") or "Sin registro"),
                            hhmm(c.get("entrada")) or "",
                            hhmm(c.get("salida")) or "",
                            "" if c.get("minutos") is None else c["minutos"],
                            c.get("marcas", 0),
                        ])
        except OSError as e:
            self.mostrar_toast(f"No se pudo exportar: {e}", "error")
            return
        self.mostrar_toast(
            f"Historial exportado a {os.path.basename(ruta)}", "exito")


    def _on_cursos_cambiados(self, cfg):
        """Cursos/secciones editados: actualizar horarios dependientes y matriz."""
        self.cfg = cfg
        self.panel_config.cfg = cfg   # panel_config parte del mismo estado
        self._actualizar_historial()
        self.mostrar_toast("Cursos y secciones guardados.")

    def _on_camaras_cambiadas(self, cfg):
        self.cfg = cfg
        self.panel_clases.cfg = cfg   # panel_clases parte del mismo estado
        en_marcha = bool(self.hilos)
        self._detener_camaras()
        self._reconstruir_paneles()
        if en_marcha:
            self._iniciar_camaras()
        self.mostrar_toast("Configuración de cámaras actualizada.")

    def _on_parametros_cambiados(self, cfg):
        self.cfg = cfg
        self.panel_clases.cfg = cfg   # panel_clases parte del mismo estado
        self._intervalo_registro = float(cfg["asistencia"]["intervalo_antiduplicado"])
        for hilo in self.hilos.values():
            hilo.umbral_global = float(cfg["asistencia"]["umbral_similitud"])
            hilo.confirm_seg = float(cfg["asistencia"]["segundos_confirmacion"])
        self.mostrar_toast(
            "Parámetros de asistencia guardados y aplicados a las cámaras en marcha.")

    def _on_horarios_cambiados(self, cfg):
        self.cfg = cfg
        self.panel_config.cfg = cfg   # panel_config parte del mismo estado
        self._actualizar_historial()
        self.mostrar_toast("Horarios de clase actualizados.")

    # ---------------- Cierre ----------------

def gestor_np(emb_lista):
    """np.array desde la lista del JSON (helper para mantener el import numpy local a rostros)."""
    import numpy as np
    return np.array(emb_lista, dtype=np.float32)


def qimg_scaled(qimg, label):
    pix = QPixmap.fromImage(qimg)
    return pix.scaled(
        label.size(),
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def _pixmap_redondeado(pix, radio):
    """Devuelve el pixmap con esquinas redondeadas (canal alfa)."""
    if pix.isNull() or radio <= 0:
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(pix.width()), float(pix.height()), radio, radio)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


def _app_icono():
    """Icono de la ventana (del repo raiz: favicon.svg)."""
    ruta = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "..", "favicon.svg")
    return QIcon(ruta)


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setWindowIcon(_app_icono())
    ventana = VentanaPrincipal()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()




