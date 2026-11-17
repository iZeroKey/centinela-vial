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

import os, sys

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QBoxLayout, QFrame,
)
from qfluentwidgets import (
    ComboBox, PushButton, LineEdit, DoubleSpinBox, SpinBox,
    TableWidget, HeaderCardWidget, MessageBoxBase, SubtitleLabel, CaptionLabel,
    SwitchButton, SwitchSettingCard, SettingCardGroup, SettingCard,
    PrimaryPushSettingCard, ScrollArea, CardWidget, TransparentToolButton,
    FluentIcon as FIcon,
)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dialogos_fluent import aviso, pregunta
from hilo_trabajo import Trabajo
from camara_hilo import probar_fuente
import config as config_app

TIPO_ETIQUETAS = {"local": "Webcam local", "http": "Stream HTTP (ESP32)"}


class DialogoCamara(MessageBoxBase):
    """Alta/edición de una cámara (diálogo Fluent)."""

    def __init__(self, camara=None, cfg=None, parent=None):
        super().__init__(parent=parent)
        self._camara = camara or {}

        self.widget.setMinimumWidth(500)

        self.titleLabel = SubtitleLabel(
            "Nueva cámara" if camara is None else "Editar cámara")
        self.viewLayout.addWidget(self.titleLabel)

        self.edit_nombre = LineEdit()
        self.edit_nombre.setText(self._camara.get("nombre", "") or "")
        self.combo_tipo = ComboBox()
        self.combo_tipo.addItems([TIPO_ETIQUETAS["local"], TIPO_ETIQUETAS["http"]])
        self.combo_tipo.setCurrentIndex(0 if self._camara.get("tipo") == "local" else 1)
        self.combo_tipo.currentIndexChanged.connect(self._actualizar_placeholder)

        self.edit_fuente = LineEdit()
        self.edit_fuente.setText(str(self._camara.get("fuente", "") or ""))
        self._actualizar_placeholder()

        # Curso cuyo asistencia registra la cámara (opcional, individual
        # por cámara; se muestra en la pestaña Vigilancia).
        self.combo_curso = ComboBox()
        self.combo_curso.addItem("(sin curso)")
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

        def fila(etiqueta, widget):
            h = QHBoxLayout()
            l = CaptionLabel(etiqueta)
            l.setFixedWidth(110)
            h.addWidget(l)
            h.addWidget(widget, 1)
            return h

        self.viewLayout.addLayout(fila("Nombre:", self.edit_nombre))
        self.viewLayout.addLayout(fila("Tipo:", self.combo_tipo))
        self.viewLayout.addLayout(fila("Fuente:", self.edit_fuente))
        self.viewLayout.addLayout(fila("Curso:", self.combo_curso))
        self.viewLayout.addLayout(fila("Intervalo:", self.spin_intervalo))

        fila_activa = QHBoxLayout()
        lbl_activa = CaptionLabel("Cámara activa (aparece en Vigilancia)")
        fila_activa.addWidget(self.chk_activa)
        fila_activa.addWidget(lbl_activa)
        fila_activa.addStretch(1)
        self.viewLayout.addLayout(fila_activa)

        ayuda = CaptionLabel(
            "Webcam local usa el índice del dispositivo (0, 1, ...).\n"
            "Stream HTTP es la URL MJPEG de la ESP32 (http://<ip>:81/stream).\n"
            "El curso es informativo e individual por cámara: se muestra en "
            "Vigilancia para saber a qué clase está marcando.")
        ayuda.setWordWrap(True)
        self.viewLayout.addWidget(ayuda)

        self.yesButton.setText("Guardar")
        self.cancelButton.setText("Cancelar")

    def _actualizar_placeholder(self, *_):
        if self._tipo() == "local":
            self.edit_fuente.setPlaceholderText("Índice de webcam (0, 1, ...)")
        else:
            self.edit_fuente.setPlaceholderText("http://<ip>:81/stream")

    def _tipo(self):
        return "local" if self.combo_tipo.currentIndex() == 0 else "http"

    def validate(self):
        """MessageBoxBase llama a validate() antes de aceptar el cierre."""
        if not self.edit_fuente.text().strip():
            aviso(self, "Falta la fuente",
                  "Indica la fuente de la cámara (índice o URL).")
            return False
        return True

    def datos(self):
        curso = ""
        if self.combo_curso.currentIndex() > 0:
            curso = self.combo_curso.currentText().strip()
        return {
            "id": self._camara.get("id"),   # None en alta; el panel lo asigna
            "nombre": self.edit_nombre.text().strip() or "Cámara",
            "tipo": self._tipo(),
            "fuente": self.edit_fuente.text().strip(),
            "curso": curso,
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

        # El panel crece con las secciones: scroll transparente para no cortar
        cuerpo = QWidget()
        cuerpo.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(cuerpo)
        layout.setContentsMargins(36, 24, 36, 24)  # margenes tipo Ajustes de Win11
        layout.setSpacing(24)

        layout.addWidget(self._construir_grupo_camaras())
        layout.addWidget(self._construir_grupo_asistencia())
        layout.addWidget(self._construir_grupo_tardanzas())
        layout.addWidget(self._construir_grupo_web())
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

    # ---------------- Cámaras ----------------
    def _construir_grupo_camaras(self):
        grupo = HeaderCardWidget()
        grupo.setTitle("Cámaras (para vigilancia y captura de fotos)")
        grupo.viewLayout.setDirection(QBoxLayout.Direction.TopToBottom)
        v = grupo.viewLayout
        v.setSpacing(10)

        self.tabla_camaras = TableWidget()
        self.tabla_camaras.setRowCount(0)
        self.tabla_camaras.setColumnCount(5)
        self.tabla_camaras.setHorizontalHeaderLabels(
            ["Nombre", "Tipo", "Fuente", "Curso", "Activa"])
        cab = self.tabla_camaras.horizontalHeader()
        cab.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        cab.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        cab.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tabla_camaras.setColumnWidth(4, 92)
        self.tabla_camaras.verticalHeader().setVisible(False)
        self.tabla_camaras.setAlternatingRowColors(True)
        self.tabla_camaras.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tabla_camaras.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tabla_camaras.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tabla_camaras.setMinimumHeight(140)
        v.addWidget(self.tabla_camaras)

        fila_botones = QHBoxLayout()
        fila_botones.setSpacing(10)
        btn_agregar = PushButton("Agregar")
        btn_agregar.clicked.connect(self._agregar_camara)
        btn_editar = PushButton("Editar")
        btn_editar.setObjectName("secundario")
        btn_editar.clicked.connect(self._editar_camara)
        btn_probar = PushButton("Probar conexión")
        btn_probar.setObjectName("secundario")
        btn_probar.clicked.connect(self._probar_camara)
        btn_quitar = PushButton("Quitar")
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
                cam.get("curso", "") or "(sin curso)",
            ]
            for col, val in enumerate(valores):
                self.tabla_camaras.setItem(i, col, QTableWidgetItem(str(val)))
            # Switch Fluent para activar/desactivar sin abrir el dialogo.
            # La senal se conecta DESPUES de fijar el estado, porque
            # setChecked() emite checkedChanged y evitamos falsos cambios.
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

    def _agregar_camara(self):
        dlg = DialogoCamara(None, self.cfg, self)
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
        dlg = DialogoCamara(self.cfg["camaras"][fila], self.cfg, self)
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
        if self._trabajo_prueba is not None and self._trabajo_prueba.isRunning():
            return
        self._trabajo_prueba = Trabajo(
            lambda: probar_fuente(cam.get("tipo"), cam.get("fuente", "")), self)
        self._trabajo_prueba.listo.connect(self._on_prueba_lista)
        self._trabajo_prueba.start()

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

        grupo = SettingCardGroup("Asistencia — parámetros de reconocimiento")
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.FILTER, "Umbral de similitud", "0.10 – 0.99", self.spin_umbral))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.STOP_WATCH, "Segundos de confirmación",
            "tiempo seguido para confirmar", self.spin_confirm))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.DATE_TIME, "Intervalo anti-duplicado",
            "mínimo entre marcas del mismo alumno", self.spin_antidup))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.SYNC, "Modo de escaneo", "uno = prioriza el rostro mayor",
            self.combo_modo, ancho=280))

        self.btn_guardar = PrimaryPushSettingCard(
            "Guardar parámetros", FIcon.SAVE, "Aplicar cambios",
            "se aplican a las cámaras en marcha")
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
        self.cfg["asistencia"] = {
            "umbral_similitud": float(self.spin_umbral.value()),
            "segundos_confirmacion": float(self.spin_confirm.value()),
            "intervalo_antiduplicado": int(self.spin_antidup.value()),
            "umbral_lcd_nombre": float(self.cfg.get("asistencia", {}).get("umbral_lcd_nombre", 0.30)),
            "duracion_resultado_lcd": float(self.cfg.get("asistencia", {}).get("duracion_resultado_lcd", 3.0)),
            "modo_escaneo": "uno" if self.combo_modo.currentIndex() == 0 else "varios",
            "tolerancia_tardanza_min": int(self.spin_tolerancia.value()),
            "aplicar_horarios": bool(self.cfg.get("asistencia", {}).get("aplicar_horarios", False)),
        }
        self._guardar_y("parametros_cambiados")

    # ---------------- Tardanzas ----------------
    def _construir_grupo_tardanzas(self):
        """Tolerancia de tardanza y aplicacion de horarios al marcado."""
        grupo = SettingCardGroup("Tardanzas")

        self.spin_tolerancia = SpinBox()
        self.spin_tolerancia.setRange(0, 60)
        self.spin_tolerancia.setSuffix(" min")
        self.spin_tolerancia.setValue(int(self.cfg.get("asistencia", {}).get(
            "tolerancia_tardanza_min", 5)))
        grupo.addSettingCard(self._tarjeta_control(
            FIcon.STOP_WATCH, "Tolerancia de tardanza",
            "minutos de gracia tras el inicio de clase", self.spin_tolerancia,
            ancho=120))

        # Switch de aplicacion al marcado (4B): instantaneo, como los de camaras.
        # La senal se conecta DESPUES de fijar el estado (setChecked emite).
        self.sw_aplicar = SwitchSettingCard(
            FIcon.CALENDAR, "Aplicar horarios al marcado",
            "solo registra asistencias dentro de la franja del curso")
        self.sw_aplicar.setChecked(bool(self.cfg.get("asistencia", {}).get(
            "aplicar_horarios", False)))
        self.sw_aplicar.checkedChanged.connect(self._on_toggle_aplicar)
        grupo.addSettingCard(self.sw_aplicar)
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
            "expone el visor y el telefono en la red local (https://<ip>:8443)")
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



