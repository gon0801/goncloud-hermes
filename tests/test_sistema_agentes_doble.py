#!/usr/bin/env python3
"""El doble convive con el hermes_constants real de la PC (SUA-07 H2, r7).

raices.hogar resuelve hermes_constants en cada llamada: en la PC el real
llega a sys.modules antes de que la prueba instale el doble. La guarda no
puede fallar solo porque el real existe; durante la prueba el resolvedor
debe ver el doble y al terminar el real queda restaurado. Mezclar el
hermes_cli real con el doble a medias sigue siendo error.
"""

import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

import hermes_falso  # noqa: E402


def hermes_constants_real(casa):
    real = types.ModuleType("hermes_constants")
    real.get_hermes_home = lambda: casa
    real.get_default_hermes_root = lambda home=None: casa
    return real


class ConviveConElReal(unittest.TestCase):
    def setUp(self):
        self.real = hermes_constants_real("/home/real/de/hermes")

    def test_instala_sobre_el_real_y_lo_restaura(self):
        with mock.patch.dict(sys.modules, {"hermes_constants": self.real}):
            hermes_falso.instalar()
            try:
                self.assertIsNot(sys.modules["hermes_constants"], self.real)
            finally:
                hermes_falso.desinstalar()
            self.assertIs(sys.modules["hermes_constants"], self.real)

    def test_el_resolvedor_ve_el_doble_no_el_real(self):
        with tempfile.TemporaryDirectory() as nombre:
            raiz = Path(nombre) / "hermes-raiz"
            perfil = raiz / "profiles" / "claw"
            perfil.mkdir(parents=True)
            with mock.patch.dict(sys.modules, {"hermes_constants": self.real}):
                hermes_falso.instalar()
                try:
                    with mock.patch.dict("os.environ", {"HERMES_HOME": str(perfil)}):
                        herramientas = importlib.import_module("raices.herramientas")
                        self.assertEqual(
                            herramientas.ruta_del_store(),
                            raiz / "sistema-agentes.db",
                        )
                finally:
                    hermes_falso.desinstalar()

    def test_un_hermes_cli_real_presente_sigue_siendo_conflicto(self):
        real = types.ModuleType("hermes_cli")
        with mock.patch.dict(sys.modules, {"hermes_cli": real}):
            with self.assertRaises(RuntimeError):
                hermes_falso.instalar()


if __name__ == "__main__":
    unittest.main()
