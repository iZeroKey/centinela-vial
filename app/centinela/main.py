#!/usr/bin/env python3
"""
Centinela Control — Punto de entrada del software.
"""

import os
import sys

if not getattr(sys, "frozen", False):
    _DIR_APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _DIR_APP not in sys.path:
        sys.path.insert(0, _DIR_APP)


def _ejecutar_check():
    """Verifica imports y recursos requeridos sin abrir la ventana principal."""
    from centinela.core import rutas, config, db, gestor_alumnos  # noqa: F401
    from centinela.vision import detector_facial, rostros, camara_hilo  # noqa: F401
    from centinela.red import servidor_web  # noqa: F401
    from centinela.ui import ventana_principal  # noqa: F401

    faltantes = []
    for etiqueta, ruta in (
        ("modelo detector", rutas.RUTA_MODELO_DETECTOR),
        ("favicon", rutas.RUTA_FAVICON),
    ):
        if not os.path.isfile(ruta):
            faltantes.append(f"{etiqueta}: {ruta}")

    ruta_log = next(
        (a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--check-log=")),
        None,
    )
    if faltantes:
        msg = "CHECK FAIL: " + "; ".join(faltantes)
        if ruta_log:
            with open(ruta_log, "w", encoding="utf-8") as f:
                f.write(msg + "\n")
        if sys.stderr is not None:
            print(msg, file=sys.stderr)
        return 1

    msg = (
        f"CHECK OK: congelado={rutas.ES_CONGELADO} "
        f"datos={rutas.DIR_DATOS} modelo={rutas.RUTA_MODELO_DETECTOR}"
    )
    if ruta_log:
        with open(ruta_log, "w", encoding="utf-8") as f:
            f.write(msg + "\n")
    if sys.stdout is not None:
        print(msg)
    return 0


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_ejecutar_check())
    from centinela.ui.ventana_principal import main
    main()
