#!/usr/bin/env python3
"""
Centinela Control — Widgets del Historial dinámico

Vista de matriz: filas = alumnos (foto + nombre + % de asistencia),
columnas = TODOS los días del rango, agrupada por curso y luego por
sección dentro de tarjetas plegables. Cada celda muestra la hora de
entrada (modo "Detalles") o solo un icono de estado (nunca caracteres
como "x" o "·"). La lógica de datos vive en ventana_principal.py.
"""

import os, sys
from datetime import datetime

from PyQt6.QtCore import Qt, QRect, QRectF, QSize, QPoint, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap, QImage, QBrush
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QFrame, QHeaderView, QTableWidgetItem,
    QLabel, QPushButton, QSpinBox, QApplication,
)
from qfluentwidgets import (
    CardWidget, SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    PushButton, PrimaryPushButton, TransparentToolButton, IconWidget, InfoBadge,
    TableWidget, TableItemDelegate, isDarkTheme, FluentIcon as FIcon,
)

from centinela.ui.ui_util import avatar_redondeado

_ICONO_CACHE = {}

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
        return "Clic para editar asistencia"
    estado = celda.get("estado", "Sin registro")
    manual = bool(celda.get("manual"))
    suf_manual = " · Editado manualmente" if manual else ""
    if estado == "Sin clase":
        return f"Sin clase este día (fuera del horario o vigencia del curso).{suf_manual}\nClic para modificar."
    if estado == "Sin registro":
        if manual:
            return "Marcado manualmente como vacío (sin registro).\nClic para modificar."
        return "El sistema no registró actividad este día.\nClic para modificar."
    if estado == "Ausente":
        return f"Sin entrada registrada (había clase).{suf_manual}\nClic para modificar."
    hora = hhmm(celda.get("entrada"))
    txt = f"Entrada {hora}" if hora else estado
    if celda.get("salida"):
        txt += f" · Salida {hhmm(celda['salida'])}"
    if celda.get("minutos"):
        txt += f" · {celda['minutos']} min"
    if celda.get("confianza") is not None:
        txt += f" · Confianza {round(celda['confianza'] * 100)}%"
    if (celda.get("marcas") or 0) > 1:
        txt += f" · {celda['marcas']} marcas"
    return f"{txt}{suf_manual}\nClic para modificar."


def _icono_estado(estado, color):
    """QIcon de un estado con el color dado (cacheado por estado+color)."""
    clave = (estado, color.name() if isinstance(color, QColor) else str(color))
    ico = _ICONO_CACHE.get(clave)
    if ico is None:
        base = _ICONOS.get(estado, FIcon.REMOVE)
        ico = base.icon(color=color)
        _ICONO_CACHE[clave] = ico
    return ico


def _avatar_pixmap(ruta, size=32):
    """Foto redondeada del alumno desde caché de miniaturas."""
    return avatar_redondeado(ruta, size, radio_ratio=0.42, oscuro=isDarkTheme())


