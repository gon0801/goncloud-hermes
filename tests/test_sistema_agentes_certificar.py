#!/usr/bin/env python3
"""certificar.py con todas las piezas inyectadas: la candidata solo sale de
una batería que pasó en la COPIA del home (el original nunca se toca) y el
archivo de certificados solo cambia con --escribir."""

import importlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

capacidades = importlib.import_module("raices.capacidades")
certificar = importlib.import_module("raices.certificar")

SHA = "a" * 40
PAR = {
    "version": "0.21.5",
    "code_sha": SHA,
    "texto_version": f"Hermes Agent v0.21.5+5822.g{SHA[:7]} (2026.9.24)",
}
SALIDA_VERDE = (
    "test_dedupe (...) ... ok\n"
    "test_write_txn (...) ... ok\n"
    "test_link (...) ... ok\n"
    "\n"
    "Ran 3 tests in 0.413s\n"
    "\n"
    "OK\n"
)


def entrada_del_par():
    return {
        "version": "0.21.5",
        "code_sha": SHA,
        "certificadoEn": "2026-10-17T00:00:00Z",
        "evidencia": {"pruebas": "tests.test_sistema_agentes_kanban_real"},
    }


class Certificar(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.home = Path(self.directorio.name) / "hermes"
        self.home.mkdir()
        (self.home / "kanban.db").write_text("db", encoding="utf-8")
        self.ruta = Path(self.directorio.name) / "certificadas.json"
        self.copiadas: list = []
        self.pruebas: list = []

    def copiar(self, origen, destino):
        self.copiadas.append((str(origen), str(destino)))

    def correr_pruebas(self, salida=SALIDA_VERDE):
        def correr(home_copia, repo, python=sys.executable):
            self.pruebas.append((str(home_copia), str(repo), python))
            return salida

        return correr

    def certificar(
        self,
        argv,
        salida=SALIDA_VERDE,
        leer_par=None,
        ruta=None,
    ):
        lector = leer_par or (lambda: (False, "version-no-certificada", PAR))
        bufer = StringIO()
        with redirect_stdout(bufer):
            codigo = certificar.main(
                argv,
                leer_par=lector,
                copiar_home=self.copiar,
                correr_pruebas=self.correr_pruebas(salida),
                ruta_certificados=ruta or self.ruta,
            )
        return codigo, bufer.getvalue()

    def argv(self, *banderas):
        return ["--home", str(self.home), *banderas]

    def test_el_par_vivo_nuevo_imprime_la_candidata_y_sale_cero(self):
        codigo, salida = self.certificar(self.argv())
        self.assertEqual(codigo, 0)
        candidata = json.loads(salida)
        self.assertEqual(candidata["version"], "0.21.5")
        self.assertEqual(candidata["code_sha"], SHA)
        self.assertEqual(
            candidata["evidencia"]["pruebas"],
            "tests.test_sistema_agentes_kanban_real",
        )
        self.assertEqual(candidata["evidencia"]["resumen"], "OK")
        self.assertEqual(candidata["evidencia"]["hermesVersion"], PAR["texto_version"])
        self.assertRegex(
            candidata["certificadoEn"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
        )
        self.assertIn("corridoEn", candidata["evidencia"])
        # La copia recibió el home original y las pruebas corrieron sobre la
        # copia, nunca sobre el original.
        self.assertEqual(len(self.copiadas), 1)
        self.assertEqual(self.copiadas[0][0], str(self.home))
        self.assertEqual(self.pruebas[0][0], self.copiadas[0][1])
        self.assertNotEqual(self.pruebas[0][0], str(self.home))
        self.assertTrue((self.home / "kanban.db").exists())

    def test_el_par_ya_certificado_sale_cero_sin_copiar_nada(self):
        codigo, salida = self.certificar(
            self.argv(), leer_par=lambda: (True, None, PAR)
        )
        self.assertEqual(codigo, 0)
        self.assertIn("ya", salida)
        self.assertEqual(self.copiadas, [])
        self.assertEqual(self.pruebas, [])

    def test_una_bateria_que_no_pasa_no_deja_candidata(self):
        fallidas = {
            "failed": SALIDA_VERDE.replace("OK\n", "FAILED (failures=1)\n"),
            "dos-pruebas": SALIDA_VERDE.replace("Ran 3 tests", "Ran 2 tests"),
            "skipped": SALIDA_VERDE.replace("OK\n", "OK (skipped=3)\n"),
        }
        for nombre, salida_pruebas in fallidas.items():
            with self.subTest(fallo=nombre):
                codigo, salida = self.certificar(self.argv(), salida=salida_pruebas)
                self.assertNotEqual(codigo, 0)
                self.assertNotIn('"code_sha"', salida)

    def test_escribir_agrega_la_candidata_al_archivo(self):
        self.ruta.write_text("[]", encoding="utf-8")
        codigo, _ = self.certificar(self.argv("--escribir"))
        self.assertEqual(codigo, 0)
        escritas = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.assertEqual(len(escritas), 1)
        self.assertEqual(escritas[0]["version"], "0.21.5")
        self.assertEqual(capacidades.validar_certificadas(escritas), [])

    def test_escribir_con_duplicado_no_reescribe(self):
        self.ruta.write_text(json.dumps([entrada_del_par()]), encoding="utf-8")
        antes = self.ruta.read_text(encoding="utf-8")
        codigo, salida = self.certificar(self.argv("--escribir"))
        self.assertNotEqual(codigo, 0)
        self.assertEqual(self.ruta.read_text(encoding="utf-8"), antes)
        self.assertTrue(salida)

    def test_sin_escribir_jamas_escribe(self):
        self.ruta.write_text("[]", encoding="utf-8")
        codigo, _ = self.certificar(self.argv())
        self.assertEqual(codigo, 0)
        self.assertEqual(self.ruta.read_text(encoding="utf-8"), "[]")

    def test_la_causa_que_no_es_version_no_certifica_nada(self):
        codigo, salida = self.certificar(
            self.argv(), leer_par=lambda: (False, "par-incierto", PAR)
        )
        self.assertEqual(codigo, 1)
        self.assertEqual(salida.strip(), "par-incierto")
        self.assertEqual(self.copiadas, [])


if __name__ == "__main__":
    unittest.main()
