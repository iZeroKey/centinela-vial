"""Widget de vista previa para la pantalla LCD 16x2."""

import os
import sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QBoxLayout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qfluentwidgets import HeaderCardWidget
from estado_lcd import obtener_datos_lcd


class SimuladorLCD(HeaderCardWidget):
    """Muestra en la UI las dos filas del estado LCD actual."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTitle("Simulación LCD 16x2")
        self.setMinimumHeight(126)
        self.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        layout = self.viewLayout
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self.lbl_pantalla = QLabel()
        self.lbl_pantalla.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_pantalla.setMinimumHeight(62)
        self.lbl_pantalla.setStyleSheet(
            "background: #0a170c; color: #8dff9c;"
            "border: 2px solid #315d38; border-radius: 8px;"
            "padding: 10px; font-family: 'Consolas', 'Courier New';"
            "font-size: 18px; font-weight: bold; letter-spacing: 2px;"
        )
        layout.addWidget(self.lbl_pantalla)

        self.lbl_estado = QLabel()
        self.lbl_estado.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_estado.setStyleSheet(
            "color: #8b97a8; font-size: 11px;"
            "background: transparent; border: none;"
        )
        layout.addWidget(self.lbl_estado)
        self.actualizar()

    def actualizar(self, datos=None):
        """Refresca la vista con el estado dado o con el estado compartido."""
        datos = datos or obtener_datos_lcd()
        fila_1 = (datos.get("fila_1") or "").replace(" ", "\u00a0")
        fila_2 = (datos.get("fila_2") or "").replace(" ", "\u00a0")
        self.lbl_pantalla.setText(f"{fila_1}\n{fila_2}")
        self.lbl_estado.setText(
            f"Estado: {datos.get('estado', 'esperando')}"
            f"  ·  Actualizado: {datos.get('actualizado_en', '—')}"
        )
