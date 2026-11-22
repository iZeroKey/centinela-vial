#!/usr/bin/env python3
"""
Centinela Control — Diálogo de alta/edición de alumnos (v6)

Gestiona exclusivamente la ficha personal del alumno y su foto facial.
La asignación de alumnos a cursos y secciones se realiza desde la pestaña
«Cursos y horarios». Al guardar, un trabajo en segundo plano valida la cara,
regenera el embedding y sincroniza JSON + SQLite vía gestor_alumnos; la UI
no se congela.

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
    FluentIcon as FIcon,
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

        self.widget.setMinimumWidth(740)

        self.titleLabel = SubtitleLabel(
            "Nuevo alumno" if self._nuevo else "Editar alumno")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        # --- Campos del formulario ---
        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("Ej. Erick Obed")
        self.edit_nombre.setClearButtonEnabled(True)
        self.edit_nombre.setText(self._alumno.get("nombre", "") or "")

        self.edit_apellidos = LineEdit()
        self.edit_apellidos.setPlaceholderText("Ej. Flores Ricra")
        self.edit_apellidos.setClearButtonEnabled(True)
        self.edit_apellidos.setText(self._alumno.get("apellidos", "") or "")

        self.edit_dni = LineEdit()
        self.edit_dni.setPlaceholderText("Documento de identidad (opcional)")
        self.edit_dni.setClearButtonEnabled(True)
        self.edit_dni.setText(self._alumno.get("dni", "") or "")

        self.edit_telefono = LineEdit()
        self.edit_telefono.setPlaceholderText("Ej. 987 654 321 (opcional)")
        self.edit_telefono.setText(self._alumno.get("telefono", "") or "")

        self.edit_email = LineEdit()
        self.edit_email.setPlaceholderText("correo@ejemplo.com (opcional)")
        self.edit_email.setText(self._alumno.get("email", "") or "")

        self.edit_tutor = LineEdit()
        self.edit_tutor.setPlaceholderText("Nombre del apoderado (opcional)")
        self.edit_tutor.setText(self._alumno.get("tutor_nombre", "") or "")

        self.edit_tutor_tel = LineEdit()
        self.edit_tutor_tel.setPlaceholderText("Teléfono del apoderado (opcional)")
        self.edit_tutor_tel.setText(self._alumno.get("tutor_telefono", "") or "")

        self.combo_condicion = ComboBox()
        self.combo_condicion.addItems(["Regular", "Irregular"])
        cond_actual = str(self._alumno.get("condicion", "regular") or "regular").strip().lower()
        self.combo_condicion.setCurrentIndex(1 if cond_actual == "irregular" else 0)

        self.edit_observaciones = TextEdit()
        self.edit_observaciones.setPlaceholderText("Notas médicas, permisos o comentarios (opcional)…")
        self.edit_observaciones.setPlainText(
            self._alumno.get("observaciones", "") or "")
        self.edit_observaciones.setFixedHeight(64)

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = CaptionLabel(etiqueta)
            l.setFixedWidth(92)
            h.addWidget(l)
            h.addWidget(widget, 1)
            return h

        cuerpo = QWidget()
        cuerpo.setStyleSheet("background: transparent;")
        form = QVBoxLayout(cuerpo)
        form.setSpacing(8)
        form.setContentsMargins(0, 4, 10, 4)
        form.addLayout(fila("Nombre:", self.edit_nombre))
        form.addLayout(fila("Apellidos:", self.edit_apellidos))
        form.addLayout(fila("DNI:", self.edit_dni))
        form.addLayout(fila("Teléfono:", self.edit_telefono))
        form.addLayout(fila("Email:", self.edit_email))
        form.addLayout(fila("Tutor:", self.edit_tutor))
        form.addLayout(fila("Tel. tutor:", self.edit_tutor_tel))
        form.addLayout(fila("Condición:", self.combo_condicion))
        form.addLayout(fila("Notas:", self.edit_observaciones))

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cuerpo)
        scroll.setStyleSheet(
            "QScrollArea { border: none; background: transparent; }"
            " QScrollArea > QWidget > QWidget { background: transparent; }")
        scroll.setMinimumHeight(390)

        # --- Panel derecho: Foto + acciones ---
        oscuro = isDarkTheme()
        fondo_img = "rgba(255, 255, 255, 0.06)" if oscuro else "rgba(0, 0, 0, 0.05)"
        borde_img = "rgba(255, 255, 255, 0.14)" if oscuro else "rgba(0, 0, 0, 0.12)"
        self.lbl_imagen = QLabel()
        self.lbl_imagen.setFixedSize(196, 196)
        self.lbl_imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_imagen.setWordWrap(True)
        self.lbl_imagen.setStyleSheet(
            f"background: {fondo_img}; border: 1px solid {borde_img};"
            " border-radius: 12px; color: rgba(148, 163, 184, 0.9); font-size: 12px;"
            " padding: 10px;"
        )

        self.btn_capturar = PushButton(FIcon.CAMERA, "Capturar con cámara…")
        self.btn_capturar.setMinimumHeight(34)
        self.btn_capturar.clicked.connect(self._capturar)
        self.btn_archivo = PushButton(FIcon.FOLDER, "Elegir archivo…")
        self.btn_archivo.setMinimumHeight(34)
        self.btn_archivo.clicked.connect(self._elegir_archivo)

        col_foto = QVBoxLayout()
        col_foto.setSpacing(10)
        col_foto.setContentsMargins(0, 4, 0, 0)
        col_foto.addWidget(self.lbl_imagen, 0, Qt.AlignmentFlag.AlignHCenter)
        col_foto.addWidget(self.btn_capturar)
        col_foto.addWidget(self.btn_archivo)

        if not self._nuevo:
            inactivo = self._alumno.get("estado") == "inactivo"
            col_foto.addSpacing(8)
            lbl_est = CaptionLabel(
                "Estado: inactivo (oculto)" if inactivo else "Estado: activo")
            lbl_est.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl_est.setStyleSheet(
                "color: #fbbf24;" if inactivo else "color: #34d399;")
            self.btn_estado = PushButton("Reactivar" if inactivo else "Dar de baja…")
            self.btn_estado.setMinimumHeight(32)
            self.btn_estado.clicked.connect(self._cambiar_estado)
            col_foto.addWidget(lbl_est)
            col_foto.addWidget(self.btn_estado)

        col_foto.addStretch(1)

        panel_foto = QWidget()
        panel_foto.setStyleSheet("background: transparent;")
        panel_foto.setFixedWidth(212)
        panel_foto.setLayout(col_foto)

        fila_principal = QHBoxLayout()
        fila_principal.setSpacing(18)
        fila_principal.addWidget(scroll, 1)
        fila_principal.addWidget(panel_foto)
        self.viewLayout.addLayout(fila_principal)

        nota_cursos = CaptionLabel(
            "La asignación a cursos y secciones se gestiona desde «Cursos y horarios».")
        nota_cursos.setWordWrap(True)
        nota_cursos.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        self.viewLayout.addWidget(nota_cursos)

        self.lbl_estado = CaptionLabel("")
        self.lbl_estado.setWordWrap(True)
        self.lbl_estado.setVisible(False)
        self.viewLayout.addWidget(self.lbl_estado)

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

    # ---------------- Preview de imagen ----------------
    def _texto_preview(self, texto):
        self.lbl_imagen.setPixmap(QPixmap())
        self.lbl_imagen.setText(texto)

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
        self.lbl_estado.setVisible(bool(texto))

    def _guardar(self):
        nombre = self.edit_nombre.text().strip()
        if not nombre:
            aviso(self, "Datos incompletos", "El nombre es obligatorio.")
            return
        if not any(c.isalpha() for c in nombre):
            aviso(self, "Datos incompletos",
                  "El nombre debe tener al menos una letra (no solo símbolos).")
            return
        apellidos = self.edit_apellidos.text().strip()
        dni = self.edit_dni.text().strip()
        telefono = self.edit_telefono.text().strip()
        email = self.edit_email.text().strip()
        tutor = self.edit_tutor.text().strip()
        tutor_tel = self.edit_tutor_tel.text().strip()
        condicion = self.combo_condicion.currentText().strip().lower()
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
            fn = lambda: gestor.agregar(nombre, inscripciones=[],
                                        imagen=self._imagen_nueva, **extra)
        else:
            fn = lambda: gestor.editar(self._alumno["alumno_id"], nombre=nombre,
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
