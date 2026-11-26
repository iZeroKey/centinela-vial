#!/usr/bin/env python3
"""
Centinela Control — Script de compilación de ejecutable para Windows (.exe).

Uso:
    python scripts/construir_exe.py --limpiar
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys

DIR_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_APP = os.path.join(DIR_RAIZ, "app")
ENTRYPOINT = os.path.join(DIR_APP, "centinela", "main.py")
RUTA_MODELO = os.path.join(DIR_RAIZ, "models", "detector_facial.pth")
RUTA_SVG = os.path.join(DIR_RAIZ, "assets", "favicon.svg")
RUTA_FACENET = os.path.join(
    os.path.expanduser("~"), ".cache", "torch", "checkpoints", "20180402-114759-vggface2.pt"
)


def _generar_ico(ruta_svg, ruta_ico):
    """Renderiza favicon.svg a un archivo .ico multi-resolución."""
    if not os.path.isfile(ruta_svg):
        return None
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QGuiApplication, QImage, QPainter
    from PyQt6.QtSvg import QSvgRenderer

    _app = QGuiApplication.instance() or QGuiApplication([])
    renderer = QSvgRenderer(ruta_svg)
    if not renderer.isValid():
        raise RuntimeError(f"SVG inválido: {ruta_svg}")

    tamanos = (16, 24, 32, 48, 64, 128, 256)
    imagenes = []
    for tam in tamanos:
        qimg = QImage(tam, tam, QImage.Format.Format_RGBA8888)
        qimg.fill(Qt.GlobalColor.transparent)
        painter = QPainter(qimg)
        renderer.render(painter)
        painter.end()
        ptr = qimg.bits()
        ptr.setsize(qimg.sizeInBytes())
        pil_img = Image.frombuffer(
            "RGBA", (tam, tam), bytes(ptr), "raw", "RGBA", 0, 1
        ).copy()
        imagenes.append(pil_img)

    os.makedirs(os.path.dirname(ruta_ico), exist_ok=True)
    imagenes[-1].save(
        ruta_ico,
        format="ICO",
        sizes=[(t, t) for t in tamanos],
        append_images=imagenes[:-1],
    )
    return ruta_ico


def _preparar_datos(destino_datos, copiar_actuales):
    """Inicializa o copia la carpeta data/ editable junto al ejecutable."""
    if copiar_actuales:
        origen = os.path.join(DIR_RAIZ, "app", "data")
        if os.path.isdir(destino_datos):
            shutil.rmtree(destino_datos)
        os.makedirs(destino_datos, exist_ok=True)
        for sub in ("centinela", "certs"):
            src = os.path.join(origen, sub)
            if os.path.isdir(src):
                shutil.copytree(src, os.path.join(destino_datos, sub), dirs_exist_ok=True)
        src_asist = os.path.join(origen, "asistencia")
        if os.path.isdir(src_asist):
            dst_asist = os.path.join(destino_datos, "asistencia")
            os.makedirs(dst_asist, exist_ok=True)
            alumnos_json = os.path.join(src_asist, "alumnos.json")
            if os.path.isfile(alumnos_json):
                shutil.copy2(alumnos_json, os.path.join(dst_asist, "alumnos.json"))
            src_ref = os.path.join(src_asist, "referencia")
            if os.path.isdir(src_ref):
                shutil.copytree(
                    src_ref, os.path.join(dst_asist, "referencia"), dirs_exist_ok=True
                )
        return

    for sub in (
        "centinela",
        os.path.join("asistencia", "referencia"),
        "certs",
    ):
        os.makedirs(os.path.join(destino_datos, sub), exist_ok=True)


def _construir_comando(args, ruta_ico):
    """Arma la línea de comandos de PyInstaller."""
    sep = os.pathsep
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--name",
        args.nombre,
        "--specpath",
        os.path.join(DIR_RAIZ, "build"),
        "--distpath",
        os.path.join(DIR_RAIZ, "dist"),
        "--workpath",
        os.path.join(DIR_RAIZ, "build", "pyinstaller"),
        "--paths",
        DIR_APP,
        "--onefile" if args.onefile else "--onedir",
        "--console" if args.consola else "--windowed",
        "--add-data",
        f"{RUTA_MODELO}{sep}models",
        "--add-data",
        f"{RUTA_SVG}{sep}assets",
    ]
    if ruta_ico:
        cmd.extend(["--icon", ruta_ico])

    if args.incluir_facenet:
        if not os.path.isfile(RUTA_FACENET):
            raise FileNotFoundError(
                f"No se encontró el peso local de FaceNet en: {RUTA_FACENET}"
            )
        cmd.extend(["--add-data", f"{RUTA_FACENET}{sep}{os.path.join('models', 'checkpoints')}"])

    for pkg in ("qfluentwidgets",):
        cmd.extend(["--collect-data", pkg])
    for pkg in ("centinela", "qfluentwidgets", "zeroconf", "facenet_pytorch", "cryptography"):
        cmd.extend(["--collect-submodules", pkg])

    for mod in (
        "PyQt6.QtSvg",
        "PyQt6.QtSvgWidgets",
        "darkdetect",
        "centinela.vision.detector_facial",
    ):
        cmd.extend(["--hidden-import", mod])

    for mod in (
        "tkinter",
        "matplotlib",
        "pytest",
        "IPython",
        "jupyter",
        "mediapipe",
        "jax",
        "jaxlib",
        "scipy",
        "tensorflow",
        "tensorboard",
    ):
        cmd.extend(["--exclude-module", mod])

    cmd.append(ENTRYPOINT)
    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Genera el ejecutable de Windows (.exe) para Centinela Control."
    )
    parser.add_argument("--nombre", default="CentinelaControl", help="Nombre del ejecutable")
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Empaquetar en un único archivo .exe (por defecto usa carpeta --onedir)",
    )
    parser.add_argument(
        "--consola",
        action="store_true",
        help="Mostrar ventana de consola para diagnóstico",
    )
    parser.add_argument(
        "--con-datos",
        action="store_true",
        help="Copiar app/data/ actual junto al ejecutable",
    )
    parser.add_argument(
        "--incluir-facenet",
        action="store_true",
        help="Incluir peso local de FaceNet (vggface2) para uso sin internet",
    )
    parser.add_argument(
        "--limpiar",
        action="store_true",
        help="Eliminar build/ y dist/<nombre> antes de compilar",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validar rutas, generar icono y mostrar comando sin ejecutar PyInstaller",
    )
    args = parser.parse_args()

    if importlib.util.find_spec("PyInstaller") is None:
        print(
            "PyInstaller no está instalado en este entorno.\n"
            f"Instálalo con:\n  {sys.executable} -m pip install pyinstaller",
            file=sys.stderr,
        )
        return 1

    for etiqueta, ruta in (
        ("entrypoint", ENTRYPOINT),
        ("modelo detector", RUTA_MODELO),
        ("favicon", RUTA_SVG),
    ):
        if not os.path.isfile(ruta):
            print(f"Falta {etiqueta}: {ruta}", file=sys.stderr)
            return 1

    dir_build = os.path.join(DIR_RAIZ, "build")
    dir_dist_app = (
        os.path.join(DIR_RAIZ, "dist", f"{args.nombre}.exe")
        if args.onefile
        else os.path.join(DIR_RAIZ, "dist", args.nombre)
    )
    if args.limpiar:
        shutil.rmtree(dir_build, ignore_errors=True)
        if os.path.isdir(dir_dist_app):
            shutil.rmtree(dir_dist_app, ignore_errors=True)
        elif os.path.isfile(dir_dist_app):
            os.remove(dir_dist_app)

    ruta_ico = _generar_ico(RUTA_SVG, os.path.join(dir_build, "centinela.ico"))
    cmd = _construir_comando(args, ruta_ico)

    print("Comando PyInstaller:")
    print(" ", " ".join(f'"{c}"' if " " in c else c for c in cmd))
    if args.dry_run:
        print(f"Icono generado en: {ruta_ico}")
        return 0

    res = subprocess.run(cmd, cwd=DIR_RAIZ)
    if res.returncode != 0:
        return res.returncode

    dir_datos = (
        os.path.join(DIR_RAIZ, "dist", "data")
        if args.onefile
        else os.path.join(DIR_RAIZ, "dist", args.nombre, "data")
    )
    _preparar_datos(dir_datos, args.con_datos)

    ruta_exe = (
        os.path.join(DIR_RAIZ, "dist", f"{args.nombre}.exe")
        if args.onefile
        else os.path.join(DIR_RAIZ, "dist", args.nombre, f"{args.nombre}.exe")
    )
    tam_mb = os.path.getsize(ruta_exe) / (1024 * 1024)
    print(f"\nEjecutable generado: {ruta_exe} ({tam_mb:.1f} MB)")
    print(f"Carpeta de datos:    {dir_datos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
