#!/usr/bin/env python3
"""
Centinela Control — Pestaña Cursos y horarios

Gestión jerárquica con submenús desplegables:
  Curso -> Secciones -> Alumnos asignados + Franjas horarias.

Mantiene compatibilidad total con config.json (cfg["cursos"] y cfg["horarios"])
y propaga cambios de cursos, secciones y asignaciones a alumnos.json y SQLite.
"""

import os, sys

from PyQt6.QtCore import Qt, QSize
from PyQt6.QtGui import QColor, QIcon
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame, QHeaderView,
    QAbstractItemView, QTableWidgetItem,
)
from qfluentwidgets import (
    PushButton, PrimaryPushButton, LineEdit, SearchLineEdit, SpinBox,
    CheckBox, TableWidget, CardWidget, MessageBoxBase, SubtitleLabel,
    StrongBodyLabel, BodyLabel, CaptionLabel, ScrollArea,
    TransparentToolButton, IconWidget, InfoBadge, isDarkTheme,
    FluentIcon as FIcon,
)

from centinela.ui.dialogos.dialogos_fluent import aviso, pregunta
from centinela.ui.dialogos.dialogo_horarios import DIAS_CORTOS, DialogoFranja
from centinela.ui.ui_util import avatar_redondeado
from centinela.core import config as config_app
from centinela.core import gestor_alumnos as gestor


class DialogoCurso(MessageBoxBase):
    """Alta o edición de un curso (nombre, límite de clases y secciones iniciales)."""

    def __init__(self, curso=None, otros_cursos=None, parent=None):
        super().__init__(parent=parent)
        self._curso = curso or {}
        self._es_nuevo = curso is None
        nombre_actual = str(self._curso.get("nombre", "") or "")
        self._otros = [
            str(c.get("nombre", "") or "").strip()
            for c in (otros_cursos or [])
            if str(c.get("nombre", "") or "").strip() != nombre_actual.strip()
        ]
        self.widget.setMinimumWidth(520)

        self.titleLabel = SubtitleLabel(
            "Nuevo curso" if self._es_nuevo else "Editar curso")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        fila_nombre = QHBoxLayout()
        fila_nombre.setSpacing(10)
        lbl_nombre = CaptionLabel("Nombre")
        lbl_nombre.setFixedWidth(90)
        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("Ej. Gestión de Internet de las Cosas")
        self.edit_nombre.setClearButtonEnabled(True)
        self.edit_nombre.setText(nombre_actual)
        fila_nombre.addWidget(lbl_nombre)
        fila_nombre.addWidget(self.edit_nombre, 1)
        self.viewLayout.addLayout(fila_nombre)

        fila_clases = QHBoxLayout()
        fila_clases.setSpacing(10)
        lbl_clases = CaptionLabel("Clases")
        lbl_clases.setFixedWidth(90)
        self.spin_clases = SpinBox()
        self.spin_clases.setRange(0, 999)
        self.spin_clases.setSpecialValueText("Sin límite")
        self.spin_clases.setFixedWidth(150)
        try:
            self.spin_clases.setValue(int(self._curso.get("clases", 0) or 0))
        except (TypeError, ValueError):
            self.spin_clases.setValue(0)
        fila_clases.addWidget(lbl_clases)
        fila_clases.addWidget(self.spin_clases)
        fila_clases.addStretch(1)
        self.viewLayout.addLayout(fila_clases)

        fila_tol = QHBoxLayout()
        fila_tol.setSpacing(10)
        lbl_tol = CaptionLabel("Tardanza")
        lbl_tol.setFixedWidth(90)
        self.spin_tolerancia = SpinBox()
        self.spin_tolerancia.setRange(0, 120)
        self.spin_tolerancia.setSuffix(" min")
        self.spin_tolerancia.setFixedWidth(150)
        raw_tol = self._curso.get("tolerancia_tardanza_min")
        try:
            self.spin_tolerancia.setValue(max(0, int(raw_tol)) if raw_tol is not None else 5)
        except (TypeError, ValueError):
            self.spin_tolerancia.setValue(5)
        lbl_tol_hint = CaptionLabel("minutos de gracia tras el inicio de clase")
        lbl_tol_hint.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        fila_tol.addWidget(lbl_tol)
        fila_tol.addWidget(self.spin_tolerancia)
        fila_tol.addWidget(lbl_tol_hint, 1)
        self.viewLayout.addLayout(fila_tol)

        self.edit_secciones = None
        if self._es_nuevo:
            fila_sec = QHBoxLayout()
            fila_sec.setSpacing(10)
            lbl_sec = CaptionLabel("Secciones")
            lbl_sec.setFixedWidth(90)
            self.edit_secciones = LineEdit()
            self.edit_secciones.setPlaceholderText("Ej. C1, C2 (opcional, separadas por coma)")
            self.edit_secciones.setClearButtonEnabled(True)
            fila_sec.addWidget(lbl_sec)
            fila_sec.addWidget(self.edit_secciones, 1)
            self.viewLayout.addLayout(fila_sec)

        ayuda = CaptionLabel(
            "La cantidad de clases limita las sesiones visibles en Asistencias (0 = sin límite). "
            "Cada sección hereda la tolerancia del curso salvo que actives un margen propio.")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        self.viewLayout.addWidget(ayuda)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _secciones_iniciales(self):
        if self.edit_secciones is None:
            return list(self._curso.get("secciones", []) or [])
        res = []
        for parte in self.edit_secciones.text().split(","):
            s = parte.strip()
            if s and s not in res:
                res.append(s)
        return res

    def datos(self):
        return {
            "nombre": self.edit_nombre.text().strip(),
            "secciones": self._secciones_iniciales(),
            "clases": int(self.spin_clases.value()),
            "tolerancia_tardanza_min": int(self.spin_tolerancia.value()),
            "tolerancia_secciones": dict(self._curso.get("tolerancia_secciones") or {}),
        }

    def validate(self):
        d = self.datos()
        if not d["nombre"]:
            aviso(self, "Curso sin nombre", "Indica el nombre del curso.")
            return False
        if d["nombre"] in self._otros:
            aviso(self, "Curso duplicado",
                  f"El curso «{d['nombre']}» ya existe.")
            return False
        return True


