#!/usr/bin/env python3
"""
Centinela Control — Diálogo de captura de foto de alumno (v6)

Modal de captura: elige una cámara configurada, muestra preview en vivo
(sin inferencia) y captura el frame actual. Reutiliza CamaraHilo con
reconocer=False para heredar todo el manejo de streams MJPEG/ESP32.

Si la fuente elegida está en uso por la Vigilancia, los callbacks
pausar_fuente/reanudar (provistos por la ventana) la detienen y
reanudan: en Windows no se puede abrir la misma webcam dos veces.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QDialogButtonBox,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from camara_hilo import CamaraHilo
from estilos import ESTILO
from ui_util import ndarray_a_pixmap, escalar_pixmap


class DialogoCaptura(QDialog):
    """Captura una foto desde una cámara configurada. Resultado: self.imagen."""

    def __init__(self, camaras, pausar_fuente=None, reanudar=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Capturar foto del alumno")
        self.setMinimumSize(680, 620)
        self.setStyleSheet(ESTILO)

        self._camaras = camaras
        self._pausar = pausar_fuente
        self._reanudar = reanudar
        self._pausado = False
        self.hilo = None
        self.imagen = None          # ndarray BGR capturado (resultado)
        self._congelado = False
        self._ultimo_frame = None

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        fila_cam = QHBoxLayout()
        lbl = QLabel("Cámara:")
        lbl.setStyleSheet("color:#a3a3a3; font-size:12px; font-weight:bold; background:transparent; border:none;")
        self.combo = QComboBox()
        for cam in camaras:
            tipo_txt = "Webcam local" if cam.get("tipo") == "local" else "Stream HTTP"
            self.combo.addItem(f"{cam.get('nombre') or cam.get('id', '?')} — {tipo_txt}")
        self.combo.currentIndexChanged.connect(self._cambio_camara)
        fila_cam.addWidget(lbl)
        fila_cam.addWidget(self.combo, 1)
        layout.addLayout(fila_cam)

        self.lbl_video = QLabel("Iniciando cámara…")
        self.lbl_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_video.setMinimumSize(560, 380)
        self.lbl_video.setStyleSheet(
            "background: #0d0d0d; border: 2px dashed #333333; border-radius: 12px;"
            " color: #6b6b6b; font-size: 15px;"
        )
        layout.addWidget(self.lbl_video, 1)

        self.lbl_error = QLabel("")
        self.lbl_error.setWordWrap(True)
        self.lbl_error.setStyleSheet("color:#e58a8a; font-size: 12px; background:transparent; border:none;")
        layout.addWidget(self.lbl_error)

        fila_botones = QHBoxLayout()
        self.btn_capturar = QPushButton("Capturar")
        self.btn_capturar.setObjectName("exito")
        self.btn_capturar.clicked.connect(self._capturar)
        self.btn_repetir = QPushButton("Repetir")
        self.btn_repetir.setObjectName("secundario")
        self.btn_repetir.setEnabled(False)
        self.btn_repetir.clicked.connect(self._repetir)
        self.btn_usar = QPushButton("Usar foto")
        self.btn_usar.setEnabled(False)
        self.btn_usar.clicked.connect(self.accept)
        self.btn_cancelar = QPushButton("Cancelar")
        self.btn_cancelar.setObjectName("secundario")
        self.btn_cancelar.clicked.connect(self.reject)
        fila_botones.addWidget(self.btn_capturar, 1)
        fila_botones.addWidget(self.btn_repetir, 1)
        fila_botones.addWidget(self.btn_usar, 1)
        fila_botones.addWidget(self.btn_cancelar, 1)
        layout.addLayout(fila_botones)

    # ---------------- Lógica ----------------
    def _camara_actual(self):
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self._camaras):
            return self._camaras[idx]
        return None

    def _fuente_actual(self):
        cam = self._camara_actual()
        if cam is None:
            return ""
        fuente = str(cam.get("fuente", "")).strip()
        if cam.get("tipo") == "http":
            return fuente
        return fuente or "0"

    def _iniciar_preview(self):
        self._detener_hilo()
        fuente = self._fuente_actual()
        if not fuente:
            self.lbl_error.setText("La cámara seleccionada no tiene fuente configurada.")
            return
        self.lbl_error.setText("")
        if self._pausar is not None and not self._pausado:
            # Libera la fuente por si la Vigilancia la tiene abierta
            self._pausar(fuente)
            self._pausado = True
        self._congelado = False
        self.btn_usar.setEnabled(False)
        self.btn_repetir.setEnabled(False)
        self.hilo = CamaraHilo(fuente, [], reconocer=False)
        self.hilo.frame_listo.connect(self._on_frame)
        self.hilo.error.connect(self._on_error)
        self.hilo.start()

    def _detener_hilo(self):
        if self.hilo is not None:
            self.hilo.detener()
            self.hilo = None

    def _cerrar(self):
        self._detener_hilo()
        if self._pausado and self._reanudar is not None:
            self._reanudar()
            self._pausado = False

    # ---------------- Eventos ----------------
    def showEvent(self, event):
        super().showEvent(event)
        self._iniciar_preview()

    def _cambio_camara(self, _idx):
        self._iniciar_preview()

    def _on_frame(self, frame, _detecciones):
        if frame is None or self._congelado:
            return
        self._ultimo_frame = frame
        self.lbl_video.setPixmap(escalar_pixmap(ndarray_a_pixmap(frame),
                                                self.lbl_video.width(),
                                                self.lbl_video.height()))

    def _on_error(self, mensaje):
        self.lbl_error.setText(mensaje)

    def _capturar(self):
        if self._ultimo_frame is None:
            self.lbl_error.setText("Aún no llega imagen de la cámara.")
            return
        self.imagen = self._ultimo_frame.copy()
        self._congelado = True
        self.btn_usar.setEnabled(True)
        self.btn_repetir.setEnabled(True)
        self.lbl_error.setText("")
        self.lbl_video.setPixmap(escalar_pixmap(ndarray_a_pixmap(self.imagen),
                                                self.lbl_video.width(),
                                                self.lbl_video.height()))

    def _repetir(self):
        self.imagen = None
        self._congelado = False
        self.btn_usar.setEnabled(False)
        self.btn_repetir.setEnabled(False)

    def accept(self):
        self._cerrar()
        super().accept()

    def reject(self):
        self._cerrar()
        super().reject()

    def closeEvent(self, event):
        self._cerrar()
        super().closeEvent(event)
