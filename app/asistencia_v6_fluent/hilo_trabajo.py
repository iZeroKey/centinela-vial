#!/usr/bin/env python3
"""
Centinela Control — Hilo genérico para trabajos en segundo plano (v6).

Ejecuta una función (firma: () -> (ok, mensaje)) en un QThread y emite
`listo(bool, str)`. Lo usan el guardado de alumnos, la recarga de
embeddings y la prueba de cámaras, para no congelar la UI.
"""

from PyQt6.QtCore import QThread, pyqtSignal


class Trabajo(QThread):
    listo = pyqtSignal(bool, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self):
        try:
            resultado = self._fn()
            if isinstance(resultado, tuple):
                ok, msg = resultado
            else:
                ok, msg = bool(resultado), ""
        except Exception as e:
            ok, msg = False, f"Error inesperado: {e}"
        self.listo.emit(bool(ok), str(msg))
