"""Instala el doble de hermes_cli (tests/fake_hermes_cli) en sys.modules.

El doble nunca va al sys.path: se instala por modulo y se desinstala entero,
asi las pruebas reales de la PC (skipUnless hermes_cli importable) no pueden
encontrarlo por accidente durante el discover.

El hermes_constants y el hermes_cli REALES pueden estar presentes (en la PC
el venv trae hermes_cli importable y una prueba que sondee capacidades sin
inyectar el importador lo deja cargado en sys.modules): instalar GUARDA lo que
haya de ambos y desinstalar lo RESTAURA, sin importar el orden de las
pruebas. Asi un residuo de otra prueba no rompe la suite: cada prueba ve el
mismo doble y al salir el modulo queda como estaba.
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
    "hermes_constants",
)

_previos = None


def instalar():
    global _previos
    _previos = {
        nombre: sys.modules[nombre] for nombre in MODULOS if nombre in sys.modules
    }
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
    global _previos
    for nombre in MODULOS:
        sys.modules.pop(nombre, None)
    if _previos is not None:
        sys.modules.update(_previos)
        _previos = None
