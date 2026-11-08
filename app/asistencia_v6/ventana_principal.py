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

import os, sys, time

import cv2

from PyQt6.QtCore import Qt, QTimer, QDate, pyqtSignal
from PyQt6.QtGui import QImage, QPixmap, QFont, QColor, QShortcut, QKeySequence
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem, QSplitter,
    QFrame, QGroupBox, QMessageBox, QHeaderView, QTabWidget,
    QDateEdit, QProgressBar, QScrollArea, QGridLayout, QCheckBox,
    QGraphicsOpacityEffect,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camara_hilo import CamaraHilo
from estilos import ESTILO
from hilo_trabajo import Trabajo
from ui_util import ndarray_a_pixmap
from dialogo_alumno import DialogoAlumno
from panel_configuracion import PanelConfiguracion
from simulador_lcd import SimuladorLCD
from toast import Toast
import config as config_app
import gestor_alumnos as gestor
import db as db_centinela
import estado_lcd
from enlace_wifi import EnlaceWifi


class FilaProgreso(QFrame):
    """Fila individual con nombre y barra de progreso para una cara en verificación."""

    def __init__(self, track_id, nombre, parent=None):
        super().__init__(parent)
        self.track_id = track_id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("FilaProgreso { background: #1a1a1a; border: 1px solid #333333; border-radius: 8px; }")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)

        self.lbl_nombre = QLabel(nombre)
        self.lbl_nombre.setStyleSheet("color:#ededed; font-weight: bold; background: transparent; border: none;")
        self.lbl_nombre.setMinimumWidth(160)

        self.barra = QProgressBar()
        self.barra.setRange(0, 100)
        self.barra.setValue(0)
        self.barra.setFixedWidth(220)

        self.lbl_estado = QLabel("Verificando...")
        self.lbl_estado.setStyleSheet("color:#a3a3a3; font-size: 12px; background: transparent; border: none;")

        layout.addWidget(self.lbl_nombre, 0)
        layout.addWidget(self.barra, 1)
        layout.addWidget(self.lbl_estado, 0)

    def actualizar(self, progreso_0_1, estado_texto):
        self.barra.setValue(int(progreso_0_1 * 100))
        self.lbl_estado.setText(estado_texto)
        if progreso_0_1 >= 1.0:
            self.barra.setStyleSheet("QProgressBar::chunk { background: #5fd08b; border-radius: 6px; }")
            self.lbl_estado.setStyleSheet("color:#5fd08b; font-size: 12px; background: transparent; border: none;")
        else:
            self.barra.setStyleSheet("QProgressBar::chunk { background: #e6e6e6; border-radius: 6px; }")
            self.lbl_estado.setStyleSheet("color:#a3a3a3; font-size: 12px; background: transparent; border: none;")

    def confirmado(self):
        self.barra.setValue(100)
        self.barra.setStyleSheet("QProgressBar::chunk { background: #5fd08b; border-radius: 6px; }")
        self.lbl_estado.setText("Confirmado")
        self.lbl_estado.setStyleSheet("color:#5fd08b; font-weight: bold; background: transparent; border: none;")


