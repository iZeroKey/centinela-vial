"""Widget de vista previa para la pantalla LCD 16x2.

Muestra exactamente las dos filas que se envian al LCD fisico.
"""

import os
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import QLabel, QBoxLayout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qfluentwidgets import HeaderCardWidget
from estado_lcd import obtener_datos_lcd
from lcd_animacion import obtener_filas_animadas


class SimuladorLCD(HeaderCardWidget):
    """Muestra en la UI las dos filas del estado LCD actual."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Simulacion LCD 16x2")
        self.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout = self.viewLayout
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.lbl_pantalla = QLabel()
        self.lbl_pantalla.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pantalla.setTextFormat(Qt.TextFormat.PlainText)
        self.lbl_pantalla.setStyleSheet(
            "background: #0f1a12; color: #7cffb2;"
            "border: 1px solid #2a4a34; border-radius: 10px;"
            "padding: 10px 12px; font-family: 'Consolas', 'Courier New';"
            "font-size: 16px; font-weight: 600; letter-spacing: 2px;"
        )
        fuente = QFont("Consolas", 16)
        fuente.setWeight(QFont.Weight.DemiBold)
        fuente.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        self.lbl_pantalla.setFont(fuente)
        fm = QFontMetrics(fuente)
        ancho = fm.horizontalAdvance("M" * 16) + 28
        self.lbl_pantalla.setFixedWidth(ancho)
        self.lbl_pantalla.setMinimumHeight(54)
        layout.addWidget(self.lbl_pantalla, alignment=Qt.AlignmentFlag.AlignCenter)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.actualizar)
        self._timer.start(100)

        self.actualizar()

    def actualizar(self, datos=None):
        datos = datos or obtener_datos_lcd()
        fila_1, fila_2 = obtener_filas_animadas()
        # Espacios no separables para que Qt no colapse los espacios finales
        f1 = fila_1.replace(" ", "\u00a0")
        f2 = fila_2.replace(" ", "\u00a0")
        self.lbl_pantalla.setText(f"{f1}\n{f2}")