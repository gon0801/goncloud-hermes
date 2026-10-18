#!/usr/bin/env python3
"""El doble convive con el hermes_constants y el hermes_cli reales de la PC
(SUA-07 H2, r7; defensa r2).

raices.hogar resuelve hermes_constants en cada llamada: en la PC el real
llega a sys.modules antes de que la prueba instale el doble. Y el hermes_cli
real del venv puede quedar cargado por un residuo de otra prueba. instalar
GUARDA lo que haya de ambos, monta el doble, y desinstalar RESTAURA el
modulo exacto de antes: el orden de las pruebas no rompe la suite ni deja
mezclas a medias.
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

    def test_un_hermes_cli_real_presente_se_guarda_y_se_restaura(self):
        # Defensa (r2): el hermes_cli real del venv cargado por un residuo de
        # otra prueba ya no revienta instalar. Durante la prueba el modulo es
        # el doble y al desinstalar el real queda EXACTO como estaba (el
        # mismo objeto, con sus submodulos), igual que hermes_constants.
        real_cli = types.ModuleType("hermes_cli")
        real_db = types.ModuleType("hermes_cli.kanban_db")
        real_connect = types.ModuleType("hermes_cli.kanban_db_connect")
        with mock.patch.dict(
            sys.modules,
            {
                "hermes_cli": real_cli,
                "hermes_cli.kanban_db": real_db,
                "hermes_cli.kanban_db_connect": real_connect,
            },
        ):
            hermes_falso.instalar()
            try:
                self.assertIsNot(
                    sys.modules["hermes_cli"],
                    real_cli,
                    "durante la prueba el hermes_cli es el doble",
                )
            finally:
                hermes_falso.desinstalar()
            self.assertIs(sys.modules["hermes_cli"], real_cli)
            self.assertIs(sys.modules["hermes_cli.kanban_db"], real_db)
            self.assertIs(sys.modules["hermes_cli.kanban_db_connect"], real_connect)

    def test_instalar_sin_nada_previo_no_deja_residuos(self):
        # F5: "sin residuos" compara contra el estado capturado ANTES de
        # instalar: desinstalar restaura lo previo, así que si un módulo
        # existía antes (el real de la PC, un residuo de otra prueba) debe
        # seguir estando tras desinstalar, y si no estaba, no aparece.
        antes = {nombre: sys.modules.get(nombre) for nombre in hermes_falso.MODULOS}
        hermes_falso.instalar()
        try:
            self.assertIn("hermes_cli", sys.modules)
        finally:
            hermes_falso.desinstalar()
        for nombre, modulo in antes.items():
            if modulo is None:
                self.assertNotIn(nombre, sys.modules, f"{nombre} es residuo")
            else:
                self.assertIs(
                    sys.modules.get(nombre), modulo, f"{nombre} no quedó restaurado"
                )


if __name__ == "__main__":
    unittest.main()