class TarjetaAlumno(QFrame):
    """Tarjeta clicable de la galería de alumnos. Nunca muestra el ID."""

    clickeado = pyqtSignal(dict)

    def __init__(self, alumno, parent=None):
        super().__init__(parent)
        self.alumno = alumno
        self.setObjectName("card")
        self.setFixedSize(192, 262)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(6)

        estilo_imagen = "background: #0d0d0d; border: 1px solid #262626; border-radius: 8px;"
        img = QLabel()
        img.setFixedSize(170, 168)
        img.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ruta = gestor.ruta_foto_absoluta(alumno.get("foto_referencia"))
        pix = QPixmap(ruta) if ruta else QPixmap()
        if not pix.isNull():
            img.setPixmap(pix.scaled(
                168, 168,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            ))
            img.setStyleSheet(estilo_imagen)
        else:
            img.setText("Sin foto")
            img.setStyleSheet(estilo_imagen + " color: #5a5a5a; font-size: 12px;")
        v.addWidget(img, 0, Qt.AlignmentFlag.AlignHCenter)

        nombre = (alumno.get("nombre") or "").strip() or "(sin nombre)"
        lbl_nombre = QLabel(nombre)
        lbl_nombre.setWordWrap(True)
        lbl_nombre.setStyleSheet("color:#ededed; font-size: 13px; font-weight: bold; background: transparent; border: none;")
        v.addWidget(lbl_nombre)

        info = []
        if (alumno.get("curso") or "").strip():
            info.append(alumno["curso"].strip())
        if (alumno.get("grupo") or "").strip():
            info.append(alumno["grupo"].strip())
        if alumno.get("estado") == "inactivo":
            info.append("Inactivo")
        lbl_extra = QLabel(" · ".join(info))
        lbl_extra.setStyleSheet(
            "color:" + ("#e58a8a" if alumno.get("estado") == "inactivo" else "#8a8a8a")
            + "; font-size: 12px; background: transparent; border: none;")
        lbl_extra.setWordWrap(True)
        v.addWidget(lbl_extra)

        if alumno.get("embedding") is None:
            lbl_aviso = QLabel("⚠ sin embedding")
            lbl_aviso.setStyleSheet("color:#e5c07b; font-size: 11px; background: transparent; border: none;")
            v.addWidget(lbl_aviso)

        v.addStretch(1)

    def mousePressEvent(self, event):
        self.clickeado.emit(self.alumno)
        super().mousePressEvent(event)


