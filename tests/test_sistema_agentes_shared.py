#!/usr/bin/env python3
"""El despliegue lleva shared/sistema-agentes junto al plugin (hallazgo F1).

En la copia desplegada, compartido.py busca la copia fijada en parents[3]:
profiles/<rol>/shared/sistema-agentes. configure_team debe copiarla a cada
perfil que recibe el plugin. Las pruebas que no necesitan yaml ni hermes_cli
corren en la Mac; la del despliegue real queda para la PC.
"""

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

SPEC = importlib.util.spec_from_file_location(
    "configure_team", REPO / "scripts/configure_team.py"
)
configure_team = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_team)

COMPARTIDO = REPO / "plugins/goncloud-team/raices/compartido.py"
ORIGEN = REPO / "shared/sistema-agentes"


class ArchivosCompartidos(unittest.TestCase):
    def test_trae_exactamente_los_archivos_de_la_copia_fijada(self):
        en_disco = {
            f"shared/sistema-agentes/{ruta.relative_to(ORIGEN).as_posix()}": (
                ruta.read_bytes()
            )
            for ruta in sorted(ORIGEN.rglob("*"))
            if ruta.is_file() and "__pycache__" not in ruta.parts
        }
        self.assertEqual(configure_team.archivos_compartidos(), en_disco)

    def test_la_copia_fijada_trae_el_pin_y_la_politica(self):
        archivos = configure_team.archivos_compartidos()
        self.assertIn("shared/sistema-agentes/pin.json", archivos)
        self.assertIn(
            "shared/sistema-agentes/g/docs/evidence/agent-work/politica-calidad.json",
            archivos,
        )
        json.loads(archivos["shared/sistema-agentes/pin.json"])


class CopiaDesplegada(unittest.TestCase):
    def test_compartido_halla_la_copia_fijada_junto_al_plugin_copiado(self):
        with tempfile.TemporaryDirectory() as directorio:
            perfil = Path(directorio) / "profiles" / "claw"
            destino_plugin = perfil / "plugins" / "goncloud-team" / "raices"
            destino_plugin.mkdir(parents=True)
            (destino_plugin / "compartido.py").write_bytes(COMPARTIDO.read_bytes())
            for relativa, contenido in configure_team.archivos_compartidos().items():
                destino = perfil / relativa
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(contenido)

            spec = importlib.util.spec_from_file_location(
                "compartido_desplegado", destino_plugin / "compartido.py"
            )
            compartido = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(compartido)
            self.assertTrue(compartido.G_DIR.is_dir())
            self.assertTrue(compartido.PIN.is_file())
            plan = (
                compartido.G_DIR
                / "scripts/tests/fixtures/sistema-agentes/planes/validos/"
                "pedido-chico.json"
            )
            self.assertEqual(
                compartido.contrato.validar_plan(compartido.contrato.cargar_json(plan)),
                [],
            )


UPSTREAM = Path(
    __import__("os").environ.get(
        "HERMES_INSTALL_DIR",
        Path(__import__("os").environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
    )
)


@unittest.skipUnless(
    (UPSTREAM / "hermes_cli/profiles.py").is_file(),
    "Run tests with the installed Hermes runtime",
)
class DespliegueReal(unittest.TestCase):
    def test_cada_perfil_recibe_la_copia_fijada_junto_a_su_plugin(self):
        with tempfile.TemporaryDirectory() as directorio:
            runtime = Path(directorio) / "hermes-test"
            resultado = subprocess.run(
                [
                    sys.executable,
                    str(REPO / "scripts/configure_team.py"),
                    "--home",
                    str(runtime),
                    "--install-dir",
                    str(UPSTREAM),
                ],
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(Path(directorio) / "home"),
                    # Path.home() en Windows lee USERPROFILE, no HOME.
                    "USERPROFILE": str(Path(directorio) / "home"),
                    "LOCALAPPDATA": str(Path(directorio) / "local"),
                    "PYTHONUTF8": "1",
                },
            )
            self.assertEqual(resultado.returncode, 0, resultado.stderr)
            for rol in ("claw", "scout", "amazon"):
                pin = runtime / "profiles" / rol / ("shared/sistema-agentes/pin.json")
                if rol == "amazon":
                    self.assertFalse(
                        pin.exists(),
                        "un especialista sin perfil no debe recibir copia",
                    )
                    continue
                self.assertTrue(pin.is_file(), rol)
                self.assertEqual(
                    pin.read_bytes(),
                    (ORIGEN / "pin.json").read_bytes(),
                    rol,
                )
                self.assertTrue(
                    (
                        runtime
                        / "profiles"
                        / rol
                        / "plugins/goncloud-team/raices/compartido.py"
                    ).is_file(),
                    rol,
                )


if __name__ == "__main__":
    unittest.main()
