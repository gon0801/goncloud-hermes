"""Instala el doble de hermes_cli (tests/fake_hermes_cli) en sys.modules.

El doble nunca va al sys.path: se instala por modulo y se desinstala entero,
asi las pruebas reales de la PC (skipUnless hermes_cli importable) no pueden
encontrarlo por accidente durante el discover.
"""

import importlib.util
import sys
from pathlib import Path

DOBLE = Path(__file__).resolve().parent / "fake_hermes_cli"
MODULOS = (
    "hermes_cli",
    "hermes_cli.kanban_db_connect",
    "hermes_cli.kanban_db",
    "hermes_cli.kanban_db_graph",
)


def instalar():
    presentes = [nombre for nombre in MODULOS if nombre in sys.modules]
    if presentes:
        raise RuntimeError(f"ya hay un hermes_cli instalado: {presentes}")
    for nombre in MODULOS:
        if nombre == "hermes_cli":
            ruta = DOBLE / "hermes_cli" / "__init__.py"
        else:
            ruta = DOBLE.joinpath(*nombre.split(".")).with_suffix(".py")
        spec = importlib.util.spec_from_file_location(nombre, ruta)
        modulo = importlib.util.module_from_spec(spec)
        sys.modules[nombre] = modulo
        spec.loader.exec_module(modulo)


def desinstalar():
    for nombre in MODULOS:
        sys.modules.pop(nombre, None)
