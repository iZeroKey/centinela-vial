#!/usr/bin/env python3
"""
Centinela Control — Panel de Dispositivos

Lista los teléfonos conectados al servidor web (cámara + pantalla) y deja
controlar cada uno desde el PC, para fijar un dispositivo en el aula:

  - Libre: el alumno puede elegir cámara y ver su nombre.
  - Ocultar controles: solo el video del escaneo (sin botones).
  - Bloquear: video a pantalla completa, sin interacción.

La política la aplica la PÁGINA del teléfono (poll cada 1 s). Límite
documentado del modo kiosko web: el navegador siempre puede cerrar la
pestaña; para un dispositivo fijo definitivo, usar "Pantalla fija" de
Android (sin código).
"""

import os, sys, time

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFrame,
)
from qfluentwidgets import (
    CardWidget, SubtitleLabel, BodyLabel, CaptionLabel, StrongBodyLabel,
    PushButton, PrimaryPushButton, SmoothScrollArea, InfoBadge,
    IconWidget, FluentIcon as FIcon,
)

from centinela.red.servidor_web import ip_local


class TarjetaTelefono(CardWidget):
    """Tarjeta de un teléfono: identidad, last-seen y botones de política."""

    def __init__(self, tel_id, padre=None):
        super().__init__(padre)
        self.tel_id = tel_id
        self.padre = padre
        self.setMinimumHeight(88)

        h = QHBoxLayout(self)
        h.setContentsMargins(16, 14, 16, 14)
        h.setSpacing(12)

        col = QVBoxLayout()
        col.setSpacing(4)
        self.lbl_nombre = StrongBodyLabel("—", self)
        self.lbl_detalle = CaptionLabel("", self)
        col.addWidget(self.lbl_nombre)
        col.addWidget(self.lbl_detalle)
        h.addLayout(col, 1)

        self.badge = InfoBadge.custom("libre", "#12261f", "#12261f", parent=self)
        h.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)

        self.btn_libre = PushButton("Liberar", self)
        self.btn_ocultar = PushButton("Ocultar controles", self)
        self.btn_bloquear = PrimaryPushButton("Bloquear", self)
        for b in (self.btn_libre, self.btn_ocultar, self.btn_bloquear):
            b.setMinimumHeight(32)
            h.addWidget(b)

        self.btn_libre.clicked.connect(lambda: self.padre.cambiar_estado(
            self.tel_id, "libre"))
        self.btn_ocultar.clicked.connect(lambda: self.padre.cambiar_estado(
            self.tel_id, "controles_ocultos"))
        self.btn_bloquear.clicked.connect(lambda: self.padre.cambiar_estado(
            self.tel_id, "bloqueado"))

    def actualizar(self, reg, ahora):
        """Pinta el registro del teléfono (dict de servidor_web)."""
        nombre = reg.get("nombre") or f"Teléfono {self.tel_id[:8]}"
        self.lbl_nombre.setText(nombre)
        camara = "trasera" if reg.get("camara") == "environment" else \
            ("frontal" if reg.get("camara") == "user" else "—")
        hace = max(0, round(ahora - reg.get("visto", ahora)))
        online = reg.get("online", False)
        self.lbl_detalle.setText(
            f"{reg.get('ip', '—')} · cámara {camara} · "
            f"{'visto hace ' + str(hace) + ' s' if online else 'DESCONECTADO'}")
        self.lbl_detalle.setStyleSheet(
            "color: #34d399;" if online else "color: #f87171;")

        estado = reg.get("estado", "libre")
        texto, bg, fg = {
            "libre": ("Libre", "#12261f", "#34d399"),
            "controles_ocultos": ("Controles ocultos", "#2a2414", "#fbbf24"),
            "bloqueado": ("Bloqueado", "#2a1414", "#f87171"),
        }.get(estado, (estado, "#18202b", "#cbd5e1"))
        self.badge.setText(texto)
        self.badge.setCustomBackgroundColor(bg, bg)
        self.badge.setStyleSheet(
            f"color: {fg}; font-weight: 600; padding: 3px 10px; border-radius: 999px;"
        )