class VentanaPrincipal(QMainWindow):
    TIEMPO_SIN_PROGRESO = 1.5

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Centinela Control v6 — Asistencia y vigilancia de aula")
        self.resize(1500, 900)
        self.setStyleSheet(ESTILO)

        # Configuración persistente
        self.cfg = config_app.cargar()

        # Estado de vigilancia
        self.referencias = []
        self.hilos = {}             # cam_id -> CamaraHilo
        self.labels_video = {}      # cam_id -> QLabel
        self.labels_estado_cam = {} # cam_id -> QLabel
        self.filas_progreso = {}    # (cam_id, track_id) -> FilaProgreso
        self._ultimo_progreso_track = {}  # (cam_id, track_id) -> monotonic timestamp
        self.layout_rostros = None
        self._ultimo_registro = {}  # alumno_id -> timestamp
        self._intervalo_registro = float(self.cfg["asistencia"]["intervalo_antiduplicado"])
        self._camaras_pausadas = []  # cam_ids detenidos temporalmente por una captura
        self._enlace_wifi = None
        self._toasts = []

        # Trabajos en segundo plano
        self._trabajo_recarga = None

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

    # ---------------- Toasts y atajos ----------------
    def mostrar_toast(self, mensaje, tipo="info", duracion_ms=3000):
        toast = Toast(self, mensaje, tipo, duracion_ms)
        self._toasts.append(toast)
        toast.destroyed.connect(lambda *_: self._quitar_toast(toast))
        self._reposicionar_toasts()

    def _quitar_toast(self, toast):
        if toast in self._toasts:
            self._toasts.remove(toast)
        self._reposicionar_toasts()

    def _reposicionar_toasts(self):
        margen = 16
        y = margen
        for t in self._toasts:
            x = self.width() - t.width() - margen
            t.move(x, y)
            y += t.height() + 8

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposicionar_toasts()

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
        central = QWidget()
        self.setCentralWidget(central)
        layout_principal = QVBoxLayout(central)
        layout_principal.setContentsMargins(16, 16, 16, 16)
        layout_principal.setSpacing(14)

        self.tabs = QTabWidget()
        self.tab_vigilancia = self._construir_tab_vigilancia()
        self.tab_historial = self._construir_tab_historial()
        self.tab_alumnos = self._construir_tab_alumnos()
        self.panel_config = PanelConfiguracion(callbacks={
            "camaras_cambiadas": self._on_camaras_cambiadas,
            "parametros_cambiados": self._on_parametros_cambiados,
        })
        self.tabs.addTab(self.tab_vigilancia, "  Vigilancia en vivo  ")
        self.tabs.addTab(self.tab_historial,  "  Historial por día  ")
        self.tabs.addTab(self.tab_alumnos,    "  Alumnos  ")
        self.tabs.addTab(self.panel_config,   "  Configuración  ")
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout_principal.addWidget(self.tabs, stretch=1)

        self.statusBar().showMessage(
            "Listo. Configure las cámaras en la pestaña Configuración e inicie la vigilancia.")

    def _on_tab_changed(self, idx):
        widget = self.tabs.widget(idx)
        if widget is self.tab_historial:
            self._actualizar_historial()
        elif widget is self.tab_alumnos:
            self._actualizar_galeria()

    # ---------------- Pestaña Vigilancia ----------------
    def _construir_tab_vigilancia(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Izquierda: paneles de cámaras (dinámicos, dentro de scroll)
        self.scroll_paneles = QScrollArea()
        self.scroll_paneles.setWidgetResizable(True)
        self.scroll_paneles.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_paneles.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self._reconstruir_paneles()
        splitter.addWidget(self.scroll_paneles)

        # Derecha: controles y rostros en verificación
        panel_derecho = QWidget()
        layout_derecho = QVBoxLayout(panel_derecho)
        layout_derecho.setContentsMargins(8, 0, 0, 0)
        layout_derecho.setSpacing(12)

        grupo_controles = QGroupBox("Control")
        layout_controles = QVBoxLayout(grupo_controles)
        layout_controles.setSpacing(10)
        layout_controles.setContentsMargins(14, 6, 14, 12)

        self.btn_iniciar = QPushButton("Iniciar cámaras")
        self.btn_iniciar.setObjectName("exito")
        self.btn_iniciar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_iniciar.clicked.connect(self._iniciar_camaras)

        self.btn_detener = QPushButton("Detener todas")
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
        self.lbl_estado.setObjectName("estado_off")
        layout_controles.addWidget(self.lbl_estado)

        self.btn_recargar = QPushButton("Recargar alumnos (embeddings faltantes)")
        self.btn_recargar.setObjectName("secundario")
        self.btn_recargar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_recargar.clicked.connect(self._recargar_alumnos)
        layout_controles.addWidget(self.btn_recargar)

        layout_derecho.addWidget(grupo_controles)

        self.simulador_lcd = SimuladorLCD()
        layout_derecho.addWidget(self.simulador_lcd)

        grupo_progreso = QGroupBox("Verificando rostros")
        layout_progreso = QVBoxLayout(grupo_progreso)
        layout_progreso.setContentsMargins(8, 12, 8, 8)
        layout_progreso.setSpacing(8)

        self.lbl_sin_rostros = QLabel("Sin rostros detectados")
        self.lbl_sin_rostros.setStyleSheet("color:#6b6b6b; font-size: 13px; background: transparent; border: none;")
        layout_progreso.addWidget(self.lbl_sin_rostros)

        self.scroll_rostros_area = QScrollArea()
        self.scroll_rostros_area.setWidgetResizable(True)
        self.scroll_rostros_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_rostros_area.setStyleSheet("background: transparent; border: none;")
        self.scroll_rostros = QWidget()
        self.layout_rostros = QVBoxLayout(self.scroll_rostros)
        self.layout_rostros.setContentsMargins(0, 0, 0, 0)
        self.layout_rostros.setSpacing(8)
        self.layout_rostros.addStretch()
        self.scroll_rostros_area.setWidget(self.scroll_rostros)
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
        contenedor.setStyleSheet("background: #0a0a0a;")
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
                "background: #0d0d0d; border: 2px dashed #333333; border-radius: 12px;"
                " color: #6b6b6b; font-size: 15px;"
            )
            grid.addWidget(lbl, 0, 0)
        else:
            cols = 2 if len(activas) > 1 else 1
            for i, cam in enumerate(activas):
                cam_id = cam.get("id")
                grupo = QGroupBox(cam.get("nombre") or str(cam_id))
                grupo.setAlignment(Qt.AlignmentFlag.AlignCenter)
                v = QVBoxLayout(grupo)
                v.setContentsMargins(10, 2, 10, 8)
                v.setSpacing(8)
                lbl_video = QLabel("Sin señal")
                lbl_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
                lbl_video.setMinimumSize(460, 360)
                lbl_video.setStyleSheet(
                    "background: #0d0d0d; border: 2px dashed #333333; border-radius: 12px;"
                    " color: #6b6b6b; font-size: 15px;"
                )
                lbl_estado = QLabel("Cámara: detenida")
                lbl_estado.setObjectName("estado_off")
                lbl_estado.setAlignment(Qt.AlignmentFlag.AlignCenter)
                v.addWidget(lbl_video, 1)
                v.addWidget(lbl_estado, 0)
                self.labels_video[cam_id] = lbl_video
                self.labels_estado_cam[cam_id] = lbl_estado
                grid.addWidget(grupo, i // cols, i % cols)
            for c in range(cols):
                grid.setColumnStretch(c, 1)

        # setWidget() borra por sí mismo el contenedor anterior (y sus hijos);
        # los dicts labels_video/labels_estado_cam ya se vaciaron arriba.
        self.scroll_paneles.setWidget(contenedor)

    def _modulo_asistencia_activo(self):
        return True

    # ---------------- Pestaña Historial ----------------
    def _construir_tab_historial(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        grupo_fecha = QGroupBox("Seleccionar día")
        layout_fecha = QHBoxLayout(grupo_fecha)
        self.date_edit = QDateEdit(QDate.currentDate())
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("dd/MM/yyyy")
        self.date_edit.dateChanged.connect(lambda _: self._actualizar_historial())
        btn_refresh = QPushButton("Actualizar")
        btn_refresh.clicked.connect(self._actualizar_historial)
        layout_fecha.addWidget(QLabel("Fecha:"))
        layout_fecha.addWidget(self.date_edit)
        layout_fecha.addStretch()
        layout_fecha.addWidget(btn_refresh)
        layout.addWidget(grupo_fecha)

        grupo_tabla = QGroupBox("Asistencias del día seleccionado")
        layout_tabla = QVBoxLayout(grupo_tabla)
        self.tabla_historial = QTableWidget(0, 6)
        self.tabla_historial.setHorizontalHeaderLabels(
            ["Nombre", "Curso", "Grupo", "Estado", "Entrada", "Salida / Permanencia"])
        self.tabla_historial.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabla_historial.verticalHeader().setVisible(False)
        self.tabla_historial.setAlternatingRowColors(True)
        layout_tabla.addWidget(self.tabla_historial)
        layout.addWidget(grupo_tabla, stretch=1)

        return tab

    # ---------------- Pestaña Alumnos ----------------
    def _construir_tab_alumnos(self):
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(12)

        barra = QHBoxLayout()
        barra.setSpacing(10)
        self.btn_agregar_alumno = QPushButton("Agregar alumno")
        self.btn_agregar_alumno.setObjectName("exito")
        self.btn_agregar_alumno.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_agregar_alumno.clicked.connect(self._agregar_alumno)
        self.chk_inactivos = QCheckBox("Mostrar dados de baja")
        self.chk_inactivos.toggled.connect(lambda _v: self._actualizar_galeria())
        barra.addWidget(self.btn_agregar_alumno)
        barra.addWidget(self.chk_inactivos)
        barra.addStretch(1)
        layout.addLayout(barra)

        grupo_galeria = QGroupBox("Alumnos registrados — clic en una tarjeta para editar")
        layout_galeria = QVBoxLayout(grupo_galeria)
        layout_galeria.setContentsMargins(10, 6, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.galeria_alumnos = QWidget()
        self.galeria_alumnos.setStyleSheet("background: #0a0a0a;")
        self.grid_alumnos = QGridLayout(self.galeria_alumnos)
        self.grid_alumnos.setContentsMargins(0, 0, 0, 0)
        self.grid_alumnos.setSpacing(12)
        scroll.setWidget(self.galeria_alumnos)
        layout_galeria.addWidget(scroll)
        layout.addWidget(grupo_galeria)

        return tab

    def _actualizar_galeria(self):
        while self.grid_alumnos.count():
            item = self.grid_alumnos.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        alumnos = gestor.listar(incluir_inactivos=self.chk_inactivos.isChecked())
        columnas = 5
        self.grid_alumnos.setColumnStretch(0, 1)
        self.grid_alumnos.setColumnStretch(columnas + 1, 1)
        for i, alumno in enumerate(alumnos):
            tarjeta = TarjetaAlumno(alumno)
            tarjeta.clickeado.connect(self._editar_alumno)
            self.grid_alumnos.addWidget(tarjeta, i // columnas, 1 + i % columnas)
        if not alumnos:
            lbl = QLabel("No hay alumnos registrados. Use \"Agregar alumno\".")
            lbl.setStyleSheet("color:#6b6b6b; background: transparent; border: none;")
            self.grid_alumnos.addWidget(lbl, 0, 1)

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
        self.statusBar().showMessage(mensaje)

    # ---------------- Referencias / reconocimiento ----------------
    def _cargar_referencias(self):
        self.referencias = []
        for a in gestor.listar():
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
        self.statusBar().showMessage("Recalculando embeddings faltantes…")
        self._trabajo_recarga = Trabajo(gestor.completar_embeddings, self)
        self._trabajo_recarga.listo.connect(self._on_recarga_lista)
        self._trabajo_recarga.start()

    def _on_recarga_lista(self, _ok, mensaje):
        self.btn_recargar.setEnabled(True)
        self._cargar_referencias()
        self._actualizar_galeria()
        self.statusBar().showMessage(mensaje)

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
        self.btn_iniciar.setEnabled(False)
        self.btn_detener.setEnabled(True)
        self.lbl_estado.setText(f"Cámaras: iniciando ({len(self.hilos)})…")
        self.lbl_estado.setObjectName("estado_off")
        self._repolish(self.lbl_estado)

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
        self._limpiar_filas()
        self._tras_detener()

        if self._enlace_wifi is not None:
            self._enlace_wifi.detener()
            self._enlace_wifi = None

    def _tras_detener(self):
        estado_lcd.actualizar_esperando()
        self._actualizar_simulador_lcd()
        self.btn_iniciar.setEnabled(True)
        self.btn_detener.setEnabled(False)
        self.lbl_estado.setText("Cámaras: detenidas")
        self.lbl_estado.setObjectName("estado_off")
        self._repolish(self.lbl_estado)
        for lbl in self.labels_estado_cam.values():
            lbl.setText("Cámara: detenida")
            lbl.setObjectName("estado_off")
            self._repolish(lbl)
        for lbl in self.labels_video.values():
            lbl.setText("Sin señal")
            lbl.setPixmap(QPixmap())

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

    def _repolish(self, widget):
        widget.style().unpolish(widget)
        widget.style().polish(widget)

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
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
        lbl.setPixmap(qimg_scaled(qimg, lbl))

    def _on_estado_cam(self, cam_id, activa):
        lbl = self.labels_estado_cam.get(cam_id)
        if lbl is not None:
            lbl.setText("Cámara: activa" if activa else "Cámara: detenida")
            lbl.setObjectName("estado_on" if activa else "estado_off")
            self._repolish(lbl)
        if activa:
            self.lbl_estado.setText(f"Cámaras: activas ({len(self.hilos)})")
            self.lbl_estado.setObjectName("estado_on")
            self._repolish(self.lbl_estado)

    def _on_error_cam(self, cam_id, mensaje):
        hilo = self.hilos.pop(cam_id, None)
        if hilo is not None:
            hilo.detener()
        self.mostrar_toast(f"{self._nombre_cam(cam_id)}: {mensaje}", "error")
        if not self.hilos:
            self._tras_detener()

    def _on_progreso(self, cam_id, track_id, progreso, nombre, alumno_id, _caja):
        self._ultimo_progreso_track[(cam_id, track_id)] = time.monotonic()
        estado_lcd.actualizar_procesamiento(nombre=nombre, progreso=progreso)
        self._actualizar_simulador_lcd()
        self.lbl_sin_rostros.hide()
        clave = (cam_id, track_id)
        if clave not in self.filas_progreso:
            fila = FilaProgreso(track_id, f"{nombre} · {self._nombre_cam(cam_id)}")
            self.filas_progreso[clave] = fila
            self.layout_rostros.insertWidget(self.layout_rostros.count() - 1, fila)
        seg_confirm = float(self.cfg["asistencia"]["segundos_confirmacion"])
        self.filas_progreso[clave].actualizar(
            progreso, f"{progreso * seg_confirm:.1f} s / {seg_confirm:.1f} s")

    def _on_confirmada(self, cam_id, track_id, nombre, alumno_id, similitud):
        estado_lcd.actualizar_confirmado(nombre=nombre)
        self._actualizar_simulador_lcd()
        clave = (cam_id, track_id)
        if clave in self.filas_progreso:
            self.filas_progreso[clave].confirmado()

        ahora_ts = time.time()
        ultimo = self._ultimo_registro.get(alumno_id, 0)
        if ahora_ts - ultimo >= self._intervalo_registro:
            self._ultimo_registro[alumno_id] = ahora_ts
            asistencia_id = db_centinela.registrar_entrada(
                alumno_id, float(similitud), self._nombre_cam(cam_id))
            if asistencia_id is not None:
                self.statusBar().showMessage(
                    f"Entrada confirmada: {nombre} ({similitud:.2f})")

        QTimer.singleShot(2000, lambda c=cam_id, t=track_id: self._quitar_fila(c, t))

    def _quitar_fila(self, cam_id, track_id):
        clave = (cam_id, track_id)
        if clave in self.filas_progreso:
            fila = self.filas_progreso.pop(clave)
            self._ultimo_progreso_track.pop(clave, None)
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
        if not hasattr(self, "date_edit"):
            return
        fecha = self.date_edit.date().toString("yyyy-MM-dd")
        alumnos = db_centinela.obtener_estado_fecha(fecha)
        self.tabla_historial.setRowCount(len(alumnos))
        for fila, a in enumerate(alumnos):
            estado = "Presente" if a.get("entrada_abierta") else "Ausente"
            entrada = a.get("entrada_abierta", "")
            salida = a.get("ultima_salida", "")
            minutos = a.get("minutos", 0)
            valores = [
                a.get("nombre", ""),
                a.get("curso", "") or "",
                a.get("grupo", "") or "",
                estado,
                entrada[11:19] if entrada else "",
                f"{salida[11:19]}  |  {minutos} min" if salida or minutos else "—",
            ]
            for col, val in enumerate(valores):
                item = QTableWidgetItem(str(val))
                if col == 3:
                    if val == "Presente":
                        item.setForeground(QColor("#5fd08b"))
                        item.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
                    else:
                        item.setForeground(QColor("#a3a3a3"))
                self.tabla_historial.setItem(fila, col, item)

    # ---------------- Callbacks de Configuración ----------------
    def _on_camaras_cambiadas(self, cfg):
        self.cfg = cfg
        en_marcha = bool(self.hilos)
        self._detener_camaras()
        self._reconstruir_paneles()
        if en_marcha:
            self._iniciar_camaras()
        self.statusBar().showMessage("Configuración de cámaras actualizada.")

    def _on_parametros_cambiados(self, cfg):
        self.cfg = cfg
        self._intervalo_registro = float(cfg["asistencia"]["intervalo_antiduplicado"])
        for hilo in self.hilos.values():
            hilo.umbral_global = float(cfg["asistencia"]["umbral_similitud"])
            hilo.confirm_seg = float(cfg["asistencia"]["segundos_confirmacion"])
        self.statusBar().showMessage(
            "Parámetros de asistencia guardados y aplicados a las cámaras en marcha.")

    # ---------------- Cierre ----------------
    def closeEvent(self, event):
        self._detener_camaras()
        event.accept()


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


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    ventana = VentanaPrincipal()
    ventana.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
