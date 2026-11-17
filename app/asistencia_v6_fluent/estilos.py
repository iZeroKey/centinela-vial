#!/usr/bin/env python3
"""
Centinela Control - Estilos compartidos de la interfaz (v6).
Tema: centro de control nocturno, acento teal, superficies en capas.
"""

ESTILO = """
/* ---------- Base ---------- */
* { font-family: "Segoe UI Variable", "Segoe UI", "Inter", sans-serif; }

QMainWindow, QDialog { background: #0b0f14; color: #e6edf3; }
QWidget { color: #e6edf3; font-size: 13px; }

QLabel { color: #e6edf3; background: transparent; border: none; }
QLabel#estado_on { color: #34d399; font-weight: 600; }
QLabel#estado_off { color: #8b97a8; font-weight: 600; }
QLabel#chip { color: #e6edf3; background: #18202b; border: 1px solid #232d3a;
              border-radius: 999px; padding: 3px 10px; font-size: 11px;
              font-weight: 600; }
QLabel#chip_presente { background: #12261f; color: #34d399; border-color: #1f4a3a; }
QLabel#chip_ausente { background: #18202b; color: #8b97a8; }
QLabel#chip_verificando { background: #18202b; color: #e6edf3; }
QLabel#chip_confirmado { background: #12261f; color: #34d399; }

/* ---------- Contenedores ---------- */
QGroupBox {
    color: #e6edf3;
    border: 1px solid #232d3a;
    border-radius: 12px;
    margin-top: 18px;
    font-weight: 600;
    font-size: 12px;
    background: #121821;
    padding: 14px 12px 12px 12px;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 14px;
    top: -1px;
    padding: 2px 10px;
    background: #0b0f14;
    color: #8b97a8;
    border: 1px solid #232d3a;
    border-radius: 6px;
    font-weight: 600;
}

QFrame#card {
    background: #121821;
    border: 1px solid #232d3a;
    border-radius: 12px;
}

/* ---------- Botones ---------- */
QPushButton {
    background: #e6edf3;
    color: #0b0f14;
    border: none;
    border-radius: 9px;
    padding: 9px 16px;
    font-size: 13px;
    font-weight: 600;
}
QPushButton:hover { background: #ffffff; }
QPushButton:pressed { background: #cbd5e1; }
QPushButton:focus { outline: none; border: 2px solid #2dd4bf; }

QPushButton#peligro {
    background: #1a1214;
    color: #fca5a5;
    border: 1px solid #5a2a2a;
}
QPushButton#peligro:hover { border-color: #ef4444; color: #fecaca; background: #221416; }

QPushButton#exito {
    background: #2dd4bf;
    color: #04201d;
}
QPushButton#exito:hover { background: #5eead4; }

QPushButton#secundario {
    background: transparent;
    color: #cbd5e1;
    border: 1px solid #334155;
}
QPushButton#secundario:hover { border-color: #2dd4bf; color: #e6edf3; background: #18202b; }

QPushButton:disabled {
    background: #18202b;
    color: #5a6573;
    border: 1px solid #232d3a;
}
QPushButton#secundario:disabled { background: transparent; border-color: #232d3a; color: #5a6573; }

/* ---------- Campos ---------- */
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit, QDateEdit, QTextEdit {
    background: #0f141b;
    color: #e6edf3;
    border: 1px solid #2a3441;
    border-radius: 8px;
    padding: 7px 12px;
    font-size: 13px;
    min-height: 20px;
    selection-background-color: #2dd4bf;
    selection-color: #04201d;
}
QComboBox:focus, QDoubleSpinBox:focus, QSpinBox:focus, QLineEdit:focus,
QDateEdit:focus, QTextEdit:focus {
    border-color: #2dd4bf;
}
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background: #121821;
    color: #e6edf3;
    border: 1px solid #232d3a;
    selection-background-color: #2dd4bf;
    selection-color: #04201d;
}

QCheckBox { color: #e6edf3; font-size: 13px; spacing: 8px; }
QCheckBox:disabled { color: #5a6573; }
QCheckBox::indicator {
    width: 16px; height: 16px;
    border: 1px solid #334155; border-radius: 4px;
    background: #0f141b;
}
QCheckBox::indicator:checked { background: #2dd4bf; border-color: #2dd4bf; }

/* ---------- Tablas ---------- */
QTableWidget {
    background: #0f141b;
    color: #e6edf3;
    gridline-color: transparent;
    border: 1px solid #232d3a;
    border-radius: 10px;
    alternate-background-color: #121821;
}
QTableWidget::item {
    padding: 10px 12px;
    border-bottom: 1px solid #1a222d;
}
QTableWidget::item:selected {
    background: #1a2f2c;
    color: #e6edf3;
}
QHeaderView::section {
    background: #0b0f14;
    color: #8b97a8;
    padding: 10px 12px;
    border: none;
    border-bottom: 1px solid #232d3a;
    font-weight: 600;
    font-size: 11px;
    text-transform: uppercase;
}
QHeaderView::section:hover { color: #cbd5e1; }

QListWidget {
    background: #0f141b;
    color: #e6edf3;
    border: 1px solid #232d3a;
    border-radius: 10px;
}
QListWidget::item { padding: 8px; border-bottom: 1px solid #1a222d; }
QListWidget::item:selected { background: #1a2f2c; color: #e6edf3; }

/* ---------- Tabs ---------- */
QTabWidget::pane {
    border: 1px solid #232d3a;
    border-radius: 12px;
    background: #121821;
    margin-top: 6px;
    padding: 6px;
}
QTabBar { qproperty-drawBase: 0; }
QTabBar::tab {
    background: transparent;
    color: #8b97a8;
    padding: 10px 20px;
    font-size: 13px;
    font-weight: 600;
    border: none;
    border-bottom: 2px solid transparent;
    margin-right: 4px;
}
QTabBar::tab:selected { color: #e6edf3; border-bottom: 2px solid #2dd4bf; }
QTabBar::tab:hover:!selected { color: #cbd5e1; }

/* ---------- Progreso / scroll ---------- */
QProgressBar {
    border: none;
    border-radius: 6px;
    text-align: center;
    background: #1a222d;
    color: #e6edf3;
    height: 10px;
}
QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #2dd4bf, stop:1 #5eead4);
    border-radius: 6px;
}

QStatusBar {
    color: #8b97a8;
    background: #0b0f14;
    border-top: 1px solid #232d3a;
    font-size: 12px;
}

QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #2a3441;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover { background: #3a4656; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { height: 10px; background: transparent; }
QScrollBar::handle:horizontal { background: #2a3441; border-radius: 5px; min-width: 30px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QScrollArea { background: transparent; border: none; }

/* ---------- Diálogos ---------- */
QMessageBox { background: #0b0f14; }
QMessageBox QLabel { color: #e6edf3; }
QFileDialog { background: #0b0f14; }

QToolTip {
    background: #18202b;
    color: #e6edf3;
    border: 1px solid #232d3a;
    border-radius: 6px;
    padding: 6px 8px;
    font-size: 12px;
}

QSplitter::handle { background: #232d3a; width: 2px; }
"""
