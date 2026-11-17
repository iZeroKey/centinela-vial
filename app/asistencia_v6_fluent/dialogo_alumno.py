#!/usr/bin/env python3
"""
Centinela Control — Diálogo de alta/edición de alumnos (v6)

Datos + inscripciones (lista de curso·seccion; un curso solo puede
aparecer una vez) + imagen. La imagen puede capturarse con una cámara
configurada o elegirse de un fichero. Al guardar, un trabajo en segundo
plano valida la cara, regenera el embedding y sincroniza JSON + SQLite
vía gestor_alumnos; la UI no se congela.

Los IDs de alumno son internos y no se muestran.

El diálogo es un MessageBoxBase de qfluentwidgets (estilo Fluent de
Windows 11): máscara, botones Fluent y validación sin cerrar.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QWidget, QFileDialog,
)
from qfluentwidgets import (
    LineEdit, PushButton, ComboBox, TextEdit,
    MessageBoxBase, SubtitleLabel, CaptionLabel, ScrollArea, isDarkTheme,
    TransparentToolButton, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hilo_trabajo import Trabajo
from dialogos_fluent import aviso, pregunta
from ui_util import ndarray_a_pixmap
from dialogo_captura import DialogoCaptura
import config as config_app
import gestor_alumnos as gestor


class DialogoAlumno(MessageBoxBase):
    """Alta (alumno=None) o edición (alumno=dict) con datos y foto."""

    def __init__(self, alumno=None, callbacks_captura=None, parent=None):
        super().__init__(parent=parent)
        self._alumno = alumno if alumno is not None else {}
        self._nuevo = alumno is None
        if callbacks_captura is None:
            callbacks_captura = (None, None)
        self._cb_pausar, self._cb_reanudar = callbacks_captura
        self._imagen_nueva = None   # ndarray BGR elegido/capturado, o None
        self._trabajo = None

        self.widget.setMinimumWidth(560)

        self.titleLabel = SubtitleLabel(
            "Nuevo alumno" if self._nuevo else "Editar alumno")
        self.viewLayout.addWidget(self.titleLabel)

        # --- Campos del formulario (con scroll para no crecer sin límite) ---
        self.edit_nombre = LineEdit()
        self.edit_nombre.setText(self._alumno.get("nombre", "") or "")
        self.edit_apellidos = LineEdit()
        self.edit_apellidos.setText(self._alumno.get("apellidos", "") or "")
        self.edit_dni = LineEdit()
        self.edit_dni.setText(self._alumno.get("dni", "") or "")
        self._cfg = config_app.cargar()
        self.edit_telefono = LineEdit()
        self.edit_telefono.setText(self._alumno.get("telefono", "") or "")
        self.edit_email = LineEdit()
        self.edit_email.setText(self._alumno.get("email", "") or "")
        self.edit_tutor = LineEdit()
        self.edit_tutor.setText(self._alumno.get("tutor_nombre", "") or "")
        self.edit_tutor_tel = LineEdit()
        self.edit_tutor_tel.setText(self._alumno.get("tutor_telefono", "") or "")
        self.combo_condicion = ComboBox()
        self.combo_condicion.addItems(["regular", "irregular"])
        self.combo_condicion.setCurrentText(
            self._alumno.get("condicion", "regular") or "regular")
        self.edit_observaciones = TextEdit()
        self.edit_observaciones.setPlainText(
            self._alumno.get("observaciones", "") or "")
        self.edit_observaciones.setFixedHeight(60)

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = CaptionLabel(etiqueta)
            l.setFixedWidth(100)
            h.addWidget(l)
            h.addWidget(widget, 1)
            return h

        cuerpo = QWidget()
        cuerpo.setStyleSheet("background: transparent;")
        form = QVBoxLayout(cuerpo)
        form.setSpacing(10)
        form.setContentsMargins(0, 0, 0, 0)
        form.addLayout(fila("Nombre:", self.edit_nombre))
        form.addLayout(fila("Apellidos:", self.edit_apellidos))
        form.addLayout(fila("DNI:", self.edit_dni))

        # ---- Inscripciones: varios cursos, una seccion por curso ----
        lbl_insc = CaptionLabel("Inscripciones (curso · sección):")
        form.addWidget(lbl_insc)
        self._filas_insc = []   # (contenedor, combo_curso, combo_seccion)
        self._v_insc = QVBoxLayout()
        self._v_insc.setSpacing(6)
        form.addLayout(self._v_insc)
        btn_add_insc = PushButton("Agregar curso")
        btn_add_insc.setObjectName("secundario")
        btn_add_insc.clicked.connect(self._agregar_inscripcion)
        fila_add = QHBoxLayout()
        fila_add.addStretch(1)
        fila_add.addWidget(btn_add_insc)
        form.addLayout(fila_add)
        insc_previas = self._alumno.get("inscripciones") or []
        if not insc_previas and str(self._alumno.get("curso", "") or "").strip():
            insc_previas = [{"curso": self._alumno.get("curso", ""),
                             "seccion": self._alumno.get("seccion", "")}]
        for insc in insc_previas:
            self._agregar_inscripcion(
                str(insc.get("curso", "") or ""), str(insc.get("seccion", "") or ""))
        if not self._filas_insc:
            self._agregar_inscripcion()

        form.addLayout(fila("Telefono:", self.edit_telefono))
        form.addLayout(fila("Email:", self.edit_email))
        form.addLayout(fila("Tutor:", self.edit_tutor))
        form.addLayout(fila("Tel. tutor:", self.edit_tutor_tel))
        form.addLayout(fila("Condicion:", self.combo_condicion))
        form.addLayout(fila("Observaciones:", self.edit_observaciones))

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cuerpo)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setFixedHeight(290)
        self.viewLayout.addWidget(scroll)

        # --- Imagen ---
        oscuro = isDarkTheme()
        fondo_img = "rgba(255, 255, 255, 0.06)" if oscuro else "rgba(0, 0, 0, 0.05)"
        borde_img = "rgba(255, 255, 255, 0.14)" if oscuro else "rgba(0, 0, 0, 0.12)"
        self.lbl_imagen = QLabel()
        self.lbl_imagen.setFixedSize(180, 180)
        self.lbl_imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_imagen.setStyleSheet(
            f"background: {fondo_img}; border: 1px solid {borde_img};"
            " border-radius: 12px; color: rgba(128, 128, 128, 0.9); font-size: 12px;"
        )
        self.viewLayout.addWidget(self.lbl_imagen, 0, Qt.AlignmentFlag.AlignHCenter)

        fila_img = QHBoxLayout()
        fila_img.setSpacing(10)
        self.btn_capturar = PushButton("Capturar con cámara…")
        self.btn_capturar.clicked.connect(self._capturar)
        self.btn_archivo = PushButton("Elegir archivo…")
        self.btn_archivo.clicked.connect(self._elegir_archivo)
        fila_img.addWidget(self.btn_capturar, 1)
        fila_img.addWidget(self.btn_archivo, 1)
        self.viewLayout.addLayout(fila_img)

        self.lbl_estado = CaptionLabel("")
        self.lbl_estado.setWordWrap(True)
        self.viewLayout.addWidget(self.lbl_estado)

        # --- Estado del alumno (solo en edición) ---
        if not self._nuevo:
            inactivo = self._alumno.get("estado") == "inactivo"
            fila_estado = QHBoxLayout()
            lbl_est = CaptionLabel(
                "Estado: inactivo (no se reconoce ni se lista)"
                if inactivo else "Estado: activo")
            self.btn_estado = PushButton("Reactivar" if inactivo else "Dar de baja…")
            self.btn_estado.clicked.connect(self._cambiar_estado)
            fila_estado.addWidget(lbl_est)
            fila_estado.addStretch(1)
            fila_estado.addWidget(self.btn_estado)
            self.viewLayout.addLayout(fila_estado)

        # --- Botones Fluent del MessageBoxBase ---
        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")
        # Desactivamos el cierre por defecto: el guardado es asíncrono y el
        # diálogo solo se cierra cuando el trabajo en segundo plano termina.
        self.yesButton.clicked.disconnect()
        self.yesButton.clicked.connect(self._guardar)
        self.cancelButton.clicked.disconnect()
        self.cancelButton.clicked.connect(self.reject)

        if self._nuevo:
            self._texto_preview("Sin foto — captura con la cámara\no elige un archivo")
        else:
            self._mostrar_foto_actual()

    # ---------------- Inscripciones ----------------
    def _agregar_inscripcion(self, curso="", seccion=""):
        """Fila de inscripcion: combo de curso + combo de seccion + borrar."""
        fila = QHBoxLayout()
        fila.setSpacing(8)

        combo_curso = ComboBox()
        combo_curso.addItem("(sin curso)")
        combo_curso.addItems(config_app.nombres_cursos(self._cfg))
        idx = combo_curso.findText(str(curso or "").strip())
        combo_curso.setCurrentIndex(idx if idx > 0 else 0)

        combo_seccion = ComboBox()
        combo_seccion.addItem("(sin sección)")
        fila.addWidget(combo_curso, 1)
        fila.addWidget(combo_seccion, 1)

        btn = TransparentToolButton(FIcon.CLOSE)
        btn.setToolTip("Quitar esta inscripción")
        fila.addWidget(btn)

        cont = QWidget()
        cont.setStyleSheet("background: transparent;")
        cont.setLayout(fila)
        self._v_insc.addWidget(cont)

        def _refrescar_secciones(*_):
            texto = combo_seccion.currentText() if combo_seccion.count() else ""
            elegido = str(texto or "").replace("(sin sección)", "").strip()
            combo_seccion.blockSignals(True)
            combo_seccion.clear()
            combo_seccion.addItem("(sin sección)")
            combo_seccion.addItems(
                config_app.secciones_de_curso(self._cfg, combo_curso.currentText()))
            if elegido:
                j = combo_seccion.findText(elegido)
                combo_seccion.setCurrentIndex(j if j > 0 else 0)
            combo_seccion.blockSignals(False)

        combo_curso.currentIndexChanged.connect(_refrescar_secciones)
        btn.clicked.connect(lambda: self._quitar_inscripcion(cont))
        self._filas_insc.append((cont, combo_curso, combo_seccion))
        _refrescar_secciones()
        if str(seccion or "").strip():
            j = combo_seccion.findText(str(seccion).strip())
            if j > 0:
                combo_seccion.setCurrentIndex(j)

    def _quitar_inscripcion(self, cont):
        self._filas_insc = [(c, cu, s) for (c, cu, s) in self._filas_insc
                            if c is not cont]
        cont.setParent(None)
        cont.deleteLater()

    def inscripciones_elegidas(self):
        """Inscripciones con curso real: [{"curso","seccion"},...]."""
        res = []
        for _c, combo_curso, combo_seccion in self._filas_insc:
            if combo_curso.currentIndex() <= 0:
                continue
            seccion = ""
            if combo_seccion.currentIndex() > 0:
                seccion = combo_seccion.currentText().strip()
            res.append({"curso": combo_curso.currentText().strip(),
                        "seccion": seccion})
        return res

    # ---------------- Preview de imagen ----------------
    def _texto_preview(self, texto):
        self.lbl_imagen.setText(texto)
        self.lbl_imagen.setPixmap(QPixmap())

    def _mostrar_preview(self, img):
        pix = ndarray_a_pixmap(img).scaled(
            self.lbl_imagen.width(), self.lbl_imagen.height(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.lbl_imagen.setPixmap(pix)

    def _mostrar_foto_actual(self):
        ruta = gestor.ruta_foto_absoluta(self._alumno.get("foto_referencia"))
        if ruta:
            img = gestor.leer_imagen(ruta)
            if img is not None:
                self._mostrar_preview(img)
                return
        self._texto_preview("Sin foto")

    # ---------------- Orígenes de la imagen ----------------
    def _elegir_archivo(self):
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Elegir foto del alumno", "",
            "Imágenes (*.jpg *.jpeg *.png *.bmp *.webp)")
        if not ruta:
            return
        img = gestor.leer_imagen(ruta)
        if img is None:
            aviso(self, "Imagen", "No se pudo leer la imagen seleccionada.")
            return
        self._imagen_nueva = img
        self._mostrar_preview(img)

    def _capturar(self):
        camaras = config_app.cargar().get("camaras", [])
        if not camaras:
            aviso(self, "Sin cámaras",
                  "No hay cámaras configuradas. Agrégalas en la pestaña Configuración.")
            return
        dlg = DialogoCaptura(camaras,
                             pausar_fuente=self._cb_pausar,
                             reanudar=self._cb_reanudar,
                             parent=self.window())
        if dlg.exec() and dlg.imagen is not None:
            self._imagen_nueva = dlg.imagen
            self._mostrar_preview(dlg.imagen)

    # ---------------- Guardado ----------------
    def _set_procesando(self, activo, texto=""):
        self.btn_capturar.setEnabled(not activo)
        self.btn_archivo.setEnabled(not activo)
        if hasattr(self, "btn_estado"):
            self.btn_estado.setEnabled(not activo)
        self.yesButton.setEnabled(not activo)
        self.cancelButton.setEnabled(not activo)
        self.lbl_estado.setText(texto)

    def _guardar(self):
        nombre = self.edit_nombre.text().strip()
        if not nombre:
            aviso(self, "Datos incompletos", "El nombre es obligatorio.")
            return
        if not any(c.isalpha() for c in nombre):
            aviso(self, "Datos incompletos",
                  "El nombre debe tener al menos una letra (no solo símbolos).")
            return
        inscripciones = self.inscripciones_elegidas()
        for _c, combo_curso, _s in self._filas_insc:
            if combo_curso.currentIndex() <= 0:
                aviso(self, "Inscripciones",
                      "Elige el curso de cada inscripción o quítala.")
                return
        apellidos = self.edit_apellidos.text().strip()
        dni = self.edit_dni.text().strip()
        telefono = self.edit_telefono.text().strip()
        email = self.edit_email.text().strip()
        tutor = self.edit_tutor.text().strip()
        tutor_tel = self.edit_tutor_tel.text().strip()
        condicion = self.combo_condicion.currentText().strip()
        observaciones = self.edit_observaciones.toPlainText().strip()
        extra = dict(apellidos=apellidos, dni=dni,
                     telefono=telefono, email=email, tutor_nombre=tutor,
                     tutor_telefono=tutor_tel, condicion=condicion,
                     observaciones=observaciones)

        if self._nuevo:
            if self._imagen_nueva is None:
                aviso(self, "Falta la foto",
                      "Captura una foto con la cámara o elige un archivo.")
                return
            fn = lambda: gestor.agregar(nombre, inscripciones=inscripciones,
                                        imagen=self._imagen_nueva, **extra)
        else:
            fn = lambda: gestor.editar(self._alumno["alumno_id"], nombre=nombre,
                                       inscripciones=inscripciones,
                                       imagen=self._imagen_nueva, **extra)

        self._set_procesando(True, "Guardando y procesando imagen (puede tardar unos segundos)…")
        self._trabajo = Trabajo(fn, self)
        self._trabajo.listo.connect(self._on_trabajo)
        self._trabajo.start()

    def _cambiar_estado(self):
        inactivo = self._alumno.get("estado") == "inactivo"
        nuevo_estado = "activo" if inactivo else "inactivo"
        if not inactivo:
            ok = pregunta(
                self, "Dar de baja",
                "El alumno dejará de reconocerse y de aparecer en las listas.\n"
                "Su historial de asistencias se conserva. ¿Continuar?",
                yes="Continuar")
            if not ok:
                return
        self._set_procesando(True, "Aplicando cambio de estado…")
        alumno_id = self._alumno["alumno_id"]
        self._trabajo = Trabajo(lambda: gestor.cambiar_estado(alumno_id, nuevo_estado), self)
        self._trabajo.listo.connect(self._on_trabajo)
        self._trabajo.start()

    def _on_trabajo(self, ok, mensaje):
        self._set_procesando(False)
        if ok:
            self.accept()
        else:
            aviso(self, "No se pudo completar", mensaje)
