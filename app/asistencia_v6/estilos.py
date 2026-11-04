#!/usr/bin/env python3
"""
Centinela Control — Estilos compartidos de la interfaz (v6).
"""

ESTILO = """
QMainWindow { background: #0a0a0a; }
QDialog { background: #0a0a0a; }
QLabel { color: #ededed; font-size: 14px; }
QLabel#estado_on { color: #5fd08b; font-weight: bold; }
QLabel#estado_off { color: #8a8a8a; font-weight: bold; }
QLabel#chip { color: #ededed; background: #1f1f1f; border-radius: 12px;
              padding: 3px 12px; font-size: 12px; font-weight: bold; }
QLabel#chip_presente { background: #163024; color: #5fd08b; }
QLabel#chip_ausente { background: #1f1f1f; color: #9a9a9a; }
QLabel#chip_verificando { background: #1f1f1f; color: #ededed; }
QLabel#chip_confirmado { background: #163024; color: #5fd08b; }
QGroupBox { color: #ededed; border: 1px solid #262626; border-radius: 12px;
            margin-top: 20px; font-weight: bold; font-size: 12px;
            background: #121212; }
QGroupBox::title { subcontrol-origin: margin; subcontrol-position: top left;
                   left: 16px; padding: 2px 10px; background: #0a0a0a;
                   color: #a3a3a3; }
QPushButton { background: #f2f2f2; color: #0a0a0a; border: none; border-radius: 9px;
              padding: 9px 16px; font-size: 14px; font-weight: bold; }
QPushButton:hover { background: #ffffff; }
QPushButton:pressed { background: #d9d9d9; }
QPushButton#peligro { background: #161616; color: #e58a8a; border: 1px solid #5a3030; }
QPushButton#peligro:hover { border-color: #c94646; color: #f0a3a3; background: #1f1414; }
QPushButton#exito { background: #f2f2f2; color: #0a0a0a; }
QPushButton#exito:hover { background: #ffffff; }
QPushButton#secundario { background: transparent; color: #d4d4d4; border: 1px solid #3a3a3a; }
QPushButton#secundario:hover { border-color: #f2f2f2; color: #ffffff; background: #1a1a1a; }
QPushButton:disabled { background: #1f1f1f; color: #5a5a5a; border: none; }
QPushButton#secundario:disabled { background: transparent; border-color: #1f1f1f; }
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit, QDateEdit {
             background: #1a1a1a; color: #ededed;
             border: 1px solid #333333; border-radius: 8px; padding: 7px 12px;
             font-size: 14px; min-height: 20px; }
QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus, QDateEdit:focus {
             border-color: #f2f2f2; }
QComboBox::drop-down { border: none; width: 22px; }
QCheckBox { color: #ededed; font-size: 14px; }
QCheckBox:disabled { color: #5a5a5a; }
QTableWidget { background: #121212; color: #ededed; gridline-color: #1f1f1f;
               border: 1px solid #262626; border-radius: 8px;
               alternate-background-color: #171717; }
QTableWidget::item { padding: 8px; border-bottom: 1px solid #1f1f1f; }
QTableWidget::item:selected { background: #f2f2f2; color: #0a0a0a; }
QHeaderView::section { background: #1a1a1a; color: #9a9a9a; padding: 10px; border: none;
                       border-bottom: 1px solid #333333; font-weight: bold; font-size: 12px; }
QListWidget { background: #121212; color: #ededed; border: 1px solid #262626;
              border-radius: 8px; }
QListWidget::item { padding: 8px; border-bottom: 1px solid #1f1f1f; }
QListWidget::item:selected { background: #f2f2f2; color: #0a0a0a; }
QTabWidget::pane { border: 1px solid #262626; border-radius: 10px;
                   background: #121212; margin-top: 10px; }
QTabBar::tab { background: transparent; color: #9a9a9a; padding: 10px 22px;
               font-size: 14px; font-weight: bold;
               border: none; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #f2f2f2; border-bottom: 2px solid #f2f2f2; }
QTabBar::tab:hover:!selected { color: #ededed; }
QProgressBar { border: none; border-radius: 7px; text-align: center;
               background: #1f1f1f; color: #ededed; height: 15px; }
QProgressBar::chunk { background: #e6e6e6; border-radius: 6px; }
QStatusBar { color: #9a9a9a; background: #0a0a0a; border-top: 1px solid #262626; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px; }
QScrollBar::handle:vertical { background: #333333; border-radius: 5px; min-height: 30px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QFrame#card { background: #121212; border: 1px solid #262626; border-radius: 10px; }
"""