class _DelegadoCeldaHistorial(TableItemDelegate):
    """Pinta de forma ligera col 0 (avatar+nombre+%) y col >=1 (centrado)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._fuente_nombre = QFont()
        self._fuente_nombre.setPixelSize(13)
        self._fuente_nombre.setWeight(QFont.Weight.DemiBold)
        self._fuente_sub = QFont()
        self._fuente_sub.setPixelSize(11)

    def paint(self, painter, option, index):
        if index.column() == 0:
            info = index.data(Qt.ItemDataRole.UserRole)
            if not isinstance(info, dict):
                super().paint(painter, option, index)
                return

            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            rect = option.rect

            # Avatar redondeado (cacheado en RAM)
            tam = 34
            ay = rect.y() + (rect.height() - tam) // 2
            ax = rect.x() + 10
            pm = _avatar_pixmap(info.get("foto"), tam)
            painter.drawPixmap(ax, ay, pm)

            # Textos: Apellidos, Nombres + % de asistencias
            tx = ax + tam + 10
            tw = max(40, rect.right() - tx - 8)
            nombre = str(info.get("nombre", "") or "Sin nombre")
            oscuro = isDarkTheme()

            painter.setFont(self._fuente_nombre)
            fm_n = painter.fontMetrics()
            nombre_elid = fm_n.elidedText(nombre, Qt.TextElideMode.ElideRight, tw)
            painter.setPen(QColor("#e6edf3") if oscuro else QColor("#111827"))
            painter.drawText(
                QRect(tx, rect.y() + 6, tw, 20),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                nombre_elid,
            )

            pct = info.get("porcentaje")
            if pct is not None:
                sub_txt = f"{round(pct * 100)}% de asistencias"
                color_sub = (
                    QColor("#34d399") if pct >= 0.85
                    else (QColor("#fbbf24") if pct >= 0.60 else QColor("#f87171"))
                )
            else:
                sub_txt = "Sin clases en el rango"
                color_sub = QColor(139, 151, 168, 220)
            painter.setFont(self._fuente_sub)
            painter.setPen(color_sub)
            painter.drawText(
                QRect(tx, rect.y() + 26, tw, 18),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                sub_txt,
            )
            painter.restore()
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

        # Indicador discreto cuando la celda fue ajustada manualmente
        if index.data(Qt.ItemDataRole.UserRole + 1):
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#5eead4"))
            painter.drawEllipse(rect.right() - 7, rect.top() + 3, 4, 4)

        painter.restore()


class _Spin2Digitos(QSpinBox):
    """SpinBox de 2 dígitos con cero a la izquierda (00..23 / 00..59)."""

    def textFromValue(self, val):
        return f"{int(val):02d}"


class PopupEditarAsistencia(QFrame):
    """Desplegable flotante para editar estado y hora de una celda de asistencia."""

    guardado = pyqtSignal(str, str)  # (estado | "__auto__", "HH:MM" | "")

    _OPCIONES = [
        ("Presente", "Asistencia",  "#34d399", "rgba(16, 185, 129, 0.22)"),
        ("Tardanza", "Tardanza",    "#fbbf24", "rgba(245, 158, 11, 0.22)"),
        ("Ausente",  "Falta",       "#f87171", "rgba(239, 68, 68, 0.22)"),
        ("Vacio",    "Vacío",       "#94a3b8", "rgba(148, 163, 184, 0.20)"),
    ]

    def __init__(self, nombre, fecha, celda, hora_sugerida="08:00", parent=None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedWidth(262)

        estado_actual = (celda or {}).get("estado", "Sin registro")
        if estado_actual in ("Sin registro", "Sin clase"):
            estado_actual = "Vacio"
        self._estado = estado_actual
        self._es_manual = bool((celda or {}).get("manual"))

        ext = QVBoxLayout(self)
        ext.setContentsMargins(0, 0, 0, 0)

        card = QFrame(self)
        card.setObjectName("popupAsist")
        card.setStyleSheet(
            "QFrame#popupAsist {"
            " background: #1e242c;"
            " border: 1px solid rgba(255, 255, 255, 0.14);"
            " border-radius: 12px;"
            "}"
        )
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        lbl_nom = StrongBodyLabel(card)
        lbl_nom.setText(lbl_nom.fontMetrics().elidedText(
            str(nombre or "Alumno"), Qt.TextElideMode.ElideRight, 228))
        lbl_fec = CaptionLabel(fecha_legible(fecha), card)
        lbl_fec.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        v.addWidget(lbl_nom)
        v.addWidget(lbl_fec)

        # Botones de estado
        self._botones_estado = {}
        for clave, etiqueta, color, bg_sel in self._OPCIONES:
            btn = QPushButton(etiqueta, card)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFixedHeight(32)
            btn.setIcon(_icono_estado(
                "Sin registro" if clave == "Vacio" else clave, QColor(color)))
            btn.setIconSize(QSize(14, 14))
            btn.clicked.connect(lambda _=False, c=clave: self._seleccionar(c))
            self._botones_estado[clave] = (btn, color, bg_sel)
            v.addWidget(btn)

        # Selector de hora (solo para Asistencia / Tardanza)
        self._fila_hora_w = QWidget(card)
        self._fila_hora_w.setStyleSheet("background: transparent;")
        hh = QHBoxLayout(self._fila_hora_w)
        hh.setContentsMargins(2, 4, 2, 2)
        hh.setSpacing(6)
        lbl_h = CaptionLabel("Hora de entrada:", self._fila_hora_w)
        lbl_h.setStyleSheet("color: #cbd5e1;")

        hora_ini = hhmm((celda or {}).get("entrada")) or hora_sugerida or "08:00"
        try:
            h_val, m_val = [int(x) for x in str(hora_ini).split(":")[:2]]
        except (ValueError, AttributeError):
            h_val, m_val = 8, 0

        estilo_spin = (
            "QSpinBox { background: #131820; color: #f1f5f9;"
            " border: 1px solid rgba(255,255,255,0.16); border-radius: 7px;"
            " padding: 4px 6px; font-weight: 600; font-size: 13px; }"
            "QSpinBox:focus { border-color: #2dd4bf; }"
        )
        self.spin_h = _Spin2Digitos(self._fila_hora_w)
        self.spin_h.setRange(0, 23)
        self.spin_h.setWrapping(True)
        self.spin_h.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin_h.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spin_h.setValue(max(0, min(23, h_val)))
        self.spin_h.setFixedSize(46, 30)
        self.spin_h.setStyleSheet(estilo_spin)

        lbl_sep = QLabel(":", self._fila_hora_w)
        lbl_sep.setStyleSheet("color: #94a3b8; font-weight: 700; font-size: 14px; background: transparent;")

        self.spin_m = _Spin2Digitos(self._fila_hora_w)
        self.spin_m.setRange(0, 59)
        self.spin_m.setWrapping(True)
        self.spin_m.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.spin_m.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.spin_m.setValue(max(0, min(59, m_val)))
        self.spin_m.setFixedSize(46, 30)
        self.spin_m.setStyleSheet(estilo_spin)

        hh.addWidget(lbl_h, 1)
        hh.addWidget(self.spin_h)
        hh.addWidget(lbl_sep)
        hh.addWidget(self.spin_m)
        v.addWidget(self._fila_hora_w)

        # Acciones finales
        acc = QHBoxLayout()
        acc.setSpacing(8)
        if self._es_manual:
            btn_auto = PushButton("Auto", card)
            btn_auto.setMinimumHeight(30)
            btn_auto.setToolTip("Eliminar ajuste manual y restaurar registro automático")
            btn_auto.clicked.connect(self._restaurar_auto)
            acc.addWidget(btn_auto)
        btn_ok = PrimaryPushButton("Aplicar", card)
        btn_ok.setMinimumHeight(30)
        btn_ok.clicked.connect(self._aplicar)
        acc.addWidget(btn_ok, 1)
        v.addLayout(acc)

        ext.addWidget(card)
        self._seleccionar(self._estado)

    def _seleccionar(self, clave):
        self._estado = clave
        for k, (btn, color, bg_sel) in self._botones_estado.items():
            if k == clave:
                btn.setStyleSheet(
                    f"QPushButton {{ background: {bg_sel}; color: {color};"
                    f" border: 1px solid {color}; border-radius: 8px;"
                    " text-align: left; padding: 0 12px; font-weight: 600; font-size: 12px; }"
                )
            else:
                btn.setStyleSheet(
                    "QPushButton { background: rgba(255, 255, 255, 0.04); color: #cbd5e1;"
                    " border: 1px solid rgba(255, 255, 255, 0.08); border-radius: 8px;"
                    " text-align: left; padding: 0 12px; font-weight: 500; font-size: 12px; }"
                    "QPushButton:hover { background: rgba(255, 255, 255, 0.09); color: #f8fafc; }"
                )
        self._fila_hora_w.setVisible(clave in ("Presente", "Tardanza"))
        self.adjustSize()

    def _aplicar(self):
        hora = (
            f"{self.spin_h.value():02d}:{self.spin_m.value():02d}"
            if self._estado in ("Presente", "Tardanza") else ""
        )
        self.guardado.emit(self._estado, hora)
        self.close()

    def _restaurar_auto(self):
        self.guardado.emit("__auto__", "")
        self.close()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._aplicar()
            return
        super().keyPressEvent(event)


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

    celda_editada = pyqtSignal(dict)

    def __init__(self, seccion, dias, filas, detalles=True, parent=None,
                 hora_sugerida_fn=None):
        super().__init__(parent)
        self.filas = list(filas)
        self.dias = list(dias)
        self.seccion = str(seccion or "")
        self.detalles = bool(detalles)
        self._hora_sugerida_fn = hora_sugerida_fn
        self._popup_actual = None

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
        self.tabla.viewport().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabla.cellClicked.connect(self._on_celda_click)
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
        self.tabla.setUpdatesEnabled(False)
        try:
            for r, info in enumerate(self.filas):
                item_nombre = QTableWidgetItem()
                item_nombre.setData(Qt.ItemDataRole.UserRole, info)
                item_nombre.setToolTip(_tooltip_alumno(info))
                self.tabla.setItem(r, 0, item_nombre)
                for c, fecha in enumerate(self.dias, start=1):
                    self.tabla.setItem(r, c, self._item_celda(
                        info["celdas"].get(fecha), oscuro))
        finally:
            self.tabla.setUpdatesEnabled(True)
        v.addWidget(self.tabla)
        self._fijar_altura_tabla()

    def _fijar_altura_tabla(self, filas_visibles=None):
        """Fija la altura de la tabla a su contenido visible (sin scroll vertical)."""
        hdr = self.tabla.horizontalHeader()
        n = len(self.filas) if filas_visibles is None else max(0, int(filas_visibles))
        alto = max(hdr.height(), hdr.minimumHeight()) + n * 50 + 16
        self.tabla.setFixedHeight(alto)

    def _on_celda_click(self, row, col):
        """Abre el desplegable de edición manual para una celda de fecha."""
        if col < 1 or row < 0 or row >= len(self.filas) or (col - 1) >= len(self.dias):
            return
        info = self.filas[row]
        fecha = self.dias[col - 1]
        celda = info["celdas"].get(fecha) or {"estado": "Sin registro"}
        hora_sug = "08:00"
        if callable(self._hora_sugerida_fn):
            try:
                hora_sug = self._hora_sugerida_fn(
                    info.get("curso", ""), info.get("seccion", ""), fecha) or "08:00"
            except Exception:
                hora_sug = "08:00"

        popup = PopupEditarAsistencia(
            info.get("nombre", ""), fecha, celda, hora_sugerida=hora_sug, parent=self)
        popup.guardado.connect(
            lambda est, hr, a=info["alumno_id"], c=info.get("curso", ""),
            s=info.get("seccion", ""), f=fecha: self.celda_editada.emit({
                "alumno_id": a, "curso": c, "seccion": s, "fecha": f,
                "estado": est, "hora": hr,
            })
        )
        self._popup_actual = popup
        popup.adjustSize()

        rect = self.tabla.visualRect(self.tabla.model().index(row, col))
        pos = self.tabla.viewport().mapToGlobal(QPoint(rect.left(), rect.bottom() + 4))
        pantalla = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if pantalla is not None:
            geom = pantalla.availableGeometry()
            x = min(max(geom.left() + 8, pos.x()), geom.right() - popup.width() - 8)
            y = pos.y()
            if y + popup.height() > geom.bottom() - 8:
                pos_arriba = self.tabla.viewport().mapToGlobal(QPoint(rect.left(), rect.top() - 4))
                y = max(geom.top() + 8, pos_arriba.y() - popup.height())
            pos = QPoint(x, y)
        popup.move(pos)
        popup.show()

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
        item.setData(Qt.ItemDataRole.UserRole + 1, bool(celda.get("manual")))
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
