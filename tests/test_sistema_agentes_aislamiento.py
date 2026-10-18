#!/usr/bin/env python3
"""El espejo de G corre sin openclaw en el PATH y sin red.

Cada prueba lanza un hijo con el PATH apuntando a un directorio temporal
vacio y desde ese mismo directorio, asi el espejo no puede heredar nada del
entorno del repo. El hijo tacha la red reemplazando `socket.socket` antes de
cargar `compartido`, porque solo `import` después del bloqueo prueba el
aislamiento.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
COMPARTIDO = REPO / "plugins/goncloud-team/raices/compartido.py"
G = REPO / "shared/sistema-agentes/g"
PLAN_CHICO = (
    G / "scripts/tests/fixtures/sistema-agentes/planes/validos/pedido-chico.json"
)
RAIZ_ESTADOS = (
    G / "scripts/tests/fixtures/sistema-agentes/proyeccion-derivar/raiz-estados.json"
)

CARGAR_COMPARTIDO = f"""\
import importlib.util

spec = importlib.util.spec_from_file_location("compartido", {str(COMPARTIDO)!r})
compartido = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compartido)
"""

GUION_SIN_OPENCLAW = (
    """\
import shutil
import sys

assert shutil.which("openclaw") is None
assert shutil.which("opencode") is None

"""
    + CARGAR_COMPARTIDO
    + f"""\
assert compartido.contrato.validar_plan(
    compartido.contrato.cargar_json({str(PLAN_CHICO)!r})
) == []
print("OK")
"""
)

GUION_SIN_RED = (
    """\
import socket
from datetime import datetime, timezone


def red_bloqueada(*argumentos, **nombrados):
    raise AssertionError("red bloqueada")


socket.socket = red_bloqueada
socket.create_connection = red_bloqueada

"""
    + CARGAR_COMPARTIDO
    + f"""\
assert compartido.contrato.validar_plan(
    compartido.contrato.cargar_json({str(PLAN_CHICO)!r})
) == []
proyeccion = compartido.derivar.derivar(
    compartido.contrato.cargar_json({str(RAIZ_ESTADOS)!r}),
    ahora=datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc),
    stale_after_ms=300000,
    intervalo_ms=300000,
    notados={{}},
    host_gateway="gateway-1",
)
assert proyeccion["schema"] == "sistema-agentes.proyeccion.v1"
print("OK")
"""
)


class Aislamiento(unittest.TestCase):
    maxDiff = None

    def correr_hijo(self, guion):
        with tempfile.TemporaryDirectory() as temporal:
            proceso = subprocess.run(
                [sys.executable, "-c", guion],
                # Un PATH a un directorio propio vacio: en Windows,
                # "/usr/bin:/bin" no existe ni usa el separador correcto, y la
                # prueba quedaba sin efecto real como aislamiento.
                env={"PATH": str(temporal)},
                cwd=temporal,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertTrue(
            proceso.stdout.endswith("OK\n"), proceso.stdout + proceso.stderr
        )
        return proceso

    def test_corre_sin_openclaw_en_el_path(self):
        self.correr_hijo(GUION_SIN_OPENCLAW)

    def test_corre_sin_red(self):
        self.correr_hijo(GUION_SIN_RED)


if __name__ == "__main__":
    unittest.main()
