#!/usr/bin/env python3
"""
Toast no bloqueante para Centinela Control.

Notificación flotante que aparece en la esquina superior derecha de la ventana
padre y se cierra sola tras `duracion_ms`. No bloquea la interacción con la UI
(como sí lo hace QMessageBox).

Tipos: "info", "success", "warning", "error".
"""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PyQt6.QtWidgets import QFrame, QLabel, QHBoxLayout, QGraphicsOpacityEffect


_COLORES = {
    "info": ("#1a1a1a", "#ededed", "#3a3a3a"),
    "success": ("#163024", "#5fd08b", "#2a5a40"),
    "warning": ("#2a2414", "#e6c87a", "#5a4a20"),
    "error": ("#2a1414", "#e58a8a", "#5a3030"),
}


class Toast(QFrame):
    def __init__(self, parent, mensaje, tipo="info", duracion_ms=3000):
        super().__init__(parent)
        self.duracion_ms = duracion_ms
        bg, fg, border = _COLORES.get(tipo, _COLORES["info"])

        self.setObjectName("toast")
        self.setStyleSheet(
            f"QFrame#toast {{ background: {bg}; color: {fg}; border: 1px solid {border}; "
            f"border-radius: 10px; }} "
            f"QLabel {{ color: {fg}; background: transparent; border: none; "
            f"font-size: 13px; }}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        lbl = QLabel(mensaje)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(380)
        layout.addWidget(lbl)

        self.adjustSize()

        self._opacity = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._opacity)
        self._opacity.setOpacity(0.0)

        self._anim_in = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim_in.setDuration(180)
        self._anim_in.setStartValue(0.0)
        self._anim_in.setEndValue(1.0)
        self._anim_in.setEasingCurve(QEasingCurve.Type.OutCubic)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._cerrar)

        self.show()
        self._anim_in.start()
        self._timer.start(self.duracion_ms)

    def _cerrar(self):
        self._anim_out = QPropertyAnimation(self._opacity, b"opacity", self)
        self._anim_out.setDuration(220)
        self._anim_out.setStartValue(1.0)
        self._anim_out.setEndValue(0.0)
        self._anim_out.setEasingCurve(QEasingCurve.Type.InCubic)
        self._anim_out.finished.connect(self.deleteLater)
        self._anim_out.start()
