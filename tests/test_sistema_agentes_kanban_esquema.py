#!/usr/bin/env python3
"""Contrato: el esquema del doble de kanban es el del kanban REAL.

El bug H-payload (2026-10-19): el doble definia task_events.payload_json,
la base real tiene `payload` — las pruebas pasaban y en vivo todo start con
una cadena parada tronaba (`no column named payload_json`). La copia del
esquema real vive en tests/fixtures/kanban_schema_real.json; si el doble se
desalinea otra vez, esto falla en CI.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests" / "fake_hermes_cli"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from hermes_cli.kanban_db_connect import connect  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "kanban_schema_real.json"


class EsquemaDelDobleTests(unittest.TestCase):
    def test_columnas_del_doble_son_las_del_kanban_real(self):
        esperado = json.loads(FIXTURE.read_text(encoding="utf-8"))
        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, temporal)
        with temporal as tmp:
            conn = connect(Path(tmp) / "kanban.db")
            try:
                real = {
                    fila[0]
                    for fila in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                for tabla, columnas in esperado.items():
                    self.assertIn(tabla, real)
                    with self.subTest(tabla=tabla):
                        actuales = [
                            fila[1]
                            for fila in conn.execute(f"PRAGMA table_info({tabla})")
                        ]
                        self.assertEqual(actuales, columnas)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
