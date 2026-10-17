#!/usr/bin/env python3
"""La puerta de capacidades de la ruta de raíces (SUA-07 H4a).

certificadas.json lista los pares (version, code_sha) acreditados en la PC.
La forma la valida un solo punto, validar_certificadas, que comparten la
prueba de forma sobre el archivo real del repo y certificar.py antes de
escribir. Sin entradas válidas no hay puerta que abra.
"""

import importlib
import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

capacidades = importlib.import_module("raices.capacidades")

CERTIFICADAS = REPO / "plugins/goncloud-team/raices/certificadas.json"


class Certificadas(unittest.TestCase):
    def test_el_archivo_del_repo_es_una_lista_json(self):
        entradas = json.loads(CERTIFICADAS.read_text(encoding="utf-8"))
        self.assertIsInstance(entradas, list)

    def test_las_entradas_del_repo_tienen_la_forma_del_contrato(self):
        entradas = json.loads(CERTIFICADAS.read_text(encoding="utf-8"))
        self.assertEqual(capacidades.validar_certificadas(entradas), [])


if __name__ == "__main__":
    unittest.main()
