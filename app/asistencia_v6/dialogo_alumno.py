#!/usr/bin/env python3
"""
Centinela Control — Diálogo de alta/edición de alumnos (v6)

Datos (nombre, curso, grupo) + imagen. La imagen puede capturarse con
una cámara configurada o elegirse de un fichero. Al guardar, un trabajo
en segundo plano valida la cara, regenera el embedding y sincroniza
JSON + SQLite vía gestor_alumnos; la UI no se congela.

Los IDs de alumno son internos y no se muestran.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QDialogButtonBox,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from estilos import ESTILO
from hilo_trabajo import Trabajo
from ui_util import ndarray_a_pixmap
from dialogo_captura import DialogoCaptura
import config as config_app
import gestor_alumnos as gestor


class DialogoAlumno(QDialog):
    """Alta (alumno=None) o edición (alumno=dict) con datos y foto."""

    def __init__(self, alumno=None, callbacks_captura=None, parent=None):
        super().__init__(parent)
        self._alumno = alumno if alumno is not None else {}
        self._nuevo = alumno is None
        if callbacks_captura is None:
            callbacks_captura = (None, None)
        self._cb_pausar, self._cb_reanudar = callbacks_captura
        self._imagen_nueva = None   # ndarray BGR elegido/capturado, o None
        self._trabajo = None

        self.setWindowTitle("Nuevo alumno" if self._nuevo else "Editar alumno")
        self.setMinimumWidth(500)
        self.setStyleSheet(ESTILO)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        self.edit_nombre = QLineEdit(self._alumno.get("nombre", "") or "")
        self.edit_curso = QLineEdit(self._alumno.get("curso", "") or "")
        self.edit_grupo = QLineEdit(self._alumno.get("grupo", "") or "")

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = QLabel(etiqueta)
            l.setFixedWidth(90)
            l.setStyleSheet("color:#a3a3a3; background: transparent; border: none;")
            h.addWidget(l)
            h.addWidget(widget)
            return h

        layout.addLayout(fila("Nombre:", self.edit_nombre))
        layout.addLayout(fila("Curso:", self.edit_curso))
        layout.addLayout(fila("Grupo:", self.edit_grupo))

        # --- Imagen ---
        self.lbl_imagen = QLabel()
        self.lbl_imagen.setFixedSize(220, 220)
        self.lbl_imagen.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_imagen.setStyleSheet(
            "background: #0d0d0d; border: 1px solid #333333; border-radius: 10px;"
            " color: #5a5a5a; font-size: 12px;"
        )
        layout.addWidget(self.lbl_imagen, 0, Qt.AlignmentFlag.AlignHCenter)

        fila_img = QHBoxLayout()
        fila_img.setSpacing(10)
        self.btn_capturar = QPushButton("Capturar con cámara…")
        self.btn_capturar.setObjectName("secundario")
        self.btn_capturar.clicked.connect(self._capturar)
        self.btn_archivo = QPushButton("Elegir archivo…")
        self.btn_archivo.setObjectName("secundario")
        self.btn_archivo.clicked.connect(self._elegir_archivo)
        fila_img.addWidget(self.btn_capturar, 1)
        fila_img.addWidget(self.btn_archivo, 1)
        layout.addLayout(fila_img)

        self.lbl_estado = QLabel("")
        self.lbl_estado.setWordWrap(True)
        self.lbl_estado.setStyleSheet("color:#a3a3a3; font-size: 12px; background:transparent; border:none;")
        layout.addWidget(self.lbl_estado)

        # --- Estado del alumno (solo en edición) ---
        if not self._nuevo:
            inactivo = self._alumno.get("estado") == "inactivo"
            fila_estado = QHBoxLayout()
            lbl_est = QLabel("Estado: inactivo (no se reconoce ni se lista)"
                             if inactivo else "Estado: activo")
            lbl_est.setStyleSheet("color:#a3a3a3; font-size: 12px; background:transparent; border:none;")
            self.btn_estado = QPushButton("Reactivar" if inactivo else "Dar de baja…")
            self.btn_estado.setObjectName("secundario")
            self.btn_estado.clicked.connect(self._cambiar_estado)
            fila_estado.addWidget(lbl_est)
            fila_estado.addStretch(1)
            fila_estado.addWidget(self.btn_estado)
            layout.addLayout(fila_estado)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._guardar)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet("QPushButton { min-width: 90px; padding: 6px 14px; }")
        self._btns = btns
        layout.addWidget(btns)

        if self._nuevo:
            self._texto_preview("Sin foto — captura con la cámara\no elige un archivo")
        else:
            self._mostrar_foto_actual()

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
            QMessageBox.warning(self, "Imagen", "No se pudo leer la imagen seleccionada.")
            return
        self._imagen_nueva = img
        self._mostrar_preview(img)

    def _capturar(self):
        camaras = config_app.cargar().get("camaras", [])
        if not camaras:
            QMessageBox.information(
                self, "Sin cámaras",
                "No hay cámaras configuradas. Agrégalas en la pestaña Configuración.")
            return
        dlg = DialogoCaptura(camaras,
                             pausar_fuente=self._cb_pausar,
                             reanudar=self._cb_reanudar,
                             parent=self)
        if dlg.exec() and dlg.imagen is not None:
            self._imagen_nueva = dlg.imagen
            self._mostrar_preview(dlg.imagen)

    # ---------------- Guardado ----------------
    def _set_procesando(self, activo, texto=""):
        self.btn_capturar.setEnabled(not activo)
        self.btn_archivo.setEnabled(not activo)
        if hasattr(self, "btn_estado"):
            self.btn_estado.setEnabled(not activo)
        self._btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not activo)
        self._btns.button(QDialogButtonBox.StandardButton.Cancel).setEnabled(not activo)
        self.lbl_estado.setText(texto)

    def _guardar(self):
        nombre = self.edit_nombre.text().strip()
        if not nombre:
            QMessageBox.warning(self, "Datos incompletos", "El nombre es obligatorio.")
            return
        curso = self.edit_curso.text().strip()
        grupo = self.edit_grupo.text().strip()

        if self._nuevo:
            if self._imagen_nueva is None:
                QMessageBox.warning(self, "Falta la foto",
                                    "Captura una foto con la cámara o elige un archivo.")
                return
            fn = lambda: gestor.agregar(nombre, curso=curso, grupo=grupo,
                                        imagen=self._imagen_nueva)
        else:
            fn = lambda: gestor.editar(self._alumno["alumno_id"], nombre=nombre,
                                       curso=curso, grupo=grupo,
                                       imagen=self._imagen_nueva)

        self._set_procesando(True, "Guardando y procesando imagen (puede tardar unos segundos)…")
        self._trabajo = Trabajo(fn, self)
        self._trabajo.listo.connect(self._on_trabajo)
        self._trabajo.start()

    def _cambiar_estado(self):
        inactivo = self._alumno.get("estado") == "inactivo"
        nuevo_estado = "activo" if inactivo else "inactivo"
        if not inactivo:
            respuesta = QMessageBox.question(
                self, "Dar de baja",
                "El alumno dejará de reconocerse y de aparecer en las listas.\n"
                "Su historial de asistencias se conserva. ¿Continuar?")
            if respuesta != QMessageBox.StandardButton.Yes:
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
            QMessageBox.warning(self, "No se pudo completar", mensaje)