class PanelDispositivos(QWidget):
    """Pestaña de gestión de teléfonos conectados al servidor web."""

    refresco_seg = 1.0

    def __init__(self, obtener_servidor, parent=None):
        """obtener_servidor: () -> ServidorWeb | None (inyección de la ventana)."""
        super().__init__(parent)
        self.obtener_servidor = obtener_servidor
        self._tarjetas = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(28, 20, 28, 24)
        v.setSpacing(14)

        cab = QHBoxLayout()
        self.lbl_titulo = SubtitleLabel("Dispositivos", self)
        self.lbl_contador = CaptionLabel("", self)
        self.lbl_contador.setStyleSheet("color: rgba(139, 151, 168, 0.95);")
        cab.addWidget(self.lbl_titulo)
        cab.addSpacing(8)
        cab.addWidget(self.lbl_contador)
        cab.addStretch(1)
        btn_refrescar = PushButton("Actualizar", self)
        btn_refrescar.setObjectName("secundario")
        btn_refrescar.setMinimumHeight(32)
        btn_refrescar.clicked.connect(self.refrescar)
        cab.addWidget(btn_refrescar)
        v.addLayout(cab)

        url_telefono = f"https://{ip_local()}:8443/telefono"
        self.lbl_ayuda = CaptionLabel(
            f"Abre {url_telefono} en el celular para usarlo como cámara del aula "
            "y controlar sus botones desde aquí.", self)
        self.lbl_ayuda.setWordWrap(True)
        self.lbl_ayuda.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self.lbl_ayuda)

        scroll = SmoothScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.contenedor = QWidget()
        self.contenedor.setStyleSheet("background: transparent;")
        self.v_tarjetas = QVBoxLayout(self.contenedor)
        self.v_tarjetas.setContentsMargins(0, 4, 0, 4)
        self.v_tarjetas.setSpacing(12)

        self.lbl_vacio = QWidget(self.contenedor)
        self.lbl_vacio.setStyleSheet("background: transparent;")
        v_vacio = QVBoxLayout(self.lbl_vacio)
        v_vacio.setContentsMargins(24, 56, 24, 56)
        v_vacio.setSpacing(10)
        v_vacio.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ico_vacio = IconWidget(FIcon.PHONE, self.lbl_vacio)
        ico_vacio.setFixedSize(48, 48)
        v_vacio.addWidget(ico_vacio, 0, Qt.AlignmentFlag.AlignCenter)
        tit_vacio = SubtitleLabel("Sin teléfonos conectados", self.lbl_vacio)
        v_vacio.addWidget(tit_vacio, 0, Qt.AlignmentFlag.AlignCenter)
        sub_vacio = CaptionLabel(
            f"Abre {url_telefono} en el navegador del celular para vincularlo.",
            self.lbl_vacio)
        sub_vacio.setStyleSheet("color: rgba(139, 151, 168, 0.9);")
        v_vacio.addWidget(sub_vacio, 0, Qt.AlignmentFlag.AlignCenter)

        self.v_tarjetas.addWidget(self.lbl_vacio)
        self.v_tarjetas.addStretch(1)
        scroll.setWidget(self.contenedor)
        scroll.enableTransparentBackground()
        v.addWidget(scroll, 1)

        self._temporizador = QTimer(self)
        self._temporizador.setInterval(int(self.refresco_seg * 1000))
        self._temporizador.timeout.connect(self.refrescar)

    def showEvent(self, event):
        super().showEvent(event)
        self.refrescar()
        if not self._temporizador.isActive():
            self._temporizador.start()

    def hideEvent(self, event):
        self._temporizador.stop()
        super().hideEvent(event)

    # ------------------------------------------------------------------
    def cambiar_estado(self, tel_id, estado):
        servidor = self.obtener_servidor()
        if servidor is not None:
            servidor.fijar_estado(tel_id, estado)
        self.refrescar()

    def refrescar(self):
        servidor = self.obtener_servidor()
        if servidor is None:
            self.lbl_contador.setText("Servidor web detenido")
            return
        ahora = time.time()
        telefonos = servidor.listar_telefonos()

        # tarjetas nuevas / removidas
        for tel in list(self._tarjetas):
            if tel not in telefonos:
                tarjeta = self._tarjetas.pop(tel)
                tarjeta.setParent(None)
                tarjeta.deleteLater()
        for tel, reg in telefonos.items():
            if tel not in self._tarjetas:
                tarjeta = TarjetaTelefono(tel, self)
                self._tarjetas[tel] = tarjeta
                self.v_tarjetas.insertWidget(
                    self.v_tarjetas.count() - 1, tarjeta)

        for tel, tarjeta in self._tarjetas.items():
            tarjeta.actualizar(telefonos.get(tel, {}), ahora)

        vivos = sum(1 for r in telefonos.values() if r.get("online"))
        self.lbl_contador.setText(
            f"{len(telefonos)} teléfono(s) · {vivos} en línea"
            if telefonos else "Sin teléfonos conectados")
        self.lbl_vacio.setVisible(not telefonos)