class DialogoSeccion(MessageBoxBase):
    """Alta o edición de una sección dentro de un curso (con margen de tardanza opcional)."""

    def __init__(self, curso_nombre, seccion_actual="", existentes=None,
                 tol_curso=5, tol_seccion=None, parent=None):
        super().__init__(parent=parent)
        self._actual = str(seccion_actual or "").strip()
        self._tol_curso = max(0, int(tol_curso if tol_curso is not None else 5))
        self._existentes = {
            str(s).strip() for s in (existentes or [])
            if str(s).strip() and str(s).strip() != self._actual
        }
        self.widget.setMinimumWidth(480)

        titulo = "Editar sección" if self._actual else "Nueva sección"
        self.titleLabel = SubtitleLabel(titulo)
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        sub = CaptionLabel(f"Curso: {curso_nombre}")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        self.viewLayout.addWidget(sub)

        fila = QHBoxLayout()
        fila.setSpacing(10)
        lbl = CaptionLabel("Sección")
        lbl.setFixedWidth(72)
        self.edit_seccion = LineEdit()
        self.edit_seccion.setPlaceholderText("Ej. C1, A o Turno Mañana")
        self.edit_seccion.setClearButtonEnabled(True)
        self.edit_seccion.setText(self._actual)
        fila.addWidget(lbl)
        fila.addWidget(self.edit_seccion, 1)
        self.viewLayout.addLayout(fila)

        # Override opcional de tardanza por sección
        self.chk_tol_propia = CheckBox(
            "Usar un margen de tardanza diferente en esta sección", self)
        tiene_propia = tol_seccion is not None
        self.chk_tol_propia.setChecked(tiene_propia)
        self.viewLayout.addWidget(self.chk_tol_propia)

        fila_tol = QHBoxLayout()
        fila_tol.setSpacing(10)
        lbl_t = CaptionLabel("Tardanza")
        lbl_t.setFixedWidth(72)
        self.spin_tol = SpinBox()
        self.spin_tol.setRange(0, 120)
        self.spin_tol.setSuffix(" min")
        self.spin_tol.setFixedWidth(150)
        self.spin_tol.setValue(
            max(0, int(tol_seccion)) if tiene_propia else self._tol_curso)
        self.spin_tol.setEnabled(tiene_propia)
        self.lbl_tol_info = CaptionLabel("", self)
        self.lbl_tol_info.setWordWrap(True)
        self.lbl_tol_info.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        fila_tol.addWidget(lbl_t)
        fila_tol.addWidget(self.spin_tol)
        fila_tol.addWidget(self.lbl_tol_info, 1)
        self.viewLayout.addLayout(fila_tol)

        self.chk_tol_propia.toggled.connect(self._on_toggle_tol)
        self._on_toggle_tol(tiene_propia)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _on_toggle_tol(self, activo):
        self.spin_tol.setEnabled(bool(activo))
        if activo:
            self.lbl_tol_info.setText("Margen propio para esta sección.")
        else:
            self.lbl_tol_info.setText(
                f"Mantiene el del curso ({self._tol_curso} min).")

    def nombre_seccion(self):
        return self.edit_seccion.text().strip()

    def tolerancia_personalizada(self):
        if self.chk_tol_propia.isChecked():
            return int(self.spin_tol.value())
        return None

    def validate(self):
        s = self.nombre_seccion()
        if not s:
            aviso(self, "Sección vacía", "Escribe el nombre de la sección.")
            return False
        if s in self._existentes:
            aviso(self, "Sección duplicada",
                  f"La sección «{s}» ya existe en este curso.")
            return False
        return True


