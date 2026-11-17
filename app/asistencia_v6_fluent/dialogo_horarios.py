#!/usr/bin/env python3
"""
Centinela Control — Editor de horarios de clase (v6)

Dialogo Fluent (MessageBoxBase) para gestionar franjas horarias por
curso+seccion. Cada franja define dias de la semana y hora de inicio/fin.

Convencion: franja con seccion vacia = vale para todas las secciones del
curso. Cursos y secciones se gestionan en "Cursos y secciones" (config).
Una franja totalmente vacia se descarta al guardar; una medio rellenada
dispara un aviso de validacion (el dialogo no se cierra).
"""

import os, re, sys

from PyQt6.QtCore import Qt, QRegularExpression
from PyQt6.QtGui import QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget,
)
from qfluentwidgets import (
    LineEdit, PushButton, CheckBox, EditableComboBox,
    MessageBoxBase, SubtitleLabel, CaptionLabel, ScrollArea,
    CardWidget, TransparentToolButton, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dialogos_fluent import aviso
import config as config_app

_RE_HORA = QRegularExpression(r"^([01]\d|2[0-3]):[0-5]\d$")
DIAS_CORTOS = ["L", "M", "X", "J", "V", "S", "D"]


class TarjetaFranja(CardWidget):
    """Tarjeta de edicion de una franja horaria."""

    def __init__(self, franja=None, cfg=None, on_borrar=None, parent=None):
        super().__init__(parent)
        self._f = dict(franja or {})
        self._cfg = cfg or {}
        on_borrar = on_borrar or (lambda _c: None)
        # la seccion historica 'grupo' se respeta en franjas antiguas
        self._f.setdefault("seccion", self._f.get("grupo", ""))

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)

        # Fila 1: nombre + borrar
        fila1 = QHBoxLayout()
        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("Nombre de la franja (p. ej. Clase de manana)")
        self.edit_nombre.setText(self._f.get("nombre", "") or "")
        btn_borrar = TransparentToolButton(FIcon.DELETE)
        btn_borrar.setToolTip("Quitar esta franja")
        btn_borrar.clicked.connect(lambda: on_borrar(self))
        fila1.addWidget(self.edit_nombre, 1)
        fila1.addWidget(btn_borrar)
        v.addLayout(fila1)

        # Fila 2: curso + seccion (cursos/secciones definidos en Configuracion)
        self.combo_curso = EditableComboBox()
        self.combo_curso.addItems(config_app.nombres_cursos(self._cfg))
        self.combo_curso.setText(self._f.get("curso", "") or "")
        self.combo_curso.currentTextChanged.connect(self._refrescar_secciones)

        self.combo_seccion = EditableComboBox()
        self.combo_seccion.setText(self._f.get("seccion", "") or "")
        self._refrescar_secciones()

        fila2 = QHBoxLayout()
        fila2.setSpacing(8)
        lbl_curso = CaptionLabel("Curso")
        lbl_seccion = CaptionLabel("Sección (vacía = todas)")
        fila2.addWidget(lbl_curso, 0, Qt.AlignmentFlag.AlignRight)
        fila2.addWidget(self.combo_curso, 1)
        fila2.addWidget(lbl_seccion, 0, Qt.AlignmentFlag.AlignRight)
        fila2.addWidget(self.combo_seccion, 1)
        v.addLayout(fila2)

        # Fila 3: dias de la semana
        fila3 = QHBoxLayout()
        fila3.setSpacing(6)
        lbl_dias = CaptionLabel("Dias")
        lbl_dias.setFixedWidth(108)
        fila3.addWidget(lbl_dias)
        dias_previos = set(self._f.get("dias", []) or [])
        self._checks_dias = []
        for i, letra in enumerate(DIAS_CORTOS):
            cb = CheckBox(letra)
            cb.setChecked(i in dias_previos)
            cb.setFixedWidth(34)
            self._checks_dias.append(cb)
            fila3.addWidget(cb)
        fila3.addStretch(1)
        v.addLayout(fila3)

        # Fila 4: inicio / fin
        validator = QRegularExpressionValidator(_RE_HORA)
        self.edit_inicio = LineEdit()
        self.edit_inicio.setPlaceholderText("08:00")
        self.edit_inicio.setText(self._f.get("inicio", "") or "")
        self.edit_inicio.setValidator(validator)
        self.edit_inicio.setFixedWidth(90)
        self.edit_fin = LineEdit()
        self.edit_fin.setPlaceholderText("12:00")
        self.edit_fin.setText(self._f.get("fin", "") or "")
        self.edit_fin.setValidator(validator)
        self.edit_fin.setFixedWidth(90)

        fila4 = QHBoxLayout()
        fila4.setSpacing(8)
        fila4.addWidget(CaptionLabel("Inicio"), 0, Qt.AlignmentFlag.AlignRight)
        fila4.addWidget(self.edit_inicio)
        fila4.addWidget(CaptionLabel("Fin"), 0, Qt.AlignmentFlag.AlignRight)
        fila4.addWidget(self.edit_fin)
        fila4.addStretch(1)
        v.addLayout(fila4)

    def _refrescar_secciones(self, *_):
        """Sugerencias de seccion segun el curso elegido."""
        texto = self.combo_seccion.text() if hasattr(self, "combo_seccion") else ""
        self.combo_seccion.clear()
        self.combo_seccion.addItems(
            config_app.secciones_de_curso(self._cfg, self.combo_curso.text()))
        self.combo_seccion.setText(texto)

    def dias(self):
        return [i for i, cb in enumerate(self._checks_dias) if cb.isChecked()]

    def datos(self):
        # parte de la franja original para conservar campos extra
        # (desde/hasta, futuros) y solo sobreescribir lo editable
        d = dict(self._f)
        d.update({
            "id": self._f.get("id"),
            "nombre": self.edit_nombre.text().strip(),
            "curso": self.combo_curso.text().strip(),
            "seccion": self.combo_seccion.text().strip(),
            "dias": self.dias(),
            "inicio": self.edit_inicio.text().strip(),
            "fin": self.edit_fin.text().strip(),
        })
        return d

    def esta_vacia(self):
        d = self.datos()
        return (not d["nombre"] and not d["curso"] and not d["seccion"]
                and not d["dias"] and not d["inicio"] and not d["fin"])


