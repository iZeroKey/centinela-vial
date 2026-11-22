#!/usr/bin/env python3
"""
Centinela Control — Utilidades de UI y caché de miniaturas (v6).
"""

import hashlib
import os
import tempfile

import cv2
import numpy as np
from PyQt6.QtCore import Qt, QRect, QRectF, QSize
from PyQt6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPixmap

_THUMB_SIZE = 160
_THUMB_DIR = os.path.join(tempfile.gettempdir(), "centinela_thumbs")
_AVATAR_CACHE = {}
_FALLBACK_CACHE = {}


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


def _cargar_thumb_qimage(ruta):
    """Devuelve un QImage cuadrado de hasta _THUMB_SIZE px usando caché en disco."""
    try:
        st = os.stat(ruta)
        clave_str = f"{os.path.abspath(ruta)}|{st.st_mtime_ns}|{st.st_size}"
    except OSError:
        return QImage()

    h = hashlib.sha1(clave_str.encode("utf-8")).hexdigest()[:16]
    thumb_path = os.path.join(_THUMB_DIR, f"{h}_{_THUMB_SIZE}.jpg")
    if os.path.isfile(thumb_path):
        img = QImage(thumb_path)
        if not img.isNull():
            return img

    try:
        datos = np.fromfile(ruta, dtype=np.uint8)
        flag = (cv2.IMREAD_REDUCED_COLOR_4
                if st.st_size > 80_000 else cv2.IMREAD_COLOR)
        bgr = cv2.imdecode(datos, flag)
        if bgr is None and flag != cv2.IMREAD_COLOR:
            bgr = cv2.imdecode(datos, cv2.IMREAD_COLOR)
    except Exception:
        bgr = None

    if bgr is None or bgr.size == 0:
        return QImage()

    ih, iw = bgr.shape[:2]
    lado = min(ih, iw)
    y0 = (ih - lado) // 2
    x0 = (iw - lado) // 2
    cuadro = bgr[y0:y0 + lado, x0:x0 + lado]
    if lado != _THUMB_SIZE:
        interp = cv2.INTER_AREA if lado > _THUMB_SIZE else cv2.INTER_LINEAR
        cuadro = cv2.resize(cuadro, (_THUMB_SIZE, _THUMB_SIZE), interpolation=interp)

    try:
        os.makedirs(_THUMB_DIR, exist_ok=True)
        cv2.imwrite(thumb_path, cuadro, [int(cv2.IMWRITE_JPEG_QUALITY), 88])
    except Exception:
        pass

    rgb = cv2.cvtColor(cuadro, cv2.COLOR_BGR2RGB)
    return QImage(rgb.data, _THUMB_SIZE, _THUMB_SIZE,
                  _THUMB_SIZE * 3, QImage.Format.Format_RGB888).copy()


def _avatar_fallback(size, radio_ratio, oscuro):
    clave = (size, radio_ratio, oscuro)
    pix = _FALLBACK_CACHE.get(clave)
    if pix is not None:
        return pix

    from qfluentwidgets import FluentIcon as FIcon
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    borde = 1.0
    lado = size - borde * 2
    forma = QPainterPath()
    forma.addRoundedRect(
        QRectF(borde, borde, lado, lado),
        size * radio_ratio, size * radio_ratio,
    )
    p.fillPath(forma, QColor(255, 255, 255, 22) if oscuro else QColor(0, 0, 0, 12))
    icono = FIcon.PEOPLE.icon(color=QColor("#8b97a8") if oscuro else QColor("#6b7280"))
    icono.paint(
        p,
        QRect(int(size * 0.28), int(size * 0.28),
              int(size * 0.44), int(size * 0.44)),
        Qt.AlignmentFlag.AlignCenter,
    )
    p.setPen(QPen(QColor(255, 255, 255, 28) if oscuro else QColor(0, 0, 0, 32), borde))
    p.drawPath(forma)
    p.end()
    _FALLBACK_CACHE[clave] = pix
    return pix


def avatar_redondeado(ruta, size, radio_ratio=0.24, oscuro=True):
    """Devuelve QPixmap cuadrado redondeado desde caché (0 ms en accesos repetidos)."""
    size = max(16, int(size))
    if not ruta:
        return _avatar_fallback(size, radio_ratio, oscuro)
    try:
        mtime_ns = os.stat(ruta).st_mtime_ns
    except OSError:
        return _avatar_fallback(size, radio_ratio, oscuro)

    clave = (ruta, size, radio_ratio, oscuro, mtime_ns)
    cached = _AVATAR_CACHE.get(clave)
    if cached is not None:
        return cached

    img = _cargar_thumb_qimage(ruta)
    if img.isNull():
        return _avatar_fallback(size, radio_ratio, oscuro)

    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    borde = 1.0
    lado = size - borde * 2
    forma = QPainterPath()
    forma.addRoundedRect(
        QRectF(borde, borde, lado, lado),
        size * radio_ratio, size * radio_ratio,
    )
    p.setClipPath(forma)
    escalada = img.scaled(
        size, size,
        Qt.AspectRatioMode.KeepAspectRatioByExpanding,
        Qt.TransformationMode.SmoothTransformation,
    )
    p.drawImage(0, 0, escalada)
    p.setClipping(False)
    p.setPen(QPen(QColor(255, 255, 255, 36) if oscuro else QColor(0, 0, 0, 30), borde))
    p.drawPath(forma)
    p.end()

    if len(_AVATAR_CACHE) > 600:
        _AVATAR_CACHE.clear()
    _AVATAR_CACHE[clave] = pix
    return pix