class DialogoAsignarAlumnos(MessageBoxBase):
    """Asigna alumnos de la base general a una sección específica de un curso."""

    def __init__(self, curso_nombre, seccion_nombre, parent=None):
        super().__init__(parent=parent)
        self._curso = str(curso_nombre or "").strip()
        self._seccion = str(seccion_nombre or "").strip()
        self._actualizando = False
        self._filas_meta = []

        self.widget.setMinimumWidth(680)
        self.viewLayout.setSpacing(12)

        # Cabecera con título y contador en vivo
        cab = QHBoxLayout()
        cab.setSpacing(10)
        col_tit = QVBoxLayout()
        col_tit.setSpacing(2)
        self.titleLabel = SubtitleLabel(f"Alumnos — Sección {self._seccion}")
        sub = CaptionLabel(
            f"Curso: {self._curso} · Cada alumno pertenece como máximo a una sección por curso.")
        sub.setWordWrap(True)
        sub.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        col_tit.addWidget(self.titleLabel)
        col_tit.addWidget(sub)
        cab.addLayout(col_tit, 1)

        self.badge_sel = InfoBadge.attension("0 asignados", self)
        cab.addWidget(self.badge_sel, 0, Qt.AlignmentFlag.AlignTop)
        self.viewLayout.addLayout(cab)

        alumnos = gestor.listar(incluir_inactivos=False)
        alumnos.sort(key=lambda a: (
            str(a.get("apellidos", "") or "").strip() == "",
            str(a.get("apellidos", "") or "").strip().lower(),
            str(a.get("nombre", "") or "").strip().lower(),
        ))
        mapa_curso = gestor.mapa_secciones_en_curso(self._curso)

        if not alumnos:
            vacio = QWidget(self)
            vacio.setStyleSheet("background: transparent;")
            vv = QVBoxLayout(vacio)
            vv.setContentsMargins(24, 48, 24, 48)
            vv.setSpacing(10)
            vv.setAlignment(Qt.AlignmentFlag.AlignCenter)
            ico = IconWidget(FIcon.PEOPLE, vacio)
            ico.setFixedSize(44, 44)
            vv.addWidget(ico, 0, Qt.AlignmentFlag.AlignCenter)
            tit = StrongBodyLabel("No hay alumnos registrados", vacio)
            vv.addWidget(tit, 0, Qt.AlignmentFlag.AlignCenter)
            hint = CaptionLabel(
                "Registra primero los alumnos en la pestaña «Alumnos» y vuelve aquí para asignarlos.",
                vacio)
            hint.setWordWrap(True)
            hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
            hint.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
            vv.addWidget(hint, 0, Qt.AlignmentFlag.AlignCenter)
            self.viewLayout.addWidget(vacio)
            self.tabla = None
            self.yesButton.setText("Guardar")
            self.yesButton.setEnabled(False)
            self.cancelButton.setText("Cerrar")
            return

        # Barra de búsqueda y filtro
        barra = QHBoxLayout()
        barra.setSpacing(10)
        self.edit_buscar = SearchLineEdit(self)
        self.edit_buscar.setPlaceholderText("Buscar por apellido, nombre o DNI…")
        self.edit_buscar.setClearButtonEnabled(True)
        self.edit_buscar.textChanged.connect(self._aplicar_filtro)
        self.chk_solo_sel = CheckBox("Solo asignados", self)
        self.chk_solo_sel.toggled.connect(self._aplicar_filtro)
        barra.addWidget(self.edit_buscar, 1)
        barra.addWidget(self.chk_solo_sel)
        self.viewLayout.addLayout(barra)

        # Acciones masivas + conteo visible
        fila_acc = QHBoxLayout()
        fila_acc.setSpacing(8)
        btn_sel_vis = PushButton("Marcar visibles", self)
        btn_sel_vis.setObjectName("secundario")
        btn_sel_vis.setMinimumHeight(30)
        btn_sel_vis.clicked.connect(lambda: self._marcar_visibles(True))
        btn_qui_vis = PushButton("Desmarcar visibles", self)
        btn_qui_vis.setObjectName("secundario")
        btn_qui_vis.setMinimumHeight(30)
        btn_qui_vis.clicked.connect(lambda: self._marcar_visibles(False))
        self.lbl_mostrando = CaptionLabel("", self)
        self.lbl_mostrando.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        fila_acc.addWidget(btn_sel_vis)
        fila_acc.addWidget(btn_qui_vis)
        fila_acc.addStretch(1)
        fila_acc.addWidget(self.lbl_mostrando)
        self.viewLayout.addLayout(fila_acc)

        # Tabla de selección rápida
        self.tabla = TableWidget(self)
        self.tabla.setColumnCount(4)
        self.tabla.setHorizontalHeaderLabels(["", "Alumno", "DNI", "Estado en el curso"])
        self.tabla.setBorderVisible(True)
        self.tabla.setBorderRadius(8)
        self.tabla.setWordWrap(False)
        self.tabla.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tabla.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.verticalHeader().setDefaultSectionSize(44)
        self.tabla.setIconSize(QSize(30, 30))
        self.tabla.setFixedHeight(350)

        hdr = self.tabla.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tabla.setColumnWidth(0, 40)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tabla.setColumnWidth(2, 105)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.tabla.setColumnWidth(3, 170)

        oscuro = isDarkTheme()
        self._actualizando = True
        self.tabla.setRowCount(len(alumnos))
        for row, a in enumerate(alumnos):
            aid = a.get("alumno_id", "")
            nombre = str(a.get("nombre", "") or "").strip()
            apellidos = str(a.get("apellidos", "") or "").strip()
            dni = str(a.get("dni", "") or "").strip()
            mostrar = f"{apellidos}, {nombre}" if (apellidos and nombre) else (apellidos or nombre or "Sin nombre")
            sec_orig = mapa_curso.get(aid)
            marcado = (sec_orig == self._seccion)

            it_chk = QTableWidgetItem("")
            it_chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            it_chk.setCheckState(
                Qt.CheckState.Checked if marcado else Qt.CheckState.Unchecked)
            self.tabla.setItem(row, 0, it_chk)

            it_nom = QTableWidgetItem(mostrar)
            it_nom.setFlags(Qt.ItemFlag.ItemIsEnabled)
            ruta = gestor.ruta_foto_absoluta(a.get("foto_referencia"))
            pix = avatar_redondeado(ruta, 30, radio_ratio=0.28, oscuro=oscuro)
            it_nom.setIcon(QIcon(pix))
            it_nom.setToolTip(mostrar)
            self.tabla.setItem(row, 1, it_nom)

            it_dni = QTableWidgetItem(dni or "—")
            it_dni.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it_dni.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if not dni:
                it_dni.setForeground(QColor("#64748b"))
            self.tabla.setItem(row, 2, it_dni)

            it_est = QTableWidgetItem("")
            it_est.setFlags(Qt.ItemFlag.ItemIsEnabled)
            it_est.setTextAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
            self.tabla.setItem(row, 3, it_est)

            self._filas_meta.append({
                "alumno_id": aid,
                "sec_orig": sec_orig,
                "busqueda": f"{apellidos} {nombre} {mostrar} {nombre} {apellidos} {dni} {sec_orig or ''}".lower(),
            })
            self._pintar_estado_fila(row)

        self._actualizando = False
        self.tabla.itemChanged.connect(self._on_item_changed)
        self.tabla.cellClicked.connect(self._on_cell_clicked)
        self.viewLayout.addWidget(self.tabla)

        self.lbl_sin_match = CaptionLabel("Ningún alumno coincide con el filtro actual.", self)
        self.lbl_sin_match.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_sin_match.setStyleSheet("color: rgba(139, 151, 168, 0.9); padding: 6px;")
        self.lbl_sin_match.setVisible(False)
        self.viewLayout.addWidget(self.lbl_sin_match)

        self._actualizar_contadores()

        self.yesButton.setText("Guardar asignación")
        self.cancelButton.setText("Cancelar")

    def _esta_marcado(self, row):
        it = self.tabla.item(row, 0)
        return it is not None and it.checkState() == Qt.CheckState.Checked

    def _pintar_estado_fila(self, row):
        meta = self._filas_meta[row]
        sec_orig = meta["sec_orig"]
        marcado = self._esta_marcado(row)
        it = self.tabla.item(row, 3)
        if it is None:
            return
        if marcado:
            if sec_orig == self._seccion:
                texto, color = "En esta sección", "#34d399"
            elif sec_orig:
                texto, color = f"Se moverá desde {sec_orig}", "#fbbf24"
            else:
                texto, color = "Se asignará", "#5eead4"
        else:
            if sec_orig == self._seccion:
                texto, color = "Se quitará", "#fca5a5"
            elif sec_orig:
                texto, color = f"En sección {sec_orig}", "#fbbf24"
            else:
                texto, color = "Sin asignar", "#8b97a8"
        it.setText(texto)
        it.setForeground(QColor(color))

    def _on_cell_clicked(self, row, col):
        if col == 0 or self._actualizando:
            return
        it = self.tabla.item(row, 0)
        if it is None:
            return
        nuevo = (
            Qt.CheckState.Unchecked
            if it.checkState() == Qt.CheckState.Checked
            else Qt.CheckState.Checked
        )
        it.setCheckState(nuevo)

    def _on_item_changed(self, item):
        if self._actualizando or item.column() != 0:
            return
        self._actualizando = True
        try:
            self._pintar_estado_fila(item.row())
        finally:
            self._actualizando = False
        if self.chk_solo_sel.isChecked():
            self._aplicar_filtro()
        else:
            self._actualizar_contadores()

    def _marcar_visibles(self, marcar):
        if self.tabla is None:
            return
        estado = Qt.CheckState.Checked if marcar else Qt.CheckState.Unchecked
        self._actualizando = True
        try:
            for row in range(self.tabla.rowCount()):
                if not self.tabla.isRowHidden(row):
                    it = self.tabla.item(row, 0)
                    if it is not None and it.checkState() != estado:
                        it.setCheckState(estado)
                        self._pintar_estado_fila(row)
        finally:
            self._actualizando = False
        self._aplicar_filtro()

    def _aplicar_filtro(self, *_):
        if self.tabla is None:
            return
        texto = self.edit_buscar.text().strip().lower()
        solo_sel = self.chk_solo_sel.isChecked()
        visibles = 0
        for row, meta in enumerate(self._filas_meta):
            ok_txt = not texto or texto in meta["busqueda"]
            ok_sel = (not solo_sel) or self._esta_marcado(row)
            ver = ok_txt and ok_sel
            self.tabla.setRowHidden(row, not ver)
            if ver:
                visibles += 1
        self.lbl_sin_match.setVisible(visibles == 0)
        self._actualizar_contadores(visibles)

    def _actualizar_contadores(self, visibles=None):
        if self.tabla is None:
            return
        total = len(self._filas_meta)
        sel = sum(1 for r in range(total) if self._esta_marcado(r))
        if visibles is None:
            visibles = sum(1 for r in range(total) if not self.tabla.isRowHidden(r))
        self.badge_sel.setText(f"{sel} asignado{'s' if sel != 1 else ''}")
        self.lbl_mostrando.setText(f"Mostrando {visibles} de {total}")

    def ids_seleccionados(self):
        if self.tabla is None:
            return []
        return [
            self._filas_meta[r]["alumno_id"]
            for r in range(len(self._filas_meta))
            if self._esta_marcado(r)
        ]


