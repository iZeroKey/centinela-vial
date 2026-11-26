#!/usr/bin/env python3
"""Permite ejecutar `python -m centinela` desde `app/`."""

import sys
from centinela.main import _ejecutar_check
from centinela.ui.ventana_principal import main

if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_ejecutar_check())
    main()
