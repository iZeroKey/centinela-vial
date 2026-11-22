#!/usr/bin/env python3
"""
Centinela Control — Pestaña Configuración (v6)

Secciones:
- Cámaras: alta/edición/baja y prueba de conexión. Preparado para 2 o más
  cámaras (lista persistida en config.json).
- Asistencia: parámetros de reconocimiento (umbral, confirmación,
  anti-duplicado) y tardanzas (tolerancia + aplicar horarios).

Los cursos/secciones y los horarios de clase se gestionan en la pestaña
"Clases" del sidebar (ver panel_clases.py).
"""

import os, sys, time

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QBoxLayout, QFrame, QStackedWidget,
)
from qfluentwidgets import (
    ComboBox, PushButton, PrimaryPushButton, LineEdit, DoubleSpinBox, SpinBox,
    TableWidget, HeaderCardWidget, MessageBoxBase, SubtitleLabel, StrongBodyLabel,
    CaptionLabel, SwitchButton, SwitchSettingCard, SettingCardGroup, SettingCard,
    PrimaryPushSettingCard, ScrollArea, CardWidget, TransparentToolButton,
    SegmentedWidget, IconWidget, FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dialogos_fluent import aviso, pregunta
from hilo_trabajo import Trabajo
from camara_hilo import probar_fuente
import config as config_app

TIPO_ETIQUETAS = {"local": "Webcam local", "http": "Stream HTTP (ESP32)",
                  "telefono": "Teléfono"}
TIPO_INDICE = {"local": 0, "http": 1, "telefono": 2}


def detectar_webcams_locales():
    """Devuelve [(indice_str, descripcion)] de las webcams detectadas en el equipo."""
    try:
        from PyQt6.QtMultimedia import QMediaDevices
        return [
            (str(i), dev.description().strip() or f"Cámara {i}")
            for i, dev in enumerate(QMediaDevices.videoInputs())
        ]
    except Exception:
        return []


class DialogoCamara(MessageBoxBase):
    """Alta/edición de una cámara (diálogo Fluent)."""

    def __init__(self, camara=None, cfg=None, parent=None, telefonos=None,
                 obtener_telefonos=None):
        super().__init__(parent=parent)
        self._camara = camara or {}
        self._telefonos = telefonos or {}
        self._obtener_telefonos = obtener_telefonos
        self._trabajo_prueba = None
        self._webcam_indices = []

        self.widget.setMinimumWidth(520)

        self.titleLabel = SubtitleLabel(
            "Nueva cámara" if camara is None else "Editar cámara")
        self.viewLayout.setSpacing(12)
        self.viewLayout.addWidget(self.titleLabel)

        self.edit_nombre = LineEdit()
        self.edit_nombre.setPlaceholderText("Ej. Cámara Aula 1")
        self.edit_nombre.setClearButtonEnabled(True)
        self.edit_nombre.setText(self._camara.get("nombre", "") or "")

        self.combo_tipo = ComboBox()
        self.combo_tipo.addItems([TIPO_ETIQUETAS["local"],
                                  TIPO_ETIQUETAS["http"],
                                  TIPO_ETIQUETAS["telefono"]])
        self.combo_tipo.setCurrentIndex(TIPO_INDICE.get(
            self._camara.get("tipo"), 0))

        # --- Fuente según tipo ---
        # 1) Webcam local: selector con dispositivos detectados + índice manual
        self.panel_webcam = QWidget()
        self.panel_webcam.setStyleSheet("background: transparent;")
        hw = QHBoxLayout(self.panel_webcam)
        hw.setContentsMargins(0, 0, 0, 0)
        hw.setSpacing(8)
        self.combo_webcam = ComboBox()
        self.spin_indice_cam = SpinBox()
        self.spin_indice_cam.setRange(0, 16)
        self.spin_indice_cam.setFixedWidth(96)
        self.spin_indice_cam.hide()
        btn_ref_cam = TransparentToolButton(FIcon.SYNC)
        btn_ref_cam.setToolTip("Detectar cámaras conectadas")
        btn_ref_cam.clicked.connect(self._refrescar_webcams)
        hw.addWidget(self.combo_webcam, 1)
        hw.addWidget(self.spin_indice_cam)
        hw.addWidget(btn_ref_cam)

        # 2) Stream HTTP
        self.edit_fuente = LineEdit()
        self.edit_fuente.setClearButtonEnabled(True)
        self.edit_fuente.setPlaceholderText("http://192.168.1.50:81/stream")
        if self._camara.get("tipo") == "http":
            self.edit_fuente.setText(str(self._camara.get("fuente", "") or ""))

        # 3) Teléfono conectado
        self.panel_telefono = QWidget()
        self.panel_telefono.setStyleSheet("background: transparent;")
        ht = QHBoxLayout(self.panel_telefono)
        ht.setContentsMargins(0, 0, 0, 0)
        ht.setSpacing(8)
        self.combo_telefono = ComboBox()
        self._tel_ids = []
        btn_ref_tel = TransparentToolButton(FIcon.SYNC)
        btn_ref_tel.setToolTip("Actualizar teléfonos conectados")
        btn_ref_tel.clicked.connect(self._actualizar_telefonos)
        ht.addWidget(self.combo_telefono, 1)
        ht.addWidget(btn_ref_tel)

        contenedor_fuente = QWidget()
        contenedor_fuente.setStyleSheet("background: transparent;")
        hf = QHBoxLayout(contenedor_fuente)
        hf.setContentsMargins(0, 0, 0, 0)
        hf.setSpacing(0)
        hf.addWidget(self.panel_webcam, 1)
        hf.addWidget(self.edit_fuente, 1)
        hf.addWidget(self.panel_telefono, 1)

        self.combo_webcam.currentIndexChanged.connect(self._on_webcam_cambiada)
        self.combo_tipo.currentIndexChanged.connect(self._actualizar_modo_fuente)

        self._refrescar_webcams(inicial=True)
        self._refrescar_combo_telefonos()

        self.combo_curso = ComboBox()
        self.combo_curso.addItem("Sin curso asignado")
        self.combo_curso.addItems(config_app.nombres_cursos(cfg or {}))
        curso_actual = str(self._camara.get("curso", "") or "").strip()
        idx_curso = self.combo_curso.findText(curso_actual)
        self.combo_curso.setCurrentIndex(idx_curso if idx_curso > 0 else 0)

        self.chk_activa = SwitchButton()
        self.chk_activa.setOnText("")
        self.chk_activa.setOffText("")
        self.chk_activa.setChecked(bool(self._camara.get("activa", True)))

        self.spin_intervalo = DoubleSpinBox()
        self.spin_intervalo.setRange(0.05, 2.0)
        self.spin_intervalo.setSingleStep(0.05)
        self.spin_intervalo.setSuffix(" s")
        self.spin_intervalo.setValue(float(self._camara.get("intervalo_proceso", 0.1)))
        self.spin_intervalo.setFixedWidth(120)

        self.combo_aspecto = ComboBox()
        self.combo_aspecto.addItems(["Original", "16:9", "4:3", "1:1", "21:9"])
        aspecto_actual = str(self._camara.get("recorte_aspecto", "Original") or "Original")
        idx_a = self.combo_aspecto.findText(aspecto_actual)
        self.combo_aspecto.setCurrentIndex(idx_a if idx_a >= 0 else 0)

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = CaptionLabel(etiqueta)
            l.setFixedWidth(96)
            h.addWidget(l)
            h.addWidget(widget, 1)
            return h

        def fila_fija(etiqueta, widget):
            h = QHBoxLayout()
            l = CaptionLabel(etiqueta)
            l.setFixedWidth(96)
            h.addWidget(l)
            h.addWidget(widget)
            h.addStretch(1)
            return h

        self.viewLayout.addLayout(fila("Nombre:", self.edit_nombre))
        self.viewLayout.addLayout(fila("Tipo:", self.combo_tipo))
        self.viewLayout.addLayout(fila("Fuente:", contenedor_fuente))
        self.viewLayout.addLayout(fila("Curso:", self.combo_curso))
        self.viewLayout.addLayout(fila_fija("Intervalo:", self.spin_intervalo))
        self.viewLayout.addLayout(fila("Recorte:", self.combo_aspecto))

        fila_activa = QHBoxLayout()
        lbl_activa = CaptionLabel("Activa en Vigilancia")
        fila_activa.addWidget(self.chk_activa)
        fila_activa.addWidget(lbl_activa)
        fila_activa.addStretch(1)

        self.btn_probar_dlg = PushButton(FIcon.PLAY, "Probar conexión")
        self.btn_probar_dlg.setObjectName("secundario")
        self.btn_probar_dlg.setMinimumHeight(30)
        self.btn_probar_dlg.clicked.connect(self._probar_en_dialogo)
        fila_activa.addWidget(self.btn_probar_dlg)
        self.viewLayout.addLayout(fila_activa)

        self.lbl_ayuda_tipo = CaptionLabel("")
        self.lbl_ayuda_tipo.setWordWrap(True)
        self.lbl_ayuda_tipo.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        self.viewLayout.addWidget(self.lbl_ayuda_tipo)

        self.lbl_prueba = CaptionLabel("")
        self.lbl_prueba.setWordWrap(True)
        self.lbl_prueba.hide()
        self.viewLayout.addWidget(self.lbl_prueba)

        self._actualizar_modo_fuente()

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _refrescar_webcams(self, inicial=False):
        actual = (
            str(self._camara.get("fuente", "0") or "0").strip()
            if inicial and self._camara.get("tipo", "local") == "local"
            else self._fuente_webcam_actual()
        )
        detectadas = detectar_webcams_locales()
        self.combo_webcam.blockSignals(True)
        self.combo_webcam.clear()
        self._webcam_indices = []
        for idx, desc in detectadas:
            self.combo_webcam.addItem(f"{idx} — {desc}")
            self._webcam_indices.append(idx)
        self.combo_webcam.addItem("Otro índice manual…")
        self._webcam_indices.append("__manual__")

        if actual in self._webcam_indices and actual != "__manual__":
            self.combo_webcam.setCurrentIndex(self._webcam_indices.index(actual))
            self.spin_indice_cam.hide()
        else:
            self.combo_webcam.setCurrentIndex(len(self._webcam_indices) - 1)
            try:
                self.spin_indice_cam.setValue(int(actual))
            except (TypeError, ValueError):
                self.spin_indice_cam.setValue(0)
            self.spin_indice_cam.show()
        self.combo_webcam.blockSignals(False)

    def _on_webcam_cambiada(self, idx):
        es_manual = (0 <= idx < len(self._webcam_indices)
                     and self._webcam_indices[idx] == "__manual__")
        self.spin_indice_cam.setVisible(es_manual)

    def _fuente_webcam_actual(self):
        idx = self.combo_webcam.currentIndex()
        if 0 <= idx < len(self._webcam_indices):
            val = self._webcam_indices[idx]
            if val != "__manual__":
                return val
        return str(int(self.spin_indice_cam.value()))

    def _actualizar_telefonos(self):
        if callable(self._obtener_telefonos):
            try:
                self._telefonos = dict(self._obtener_telefonos() or {})
            except Exception:
                pass
        self._refrescar_combo_telefonos()

    def _refrescar_combo_telefonos(self):
        """Rellena el dropdown con los teléfonos conocidos por el servidor."""
        seleccion = (
            str(self._camara.get("fuente", "") or "").strip()
            if self._tipo() == "telefono" and not self._tel_ids
            else self._telefono_seleccionado()
        )
        self.combo_telefono.clear()
        self._tel_ids = []
        items = []
        for tel_id, reg in sorted(
                self._telefonos.items(),
                key=lambda kv: (not kv[1].get("online", False),
                                str(kv[1].get("nombre", "")).lower())):
            nombre = str(reg.get("nombre", "") or "").strip() or f"Teléfono {tel_id[:8]}"
            ip = str(reg.get("ip", "") or "?")
            sufijo = "en línea" if reg.get("online", False) else "desconectado"
            items.append((f"{nombre} — {ip} ({sufijo})", tel_id))
        if seleccion and seleccion not in {t for _, t in items}:
            items.insert(0, (f"(desconectado: {seleccion[:8]})", seleccion))
        if not items:
            self.combo_telefono.addItem("Sin teléfonos conectados")
            self._tel_ids.append("")
        else:
            for texto, tel_id in items:
                self.combo_telefono.addItem(texto)
                self._tel_ids.append(tel_id)
        if seleccion in self._tel_ids:
            self.combo_telefono.setCurrentIndex(self._tel_ids.index(seleccion))

    def _telefono_seleccionado(self):
        idx = self.combo_telefono.currentIndex()
        if 0 <= idx < len(self._tel_ids):
            return self._tel_ids[idx]
        return ""

    def _actualizar_modo_fuente(self, *_):
        tipo = self._tipo()
        self.panel_webcam.setVisible(tipo == "local")
        self.edit_fuente.setVisible(tipo == "http")
        self.panel_telefono.setVisible(tipo == "telefono")
        self.btn_probar_dlg.setVisible(tipo != "telefono")
        if tipo == "telefono":
            self._actualizar_telefonos()
        ayudas = {
            "local": "Selecciona una cámara conectada al PC (o indica su índice manualmente).",
            "http": "Ingresa la URL del stream MJPEG de la cámara IP o ESP32 (ej. http://192.168.1.50:81/stream).",
            "telefono": "Abre https://<ip>:8443/telefono en el celular y activa su cámara para seleccionarlo aquí.",
        }
        self.lbl_ayuda_tipo.setText(ayudas.get(tipo, ""))
        self.lbl_prueba.hide()

    def _probar_en_dialogo(self):
        tipo = self._tipo()
        fuente = self._fuente_actual()
        if tipo == "http" and not fuente:
            self.lbl_prueba.setText("Ingresa primero la URL del stream.")
            self.lbl_prueba.setStyleSheet("color: #fbbf24;")
            self.lbl_prueba.show()
            return
        if self._trabajo_prueba is not None and self._trabajo_prueba.isRunning():
            return
        self.btn_probar_dlg.setEnabled(False)
        self.lbl_prueba.setText("Probando conexión…")
        self.lbl_prueba.setStyleSheet("color: #cbd5e1;")
        self.lbl_prueba.show()
        self._trabajo_prueba = Trabajo(lambda: probar_fuente(tipo, fuente), self)
        self._trabajo_prueba.listo.connect(self._on_prueba_dlg)
        self._trabajo_prueba.start()

    def _on_prueba_dlg(self, ok, mensaje):
        self.btn_probar_dlg.setEnabled(True)
        self.lbl_prueba.setText(mensaje)
        self.lbl_prueba.setStyleSheet("color: #34d399;" if ok else "color: #f87171;")
        self.lbl_prueba.show()

    def _tipo(self):
        return {0: "local", 1: "http", 2: "telefono"}.get(
            self.combo_tipo.currentIndex(), "local")

    def _fuente_actual(self):
        tipo = self._tipo()
        if tipo == "local":
            return self._fuente_webcam_actual()
        if tipo == "telefono":
            return self._telefono_seleccionado()
        return self.edit_fuente.text().strip()

    def validate(self):
        """MessageBoxBase llama a validate() antes de aceptar el cierre."""
        if self._tipo() == "telefono":
            if not self._telefono_seleccionado():
                aviso(self, "Falta el teléfono",
                      "No hay teléfonos conectados. Abre https://<ip>:8443/telefono en el celular.")
                return False
            return True
        if not self._fuente_actual():
            aviso(self, "Falta la fuente",
                  "Indica la fuente de la cámara.")
            return False
        return True

    def datos(self):
        curso = ""
        if self.combo_curso.currentIndex() > 0:
            curso = self.combo_curso.currentText().strip()
        return {
            "id": self._camara.get("id"),
            "nombre": self.edit_nombre.text().strip() or "Cámara",
            "tipo": self._tipo(),
            "fuente": self._fuente_actual(),
            "curso": curso,
            "activa": self.chk_activa.isChecked(),
            "intervalo_proceso": float(self.spin_intervalo.value()),
            "recorte_aspecto": self.combo_aspecto.currentText(),
        }



class PanelConfiguracion(QWidget):
    """Pestaña Configuración separada por secciones (Cámaras, Asistencia, Red)."""

    def __init__(self, callbacks=None, parent=None):
        super().__init__(parent)
        self._cb = callbacks or {}
        # () -> {tel_id: registro} | {} — teléfonos del servidor web
        self._obtener_telefonos = self._cb.get("obtener_telefonos") or (lambda: {})
        self.cfg = config_app.cargar()
        self._trabajo_prueba = None
        self._nombres_webcam = {}
        self._webcams_detectadas = False

        raiz = QVBoxLayout(self)
        raiz.setContentsMargins(28, 20, 28, 24)
        raiz.setSpacing(16)

        # Cabecera principal
        col_tit = QVBoxLayout()
        col_tit.setSpacing(2)
        lbl_tit = SubtitleLabel("Configuración", self)
        lbl_desc = CaptionLabel(
            "Administra las cámaras de vigilancia, los parámetros de reconocimiento y el servidor local.",
            self)
        lbl_desc.setStyleSheet("color: rgba(148, 163, 184, 0.95);")
        col_tit.addWidget(lbl_tit)
        col_tit.addWidget(lbl_desc)
        raiz.addLayout(col_tit)

        # Navegación segmentada por categorías
        self.nav_config = SegmentedWidget(self)
        self.stack_config = QStackedWidget(self)
        self.stack_config.setStyleSheet("background: transparent;")

        self.pag_camaras = self._envolver_scroll([self._construir_grupo_camaras()])
        self.pag_asistencia = self._envolver_scroll([
            self._construir_grupo_asistencia(),
            self._construir_grupo_tardanzas(),
        ])
        self.pag_red = self._envolver_scroll([self._construir_grupo_web()])

        self._paginas = {
            "camaras": self.pag_camaras,
            "asistencia": self.pag_asistencia,
            "red": self.pag_red,
        }
        for widget in self._paginas.values():
            self.stack_config.addWidget(widget)

        self.nav_config.addItem(
            "camaras", "Cámaras",
            lambda: self._cambiar_seccion("camaras"), icon=FIcon.CAMERA)
        self.nav_config.addItem(
            "asistencia", "Asistencia y horarios",
            lambda: self._cambiar_seccion("asistencia"), icon=FIcon.STOP_WATCH)
        self.nav_config.addItem(
            "red", "Red y servidor",
            lambda: self._cambiar_seccion("red"), icon=FIcon.GLOBE)
        self.nav_config.setCurrentItem("camaras")
        self.stack_config.setCurrentWidget(self.pag_camaras)

        raiz.addWidget(self.nav_config, 0, Qt.AlignmentFlag.AlignLeft)
        raiz.addWidget(self.stack_config, 1)

    def _cambiar_seccion(self, clave):
        pag = self._paginas.get(clave)
        if pag is not None:
            self.stack_config.setCurrentWidget(pag)

    def _envolver_scroll(self, widgets):
        """Envuelve una lista de grupos en un ScrollArea transparente."""
        cuerpo = QWidget()
        cuerpo.setStyleSheet("background: transparent;")
        v = QVBoxLayout(cuerpo)
        v.setContentsMargins(0, 4, 6, 12)
        v.setSpacing(20)
        for w in widgets:
            v.addWidget(w)
        v.addStretch(1)

        scroll = ScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        scroll.setWidget(cuerpo)
        scroll.enableTransparentBackground()
        return scroll

    # ---------------- Cámaras ----------------
    def _construir_grupo_camaras(self):
        grupo = HeaderCardWidget()
        grupo.setTitle("Cámaras configuradas")
        grupo.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        v = grupo.viewLayout
        v.setContentsMargins(16, 12, 16, 16)
        v.setSpacing(12)

        desc = CaptionLabel(
            "Configura las webcams locales, cámaras IP/ESP32 o teléfonos usados para vigilancia y captura de fotos.")
        desc.setWordWrap(True)
        desc.setStyleSheet("color: rgba(148, 163, 184, 0.9);")
        v.addWidget(desc)

        self.stack_tabla_cam = QStackedWidget()
        self.stack_tabla_cam.setStyleSheet("background: transparent;")

        # Página 0: estado vacío sin cámaras
        vacio = QWidget()
        vacio.setStyleSheet("background: transparent;")
        vv = QVBoxLayout(vacio)
        vv.setContentsMargins(24, 36, 24, 36)
        vv.setSpacing(8)
        vv.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico_v = IconWidget(FIcon.CAMERA, vacio)
        ico_v.setFixedSize(44, 44)
        vv.addWidget(ico_v, 0, Qt.AlignmentFlag.AlignCenter)
        tit_v = StrongBodyLabel("Sin cámaras configuradas", vacio)
        vv.addWidget(tit_v, 0, Qt.AlignmentFlag.AlignCenter)
        sub_v = CaptionLabel(
            "Agrega una webcam local, un stream HTTP o un teléfono conectado para iniciar la vigilancia.",
            vacio)
        sub_v.setWordWrap(True)
        sub_v.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub_v.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        vv.addWidget(sub_v)
        self.stack_tabla_cam.addWidget(vacio)

        # Página 1: tabla de cámaras
        self.tabla_camaras = TableWidget()
        self.tabla_camaras.setRowCount(0)
        self.tabla_camaras.setColumnCount(5)
        self.tabla_camaras.setHorizontalHeaderLabels(
            ["Nombre", "Tipo", "Fuente", "Curso", "Activa"])
        cab = self.tabla_camaras.horizontalHeader()
        cab.setMinimumHeight(34)
        cab.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tabla_camaras.setColumnWidth(4, 92)
        self.tabla_camaras.verticalHeader().setVisible(False)
        self.tabla_camaras.verticalHeader().setDefaultSectionSize(44)
        self.tabla_camaras.setAlternatingRowColors(True)
        self.tabla_camaras.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla_camaras.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla_camaras.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.stack_tabla_cam.addWidget(self.tabla_camaras)

        v.addWidget(self.stack_tabla_cam)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(10)
        btn_agregar = PrimaryPushButton(FIcon.ADD, "Agregar cámara")
        btn_agregar.clicked.connect(self._agregar_camara)
        self.btn_editar_cam = PushButton(FIcon.EDIT, "Editar")
        self.btn_editar_cam.setObjectName("secundario")
        self.btn_editar_cam.clicked.connect(self._editar_camara)
        self.btn_probar_cam = PushButton(FIcon.PLAY, "Probar conexión")
        self.btn_probar_cam.setObjectName("secundario")
        self.btn_probar_cam.clicked.connect(self._probar_camara)
        self.btn_quitar_cam = PushButton(FIcon.DELETE, "Quitar")
        self.btn_quitar_cam.setObjectName("peligro")
        self.btn_quitar_cam.clicked.connect(self._quitar_camara)
        for b in (btn_agregar, self.btn_editar_cam, self.btn_probar_cam, self.btn_quitar_cam):
            b.setMinimumHeight(34)
            fila_botones.addWidget(b, 1)
        v.addLayout(fila_botones)

        self._refrescar_tabla(detectar=False)
        return grupo

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, "_webcams_detectadas", False):
            self._webcams_detectadas = True
            self._refrescar_tabla(detectar=True)

    def _refrescar_tabla(self, detectar=True):
        camaras = self.cfg.get("camaras", [])
        if detectar:
            self._nombres_webcam = dict(detectar_webcams_locales())
        hay = bool(camaras)
        if hasattr(self, "stack_tabla_cam"):
            self.stack_tabla_cam.setCurrentIndex(1 if hay else 0)
        for btn in (getattr(self, "btn_editar_cam", None),
                    getattr(self, "btn_probar_cam", None),
                    getattr(self, "btn_quitar_cam", None)):
            if btn is not None:
                btn.setEnabled(hay)
        self.tabla_camaras.setRowCount(len(camaras))
        for i, cam in enumerate(camaras):
            valores = [
                cam.get("nombre", ""),
                TIPO_ETIQUETAS.get(cam.get("tipo"), str(cam.get("tipo", ""))),
                self._etiqueta_fuente(cam),
                cam.get("curso", "") or "Sin curso",
            ]
            for col, val in enumerate(valores):
                self.tabla_camaras.setItem(i, col, QTableWidgetItem(str(val)))
            contenedor = QWidget()
            h = QHBoxLayout(contenedor)
            h.setContentsMargins(6, 2, 2, 2)
            sw = SwitchButton(contenedor)
            sw.setOnText("")
            sw.setOffText("")
            sw.setChecked(bool(cam.get("activa")))
            sw.checkedChanged.connect(
                lambda activo, fila=i: self._toggle_camara(fila, activo))
            h.addWidget(sw)
            h.addStretch(1)
            self.tabla_camaras.setCellWidget(i, 4, contenedor)
        if hay:
            altura = min(340, 38 + len(camaras) * 44 + 12)
            self.tabla_camaras.setFixedHeight(altura)
            self.stack_tabla_cam.setFixedHeight(altura)
        else:
            self.stack_tabla_cam.setFixedHeight(190)

    def _toggle_camara(self, fila, activo):
        """Cambia el estado activo de una cámara y aplica el cambio."""
        camaras = self.cfg.get("camaras", [])
        if 0 <= fila < len(camaras):
            camaras[fila]["activa"] = bool(activo)
            self._guardar_y("camaras_cambiadas")

    def _camara_sel(self):
        fila = self.tabla_camaras.currentRow()
        camaras = self.cfg.get("camaras", [])
        if fila < 0 or fila >= len(camaras):
            aviso(self, "Cámaras", "Seleccione una cámara de la lista.")
            return None, fila
        return camaras[fila], fila

    def _guardar_y(self, callback):
        config_app.guardar(self.cfg)
        fn = self._cb.get(callback)
        if fn:
            fn(self.cfg)

    def _telefonos_actuales(self):
        try:
            telefonos = self._obtener_telefonos() or {}
            return dict(telefonos)
        except Exception:
            return {}

    def _etiqueta_fuente(self, cam):
        """Fuente legible para la tabla (resuelve webcam local y teléfono)."""
        tipo = cam.get("tipo")
        if tipo == "local":
            idx = str(cam.get("fuente", "0") or "0").strip()
            nombre_dev = getattr(self, "_nombres_webcam", {}).get(idx)
            return f"{idx} · {nombre_dev}" if nombre_dev else idx
        if tipo == "telefono":
            tel_id = str(cam.get("fuente", "")).strip()
            reg = self._telefonos_actuales().get(tel_id)
            if reg:
                nombre = str(reg.get("nombre", "") or "").strip() or tel_id[:8]
                ip = str(reg.get("ip", "") or "?")
                estado = "en línea" if reg.get("online", False) else "desconectado"
                return f"{nombre} — {ip} ({estado})"
            return f"(teléfono {tel_id[:8]})" if tel_id else "(sin teléfono)"
        return str(cam.get("fuente", ""))

    def _agregar_camara(self):
        dlg = DialogoCamara(None, self.cfg, self,
                            telefonos=self._telefonos_actuales(),
                            obtener_telefonos=self._telefonos_actuales)
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
        dlg = DialogoCamara(self.cfg["camaras"][fila], self.cfg, self,
                            telefonos=self._telefonos_actuales(),
                            obtener_telefonos=self._telefonos_actuales)
        if dlg.exec():
            self.cfg["camaras"][fila] = dlg.datos()
            self._guardar_y("camaras_cambiadas")
            self._refrescar_tabla()

    def _quitar_camara(self):
        _cam, fila = self._camara_sel()
        if _cam is None:
            return
        nombre = str(self.cfg["camaras"][fila].get("nombre", ""))
        if not pregunta(self, "Quitar cámara",
                        f"¿Quitar la cámara '{nombre}' de la configuración?",
                        yes="Quitar"):
            return
        del self.cfg["camaras"][fila]
        self._guardar_y("camaras_cambiadas")
        self._refrescar_tabla()

    def _probar_camara(self):
        cam, _fila = self._camara_sel()
        if cam is None:
            return
        if cam.get("tipo") == "telefono":
            self._probar_telefono(cam)
            return
        if self._trabajo_prueba is not None and self._trabajo_prueba.isRunning():
            return
        self._trabajo_prueba = Trabajo(
            lambda: probar_fuente(cam.get("tipo"), cam.get("fuente", "")), self)
        self._trabajo_prueba.listo.connect(self._on_prueba_lista)
        self._trabajo_prueba.start()

    def _probar_telefono(self, cam):
        """Prueba de una cámara de tipo teléfono (sin hilo: solo registry)."""
        tel_id = str(cam.get("fuente", "")).strip()
        reg = self._telefonos_actuales().get(tel_id)
        if reg is None:
            aviso(self, "Prueba de cámara",
                  "El teléfono no está conectado (o el servidor web está "
                  "detenido). Abre https://<ip>:8443/telefono en el celular.")
            return
        if not reg.get("online", False):
            hace = max(0, round(time.time() - reg.get("visto", time.time())))
            aviso(self, "Prueba de cámara",
                  f"Teléfono registrado pero SIN señal: visto hace {hace} s. "
                  "Revisa la página /telefono en el celular.")
            return
        camara = "trasera" if reg.get("camara") == "environment" else (
            "frontal" if reg.get("camara") == "user" else "desconocida")
        aviso(self, "Prueba de cámara",
              f"Teléfono en línea — ip {reg.get('ip', '?')} · "
              f"cámara {camara}. La imagen llegará al activar la vigilancia.")

    def _on_prueba_lista(self, ok, mensaje):
        aviso(self, "Prueba de cámara", mensaje)

    # ---------------- Asistencia ----------------
    def _construir_grupo_asistencia(self):
        """Parámetros como tarjetas de Ajustes de Windows 11."""
        asist = self.cfg.get("asistencia", {})

        self.spin_umbral = DoubleSpinBox()
        self.spin_umbral.setRange(0.10, 0.99)
        self.spin_umbral.setSingleStep(0.05)
        self.spin_umbral.setValue(float(asist.get("umbral_similitud", 0.70)))

        self.spin_confirm = DoubleSpinBox()
        self.spin_confirm.setRange(1.0, 10.0)
        self.spin_confirm.setSingleStep(0.5)
        self.spin_confirm.setSuffix(" s")
        self.spin_confirm.setValue(float(asist.get("segundos_confirmacion", 3.0)))

        self.spin_antidup = SpinBox()
        self.spin_antidup.setRange(5, 600)
        self.spin_antidup.setSuffix(" s")
        self.spin_antidup.setValue(int(asist.get("intervalo_antiduplicado", 30)))

        self.combo_modo = ComboBox()
        self.combo_modo.addItems(["Uno (solo el rostro más grande)", "Varios (todos los rostros)"])
        modo_actual = asist.get("modo_escaneo", "uno")
        self.combo_modo.setCurrentIndex(0 if modo_actual == "uno" else 1)

        self.spin_procesar = SpinBox()
        self.spin_procesar.setRange(1, 10)
        self.spin_procesar.setSuffix(" frames")
        self.spin_procesar.setValue(int(asist.get("procesar_cada_n", 1)))

        grupo = SettingCardGroup("Asistencia — parámetros de reconocimiento")
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.FILTER, "Umbral de similitud", "Rango permitido: 0.10 – 0.99", self.spin_umbral))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.STOP_WATCH, "Segundos de confirmación",
            "Tiempo seguido para confirmar un rostro", self.spin_confirm))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.DATE_TIME, "Intervalo anti-duplicado",
            "Mínimo entre marcas del mismo alumno", self.spin_antidup))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.SYNC, "Modo de escaneo", "Uno = prioriza el rostro de mayor tamaño",
            self.combo_modo, ancho=280))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.SYNC, "Procesar 1 de cada N frames",
            "Balance entre fluidez y reconocimiento", self.spin_procesar, ancho=170))

        self.btn_guardar = PrimaryPushSettingCard(
            "Guardar parámetros", FIcon.SAVE, "Aplicar cambios",
            "Se aplican a las cámaras en marcha")
        self.btn_guardar.clicked.connect(self._guardar_parametros)
        grupo.addSettingCard(self.btn_guardar)
        return grupo

    @staticmethod
    def _tarjeta_control(icono, titulo, contenido, control, ancho=140):
        """SettingCard con un control (spin/combo) a la derecha."""
        tarjeta = SettingCard(icono, titulo, contenido)
        control.setFixedWidth(ancho)
        tarjeta.hBoxLayout.addWidget(control, 0, Qt.AlignmentFlag.AlignRight)
        tarjeta.hBoxLayout.addSpacing(16)
        return tarjeta

    def _guardar_parametros(self):
        prev = self.cfg.get("asistencia", {})
        self.cfg["asistencia"] = {
            "umbral_similitud": float(self.spin_umbral.value()),
            "segundos_confirmacion": float(self.spin_confirm.value()),
            "intervalo_antiduplicado": int(self.spin_antidup.value()),
            "umbral_lcd_nombre": float(prev.get("umbral_lcd_nombre", 0.30)),
            "duracion_resultado_lcd": float(prev.get("duracion_resultado_lcd", 3.0)),
            "modo_escaneo": "uno" if self.combo_modo.currentIndex() == 0 else "varios",
            "procesar_cada_n": int(self.spin_procesar.value()),
            "tolerancia_tardanza_min": int(prev.get("tolerancia_tardanza_min", 5)),
            "aplicar_horarios": bool(prev.get("aplicar_horarios", False)),
        }
        self._guardar_y("parametros_cambiados")

    # ---------------- Horarios de clase ----------------
    def _construir_grupo_tardanzas(self):
        """Control de horarios al marcado (la tolerancia de tardanza se configura por curso/sección)."""
        grupo = SettingCardGroup("Horarios y tardanzas")

        self.sw_aplicar = SwitchSettingCard(
            FIcon.CALENDAR, "Aplicar horarios al marcado",
            "Solo registra asistencias dentro de la franja horaria del curso")
        self.sw_aplicar.setChecked(bool(self.cfg.get("asistencia", {}).get(
            "aplicar_horarios", False)))
        self.sw_aplicar.checkedChanged.connect(self._on_toggle_aplicar)
        grupo.addSettingCard(self.sw_aplicar)

        nota_tol = SettingCard(
            FIcon.STOP_WATCH, "Tolerancia de tardanza",
            "Se configura de forma independiente por curso (y opcionalmente por sección) en «Cursos y horarios».")
        grupo.addSettingCard(nota_tol)
        return grupo

    def _on_toggle_aplicar(self, checked):
        asist = self.cfg.setdefault("asistencia", {})
        asist["aplicar_horarios"] = bool(checked)
        self._guardar_y("parametros_cambiados")

    # ---------------- Red / Servidor web ----------------
    def _construir_grupo_web(self):
        """Switch para activar/desactivar el servidor web + estado actual."""
        grupo = SettingCardGroup("Red / Servidor web")

        self.sw_web = SwitchSettingCard(
            FIcon.GLOBE, "Servidor web",
            "Expone el visor y el teléfono en la red local (https://<ip>:8443)")
        self.sw_web.setChecked(bool(self.cfg.get("web", {}).get("habilitar", True)))
        self.sw_web.checkedChanged.connect(self._on_toggle_web)
        grupo.addSettingCard(self.sw_web)

        # Tarjeta de solo lectura con el estado real (la rellena la ventana)
        self.tarjeta_estado_web = SettingCard(
            FIcon.LINK, "Estado del servidor web", "—")
        grupo.addSettingCard(self.tarjeta_estado_web)
        return grupo

    def _on_toggle_web(self, checked):
        self.cfg["web"] = {"habilitar": bool(checked)}
        self._guardar_y("web_cambiado")

    def actualizar_estado_web(self, activo, url=""):
        """Rellena la tarjeta Estado con el estado real del servidor."""
        tarjeta = getattr(self, "tarjeta_estado_web", None)
        if tarjeta is None:
            return
        if activo:
            tarjeta.setContent(url or "https://centinela.local:8443")
        else:
            tarjeta.setContent("detenido — sin acceso por red")



