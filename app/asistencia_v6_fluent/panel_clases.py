#!/usr/bin/env python3
"""
Centinela Control — Pestaña Clases (v6)

Sidebar con la configuración de las clases:
- Cursos y secciones: nombre, secciones y CANTIDAD DE CLASES de cada curso.
- Horarios de clase: franjas horarias (curso+sección, días, horas, vigencia).

El Historial deriva de aquí sus columnas: sesiones = días con franja desde
el inicio de la vigencia (o la primera actividad) hasta hoy, limitadas a la
cantidad de clases del curso.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidgetItem,
    QHeaderView, QFrame, QBoxLayout,
)
from qfluentwidgets import (
    PushButton, LineEdit, SpinBox,
    TableWidget, HeaderCardWidget, MessageBoxBase, SubtitleLabel, CaptionLabel,
    ScrollArea, TransparentToolButton, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dialogos_fluent import aviso, pregunta
from dialogo_horarios import TarjetaFranja, DIAS_CORTOS, DialogoFranja
import config as config_app


class DialogoCurso(MessageBoxBase):
    """Alta/edición de un curso: nombre, secciones y cantidad de clases."""

    def __init__(self, curso=None, otros_cursos=None, parent=None):
        super().__init__(parent=parent)
        self._curso = curso or {}
        nombre_actual = str(self._curso.get("nombre", "") or "")
        self._otros = [str(c.get("nombre", "") or "") for c in (otros_cursos or [])
                       if str(c.get("nombre", "") or "") != nombre_actual]
        self.widget.setMinimumWidth(520)

        self.titleLabel = SubtitleLabel(
            "Nuevo curso" if curso is None else "Editar curso")
        self.viewLayout.addWidget(self.titleLabel)

        fila_nombre = QHBoxLayout()
        lbl_nombre = CaptionLabel("Nombre:")
        lbl_nombre.setFixedWidth(70)
        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("GESTION DE INTERNET DE LAS COSAS")
        self.edit_nombre.setText(nombre_actual)
        fila_nombre.addWidget(lbl_nombre)
        fila_nombre.addWidget(self.edit_nombre, 1)
        self.viewLayout.addLayout(fila_nombre)

        fila_clases = QHBoxLayout()
        lbl_clases = CaptionLabel("Clases:")
        lbl_clases.setFixedWidth(70)
        self.spin_clases = SpinBox()
        self.spin_clases.setRange(0, 999)
        self.spin_clases.setSpecialValueText("Sin límite")
        try:
            self.spin_clases.setValue(int(self._curso.get("clases", 0) or 0))
        except (TypeError, ValueError):
            self.spin_clases.setValue(0)
        fila_clases.addWidget(lbl_clases)
        fila_clases.addWidget(self.spin_clases)
        fila_clases.addStretch(1)
        self.viewLayout.addLayout(fila_clases)

        lbl_secs = CaptionLabel("Secciones:")
        self.viewLayout.addWidget(lbl_secs)
        self._filas = []          # (contenedor, LineEdit)
        self._v_secs = QVBoxLayout()
        self._v_secs.setSpacing(6)
        self.viewLayout.addLayout(self._v_secs)
        btn_add = PushButton("Agregar sección")
        btn_add.setObjectName("secundario")
        btn_add.clicked.connect(lambda: self._agregar_seccion())
        fila_btn = QHBoxLayout()
        fila_btn.addStretch(1)
        fila_btn.addWidget(btn_add)
        self.viewLayout.addLayout(fila_btn)

        for s in (self._curso.get("secciones", []) or []):
            self._agregar_seccion(s)
        if not self._filas:
            self._agregar_seccion()

        ayuda = CaptionLabel(
            "La cantidad de clases limita el Historial: muestra las sesiones "
            "del curso desde su inicio hasta hoy (0 = sin límite).")
        ayuda.setWordWrap(True)
        self.viewLayout.addWidget(ayuda)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _agregar_seccion(self, texto=""):
        fila = QHBoxLayout()
        edit = LineEdit()
        edit.setPlaceholderText("C1")
        edit.setText(str(texto or ""))
        btn = TransparentToolButton(FIcon.CLOSE)
        btn.setToolTip("Quitar esta sección")
        fila.addWidget(edit, 1)
        fila.addWidget(btn)
        cont = QWidget()
        cont.setStyleSheet("background: transparent;")
        cont.setLayout(fila)
        self._v_secs.addWidget(cont)
        btn.clicked.connect(lambda: self._quitar_seccion(cont))
        self._filas.append((cont, edit))

    def _quitar_seccion(self, cont):
        self._filas = [(c, e) for (c, e) in self._filas if c is not cont]
        cont.setParent(None)
        cont.deleteLater()

    def _secciones(self):
        res = []
        for _c, e in self._filas:
            s = e.text().strip()
            if s and s not in res:
                res.append(s)
        return res

    def datos(self):
        return {
            "nombre": self.edit_nombre.text().strip(),
            "secciones": self._secciones(),
            "clases": int(self.spin_clases.value()),
        }

    def validate(self):
        d = self.datos()
        if not d["nombre"]:
            aviso(self, "Curso sin nombre", "Indica el nombre del curso.")
            return False
        if d["nombre"] in self._otros:
            aviso(self, "Cursos duplicados",
                  f"El curso '{d['nombre']}' ya existe.")
            return False
        return True


class PanelClases(QWidget):
    """Pestaña Clases: cursos/secciones + horarios, en el sidebar."""

    def __init__(self, callbacks=None, parent=None):
        super().__init__(parent)
        self.cfg = config_app.cargar()
        self._cb = callbacks or {}

        # El panel crece con las listas: scroll transparente (como Configuración)
        cuerpo = QWidget()
        cuerpo.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(cuerpo)
        layout.setContentsMargins(36, 24, 36, 24)
        layout.setSpacing(24)

        layout.addWidget(self._construir_grupo_cursos())
        layout.addWidget(self._construir_grupo_horarios())
        layout.addStretch(1)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setWidget(cuerpo)
        scroll.enableTransparentBackground()
        externo = QVBoxLayout(self)
        externo.setContentsMargins(0, 0, 0, 0)
        externo.addWidget(scroll)

    def _guardar_y(self, callback):
        config_app.guardar(self.cfg)
        fn = self._cb.get(callback)
        if fn:
            fn(self.cfg)

    # ---------------- Cursos y secciones ----------------
    def _construir_grupo_cursos(self):
        grupo = HeaderCardWidget()
        grupo.setTitle("Cursos y secciones")
        v = grupo.viewLayout
        # viewLayout es horizontal por defecto: forzar apilado vertical
        v.setDirection(QBoxLayout.Direction.TopToBottom)
        v.setSpacing(10)

        ayuda = CaptionLabel(
            "Un alumno puede pertenecer a varios cursos, pero solo a una "
            "sección de cada curso. La cantidad de clases limita las "
            "columnas del Historial.")
        ayuda.setWordWrap(True)
        v.addWidget(ayuda)

        self.tabla_cursos = TableWidget()
        self.tabla_cursos.setColumnCount(3)
        self.tabla_cursos.setHorizontalHeaderLabels(
            ["Curso", "Secciones", "Clases"])
        cab = self.tabla_cursos.horizontalHeader()
        cab.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tabla_cursos.setEditTriggers(
            TableWidget.EditTrigger.NoEditTriggers)
        self.tabla_cursos.setSelectionMode(
            TableWidget.SelectionMode.SingleSelection)
        self.tabla_cursos.clicked.connect(
            lambda: self.tabla_cursos.selectRow(
                self.tabla_cursos.currentRow()))
        v.addWidget(self.tabla_cursos)

        fila_btn = QHBoxLayout()
        btn_add = PushButton("Agregar curso")
        btn_add.setObjectName("exito")
        btn_add.clicked.connect(self._agregar_curso)
        self.btn_edit_curso = PushButton("Editar")
        self.btn_edit_curso.setObjectName("secundario")
        self.btn_edit_curso.clicked.connect(self._editar_curso)
        self.btn_quitar_curso = PushButton("Quitar")
        self.btn_quitar_curso.setObjectName("secundario")
        self.btn_quitar_curso.clicked.connect(self._quitar_curso)
        fila_btn.addWidget(btn_add)
        fila_btn.addWidget(self.btn_edit_curso)
        fila_btn.addWidget(self.btn_quitar_curso)
        fila_btn.addStretch(1)
        v.addLayout(fila_btn)

        self._refrescar_cursos()
        return grupo

    def _refrescar_cursos(self):
        cursos = config_app.lista_cursos(self.cfg)
        self.tabla_cursos.setRowCount(len(cursos))
        for i, c in enumerate(cursos):
            self.tabla_cursos.setItem(
                i, 0, QTableWidgetItem(c["nombre"]))
            self.tabla_cursos.setItem(
                i, 1, QTableWidgetItem(", ".join(c["secciones"]) or "—"))
            self.tabla_cursos.setItem(
                i, 2, QTableWidgetItem(str(c["clases"]) if c["clases"]
                                        else "Sin límite"))
        if not cursos:
            self.tabla_cursos.setRowCount(1)
            self.tabla_cursos.setItem(
                0, 0, QTableWidgetItem("Sin cursos — usa «Agregar curso»"))
            self.tabla_cursos.setItem(0, 1, QTableWidgetItem(""))
            self.tabla_cursos.setItem(0, 2, QTableWidgetItem(""))

    def _curso_sel(self):
        fila = self.tabla_cursos.currentRow()
        cursos = config_app.lista_cursos(self.cfg)
        if fila < 0 or fila >= len(cursos):
            return None, -1
        return cursos[fila], fila

    def _agregar_curso(self):
        dlg = DialogoCurso(None, config_app.lista_cursos(self.cfg), self)
        if dlg.exec():
            self.cfg.setdefault("cursos", []).append(dlg.datos())
            self._guardar_y("cursos_cambiadas")
            self._refrescar_cursos()

    def _editar_curso(self):
        curso, fila = self._curso_sel()
        if curso is None:
            aviso(self, "Editar curso", "Selecciona un curso de la tabla.")
            return
        dlg = DialogoCurso(curso, config_app.lista_cursos(self.cfg), self)
        if dlg.exec():
            self.cfg["cursos"][fila] = dlg.datos()
            self._guardar_y("cursos_cambiadas")
            self._refrescar_cursos()

    def _quitar_curso(self):
        curso, fila = self._curso_sel()
        if curso is None:
            aviso(self, "Quitar curso", "Selecciona un curso de la tabla.")
            return
        if not pregunta(self, "Quitar curso",
                        f"¿Quitar el curso '{curso['nombre']}'?"):
            return
        self.cfg["cursos"] = [c for c in self.cfg.get("cursos", [])
                              if str(c.get("nombre", "")) != curso["nombre"]]
        self._guardar_y("cursos_cambiadas")
        self._refrescar_cursos()

    # ---------------- Horarios de clase ----------------
    def _construir_grupo_horarios(self):
        grupo = HeaderCardWidget()
        grupo.setTitle("Horarios de clase")
        v = grupo.viewLayout
        # viewLayout es horizontal por defecto: forzar apilado vertical
        v.setDirection(QBoxLayout.Direction.TopToBottom)
        v.setSpacing(10)

        ayuda = CaptionLabel(
            "Cada franja define días y horas por curso y sección "
            "(vacía = todas). El Historial usa las franjas para derivar "
            "las sesiones y calcular tardanzas.")
        ayuda.setWordWrap(True)
        v.addWidget(ayuda)

        self.tabla_franjas = TableWidget()
        self.tabla_franjas.setColumnCount(5)
        self.tabla_franjas.setHorizontalHeaderLabels(
            ["Nombre", "Curso", "Sección", "Días", "Horario"])
        cab = self.tabla_franjas.horizontalHeader()
        cab.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.tabla_franjas.setEditTriggers(
            TableWidget.EditTrigger.NoEditTriggers)
        self.tabla_franjas.setSelectionMode(
            TableWidget.SelectionMode.SingleSelection)
        self.tabla_franjas.clicked.connect(
            lambda: self.tabla_franjas.selectRow(
                self.tabla_franjas.currentRow()))
        v.addWidget(self.tabla_franjas)

        fila_btn = QHBoxLayout()
        btn_add = PushButton("Agregar franja")
        btn_add.setObjectName("exito")
        btn_add.clicked.connect(self._agregar_franja)
        self.btn_edit_franja = PushButton("Editar")
        self.btn_edit_franja.setObjectName("secundario")
        self.btn_edit_franja.clicked.connect(self._editar_franja)
        self.btn_quitar_franja = PushButton("Quitar")
        self.btn_quitar_franja.setObjectName("secundario")
        self.btn_quitar_franja.clicked.connect(self._quitar_franja)
        fila_btn.addWidget(btn_add)
        fila_btn.addWidget(self.btn_edit_franja)
        fila_btn.addWidget(self.btn_quitar_franja)
        fila_btn.addStretch(1)
        v.addLayout(fila_btn)

        self._refrescar_franjas()
        return grupo

    @staticmethod
    def _dias_texto(franja):
        dias = franja.get("dias", []) or []
        return " ".join(DIAS_CORTOS[i] for i in sorted(dias)
                        if 0 <= i < len(DIAS_CORTOS)) or "—"

    @staticmethod
    def _horario_texto(franja):
        txt = f"{franja.get('inicio', '')}–{franja.get('fin', '')}"
        if franja.get("desde") or franja.get("hasta"):
            txt += (f" (desde {franja.get('desde', '—')} "
                    f"hasta {franja.get('hasta', '—')})")
        return txt

    def _refrescar_franjas(self):
        franjas = self.cfg.get("horarios", []) or []
        self.tabla_franjas.setRowCount(len(franjas))
        for i, f in enumerate(franjas):
            self.tabla_franjas.setItem(
                i, 0, QTableWidgetItem(f.get("nombre", "") or "—"))
            self.tabla_franjas.setItem(
                i, 1, QTableWidgetItem(f.get("curso", "") or "—"))
            seccion = str(f.get("seccion") or f.get("grupo") or "").strip()
            self.tabla_franjas.setItem(
                i, 2, QTableWidgetItem(seccion or "(todas)"))
            self.tabla_franjas.setItem(
                i, 3, QTableWidgetItem(self._dias_texto(f)))
            self.tabla_franjas.setItem(
                i, 4, QTableWidgetItem(self._horario_texto(f)))
        if not franjas:
            self.tabla_franjas.setRowCount(1)
            self.tabla_franjas.setItem(
                0, 0, QTableWidgetItem("Sin franjas — usa «Agregar franja»"))
            for col in range(1, 5):
                self.tabla_franjas.setItem(0, col, QTableWidgetItem(""))

    def _franja_sel(self):
        fila = self.tabla_franjas.currentRow()
        franjas = self.cfg.get("horarios", []) or []
        if fila < 0 or fila >= len(franjas):
            return None, -1
        return franjas[fila], fila

    def _agregar_franja(self):
        dlg = DialogoFranja(None, self.cfg, self)
        if dlg.exec():
            f = dlg.datos()
            f["id"] = self._nuevo_id_franja()
            self.cfg.setdefault("horarios", []).append(f)
            self._guardar_y("horarios_cambiados")
            self._refrescar_franjas()

    def _editar_franja(self):
        franja, fila = self._franja_sel()
        if franja is None:
            aviso(self, "Editar franja", "Selecciona una franja de la tabla.")
            return
        dlg = DialogoFranja(franja, self.cfg, self)
        if dlg.exec():
            f = dlg.datos()
            f["id"] = franja.get("id")
            self.cfg["horarios"][fila] = f
            self._guardar_y("horarios_cambiados")
            self._refrescar_franjas()

    def _quitar_franja(self):
        franja, fila = self._franja_sel()
        if franja is None:
            aviso(self, "Quitar franja", "Selecciona una franja de la tabla.")
            return
        if not pregunta(self, "Quitar franja",
                        f"¿Quitar la franja '{franja.get('nombre') or franja.get('id')}'?"):
            return
        del self.cfg["horarios"][fila]
        self._guardar_y("horarios_cambiados")
        self._refrescar_franjas()

    def _nuevo_id_franja(self):
        nums = []
        for f in self.cfg.get("horarios", []) or []:
            fid = str(f.get("id", ""))
            if fid.startswith("h"):
                try:
                    nums.append(int(fid[1:]))
                except ValueError:
                    pass
        return f"h{(max(nums) + 1) if nums else 1}"