def _texto_dias(franja):
    dias = franja.get("dias", []) or []
    return " ".join(
        DIAS_CORTOS[i] for i in sorted(dias) if 0 <= i < len(DIAS_CORTOS)
    ) or "Sin días"


def _texto_horario(franja):
    txt = f"{franja.get('inicio', '')}–{franja.get('fin', '')}"
    desde = str(franja.get("desde", "") or "").strip()
    hasta = str(franja.get("hasta", "") or "").strip()
    if desde or hasta:
        txt += f" · Vigencia: {desde or '…'} → {hasta or '…'}"
    return txt


class _FilaHorario(QFrame):
    """Fila compacta de una franja horaria dentro de una sección."""

    def __init__(self, franja, on_editar, on_quitar, parent=None):
        super().__init__(parent)
        self.setObjectName("filaHorario")
        self.setStyleSheet(
            "QFrame#filaHorario {"
            " background: rgba(255, 255, 255, 0.03);"
            " border: 1px solid rgba(255, 255, 255, 0.07);"
            " border-radius: 8px;"
            "}"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 8, 10, 8)
        h.setSpacing(10)

        ico = IconWidget(FIcon.DATE_TIME, self)
        ico.setFixedSize(16, 16)
        h.addWidget(ico, 0, Qt.AlignmentFlag.AlignVCenter)

        nombre = str(franja.get("nombre", "") or "").strip() or "Clase"
        lbl_nom = BodyLabel(nombre, self)
        lbl_nom.setMinimumWidth(110)
        h.addWidget(lbl_nom)

        detalle = f"{_texto_dias(franja)}  ·  {_texto_horario(franja)}"
        lbl_det = CaptionLabel(detalle, self)
        lbl_det.setStyleSheet("color: rgba(203, 213, 225, 0.85);")
        h.addWidget(lbl_det, 1)

        btn_edit = TransparentToolButton(FIcon.EDIT, self)
        btn_edit.setToolTip("Editar horario")
        btn_edit.clicked.connect(lambda: on_editar(franja))
        btn_del = TransparentToolButton(FIcon.DELETE, self)
        btn_del.setToolTip("Quitar horario")
        btn_del.clicked.connect(lambda: on_quitar(franja))
        h.addWidget(btn_edit)
        h.addWidget(btn_del)


