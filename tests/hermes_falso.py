"""Instala el doble de hermes_cli (tests/fake_hermes_cli) en sys.modules.

El doble nunca va al sys.path: se instala por modulo y se desinstala entero,
asi las pruebas reales de la PC (skipUnless hermes_cli importable) no pueden
encontrarlo por accidente durante el discover.

El hermes_constants REAL puede estar presente (raices.hogar lo resuelve en
cada llamada y en la PC llega antes de que la prueba instale el doble):
instalar lo guarda y desinstalar lo restaura. Un hermes_cli presente si es
un mezclado a medias con el real y sigue siendo error.
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
    presentes = [nombre for nombre in MODULOS if nombre in sys.modules]
    conflicto = [nombre for nombre in presentes if nombre.split(".")[0] == "hermes_cli"]
    if conflicto:
        raise RuntimeError(f"ya hay un hermes_cli instalado: {conflicto}")
    _previos = {nombre: sys.modules[nombre] for nombre in presentes}
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
