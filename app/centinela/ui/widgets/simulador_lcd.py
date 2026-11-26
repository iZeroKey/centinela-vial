"""Widget de vista previa para la pantalla LCD 16x2.

Muestra exactamente las dos filas que se envian al LCD fisico.
"""

import os
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import QLabel, QBoxLayout

from qfluentwidgets import HeaderCardWidget
from centinela.lcd.estado_lcd import obtener_datos_lcd
from centinela.lcd.lcd_animacion import obtener_filas_animadas


class SimuladorLCD(HeaderCardWidget):
    """Muestra en la UI las dos filas del estado LCD actual."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Simulación LCD 16x2")
        self.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout = self.viewLayout
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(6)

        self.lbl_pantalla = QLabel()
        self.lbl_pantalla.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pantalla.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_pantalla.setStyleSheet(
            "background: #0b1810; color: #6ef7a7;"
            "border: 2px solid #243e2d; border-radius: 8px;"
            "padding: 10px 14px; font-family: 'Consolas', 'Courier New';"
            "font-size: 16px; font-weight: 600; letter-spacing: 2px;"
        )
        fuente = QFont("Consolas", 16)
        fuente.setWeight(QFont.Weight.DemiBold)
        fuente.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        self.lbl_pantalla.setFont(fuente)
        fm = QFontMetrics(fuente)
        ancho = fm.horizontalAdvance("M" * 16) + 32
        self.lbl_pantalla.setFixedWidth(ancho)
        self.lbl_pantalla.setMinimumHeight(60)
        layout.addWidget(self.lbl_pantalla, alignment=Qt.AlignmentFlag.AlignCenter)

        self._ultimo_texto = None
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.actualizar)
        self._timer.start(100)

        self.actualizar()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._timer.isActive():
            self._timer.start(100)
        self.actualizar()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def actualizar(self, datos=None):
        datos = datos or obtener_datos_lcd()
        fila_1, fila_2 = obtener_filas_animadas()
        # Espacios no separables para que Qt no colapse los espacios finales
        f1 = fila_1.replace(" ", "\u00a0")
        f2 = fila_2.replace(" ", "\u00a0")
        texto = f"{f1}\n{f2}"
        if texto != self._ultimo_texto:
            self._ultimo_texto = texto
            self.lbl_pantalla.setText(texto)