class TarjetaSeccionSubmenu(QFrame):
    """Submenú de una sección dentro de un curso (conteo + horarios + acciones)."""

    def __init__(self, curso_nombre, seccion_nombre, n_alumnos, franjas,
                 callbacks, es_general=False, tol_seccion=None, parent=None):
        super().__init__(parent)
        self.setObjectName("subtarjetaSeccion")
        self.setStyleSheet(
            "QFrame#subtarjetaSeccion {"
            " background: rgba(255, 255, 255, 0.025);"
            " border: 1px solid rgba(255, 255, 255, 0.08);"
            " border-radius: 10px;"
            "}"
        )

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)

        # Cabecera de la sección
        cab = QHBoxLayout()
        cab.setSpacing(10)

        ico = IconWidget(FIcon.TAG if not es_general else FIcon.GLOBE, self)
        ico.setFixedSize(16, 16)
        cab.addWidget(ico, 0, Qt.AlignmentFlag.AlignVCenter)

        titulo = (
            "Horario general (todas las secciones)"
            if es_general else f"Sección {seccion_nombre}"
        )
        lbl_sec = StrongBodyLabel(titulo, self)
        cab.addWidget(lbl_sec)

        if not es_general:
            badge_alu = InfoBadge.attension(
                f"{n_alumnos} alumno{'s' if n_alumnos != 1 else ''}", self)
            cab.addWidget(badge_alu)
            if tol_seccion is not None:
                badge_tol = InfoBadge.warning(
                    f"Tardanza {tol_seccion} min", self)
                cab.addWidget(badge_tol)

        cab.addStretch(1)

        if not es_general:
            btn_alu = PushButton(FIcon.PEOPLE, "Asignar alumnos", self)
            btn_alu.setMinimumHeight(30)
            btn_alu.clicked.connect(
                lambda: callbacks["asignar_alumnos"](curso_nombre, seccion_nombre))
            cab.addWidget(btn_alu)

        btn_add_h = PushButton(FIcon.ADD, "Agregar horario", self)
        btn_add_h.setMinimumHeight(30)
        btn_add_h.clicked.connect(
            lambda: callbacks["agregar_franja"](curso_nombre, "" if es_general else seccion_nombre))
        cab.addWidget(btn_add_h)

        if not es_general:
            btn_ren = TransparentToolButton(FIcon.EDIT, self)
            btn_ren.setToolTip("Editar sección y tolerancia de tardanza")
            btn_ren.clicked.connect(
                lambda: callbacks["renombrar_seccion"](curso_nombre, seccion_nombre))
            btn_del = TransparentToolButton(FIcon.DELETE, self)
            btn_del.setToolTip("Quitar sección")
            btn_del.clicked.connect(
                lambda: callbacks["quitar_seccion"](curso_nombre, seccion_nombre))
            cab.addWidget(btn_ren)
            cab.addWidget(btn_del)

        v.addLayout(cab)

        if franjas:
            for f in franjas:
                v.addWidget(_FilaHorario(
                    f,
                    on_editar=callbacks["editar_franja"],
                    on_quitar=callbacks["quitar_franja"],
                    parent=self,
                ))
        else:
            vacio = CaptionLabel("Sin horarios asignados a esta sección.", self)
            vacio.setStyleSheet("color: rgba(139, 151, 168, 0.85); padding-left: 26px;")
            v.addWidget(vacio)