def _validar_datos_franja(d, parent, etiqueta="Franja"):
    """Validacion compartida de una franja; True si es aceptable."""
    _RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
    if not d["curso"]:
        aviso(parent, etiqueta, "Indica el curso de la franja.")
        return False
    if not d["dias"]:
        aviso(parent, etiqueta, "Marca al menos un dia de la semana.")
        return False
    if not _RE.match(d["inicio"]) or not _RE.match(d["fin"]):
        aviso(parent, etiqueta, "Horas en formato 24 h (p. ej. 08:00 y 12:00).")
        return False
    if d["inicio"] >= d["fin"]:
        aviso(parent, etiqueta, "La hora de inicio debe ser anterior a la de fin.")
        return False
    return True


class DialogoFranja(MessageBoxBase):
    """Alta/edicion de UNA franja horaria (curso+seccion, dias, horas)."""

    def __init__(self, franja=None, cfg=None, parent=None):
        super().__init__(parent=parent)
        self._cfg = cfg or {}
        self.widget.setMinimumWidth(560)

        self.titleLabel = SubtitleLabel(
            "Nueva franja" if franja is None else "Editar franja")
        self.viewLayout.addWidget(self.titleLabel)

        self._tarjeta = TarjetaFranja(franja, self._cfg)
        self.viewLayout.addWidget(self._tarjeta)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def datos(self):
        return self._tarjeta.datos()

    def validate(self):
        d = self.datos()
        if self._tarjeta.esta_vacia():
            aviso(self, "Franja vacía",
                  "Rellena al menos curso, dias y horas.")
            return False
        return _validar_datos_franja(d, self)


class DialogoHorarios(MessageBoxBase):
    """Gestion de franjas horarias por curso+seccion."""

    def __init__(self, franjas=None, cfg=None, parent=None):
        super().__init__(parent=parent)
        franjas = franjas or []
        self._cfg = cfg or {}
        self._tarjetas = []

        self.widget.setMinimumWidth(620)

        self.titleLabel = SubtitleLabel("Horarios de clase")
        self.viewLayout.addWidget(self.titleLabel)

        ayuda = CaptionLabel(
            "Cada franja define dias y horas para un curso (y seccion opcional).\n"
            "El Historial usa el horario para calcular tardanzas y 'sin clase'.\n"
            "Cursos y secciones se definen en 'Cursos y secciones'.")
        ayuda.setWordWrap(True)
        self.viewLayout.addWidget(ayuda)

        # Lista de franjas en scroll
        self._cuerpo = QWidget()
        self._cuerpo.setStyleSheet("background: transparent;")
        self._v = QVBoxLayout(self._cuerpo)
        self._v.setContentsMargins(0, 4, 0, 4)
        self._v.setSpacing(10)
        self._v.addStretch(1)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._cuerpo)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setFixedHeight(330)
        self.viewLayout.addWidget(scroll)

        self.btn_agregar = PushButton("Agregar franja")
        self.btn_agregar.clicked.connect(lambda: self._agregar_tarjeta())
        self.viewLayout.addWidget(self.btn_agregar, 0, Qt.AlignmentFlag.AlignLeft)

        for f in franjas:
            self._agregar_tarjeta(f)
        if not self._tarjetas:
            self._agregar_tarjeta()   # arrancar con una franja lista para editar

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    # ---------------- Gestión de tarjetas ----------------
    def _agregar_tarjeta(self, franja=None):
        tarjeta = TarjetaFranja(franja, self._cfg,
                                on_borrar=self._borrar_tarjeta)
        self._tarjetas.append(tarjeta)
        # insertar antes del stretch final
        self._v.insertWidget(self._v.count() - 1, tarjeta)

    def _borrar_tarjeta(self, tarjeta):
        if tarjeta in self._tarjetas:
            self._tarjetas.remove(tarjeta)
        tarjeta.setParent(None)
        tarjeta.deleteLater()

    def franjas_finales(self):
        """Franjas validas (descarta las que estan totalmente vacias)."""
        return [t.datos() for t in self._tarjetas if not t.esta_vacia()]

    # ---------------- Validación ----------------
    def validate(self):
        """MessageBoxBase llama a validate() antes de aceptar el cierre."""
        for i, t in enumerate(self._tarjetas, start=1):
            if t.esta_vacia():
                continue   # se descarta en silencio al guardar
            if not _validar_datos_franja(t.datos(), self, f"Franja {i}"):
                return False
        return True
