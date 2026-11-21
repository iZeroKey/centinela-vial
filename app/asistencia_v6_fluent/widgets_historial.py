#!/usr/bin/env python3
"""
Centinela Control — Widgets del Historial dinámico (v6)

Vista de matriz: filas = alumnos (foto + nombre + % de asistencia),
columnas = TODOS los días del rango, agrupada por curso y luego por
sección dentro de tarjetas plegables. Cada celda muestra la hora de
entrada (modo "Detalles") o solo un icono de estado (nunca caracteres
como "x" o "·"). La lógica de datos vive en ventana_principal.py.
"""

import os, sys
from datetime import datetime

from PyQt6.QtCore import Qt, QRect, QRectF, QSize
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QImage, QBrush
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QHeaderView, QTableWidgetItem, QLabel,
)
from qfluentwidgets import (
    CardWidget, SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    TransparentToolButton, IconWidget, InfoBadge, TableWidget, TableItemDelegate,
    isDarkTheme, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DIAS_ES = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
DIAS_CORTOS = ["L", "M", "X", "J", "V", "S", "D"]
MESES_ES = ["ene", "feb", "mar", "abr", "may", "jun",
            "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_legible(fecha_iso):
    """'2026-11-12' -> 'Jueves 12 nov 2026'."""
    try:
        d = datetime.strptime(str(fecha_iso)[:10], "%Y-%m-%d")
        return f"{DIAS_ES[d.weekday()]} {d.day} {MESES_ES[d.month - 1]} {d.year}"
    except ValueError:
        return str(fecha_iso)


def hhmm(iso):
    """'2026-11-12T08:03:44' -> '08:03'. None si no hay hora."""
    return str(iso)[11:16] if iso else None


def _etiqueta_dia(fecha_iso):
    """'2026-11-12' -> 'J 12' (cabecera compacta de columna)."""
    try:
        d = datetime.strptime(str(fecha_iso)[:10], "%Y-%m-%d")
        return f"{DIAS_CORTOS[d.weekday()]} {d.day:02d}"
    except ValueError:
        return str(fecha_iso)[5:]


def badge_estado(estado, cantidad=None, parent=None):
    """InfoBadge con el color y conteo opcional según el estado."""
    etiqueta = f"{estado} · {cantidad}" if cantidad is not None else (estado or "—")
    if estado == "Presente":
        return InfoBadge.success(etiqueta, parent=parent)
    if estado == "Tardanza":
        return InfoBadge.warning(etiqueta, parent=parent)
    if estado == "Ausente":
        return InfoBadge.error(etiqueta, parent=parent)
    return InfoBadge.custom(etiqueta, QColor("#e4e4e7"),
                            QColor("#3f3f46"), parent=parent)


# (fondo, texto/icono) por estado, en tema oscuro y claro
_PALETAS = {
    "Presente": {
        True:  (QColor(16, 185, 129, 42),  QColor("#34d399")),
        False: (QColor("#d1fae5"),         QColor("#065f46")),
    },
    "Tardanza": {
        True:  (QColor(245, 158, 11, 50),  QColor("#fbbf24")),
        False: (QColor("#fef3c7"),         QColor("#92400e")),
    },
    "Ausente": {
        True:  (QColor(239, 68, 68, 40),   QColor("#f87171")),
        False: (QColor("#fee2e2"),         QColor("#991b1b")),
    },
    "Sin clase": {
        True:  (QColor(139, 151, 168, 30), QColor("#8b97a8")),
        False: (QColor("#f3f4f6"),         QColor("#6b7280")),
    },
    "Sin registro": {
        True:  (QColor(139, 151, 168, 16), QColor("#5c6572")),
        False: (QColor("#f9fafb"),         QColor("#9ca3af")),
    },
}

# icono de estado cuando NO hay hora que mostrar
_ICONOS = {
    "Presente":  FIcon.ACCEPT,
    "Tardanza":  FIcon.STOP_WATCH,
    "Ausente":   FIcon.CLOSE,
    "Sin clase": FIcon.REMOVE,
    "Sin registro": FIcon.REMOVE,
}


def _tooltip_celda(celda):
    """Detalle completo de la celda para el tooltip."""
    if not celda:
        return ""
    estado = celda.get("estado", "Sin registro")
    if estado == "Sin clase":
        return "Sin clase este día (fuera del horario o vigencia del curso)."
    if estado == "Sin registro":
        return "El sistema no registró actividad este día."
    if estado == "Ausente":
        return "Sin entrada registrada (había clase)."
    txt = f"Entrada {hhmm(celda.get('entrada'))}"
    if celda.get("salida"):
        txt += f" · Salida {hhmm(celda['salida'])}"
    if celda.get("minutos"):
        txt += f" · {celda['minutos']} min"
    if celda.get("confianza") is not None:
        txt += f" · Confianza {round(celda['confianza'] * 100)}%"
    if (celda.get("marcas") or 0) > 1:
        txt += f" · {celda['marcas']} marcas"
    return txt


def _icono_estado(estado, color):
    """QIcon de un estado con el color dado."""
    base = _ICONOS.get(estado, FIcon.REMOVE)
    return base.icon(color=color)


def _avatar_pixmap(ruta, size=32):
    """Foto redondeada del alumno; icono de persona si no hay foto."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), size / 2.4, size / 2.4)
    img = QImage(ruta) if ruta else QImage()
    if not img.isNull():
        p.setClipPath(path)
        escalada = img.scaled(size, size,
                              Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                              Qt.TransformationMode.SmoothTransformation)
        x = (size - escalada.width()) // 2
        y = (size - escalada.height()) // 2
        p.drawImage(x, y, escalada)
        p.setClipping(False)
        lapiz = QColor(255, 255, 255, 46) if isDarkTheme() else QColor(0, 0, 0, 30)
        p.setPen(lapiz)
        p.drawPath(path)
    else:
        oscuro = isDarkTheme()
        p.fillPath(path, QColor(255, 255, 255, 26) if oscuro
                   else QColor(0, 0, 0, 12))
        icono = FIcon.PEOPLE.icon(
            QColor("#8b97a8") if oscuro else QColor("#6b7280"))
        pm = icono.pixmap(QSize(size - 8, size - 8))
        p.drawPixmap((size - pm.width()) // 2, (size - pm.height()) // 2, pm)
    p.end()
    return pix


class _DelegadoCeldaHistorial(TableItemDelegate):
    """Dibuja fondo, texto o icono estrictamente centrados en columnas de días."""

    def paint(self, painter, option, index):
        if index.column() == 0:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect.adjusted(2, 3, -2, -3)

        fondo = index.data(Qt.ItemDataRole.BackgroundRole)
        if isinstance(fondo, QBrush):
            painter.setBrush(fondo)
        elif isinstance(fondo, QColor):
            painter.setBrush(QBrush(fondo))
        else:
            painter.setBrush(QColor(139, 151, 168, 16))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 6, 6)

        texto = index.data(Qt.ItemDataRole.DisplayRole)
        icono = index.data(Qt.ItemDataRole.DecorationRole)
        if texto:
            fuente = index.data(Qt.ItemDataRole.FontRole) or option.font
            painter.setFont(fuente)
            fg = index.data(Qt.ItemDataRole.ForegroundRole)
            color = fg.color() if isinstance(fg, QBrush) else (
                fg if isinstance(fg, QColor) else QColor("#e6edf3"))
            painter.setPen(color)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(texto))
        elif isinstance(icono, QIcon) and not icono.isNull():
            tam = 15
            icono_rect = QRect(0, 0, tam, tam)
            icono_rect.moveCenter(rect.center())
            icono.paint(painter, icono_rect, Qt.AlignmentFlag.AlignCenter)

        painter.restore()


class FilaNombre(QWidget):
    """Celda de nombre: foto redondeada + apellidos, nombres + % de asistencia."""

    def __init__(self, info, parent=None):
        super().__init__(parent)
        self.setStyleSheet("background: transparent;")
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(10)

        foto = QLabel(self)
        foto.setPixmap(_avatar_pixmap(info.get("foto"), 32))
        foto.setFixedSize(34, 34)
        foto.setAlignment(Qt.AlignmentFlag.AlignCenter)
        h.addWidget(foto, 0, Qt.AlignmentFlag.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        nombre = info.get("nombre", "") or "Sin nombre"
        lbl = BodyLabel(self)
        f = QFont()
        f.setWeight(QFont.Weight.DemiBold)
        lbl.setFont(f)
        lbl.setText(lbl.fontMetrics().elidedText(
            nombre, Qt.TextElideMode.ElideRight, 240))
        lbl.setWordWrap(False)
        lbl.setToolTip(_tooltip_alumno(info))
        col.addWidget(lbl)
        pct = info.get("porcentaje")
        txt = f"{round(pct * 100)}% de asistencias" if pct is not None \
            else "Sin clases en el rango"
        cap = CaptionLabel(txt, self)
        cap.setStyleSheet(
            f"color: {'#34d399' if (pct or 0) >= 0.85 else ('#fbbf24' if (pct or 0) >= 0.60 else '#f87171')};"
            if pct is not None else "color: rgba(139, 151, 168, 0.9);")
        col.addWidget(cap)
        h.addLayout(col, 1)


class TablaSeccion(QWidget):
    """Matriz de una sección: filas = alumnos, columnas = días del rango."""

    def __init__(self, seccion, dias, filas, detalles=True, parent=None):
        super().__init__(parent)
        self.filas = list(filas)
        self.dias = list(dias)
        self.seccion = str(seccion or "")
        self.detalles = bool(detalles)

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 10, 16, 10)
        v.setSpacing(8)

        cab = QHBoxLayout()
        lbl = StrongBodyLabel(
            f"Sección {self.seccion}" if self.seccion else "(sin sección)", self)
        n = CaptionLabel(
            f"· {len(filas)} alumno{'s' if len(filas) != 1 else ''}", self)
        n.setStyleSheet("color: rgba(139, 151, 168, 0.95);")
        cab.addWidget(lbl)
        cab.addWidget(n)
        cab.addStretch(1)
        v.addLayout(cab)

        self.tabla = TableWidget(self)
        self.tabla.setItemDelegate(_DelegadoCeldaHistorial(self.tabla))
        self.tabla.setRowCount(len(self.filas))
        self.tabla.setColumnCount(len(self.dias) + 1)
        self.tabla.setHorizontalHeaderLabels(
            ["Alumno"] + [_etiqueta_dia(f) for f in self.dias])
        self.tabla.horizontalHeaderItem(0).setTextAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        for c, fecha in enumerate(self.dias, start=1):
            item_h = self.tabla.horizontalHeaderItem(c)
            item_h.setToolTip(fecha_legible(fecha))
            item_h.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(TableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionMode(TableWidget.SelectionMode.NoSelection)
        self.tabla.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabla.setAlternatingRowColors(False)
        self.tabla.verticalHeader().setDefaultSectionSize(50)

        hdr = self.tabla.horizontalHeader()
        hdr.setMinimumHeight(34)
        ancho_dia = 76 if self.detalles else 48
        # Con pocos días la columna Alumno absorbe el espacio sobrante; con muchos días
        # conserva un ancho cómodo para mostrar Apellidos, Nombres sin aplastarse.
        if len(self.dias) * ancho_dia > 680:
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            self.tabla.setColumnWidth(0, 305)
        else:
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            hdr.setMinimumSectionSize(48)
            self.tabla.setColumnWidth(0, 305)
        for c in range(1, len(self.dias) + 1):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeMode.Fixed)
            self.tabla.setColumnWidth(c, ancho_dia)

        oscuro = isDarkTheme()
        for r, info in enumerate(self.filas):
            self.tabla.setCellWidget(r, 0, FilaNombre(info, self.tabla))
            item_nombre = QTableWidgetItem()
            item_nombre.setToolTip(_tooltip_alumno(info))
            self.tabla.setItem(r, 0, item_nombre)
            for c, fecha in enumerate(self.dias, start=1):
                self.tabla.setItem(r, c, self._item_celda(
                    info["celdas"].get(fecha), oscuro))
        v.addWidget(self.tabla)
        # altura de contenido: el scroll vertical lo lleva la página (scroll
        # exterior); la tabla crece con sus filas (header 30 + filas de 44 px).
        # El padding cubre la barra horizontal cuando hay muchos días.
        self._fijar_altura_tabla()

    def _fijar_altura_tabla(self):
        """Fija la altura de la tabla a su contenido (sin scroll vertical)."""
        hdr = self.tabla.horizontalHeader()
        alto = max(hdr.height(), hdr.minimumHeight()) \
            + len(self.filas) * 50 + 16
        self.tabla.setFixedHeight(alto)

    def _item_celda(self, celda, oscuro):
        """Item de celda: hora (con detalles) o icono de estado."""
        celda = celda or {"estado": "Sin registro"}
        estado = celda.get("estado", "Sin registro")
        fondo, color = _PALETAS.get(estado, _PALETAS["Sin registro"])[oscuro]
        item = QTableWidgetItem()
        item.setBackground(fondo)
        item.setForeground(color)
        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        item.setToolTip(_tooltip_celda(celda))
        hora = hhmm(celda.get("entrada"))
        if self.detalles and hora and estado in ("Presente", "Tardanza"):
            item.setText(hora)
        else:
            item.setIcon(_icono_estado(estado, color))
        return item


def _tooltip_alumno(info):
    """Resumen del alumno para el tooltip de la columna de nombre."""
    pct = info.get("porcentaje")
    partes = [str(info.get("nombre", "") or "Sin nombre")]
    if pct is not None:
        partes.append(f"Asistencia: {round(pct * 100)}%")
    return "\n".join(partes)


class TarjetaGrupoCurso(CardWidget):
    """Grupo plegable de un curso: cabecera con conteos + matriz por sección."""

    def __init__(self, curso, subtitulo, conteos, parent=None):
        super().__init__(parent)
        self.expandida = True
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(self.alternar)

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        cabecera = QWidget()
        cabecera.setStyleSheet("background: transparent;")
        h = QHBoxLayout(cabecera)
        h.setContentsMargins(16, 12, 16, 10)
        h.setSpacing(10)
        self.btn_chevron = TransparentToolButton(FIcon.CARE_UP_SOLID, cabecera)
        self.btn_chevron.clicked.connect(self.alternar)
        h.addWidget(self.btn_chevron)
        col = QVBoxLayout()
        col.setSpacing(2)
        self.lbl_curso = SubtitleLabel(str(curso) or "(sin curso)", cabecera)
        self.lbl_sub = CaptionLabel(subtitulo, cabecera)
        col.addWidget(self.lbl_curso)
        col.addWidget(self.lbl_sub)
        h.addLayout(col, 1)
        for texto, n in conteos:
            h.addSpacing(6)
            h.addWidget(badge_estado(texto, n, cabecera), 0, Qt.AlignmentFlag.AlignVCenter)
        v.addWidget(cabecera)

        self.cuerpo = QWidget()
        self.cuerpo.setStyleSheet("background: transparent;")
        self.v_cuerpo = QVBoxLayout(self.cuerpo)
        self.v_cuerpo.setContentsMargins(8, 2, 8, 12)
        self.v_cuerpo.setSpacing(8)
        v.addWidget(self.cuerpo)

    def alternar(self, *_):
        self.expandida = not self.expandida
        self.cuerpo.setVisible(self.expandida)
        self.btn_chevron.setIcon(
            FIcon.CARE_UP_SOLID if self.expandida else FIcon.CARE_DOWN_SOLID)


class EstadoVacioHistorial(QWidget):
    """Mensaje centrado cuando no hay registros (o la búsqueda no encuentra)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(24, 48, 24, 48)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        icono = IconWidget(FIcon.HISTORY, self)
        icono.setFixedSize(56, 56)
        v.addWidget(icono, 0, Qt.AlignmentFlag.AlignCenter)

        self.lbl_titulo = SubtitleLabel("Sin registros en el rango", self)
        v.addWidget(self.lbl_titulo, 0, Qt.AlignmentFlag.AlignCenter)

        self.lbl_hinta = CaptionLabel(
            "Elige otro rango de fechas o registra asistencias en Vigilancia.", self)
        v.addWidget(self.lbl_hinta, 0, Qt.AlignmentFlag.AlignCenter)

    def setTextos(self, titulo, hinta):
        self.lbl_titulo.setText(titulo)
        self.lbl_hinta.setText(hinta)