class TarjetaCurso(CardWidget):
    """Tarjeta desplegable de un curso con sus secciones y horarios como submenú."""

    def __init__(self, curso, n_alumnos, conteo_secciones, franjas_curso,
                 callbacks, expandida=True, parent=None):
        super().__init__(parent)
        self.curso_nombre = curso["nombre"]
        self.expandida = bool(expandida)
        self._callbacks = callbacks

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Cabecera del curso
        cab = QWidget(self)
        cab.setStyleSheet("background: transparent;")
        h = QHBoxLayout(cab)
        h.setContentsMargins(16, 14, 16, 12)
        h.setSpacing(12)

        self.btn_chevron = TransparentToolButton(
            FIcon.CARE_UP_SOLID if self.expandida else FIcon.CARE_DOWN_SOLID, cab)
        self.btn_chevron.setToolTip("Expandir / contraer curso")
        self.btn_chevron.clicked.connect(self.alternar)
        h.addWidget(self.btn_chevron)

        col = QVBoxLayout()
        col.setSpacing(2)
        self.lbl_titulo = SubtitleLabel(self.curso_nombre, cab)
        n_sec = len(curso.get("secciones") or [])
        clases = curso.get("clases", 0)
        txt_clases = f"{clases} clases" if clases else "Sin límite de clases"
        tol_curso = curso.get("tolerancia_tardanza_min", 5)
        n_franjas = len(franjas_curso)
        sub_txt = (
            f"{n_sec} sección{'es' if n_sec != 1 else ''} · "
            f"{n_alumnos} alumno{'s' if n_alumnos != 1 else ''} · "
            f"{n_franjas} horario{'s' if n_franjas != 1 else ''} · "
            f"{txt_clases} · Tolerancia {tol_curso} min"
        )
        self.lbl_sub = CaptionLabel(sub_txt, cab)
        self.lbl_sub.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        col.addWidget(self.lbl_titulo)
        col.addWidget(self.lbl_sub)
        h.addLayout(col, 1)

        btn_add_sec = PushButton(FIcon.ADD, "Agregar sección", cab)
        btn_add_sec.setMinimumHeight(32)
        btn_add_sec.clicked.connect(
            lambda: callbacks["agregar_seccion"](self.curso_nombre))
        btn_edit = TransparentToolButton(FIcon.EDIT, cab)
        btn_edit.setToolTip("Editar curso")
        btn_edit.clicked.connect(
            lambda: callbacks["editar_curso"](self.curso_nombre))
        btn_del = TransparentToolButton(FIcon.DELETE, cab)
        btn_del.setToolTip("Eliminar curso")
        btn_del.clicked.connect(
            lambda: callbacks["quitar_curso"](self.curso_nombre))

        h.addWidget(btn_add_sec)
        h.addWidget(btn_edit)
        h.addWidget(btn_del)
        v.addWidget(cab)

        # Cuerpo desplegable (submenú de secciones)
        self.cuerpo = QWidget(self)
        self.cuerpo.setStyleSheet("background: transparent;")
        vc = QVBoxLayout(self.cuerpo)
        vc.setContentsMargins(16, 2, 16, 16)
        vc.setSpacing(10)

        secciones = list(curso.get("secciones") or [])
        por_sec = {}
        generales = []
        for f in franjas_curso:
            sec = str(f.get("seccion") or f.get("grupo") or "").strip()
            if not sec:
                generales.append(f)
            else:
                por_sec.setdefault(sec, []).append(f)

        # Incluir secciones que aparezcan en horarios aunque no estén listadas aún
        todas_sec = list(secciones)
        for sec in por_sec:
            if sec not in todas_sec:
                todas_sec.append(sec)

        if not todas_sec and not generales:
            hint = CaptionLabel(
                "Sin secciones configuradas. Pulsa «Agregar sección» para crear una (ej. C1, A).",
                self.cuerpo)
            hint.setStyleSheet("color: rgba(139, 151, 168, 0.9); padding: 6px 4px;")
            vc.addWidget(hint)
        else:
            tol_secs = curso.get("tolerancia_secciones") or {}
            for sec in todas_sec:
                n_alu_sec = conteo_secciones.get((self.curso_nombre, sec), 0)
                vc.addWidget(TarjetaSeccionSubmenu(
                    self.curso_nombre, sec, n_alu_sec,
                    por_sec.get(sec, []), callbacks, es_general=False,
                    tol_seccion=tol_secs.get(sec),
                    parent=self.cuerpo,
                ))
            if generales:
                vc.addWidget(TarjetaSeccionSubmenu(
                    self.curso_nombre, "", n_alumnos,
                    generales, callbacks, es_general=True,
                    parent=self.cuerpo,
                ))

        # Pie discreto para horario general cuando ya hay secciones
        if todas_sec and not generales:
            pie = QHBoxLayout()
            pie.addStretch(1)
            btn_gen = PushButton(FIcon.DATE_TIME, "Agregar horario para todas las secciones", self.cuerpo)
            btn_gen.setObjectName("secundario")
            btn_gen.setMinimumHeight(30)
            btn_gen.clicked.connect(
                lambda: callbacks["agregar_franja"](self.curso_nombre, ""))
            pie.addWidget(btn_gen)
            vc.addLayout(pie)

        self.cuerpo.setVisible(self.expandida)
        v.addWidget(self.cuerpo)

    def alternar(self, *_):
        self.expandida = not self.expandida
        self.cuerpo.setVisible(self.expandida)
        self.btn_chevron.setIcon(
            FIcon.CARE_UP_SOLID if self.expandida else FIcon.CARE_DOWN_SOLID)
        self._callbacks["on_toggle"](self.curso_nombre, self.expandida)


