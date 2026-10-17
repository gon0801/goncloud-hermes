#!/usr/bin/env python3
"""El pin del espejo de G: lo que lista pin.json es exactamente lo que hay y está íntegro.

`shared/sistema-agentes/pin.json` fija repo, commit y sha256 de cada archivo copiado de G.
El espejo se carga por ruta porque el paquete padre `goncloud-team` lleva guion y no es
importable.
"""

import hashlib
import importlib.util
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "compartido", REPO / "plugins/goncloud-team/raices/compartido.py"
)
compartido = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compartido)


class Espejo(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        with open(compartido.PIN, encoding="utf-8") as archivo:
            self.pin = json.load(archivo)
        self.g_dir = compartido.G_DIR

    def test_cada_archivo_del_pin_existe_y_su_sha256_coincide(self):
        for ruta, fijado in self.pin["files"].items():
            with self.subTest(ruta=ruta):
                archivo = self.g_dir / ruta
                self.assertTrue(archivo.is_file(), f"falta {ruta} en el espejo")
                digest = hashlib.sha256(archivo.read_bytes()).hexdigest()
                self.assertEqual(f"sha256:{digest}", fijado)

    def test_no_hay_archivos_de_mas_ni_de_menos_en_el_espejo(self):
        en_disco = {
            ruta.relative_to(self.g_dir).as_posix()
            for ruta in self.g_dir.rglob("*")
            if ruta.is_file()
            and not any(parte == "__pycache__" for parte in ruta.parts)
            and ruta.suffix != ".pyc"
            and ruta.name != ".DS_Store"
        }
        self.assertEqual(en_disco, set(self.pin["files"]))

    def test_el_pin_trae_repo_commit_y_files_ordenados(self):
        self.assertEqual(self.pin["repo"], "gon0801/goncloud-openclaw")
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", self.pin["commit"]))
        claves = list(self.pin["files"])
        self.assertEqual(claves, sorted(claves))
        for valor in self.pin["files"].values():
            self.assertTrue(valor.startswith("sha256:"))

    def test_el_contrato_del_espejo_halla_su_politica_sin_editarse(self):
        contrato = compartido.contrato
        plan = (
            self.g_dir
            / "scripts/tests/fixtures/sistema-agentes/planes/validos/pedido-chico.json"
        )
        self.assertEqual(contrato.validar_plan(contrato.cargar_json(plan)), [])


if __name__ == "__main__":
    unittest.main()
