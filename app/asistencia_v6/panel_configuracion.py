#!/usr/bin/env python3
"""
Centinela Control — Pestaña Configuración (v6)

Secciones:
- Cámaras: alta/edición/baja y prueba de conexión. Preparado para 2 o más
  cámaras (lista persistida en config.json).
- Asistencia: parámetros de reconocimiento (umbral, confirmación,
  anti-duplicado).
- Módulos: vigilancias activables; los no disponibles van deshabilitados.
"""

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QDoubleSpinBox, QSpinBox, QCheckBox,
    QGroupBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QDialog, QDialogButtonBox, QMessageBox, QAbstractItemView,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from estilos import ESTILO
from hilo_trabajo import Trabajo
from camara_hilo import probar_fuente
import config as config_app

TIPO_ETIQUETAS = {"local": "Webcam local", "http": "Stream HTTP (ESP32)"}


class DialogoCamara(QDialog):
    """Alta/edición de una cámara."""

    def __init__(self, camara=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Nueva cámara" if camara is None else "Editar cámara")
        self.setMinimumWidth(480)
        self.setStyleSheet(ESTILO)
        self._camara = camara or {}

        self.edit_nombre = QLineEdit(self._camara.get("nombre", "") or "")
        self.combo_tipo = QComboBox()
        self.combo_tipo.addItems([TIPO_ETIQUETAS["local"], TIPO_ETIQUETAS["http"]])
        self.combo_tipo.setCurrentIndex(0 if self._camara.get("tipo") == "local" else 1)
        self.combo_tipo.currentIndexChanged.connect(self._actualizar_placeholder)

        self.edit_fuente = QLineEdit(str(self._camara.get("fuente", "") or ""))
        self._actualizar_placeholder()

        self.chk_activa = QCheckBox("Cámara activa (aparece en Vigilancia)")
        self.chk_activa.setChecked(bool(self._camara.get("activa", True)))

        self.spin_intervalo = QDoubleSpinBox()
        self.spin_intervalo.setRange(0.05, 2.0)
        self.spin_intervalo.setSingleStep(0.05)
        self.spin_intervalo.setSuffix(" s")
        self.spin_intervalo.setValue(float(self._camara.get("intervalo_proceso", 0.1)))
        self.spin_intervalo.setFixedWidth(120)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(20, 20, 20, 20)

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = QLabel(etiqueta)
            l.setFixedWidth(130)
            l.setStyleSheet("color:#a3a3a3; background: transparent; border: none;")
            h.addWidget(l)
            h.addWidget(widget)
            return h

        layout.addLayout(fila("Nombre:", self.edit_nombre))
        layout.addLayout(fila("Tipo:", self.combo_tipo))
        layout.addLayout(fila("Fuente:", self.edit_fuente))
        layout.addLayout(fila("Intervalo:", self.spin_intervalo))
        layout.addWidget(self.chk_activa)

        ayuda = QLabel("Webcam local usa el índice del dispositivo (0, 1, ...).\n"
                       "Stream HTTP es la URL MJPEG de la ESP32 (http://<ip>:81/stream).")
        ayuda.setWordWrap(True)
        ayuda.setStyleSheet("color:#8a8a8a; font-size: 12px; background:transparent; border:none;")
        layout.addWidget(ayuda)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self._validar)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet("QPushButton { min-width: 90px; padding: 6px 14px; }")
        layout.addWidget(btns)

    def _actualizar_placeholder(self, *_):
        if self._tipo() == "local":
            self.edit_fuente.setPlaceholderText("Índice de webcam (0, 1, ...)")
        else:
            self.edit_fuente.setPlaceholderText("http://<ip>:81/stream")

    def _tipo(self):
        return "local" if self.combo_tipo.currentIndex() == 0 else "http"

    def _validar(self):
        if not self.edit_fuente.text().strip():
            QMessageBox.warning(self, "Falta la fuente",
                                "Indica la fuente de la cámara (índice o URL).")
            return
        self.accept()

    def datos(self):
        return {
            "id": self._camara.get("id"),   # None en alta; el panel lo asigna
            "nombre": self.edit_nombre.text().strip() or "Cámara",
            "tipo": self._tipo(),
            "fuente": self.edit_fuente.text().strip(),
            "activa": self.chk_activa.isChecked(),
            "intervalo_proceso": float(self.spin_intervalo.value()),
        }