class PanelClases(QWidget):
    """Pestaña Cursos y horarios con gestión jerárquica (curso -> secciones -> horarios)."""

    def __init__(self, callbacks=None, parent=None):
        super().__init__(parent)
        self.cfg = config_app.cargar()
        self._cb = callbacks or {}
        self._expandidos = {}  # curso_nombre -> bool

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(28, 20, 28, 24)
        raiz.setSpacing(16)

        # Cabecera principal
        cab = QHBoxLayout()
        cab.setSpacing(12)
        col_tit = QVBoxLayout()
        col_tit.setSpacing(2)
        lbl_tit = SubtitleLabel("Cursos y horarios", self)
        lbl_desc = CaptionLabel(
            "Organiza cada curso con sus secciones, asigna alumnos y define las franjas horarias.",
            self)
        lbl_desc.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        col_tit.addWidget(lbl_tit)
        col_tit.addWidget(lbl_desc)
        cab.addLayout(col_tit, 1)

        btn_nuevo = PrimaryPushButton(FIcon.ADD, "Nuevo curso", self)
        btn_nuevo.setMinimumHeight(34)
        btn_nuevo.clicked.connect(self._agregar_curso)
        cab.addWidget(btn_nuevo)
        raiz.addLayout(cab)

        # Área con scroll de tarjetas de curso
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._cuerpo = QWidget()
        self._cuerpo.setStyleSheet("background: transparent;")
        self._v_cursos = QVBoxLayout(self._cuerpo)
        self._v_cursos.setContentsMargins(0, 2, 0, 8)
        self._v_cursos.setSpacing(14)

        self._estado_vacio = self._construir_estado_vacio()
        self._v_cursos.addWidget(self._estado_vacio)
        self._v_cursos.addStretch(1)

        scroll.setWidget(self._cuerpo)
        scroll.enableTransparentBackground()
        raiz.addWidget(scroll, 1)

        self._sucio = True

    def marcar_sucio(self):
        self._sucio = True
        if self.isVisible():
            self.refrescar()

    def showEvent(self, event):
        super().showEvent(event)
        if self._sucio:
            self.refrescar()

    def _construir_estado_vacio(self):
        w = QWidget(self._cuerpo)
        w.setStyleSheet("background: transparent;")
        v = QVBoxLayout(w)
        v.setContentsMargins(24, 64, 24, 64)
        v.setSpacing(10)
        v.setAlignment(Qt.AlignmentFlag.AlignCenter)

        ico = IconWidget(FIcon.CALENDAR, w)
        ico.setFixedSize(52, 52)
        v.addWidget(ico, 0, Qt.AlignmentFlag.AlignCenter)

        tit = SubtitleLabel("Sin cursos configurados", w)
        v.addWidget(tit, 0, Qt.AlignmentFlag.AlignCenter)

        sub = CaptionLabel(
            "Crea tu primer curso para organizar secciones, horarios y el historial.",
            w)
        sub.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        v.addWidget(sub, 0, Qt.AlignmentFlag.AlignCenter)

        btn = PrimaryPushButton(FIcon.ADD, "Crear primer curso", w)
        btn.setMinimumHeight(34)
        btn.clicked.connect(self._agregar_curso)
        v.addSpacing(6)
        v.addWidget(btn, 0, Qt.AlignmentFlag.AlignCenter)
        return w

    def _guardar_y(self, callback):
        config_app.guardar(self.cfg)
        fn = self._cb.get(callback)
        if fn:
            fn(self.cfg)

    def _on_toggle(self, curso_nombre, expandida):
        self._expandidos[curso_nombre] = expandida

    def refrescar(self):
        """Reconstruye las tarjetas de cursos, secciones y horarios."""
        self._sucio = False
        while self._v_cursos.count():
            item = self._v_cursos.takeAt(0)
            w = item.widget()
            if w is not None and w is not self._estado_vacio:
                w.hide()
                w.setParent(None)
                w.deleteLater()

        self._v_cursos.addWidget(self._estado_vacio)
        cursos = config_app.lista_cursos(self.cfg)
        self._estado_vacio.setVisible(not cursos)

        if cursos:
            try:
                conteo_curso, conteo_sec = gestor.contar_por_curso_seccion()
            except Exception:
                conteo_curso, conteo_sec = {}, {}

            callbacks = {
                "agregar_seccion": self._agregar_seccion,
                "renombrar_seccion": self._renombrar_seccion,
                "quitar_seccion": self._quitar_seccion,
                "asignar_alumnos": self._asignar_alumnos,
                "editar_curso": self._editar_curso,
                "quitar_curso": self._quitar_curso,
                "agregar_franja": self._agregar_franja,
                "editar_franja": self._editar_franja,
                "quitar_franja": self._quitar_franja,
                "on_toggle": self._on_toggle,
            }
            for c in cursos:
                nombre = c["nombre"]
                franjas = [
                    f for f in (self.cfg.get("horarios", []) or [])
                    if str(f.get("curso", "") or "").strip() == nombre
                ]
                expandida = self._expandidos.get(nombre, True)
                tarjeta = TarjetaCurso(
                    c,
                    n_alumnos=conteo_curso.get(nombre, 0),
                    conteo_secciones=conteo_sec,
                    franjas_curso=franjas,
                    callbacks=callbacks,
                    expandida=expandida,
                    parent=self._cuerpo,
                )
                self._v_cursos.addWidget(tarjeta)

        self._v_cursos.addStretch(1)

    # ---------------- Acciones sobre cursos ----------------
    def _buscar_curso_idx(self, nombre):
        for i, c in enumerate(self.cfg.get("cursos", []) or []):
            if str(c.get("nombre", "") or "").strip() == nombre:
                return c, i
        return None, -1

    def _agregar_curso(self):
        dlg = DialogoCurso(None, config_app.lista_cursos(self.cfg), self)
        if dlg.exec():
            datos = dlg.datos()
            self.cfg.setdefault("cursos", []).append(datos)
            self._expandidos[datos["nombre"]] = True
            self._guardar_y("cursos_cambiadas")
            self.refrescar()

    def _editar_curso(self, nombre):
        curso, idx = self._buscar_curso_idx(nombre)
        if curso is None:
            return
        curso_norm = next(
            (c for c in config_app.lista_cursos(self.cfg) if c["nombre"] == nombre),
            curso,
        )
        dlg = DialogoCurso(curso_norm, config_app.lista_cursos(self.cfg), self)
        if dlg.exec():
            datos = dlg.datos()
            viejo = str(curso.get("nombre", "") or "").strip()
            nuevo = datos["nombre"]
            self.cfg["cursos"][idx] = {
                "nombre": nuevo,
                "secciones": list(curso.get("secciones") or []),
                "clases": datos["clases"],
                "tolerancia_tardanza_min": datos["tolerancia_tardanza_min"],
                "tolerancia_secciones": dict(curso.get("tolerancia_secciones") or {}),
            }
            if viejo != nuevo:
                config_app.renombrar_curso(self.cfg, viejo, nuevo)
                gestor.renombrar_curso(viejo, nuevo)
                self._expandidos[nuevo] = self._expandidos.pop(viejo, True)
            self._guardar_y("cursos_cambiadas")
            self.refrescar()

    def _quitar_curso(self, nombre):
        _curso, idx = self._buscar_curso_idx(nombre)
        if idx < 0:
            return
        if not pregunta(
                self, "Eliminar curso",
                f"¿Eliminar el curso «{nombre}» y sus horarios asociados?\n"
                "Las asignaciones de alumnos a este curso se quitarán "
                "(los alumnos se conservan en la base de Alumnos).",
                yes="Eliminar"):
            return
        del self.cfg["cursos"][idx]
        self.cfg["horarios"] = [
            h for h in (self.cfg.get("horarios", []) or [])
            if str(h.get("curso", "") or "").strip() != nombre
        ]
        gestor.quitar_inscripciones_curso(nombre)
        self._guardar_y("cursos_cambiadas")
        self.refrescar()

    # ---------------- Acciones sobre secciones ----------------
    def _asignar_alumnos(self, curso_nombre, seccion_nombre):
        dlg = DialogoAsignarAlumnos(curso_nombre, seccion_nombre, self.window())
        if dlg.exec():
            agregados, quitados, movidos = gestor.asignar_alumnos_seccion(
                curso_nombre, seccion_nombre, dlg.ids_seleccionados())
            self.refrescar()
            fn = self._cb.get("asignaciones_cambiadas")
            if fn:
                fn((curso_nombre, seccion_nombre, agregados, quitados, movidos))

    def _agregar_seccion(self, curso_nombre):
        curso, idx = self._buscar_curso_idx(curso_nombre)
        if curso is None:
            return
        existentes = curso.get("secciones") or []
        tol_curso = config_app.tolerancia_de(self.cfg, curso_nombre)
        dlg = DialogoSeccion(
            curso_nombre, "", existentes, tol_curso=tol_curso, tol_seccion=None, parent=self)
        if dlg.exec():
            sec = dlg.nombre_seccion()
            secs = [str(s).strip() for s in existentes if str(s).strip()]
            if sec not in secs:
                secs.append(sec)
            self.cfg["cursos"][idx]["secciones"] = secs
            tol_propia = dlg.tolerancia_personalizada()
            tol_map = dict(curso.get("tolerancia_secciones") or {})
            if tol_propia is not None:
                tol_map[sec] = tol_propia
            else:
                tol_map.pop(sec, None)
            self.cfg["cursos"][idx]["tolerancia_secciones"] = tol_map
            self._expandidos[curso_nombre] = True
            self._guardar_y("cursos_cambiadas")
            self.refrescar()

    def _renombrar_seccion(self, curso_nombre, seccion_vieja):
        curso, idx = self._buscar_curso_idx(curso_nombre)
        if curso is None:
            return
        tol_curso = config_app.tolerancia_de(self.cfg, curso_nombre)
        tol_map = dict(curso.get("tolerancia_secciones") or {})
        tol_actual = tol_map.get(seccion_vieja)
        dlg = DialogoSeccion(
            curso_nombre, seccion_vieja, curso.get("secciones") or [],
            tol_curso=tol_curso, tol_seccion=tol_actual, parent=self)
        if dlg.exec():
            nueva = dlg.nombre_seccion()
            if nueva != seccion_vieja:
                config_app.renombrar_seccion(
                    self.cfg, curso_nombre, seccion_vieja, nueva)
                gestor.renombrar_seccion(curso_nombre, seccion_vieja, nueva)
            tol_propia = dlg.tolerancia_personalizada()
            tol_map_nuevo = dict(self.cfg["cursos"][idx].get("tolerancia_secciones") or {})
            tol_map_nuevo.pop(seccion_vieja, None)
            if tol_propia is not None:
                tol_map_nuevo[nueva] = tol_propia
            else:
                tol_map_nuevo.pop(nueva, None)
            self.cfg["cursos"][idx]["tolerancia_secciones"] = tol_map_nuevo
            self._guardar_y("cursos_cambiadas")
            self.refrescar()

    def _quitar_seccion(self, curso_nombre, seccion):
        curso, idx = self._buscar_curso_idx(curso_nombre)
        if curso is None:
            return
        if not pregunta(
                self, "Quitar sección",
                f"¿Quitar la sección «{seccion}» de «{curso_nombre}»?\n"
                "Sus franjas horarias y asignaciones de alumnos se quitarán "
                "(los alumnos se conservan en la base de Alumnos).",
                yes="Quitar"):
            return
        self.cfg["cursos"][idx]["secciones"] = [
            s for s in (curso.get("secciones") or [])
            if str(s).strip() != seccion
        ]
        tol_map = dict(curso.get("tolerancia_secciones") or {})
        tol_map.pop(seccion, None)
        self.cfg["cursos"][idx]["tolerancia_secciones"] = tol_map
        self.cfg["horarios"] = [
            h for h in (self.cfg.get("horarios", []) or [])
            if not (
                str(h.get("curso", "") or "").strip() == curso_nombre
                and str(h.get("seccion") or h.get("grupo") or "").strip() == seccion
            )
        ]
        gestor.quitar_inscripciones_seccion(curso_nombre, seccion)
        self._guardar_y("cursos_cambiadas")
        self.refrescar()

    # ---------------- Acciones sobre horarios ----------------
    def _agregar_franja(self, curso_nombre, seccion_nombre=""):
        dlg = DialogoFranja(
            None, self.cfg, self,
            curso_prefijo=curso_nombre, seccion_prefija=seccion_nombre)
        if dlg.exec():
            f = dlg.datos()
            f["id"] = config_app.nuevo_id_horario(self.cfg)
            self.cfg.setdefault("horarios", []).append(f)
            self._guardar_y("horarios_cambiados")
            self.refrescar()

    def _editar_franja(self, franja):
        franjas = self.cfg.get("horarios", []) or []
        try:
            idx = franjas.index(franja)
        except ValueError:
            fid = franja.get("id")
            idx = next((i for i, x in enumerate(franjas) if x.get("id") == fid), -1)
        if idx < 0:
            return
        dlg = DialogoFranja(franja, self.cfg, self)
        if dlg.exec():
            f = dlg.datos()
            f["id"] = franja.get("id")
            self.cfg["horarios"][idx] = f
            self._guardar_y("horarios_cambiados")
            self.refrescar()

    def _quitar_franja(self, franja):
        franjas = self.cfg.get("horarios", []) or []
        try:
            idx = franjas.index(franja)
        except ValueError:
            fid = franja.get("id")
            idx = next((i for i, x in enumerate(franjas) if x.get("id") == fid), -1)
        if idx < 0:
            return
        nombre = franja.get("nombre") or f"{franja.get('inicio', '')}–{franja.get('fin', '')}"
        if not pregunta(self, "Quitar horario",
                        f"¿Quitar el horario «{nombre}»?", yes="Quitar"):
            return
        del self.cfg["horarios"][idx]
        self._guardar_y("horarios_cambiados")
        self.refrescar()
