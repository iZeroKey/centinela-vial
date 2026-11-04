#!/usr/bin/env python3
"""
Centinela Control — Utilidades de UI (v6).
"""

import cv2
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPixmap


def ndarray_a_pixmap(frame_bgr):
    """Convierte un ndarray BGR de OpenCV en QPixmap (copia el buffer)."""
    rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    qimg = QImage(rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
    return QPixmap.fromImage(qimg)


def escalar_pixmap(pix, ancho, alto):
    """Escala un pixmap manteniendo proporción, con suavizado."""
    return pix.scaled(ancho, alto,
                      Qt.AspectRatioMode.KeepAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
