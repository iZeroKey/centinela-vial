#!/usr/bin/env python3
"""
Centinela Control — Diálogo de captura de foto de alumno

Modal de captura: elige una cámara configurada, muestra preview en vivo
(sin inferencia) y captura el frame actual. Reutiliza CamaraHilo con
reconocer=False para heredar todo el manejo de streams MJPEG/ESP32.

Si la fuente elegida está en uso por la Vigilancia, los callbacks
pausar_fuente/reanudar (provistos por la ventana) la detienen y
reanudan: en Windows no se puede abrir la misma webcam dos veces.

El diálogo es un MessageBoxBase de qfluentwidgets (estilo Fluent):
"Usar foto" es el botón primario (yesButton) y "Cancelar" el secundario.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QLabel
from qfluentwidgets import (
    ComboBox, PushButton, PrimaryPushButton,
    MessageBoxBase, SubtitleLabel, CaptionLabel, isDarkTheme,
)

from centinela.vision.camara_hilo import CamaraHilo
from centinela.ui.ui_util import ndarray_a_pixmap, escalar_pixmap
from centinela.core import config as config_app


class DialogoCaptura(MessageBoxBase):
    """Captura una foto desde una cámara configurada. Resultado: self.imagen."""

    def __init__(self, camaras, pausar_fuente=None, reanudar=None, parent=None):
        super().__init__(parent=parent)
        self._camaras = camaras
        self._pausar = pausar_fuente
        self._reanudar = reanudar
        self._pausado = False
        self.hilo = None
        self.imagen = None          # ndarray BGR capturado (resultado)
        self._congelado = False
        self._ultimo_frame = None

        self.widget.setMinimumWidth(680)

        self.titleLabel = SubtitleLabel("Capturar foto del alumno")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        fila_cam = QHBoxLayout()
        lbl = CaptionLabel("Cámara:")
        self.combo = ComboBox()
        _etiquetas_tipo = {
            "local": "Webcam local",
            "esp32": "ESP32-CAM",
            "http": "Stream HTTP",
            "telefono": "Teléfono",
        }
        for cam in camaras:
            tipo_txt = _etiquetas_tipo.get(cam.get("tipo"), "Stream HTTP")
            self.combo.addItem(f"{cam.get('nombre') or cam.get('id', '?')} — {tipo_txt}")
        self.combo.currentIndexChanged.connect(self._cambio_camara)
        fila_cam.addWidget(lbl)
        fila_cam.addWidget(self.combo, 1)
        self.viewLayout.addLayout(fila_cam)

        oscuro = isDarkTheme()
        fondo = "rgba(255, 255, 255, 0.05)" if oscuro else "rgba(0, 0, 0, 0.04)"
        borde = "rgba(255, 255, 255, 0.16)" if oscuro else "rgba(0, 0, 0, 0.14)"
        self.lbl_video = QLabel("Iniciando cámara…")
        self.lbl_video.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_video.setMinimumSize(600, 380)
        self.lbl_video.setStyleSheet(
            f"background: {fondo}; border: 1px dashed {borde}; border-radius: 12px;"
            " color: rgba(128, 128, 128, 0.9); font-size: 14px;"
        )
        self.viewLayout.addWidget(self.lbl_video, 1)

        self.lbl_error = CaptionLabel("")
        self.lbl_error.setWordWrap(True)
        self.viewLayout.addWidget(self.lbl_error)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(10)
        self.btn_capturar = PrimaryPushButton("Capturar")
        self.btn_capturar.setMinimumHeight(32)
        self.btn_capturar.clicked.connect(self._capturar)
        self.btn_repetir = PushButton("Repetir")
        self.btn_repetir.setMinimumHeight(32)
        self.btn_repetir.setEnabled(False)
        self.btn_repetir.clicked.connect(self._repetir)
        fila_botones.addWidget(self.btn_capturar, 1)
        fila_botones.addWidget(self.btn_repetir, 1)
        self.viewLayout.addLayout(fila_botones)

        # "Usar foto" = botón primario Fluent del diálogo; "Cancelar" = secundario.
        self.yesButton.setText("Usar foto")
        self.cancelButton.setText("Cancelar")
        self.yesButton.setEnabled(False)
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self.accept)
        self.cancelButton.clicked.disconnect()
        self.cancelButton.clicked.connect(self.reject)

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
        return config_app.fuente_de_camara(cam)

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
        self.yesButton.setEnabled(False)
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
        if self.hilo is not None:
            # Preview/overlay a resolucion del label (no de la captura)
            self.hilo.set_tamano_display(self.lbl_video.width(), self.lbl_video.height())
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
        self.yesButton.setEnabled(True)
        self.btn_repetir.setEnabled(True)
        self.lbl_error.setText("")
        self.lbl_video.setPixmap(escalar_pixmap(ndarray_a_pixmap(self.imagen),
                                                self.lbl_video.width(),
                                                self.lbl_video.height()))

    def _repetir(self):
        self.imagen = None
        self._congelado = False
        self.yesButton.setEnabled(False)
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
