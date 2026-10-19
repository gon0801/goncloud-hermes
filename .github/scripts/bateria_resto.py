#!/usr/bin/env python3
"""La mitad de la bateria que el patron `test_sistema_agentes_*` no cubre.

Orden del dueno 2026-10-19 (H-ci-completa): la bateria de CI corre TODOS
los tests/test_*.py — el rojo de test_subscription_tools entro porque el
patron dejaba 28 archivos fuera y nadie lo notaba fuera de la PC. El
workflow corre dos mitades cuya UNION es la bateria completa:

1. `unittest discover -p "test_sistema_agentes_*.py"` — su mundo exacto,
   SIN el doble en PYTHONPATH: las guardias skipUnless de hermes_cli real
   (kanban_real) siguen decidiendo solas.
2. ESTE script: todo test_*.py que no sea sistema_agentes, con el doble
   tests/fake_hermes_cli importable (los archivos que piden hermes_cli a
   nivel de modulo) y sin OPENCODE_CONFIG_CONTENT.

La seleccion vive aqui y el contrato la lee (tests/
test_sistema_agentes_ci_completa.py): patrones del workflow + esta lista =
la bateria completa, sin recortes ni corridas dobles.
"""

import fnmatch
import sys
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
PRUEBAS = RAIZ / "tests"
PATRON_SISTEMA = "test_sistema_agentes_*.py"


def resto():
    """Los modulos de pruebas que el patron sistema NO cubre."""
    return sorted(
        ruta.stem
        for ruta in PRUEBAS.glob("test_*.py")
        if not fnmatch.fnmatch(ruta.name, PATRON_SISTEMA)
    )


if __name__ == "__main__":
    sys.path.insert(0, str(PRUEBAS))
    cargador = unittest.TestLoader()
    suite = unittest.TestSuite(cargador.loadTestsFromName(nombre) for nombre in resto())
    resultado = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if resultado.wasSuccessful() else 1)