class PanelConfiguracion(QWidget):
    """Pestaña Configuración completa."""

    def __init__(self, callbacks=None, parent=None):
        super().__init__(parent)
        self._cb = callbacks or {}
        self.cfg = config_app.cargar()
        self._trabajo_prueba = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(14)

        layout.addWidget(self._construir_grupo_camaras())
        layout.addWidget(self._construir_grupo_asistencia())
        layout.addStretch(1)

    # ---------------- Cámaras ----------------
    def _construir_grupo_camaras(self):
        grupo = QGroupBox("Cámaras (para vigilancia y captura de fotos)")
        v = QVBoxLayout(grupo)
        v.setSpacing(10)

        self.tabla_camaras = QTableWidget(0, 4)
        self.tabla_camaras.setHorizontalHeaderLabels(["Nombre", "Tipo", "Fuente", "Activa"])
        self.tabla_camaras.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.tabla_camaras.verticalHeader().setVisible(False)
        self.tabla_camaras.setAlternatingRowColors(True)
        self.tabla_camaras.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla_camaras.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla_camaras.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla_camaras.setMinimumHeight(140)
        v.addWidget(self.tabla_camaras)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(10)
        btn_agregar = QPushButton("Agregar")
        btn_agregar.clicked.connect(self._agregar_camara)
        btn_editar = QPushButton("Editar")
        btn_editar.setObjectName("secundario")
        btn_editar.clicked.connect(self._editar_camara)
        btn_probar = QPushButton("Probar conexión")
        btn_probar.setObjectName("secundario")
        btn_probar.clicked.connect(self._probar_camara)
        btn_quitar = QPushButton("Quitar")
        btn_quitar.setObjectName("peligro")
        btn_quitar.clicked.connect(self._quitar_camara)
        for b in (btn_agregar, btn_editar, btn_probar, btn_quitar):
            fila_botones.addWidget(b, 1)
        v.addLayout(fila_botones)

        self._refrescar_tabla()
        return grupo

    def _refrescar_tabla(self):
        camaras = self.cfg.get("camaras", [])
        self.tabla_camaras.setRowCount(len(camaras))
        for i, cam in enumerate(camaras):
            valores = [
                cam.get("nombre", ""),
                TIPO_ETIQUETAS.get(cam.get("tipo"), str(cam.get("tipo", ""))),
                cam.get("fuente", ""),
                "Sí" if cam.get("activa") else "No",
            ]
            for col, val in enumerate(valores):
                self.tabla_camaras.setItem(i, col, QTableWidgetItem(str(val)))

    def _camara_sel(self):
        fila = self.tabla_camaras.currentRow()
        camaras = self.cfg.get("camaras", [])
        if fila < 0 or fila >= len(camaras):
            QMessageBox.information(self, "Cámaras", "Seleccione una cámara de la lista.")
            return None, fila
        return camaras[fila], fila

    def _guardar_y(self, callback):
        config_app.guardar(self.cfg)
        fn = self._cb.get(callback)
        if fn:
            fn(self.cfg)

    def _agregar_camara(self):
        dlg = DialogoCamara(parent=self)
        if dlg.exec():
            cam = dlg.datos()
            cam["id"] = config_app.nuevo_id_camara(self.cfg)
            self.cfg.setdefault("camaras", []).append(cam)
            self._guardar_y("camaras_cambiadas")
            self._refrescar_tabla()

    def _editar_camara(self):
        _cam, fila = self._camara_sel()
        if _cam is None:
            return
        dlg = DialogoCamara(self.cfg["camaras"][fila], self)
        if dlg.exec():
            self.cfg["camaras"][fila] = dlg.datos()
            self._guardar_y("camaras_cambiadas")
            self._refrescar_tabla()

    def _quitar_camara(self):
        _cam, fila = self._camara_sel()
        if _cam is None:
            return
        nombre = str(self.cfg["camaras"][fila].get("nombre", ""))
        respuesta = QMessageBox.question(
            self, "Quitar cámara",
            f"¿Quitar la cámara '{nombre}' de la configuración?")
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        del self.cfg["camaras"][fila]
        self._guardar_y("camaras_cambiadas")
        self._refrescar_tabla()

    def _probar_camara(self):
        cam, _fila = self._camara_sel()
        if cam is None:
            return
        if self._trabajo_prueba is not None and self._trabajo_prueba.isRunning():
            return
        self._trabajo_prueba = Trabajo(
            lambda: probar_fuente(cam.get("tipo"), cam.get("fuente", "")), self)
        self._trabajo_prueba.listo.connect(self._on_prueba_lista)
        self._trabajo_prueba.start()

    def _on_prueba_lista(self, ok, mensaje):
        if ok:
            QMessageBox.information(self, "Prueba de cámara", mensaje)
        else:
            QMessageBox.warning(self, "Prueba de cámara", mensaje)

    # ---------------- Asistencia ----------------
    def _construir_grupo_asistencia(self):
        grupo = QGroupBox("Asistencia — parámetros de reconocimiento")
        v = QVBoxLayout(grupo)
        v.setSpacing(10)

        asist = self.cfg.get("asistencia", {})

        self.spin_umbral = QDoubleSpinBox()
        self.spin_umbral.setRange(0.10, 0.99)
        self.spin_umbral.setSingleStep(0.05)
        self.spin_umbral.setValue(float(asist.get("umbral_similitud", 0.70)))
        self.spin_umbral.setFixedWidth(130)

        self.spin_confirm = QDoubleSpinBox()
        self.spin_confirm.setRange(1.0, 10.0)
        self.spin_confirm.setSingleStep(0.5)
        self.spin_confirm.setSuffix(" s")
        self.spin_confirm.setValue(float(asist.get("segundos_confirmacion", 3.0)))
        self.spin_confirm.setFixedWidth(130)

        self.spin_antidup = QSpinBox()
        self.spin_antidup.setRange(5, 600)
        self.spin_antidup.setSuffix(" s")
        self.spin_antidup.setValue(int(asist.get("intervalo_antiduplicado", 30)))
        self.spin_antidup.setFixedWidth(130)

        def fila(etiqueta, widget, ayuda=""):
            h = QHBoxLayout()
            l = QLabel(etiqueta)
            l.setFixedWidth(240)
            l.setStyleSheet("color:#a3a3a3; background: transparent; border: none;")
            h.addWidget(l)
            h.addWidget(widget)
            h.addStretch(1)
            if ayuda:
                a = QLabel(ayuda)
                a.setStyleSheet("color:#8a8a8a; font-size: 12px; background:transparent; border:none;")
                h.addWidget(a)
            return h

        v.addLayout(fila("Umbral de similitud", self.spin_umbral, "0.10 – 0.99"))
        v.addLayout(fila("Segundos de confirmación", self.spin_confirm, "tiempo seguido para confirmar"))
        v.addLayout(fila("Intervalo anti-duplicado", self.spin_antidup, "mínimo entre marcas del mismo alumno"))

        btn_guardar = QPushButton("Guardar parámetros")
        btn_guardar.clicked.connect(self._guardar_parametros)
        v.addWidget(btn_guardar, 0, Qt.AlignmentFlag.AlignLeft)
        return grupo

    def _guardar_parametros(self):
        self.cfg["asistencia"] = {
            "umbral_similitud": float(self.spin_umbral.value()),
            "segundos_confirmacion": float(self.spin_confirm.value()),
            "intervalo_antiduplicado": int(self.spin_antidup.value()),
        }
        self._guardar_y("parametros_cambiados")
