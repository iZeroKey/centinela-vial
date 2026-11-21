#!/usr/bin/env python3
"""
Centinela Control — Editor de horarios de clase (v6)

Diálogo Fluent (MessageBoxBase) para gestionar franjas horarias por
curso+sección. Cada franja define días de la semana, hora de inicio/fin
(TimePicker 24 h) y vigencia opcional por fechas (CalendarPicker).
"""

import os, sys

from PyQt6.QtCore import Qt, QTime, QDate
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QWidget
from qfluentwidgets import (
    LineEdit, PushButton, CheckBox, EditableComboBox,
    TimePicker, CalendarPicker,
    MessageBoxBase, SubtitleLabel, CaptionLabel, ScrollArea,
    CardWidget, TransparentToolButton, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dialogos_fluent import aviso
import config as config_app

DIAS_CORTOS = ["L", "M", "X", "J", "V", "S", "D"]


def _parse_qtime(hhmm, fallback_h=8, fallback_m=0):
    """Convierte 'HH:MM' a QTime válido (con fallback)."""
    try:
        partes = str(hhmm or "").strip().split(":")
        if len(partes) >= 2:
            t = QTime(int(partes[0]), int(partes[1]))
            if t.isValid():
                return t
    except (TypeError, ValueError):
        pass
    return QTime(fallback_h, fallback_m)


def _parse_qdate(iso):
    """Convierte 'YYYY-MM-DD' a QDate (inválido si está vacío)."""
    s = str(iso or "").strip()[:10]
    if not s:
        return QDate()
    d = QDate.fromString(s, "yyyy-MM-dd")
    return d if d.isValid() else QDate()


class TarjetaFranja(CardWidget):
    """Tarjeta de edición de una franja horaria."""

    def __init__(self, franja=None, cfg=None, on_borrar=None,
                 mostrar_borrar=True, bloquear_curso=False, parent=None):
        super().__init__(parent)
        self._f = dict(franja or {})
        self._cfg = cfg or {}
        on_borrar = on_borrar or (lambda _c: None)
        self._f.setdefault("seccion", self._f.get("grupo", ""))

        v = QVBoxLayout(self)
        v.setContentsMargins(16, 14, 16, 14)
        v.setSpacing(12)

        # Fila 1: nombre + borrar
        fila1 = QHBoxLayout()
        fila1.setSpacing(8)
        lbl_nom = CaptionLabel("Nombre")
        lbl_nom.setFixedWidth(64)
        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("Ej. Turno mañana o Laboratorio")
        self.edit_nombre.setText(self._f.get("nombre", "") or "")
        self.edit_nombre.setClearButtonEnabled(True)
        fila1.addWidget(lbl_nom)
        fila1.addWidget(self.edit_nombre, 1)
        if mostrar_borrar:
            btn_borrar = TransparentToolButton(FIcon.DELETE)
            btn_borrar.setToolTip("Quitar esta franja")
            btn_borrar.clicked.connect(lambda: on_borrar(self))
            fila1.addWidget(btn_borrar)
        v.addLayout(fila1)

        # Fila 2: curso + sección
        self.combo_curso = EditableComboBox()
        self.combo_curso.setPlaceholderText("Elige o escribe el curso")
        self.combo_curso.addItems(config_app.nombres_cursos(self._cfg))
        self.combo_curso.setText(self._f.get("curso", "") or "")
        self.combo_curso.setCursorPosition(0)
        self.combo_curso.currentTextChanged.connect(self._refrescar_secciones)

        self.combo_seccion = EditableComboBox()
        self.combo_seccion.setPlaceholderText("Todas las secciones")
        self.combo_seccion.setText(self._f.get("seccion", "") or "")
        self.combo_seccion.setCursorPosition(0)
        self._refrescar_secciones()

        if bloquear_curso:
            self.combo_curso.setEnabled(False)

        fila2 = QHBoxLayout()
        fila2.setSpacing(10)
        lbl_curso = CaptionLabel("Curso")
        lbl_curso.setFixedWidth(64)
        lbl_seccion = CaptionLabel("Sección")
        fila2.addWidget(lbl_curso)
        fila2.addWidget(self.combo_curso, 1)
        fila2.addWidget(lbl_seccion)
        fila2.addWidget(self.combo_seccion, 1)
        v.addLayout(fila2)

        # Fila 3: días de la semana
        fila3 = QHBoxLayout()
        fila3.setSpacing(8)
        lbl_dias = CaptionLabel("Días")
        lbl_dias.setFixedWidth(64)
        fila3.addWidget(lbl_dias)
        dias_previos = set(self._f.get("dias", []) or [])
        self._checks_dias = []
        for i, letra in enumerate(DIAS_CORTOS):
            cb = CheckBox(letra)
            cb.setChecked(i in dias_previos)
            cb.setMinimumWidth(46)
            self._checks_dias.append(cb)
            fila3.addWidget(cb)
        fila3.addStretch(1)
        v.addLayout(fila3)

        # Fila 4: hora de inicio / fin (TimePicker 24 h)
        self.picker_inicio = TimePicker(showSeconds=False)
        self.picker_inicio.setColumnWidth(0, 72)
        self.picker_inicio.setColumnWidth(1, 72)
        self.picker_inicio.setTime(_parse_qtime(self._f.get("inicio"), 8, 0))

        self.picker_fin = TimePicker(showSeconds=False)
        self.picker_fin.setColumnWidth(0, 72)
        self.picker_fin.setColumnWidth(1, 72)
        self.picker_fin.setTime(_parse_qtime(self._f.get("fin"), 12, 0))

        fila4 = QHBoxLayout()
        fila4.setSpacing(10)
        lbl_ini = CaptionLabel("Inicio")
        lbl_ini.setFixedWidth(64)
        fila4.addWidget(lbl_ini)
        fila4.addWidget(self.picker_inicio)
        fila4.addSpacing(12)
        fila4.addWidget(CaptionLabel("Fin"))
        fila4.addWidget(self.picker_fin)
        fila4.addStretch(1)
        v.addLayout(fila4)

        # Fila 5: vigencia opcional por fechas
        desde_ini = _parse_qdate(self._f.get("desde"))
        hasta_ini = _parse_qdate(self._f.get("hasta"))
        tiene_vigencia = desde_ini.isValid() or hasta_ini.isValid()

        self.chk_vigencia = CheckBox("Limitar vigencia por fechas")
        self.chk_vigencia.setChecked(tiene_vigencia)

        self.picker_desde = CalendarPicker()
        self.picker_desde.setResetEnabled(True)
        if desde_ini.isValid():
            self.picker_desde.setDate(desde_ini)
        else:
            self.picker_desde.setText("Desde")

        self.picker_hasta = CalendarPicker()
        self.picker_hasta.setResetEnabled(True)
        if hasta_ini.isValid():
            self.picker_hasta.setDate(hasta_ini)
        else:
            self.picker_hasta.setText("Hasta")

        self._fila_fechas = QWidget()
        self._fila_fechas.setStyleSheet("background: transparent;")
        hf = QHBoxLayout(self._fila_fechas)
        hf.setContentsMargins(64, 0, 0, 0)
        hf.setSpacing(10)
        hf.addWidget(CaptionLabel("Desde"))
        hf.addWidget(self.picker_desde)
        hf.addSpacing(8)
        hf.addWidget(CaptionLabel("Hasta"))
        hf.addWidget(self.picker_hasta)
        hf.addStretch(1)

        self.chk_vigencia.toggled.connect(self._fila_fechas.setVisible)
        self._fila_fechas.setVisible(tiene_vigencia)

        v.addWidget(self.chk_vigencia)
        v.addWidget(self._fila_fechas)

    def _refrescar_secciones(self, *_):
        """Sugerencias de sección según el curso elegido."""
        texto = self.combo_seccion.text() if hasattr(self, "combo_seccion") else ""
        self.combo_seccion.clear()
        self.combo_seccion.addItems(
            config_app.secciones_de_curso(self._cfg, self.combo_curso.text()))
        self.combo_seccion.setText(texto)
        self.combo_seccion.setCursorPosition(0)

    def dias(self):
        return [i for i, cb in enumerate(self._checks_dias) if cb.isChecked()]

    def datos(self):
        d = dict(self._f)
        t_ini = self.picker_inicio.getTime()
        t_fin = self.picker_fin.getTime()
        d.update({
            "id": self._f.get("id"),
            "nombre": self.edit_nombre.text().strip(),
            "curso": self.combo_curso.text().strip(),
            "seccion": self.combo_seccion.text().strip(),
            "dias": self.dias(),
            "inicio": t_ini.toString("HH:mm") if t_ini.isValid() else "08:00",
            "fin": t_fin.toString("HH:mm") if t_fin.isValid() else "12:00",
        })
        if self.chk_vigencia.isChecked():
            d_desde = self.picker_desde.getDate()
            d_hasta = self.picker_hasta.getDate()
            if d_desde.isValid():
                d["desde"] = d_desde.toString("yyyy-MM-dd")
            else:
                d.pop("desde", None)
            if d_hasta.isValid():
                d["hasta"] = d_hasta.toString("yyyy-MM-dd")
            else:
                d.pop("hasta", None)
        else:
            d.pop("desde", None)
            d.pop("hasta", None)
        return d

    def esta_vacia(self):
        d = self.datos()
        return not d["nombre"] and not d["curso"] and not d["dias"]


def _validar_datos_franja(d, parent, etiqueta="Franja"):
    """Validación compartida de una franja; True si es aceptable."""
    if not d["curso"]:
        aviso(parent, etiqueta, "Indica el curso de la franja.")
        return False
    if not d["dias"]:
        aviso(parent, etiqueta, "Marca al menos un día de la semana.")
        return False
    if d["inicio"] >= d["fin"]:
        aviso(parent, etiqueta, "La hora de inicio debe ser anterior a la de fin.")
        return False
    desde = d.get("desde", "")
    hasta = d.get("hasta", "")
    if desde and hasta and desde > hasta:
        aviso(parent, etiqueta, "La fecha de inicio de vigencia no puede ser posterior al fin.")
        return False
    return True


class DialogoFranja(MessageBoxBase):
    """Alta/edición de UNA franja horaria (curso+sección, días, horas)."""

    def __init__(self, franja=None, cfg=None, parent=None,
                 curso_prefijo=None, seccion_prefija=None):
        super().__init__(parent=parent)
        self._cfg = cfg or {}
        self.widget.setMinimumWidth(600)

        base = dict(franja or {})
        if franja is None and curso_prefijo:
            base.setdefault("curso", curso_prefijo)
            if seccion_prefija is not None:
                base.setdefault("seccion", seccion_prefija)

        self.titleLabel = SubtitleLabel(
            "Nueva franja horaria" if franja is None else "Editar franja horaria")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        self._tarjeta = TarjetaFranja(
            base, self._cfg, mostrar_borrar=False,
            bloquear_curso=bool(curso_prefijo and franja is None))
        self.viewLayout.addWidget(self._tarjeta)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def datos(self):
        return self._tarjeta.datos()

    def validate(self):
        return _validar_datos_franja(self.datos(), self)


class DialogoHorarios(MessageBoxBase):
    """Gestión múltiple de franjas horarias por curso+sección."""

    def __init__(self, franjas=None, cfg=None, parent=None):
        super().__init__(parent=parent)
        franjas = franjas or []
        self._cfg = cfg or {}
        self._tarjetas = []

        self.widget.setMinimumWidth(660)

        self.titleLabel = SubtitleLabel("Horarios de clase")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        ayuda = CaptionLabel(
            "Define días, horas y vigencia por curso y sección. "
            "El historial usa estas franjas para calcular sesiones y tardanzas.")
        ayuda.setWordWrap(True)
        self.viewLayout.addWidget(ayuda)

        self._cuerpo = QWidget()
        self._cuerpo.setStyleSheet("background: transparent;")
        self._v = QVBoxLayout(self._cuerpo)
        self._v.setContentsMargins(0, 4, 10, 4)
        self._v.setSpacing(12)
        self._v.addStretch(1)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cuerpo)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setFixedHeight(360)
        self.viewLayout.addWidget(scroll)

        self.btn_agregar = PushButton("Agregar franja")
        self.btn_agregar.setMinimumHeight(32)
        self.btn_agregar.clicked.connect(lambda: self._agregar_tarjeta())
        self.viewLayout.addWidget(self.btn_agregar, 0, Qt.AlignmentFlag.AlignLeft)

        for f in franjas:
            self._agregar_tarjeta(f)
        if not self._tarjetas:
            self._agregar_tarjeta()

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _agregar_tarjeta(self, franja=None):
        tarjeta = TarjetaFranja(franja, self._cfg,
                                on_borrar=self._borrar_tarjeta)
        self._tarjetas.append(tarjeta)
        self._v.insertWidget(self._v.count() - 1, tarjeta)

    def _borrar_tarjeta(self, tarjeta):
        if tarjeta in self._tarjetas:
            self._tarjetas.remove(tarjeta)
        tarjeta.setParent(None)
        tarjeta.deleteLater()

    def franjas_finales(self):
        return [t.datos() for t in self._tarjetas if not t.esta_vacia()]

    def validate(self):
        for i, t in enumerate(self._tarjetas, start=1):
            if t.esta_vacia():
                continue
            if not _validar_datos_franja(t.datos(), self, f"Franja {i}"):
                return False
        return True
