#!/usr/bin/env python3
"""
Centinela Control — Diálogos Fluent (v6)

Avisos y preguntas estandarizados sobre qfluentwidgets.MessageBox,
equivalentes a QMessageBox.warning/information/question pero con el
estilo de los cuadros de diálogo de Windows 11.
"""

import os, sys

from qfluentwidgets import MessageBox

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _ventana(parent):
    """Devuelve la ventana contenedora para que la máscara cubra todo."""
    if parent is None:
        return None
    try:
        return parent.window()
    except RuntimeError:
        return parent


def aviso(parent, titulo, texto, yes="Aceptar"):
    """Cuadro informativo/de aviso con un único botón."""
    box = MessageBox(titulo, texto, _ventana(parent))
    box.yesButton.setText(yes)
    box.hideCancelButton()
    box.exec()


def pregunta(parent, titulo, texto, yes="Sí", cancel="Cancelar"):
    """Cuadro de confirmación. Devuelve True si se pulsa el botón principal."""
    box = MessageBox(titulo, texto, _ventana(parent))
    box.yesButton.setText(yes)
    box.cancelButton.setText(cancel)
    return bool(box.exec())
