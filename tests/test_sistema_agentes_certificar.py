#!/usr/bin/env python3
"""certificar.py con todas las piezas inyectadas: la candidata solo sale de
una batería que pasó en un home temporal VACÍO (el home vivo solo se lee,
nunca se copia) y el archivo de certificados solo cambia con --escribir."""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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
        self.addCleanup(cleanup_temp_directory, self.directorio)
        self.home = Path(self.directorio.name) / "hermes"
        self.home.mkdir()
        self.ruta = Path(self.directorio.name) / "certificadas.json"
        self.pruebas: list = []

    def correr_pruebas(self, salida=SALIDA_VERDE):
        def correr(home_copia, repo, python=sys.executable):
            contenidos = sorted(via.name for via in Path(home_copia).iterdir())
            self.pruebas.append((str(home_copia), str(repo), contenidos))
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
        entorno = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        with redirect_stdout(bufer), entorno:
            codigo = certificar.main(
                argv,
                leer_par=lector,
                correr_pruebas=self.correr_pruebas(salida),
                ruta_certificados=ruta or self.ruta,
            )
        return codigo, bufer.getvalue()

    def argv(self, *banderas):
        return list(banderas)

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
        # Las pruebas corrieron en un home temporal vacío, nunca en el vivo.
        self.assertEqual(len(self.pruebas), 1)
        self.assertNotEqual(self.pruebas[0][0], str(self.home))
        self.assertEqual(self.pruebas[0][2], [])

    def test_la_evidencia_hermes_version_sale_recortada(self):
        texto = f"Hermes Agent v0.21.5+5822.g{SHA[:7]} (2026.9.24)\n"
        self.assertNotEqual(texto, texto.strip(), "el repro necesita ruido alrededor")
        par_ruidoso = {**PAR, "texto_version": texto}
        codigo, salida = self.certificar(
            self.argv(),
            leer_par=lambda: (False, "version-no-certificada", par_ruidoso),
        )
        self.assertEqual(codigo, 0)
        candidata = json.loads(salida)
        self.assertEqual(
            candidata["evidencia"]["hermesVersion"],
            texto.strip(),
            "la evidencia trae el texto sin newline ni espacios de borde",
        )

    def test_corre_en_un_home_temporal_vacio_y_el_home_vivo_queda_igual(self):
        (self.home / "backup-key").write_text("llave-secreta", encoding="utf-8")
        grande = self.home / "memories.bin"
        grande.write_bytes(b"\0" * 4096)
        codigo, _ = self.certificar(self.argv())
        self.assertEqual(codigo, 0)
        recibido, _, contenidos = self.pruebas[0]
        self.assertNotEqual(recibido, str(self.home))
        self.assertNotIn("backup-key", contenidos)
        self.assertNotIn("memories.bin", contenidos)
        self.assertEqual(contenidos, [])
        self.assertEqual(
            (self.home / "backup-key").read_text(encoding="utf-8"), "llave-secreta"
        )
        self.assertEqual(grande.read_bytes(), b"\0" * 4096)

    def test_la_salida_trae_el_resumen_que_unittest_escribe_por_stderr(self):
        terminado = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=SALIDA_VERDE
        )
        with (
            tempfile.TemporaryDirectory() as temporal,
            mock.patch("raices.certificar.subprocess.run", return_value=terminado),
        ):
            salida = certificar._correr_pruebas(Path(temporal), REPO)
        self.assertIn("Ran 3 tests", salida)
        self.assertTrue(salida.endswith("OK\n"))

    def test_el_pythonpath_del_hijo_usa_el_separador_de_la_plataforma(self):
        # F3: el ":" fijo no separa en Windows (ahí es ";"). El centinela
        # fuerza un separador que no es ":" para que la prueba discrimine
        # tambien en la Mac: el hijo se monta con os.pathsep, como todo el
        # resto del repo.
        captura = {}

        def run_que_graba(argv, env=None, **kwargs):
            captura["PYTHONPATH"] = env["PYTHONPATH"]
            return subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=SALIDA_VERDE
            )

        with (
            tempfile.TemporaryDirectory() as temporal,
            mock.patch("raices.certificar.subprocess.run", run_que_graba),
            mock.patch.object(certificar.os, "pathsep", ";"),
        ):
            certificar._correr_pruebas(Path(temporal), REPO)
        esperado = ";".join([f"{REPO}/plugins/goncloud-team", f"{REPO}/tests"])
        self.assertEqual(
            captura["PYTHONPATH"],
            esperado,
            "el PYTHONPATH del hijo se une con os.pathsep, no con ':' fijo",
        )

    def test_el_par_ya_certificado_sale_cero_sin_copiar_nada(self):
        codigo, salida = self.certificar(
            self.argv(), leer_par=lambda: (True, None, PAR)
        )
        self.assertEqual(codigo, 0)
        self.assertIn("ya", salida)
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

    def test_escribir_con_certificadas_ilegible_aborta_sin_descartar_nada(self):
        # F4: ilegible no es vacío. Sobrescribir descartaría pares acreditados
        # por un error de sintaxis: abortar con código 1 y dejar el archivo
        # EXACTO como estaba.
        for contenido in ("{ no soy json", '{"version": "0.21.5"}', "nada"):
            with self.subTest(contenido=contenido):
                self.ruta.write_text(contenido, encoding="utf-8")
                antes = self.ruta.read_text(encoding="utf-8")
                codigo, salida = self.certificar(self.argv("--escribir"))
                self.assertEqual(codigo, 1)
                self.assertIn("ilegible", salida)
                self.assertEqual(
                    self.ruta.read_text(encoding="utf-8"),
                    antes,
                    "un archivo ilegible jamás se sobrescribe",
                )

    @unittest.skipUnless(os.name == "posix", "chmod 0 es de POSIX")
    def test_escribir_con_certificadas_sin_permiso_aborta_limpio(self):
        # F9: presente pero ILEGIBLE (chmod 000) es el caso de _previas que
        # falta: el read_text levanta OSError y sin atraparlo sale una traza
        # en vez del aborto limpio de F4.
        self.ruta.write_text(json.dumps([entrada_del_par()]), encoding="utf-8")
        antes = json.loads(self.ruta.read_text(encoding="utf-8"))
        self.ruta.chmod(0o000)
        codigo, salida = self.certificar(self.argv("--escribir"))
        self.ruta.chmod(0o644)  # restituir antes de verificar el contenido
        self.addCleanup(self.ruta.chmod, 0o644)
        self.assertEqual(codigo, 1)
        self.assertIn("ilegible", salida)
        self.assertEqual(
            json.loads(self.ruta.read_text(encoding="utf-8")),
            antes,
            "el archivo queda intacto",
        )

    def test_escribir_sin_archivo_o_vacio_empieza_de_cero(self):
        codigo, _ = self.certificar(self.argv("--escribir"))
        self.assertEqual(codigo, 0)
        self.assertEqual(len(json.loads(self.ruta.read_text(encoding="utf-8"))), 1)
        self.ruta.write_text("", encoding="utf-8")
        codigo, _ = self.certificar(self.argv("--escribir"))
        self.assertEqual(codigo, 0)
        self.assertEqual(len(json.loads(self.ruta.read_text(encoding="utf-8"))), 1)

    def test_la_causa_que_no_es_version_no_certifica_nada(self):
        codigo, salida = self.certificar(
            self.argv(), leer_par=lambda: (False, "par-incierto", PAR)
        )
        self.assertEqual(codigo, 1)
        self.assertEqual(salida.strip(), "par-incierto")
        self.assertEqual(self.pruebas, [])

    def test_la_version_sin_certificar_abre_con_aviso_y_certifica_igual(self):
        # Encargo H punto 3: decidir ahora abre con la causa advisoría; el
        # camino de certificar sigue leyendo la causa, no solo la puerta.
        codigo, salida = self.certificar(
            self.argv(), leer_par=lambda: (True, "version-no-certificada", PAR)
        )
        self.assertEqual(codigo, 0)
        self.assertEqual(json.loads(salida)["code_sha"], SHA)


class ElLectorUnicoDeVersion(unittest.TestCase):
    """El texto de --version se lee UNA vez: certificar reutiliza el lector de
    capacidades, que resuelve el ejecutable con shutil.which. En Windows el
    nombre pelado no encuentra hermes.CMD y la duplicación dejaba la
    evidencia con None aunque la puerta abriera."""

    def test_sin_duplicado_del_lector_en_certificar(self):
        self.assertFalse(
            hasattr(certificar, "_texto_de_version"),
            "el lector de --version vive solo en capacidades",
        )

    def test_la_evidencia_trae_el_texto_del_lector_de_capacidades(self):
        hechos = capacidades.Hechos(
            capacidades.VersionCLI("0.21.5", SHA[:7]),
            capacidades.ParGateway(
                kind="hermes-gateway",
                gateway_state="running",
                code_version="0.21.5",
                code_sha=SHA,
                hermes_home="/home/hermes",
                updated_at="2026-10-17T00:00:00Z",
            ),
            (),
            frozenset(capacidades.CAPACIDADES_REQUERIDAS),
            "/home/hermes",
        )
        texto = "Hermes Agent v0.21.5+5822.gaabbccd (2026.9.24)"
        with (
            mock.patch.object(capacidades, "hechos_vivos", lambda *a, **k: hechos),
            mock.patch.object(capacidades, "_correr_hermes_version", lambda: texto),
        ):
            _, _, par = certificar._leer_par_vivo()
        self.assertEqual(par["texto_version"], texto)


class SinHermesHome(unittest.TestCase):
    """Sin HERMES_HOME el lector de la puerta mira el directorio actual y
    contesta par-incierto, que confunde; el fallo debe nombrar la falta,
    igual que cron_ticker."""

    def test_falla_claro_y_no_consulta_el_par(self):
        consultado = []

        def lector():
            consultado.append(True)
            return (False, "par-incierto", None)

        bufer = StringIO()
        with (
            mock.patch.dict(os.environ),
            redirect_stdout(bufer),
        ):
            os.environ.pop("HERMES_HOME", None)
            codigo = certificar.main([], leer_par=lector)
        self.assertEqual(codigo, 1)
        self.assertIn("HERMES_HOME no esta definido", bufer.getvalue())
        self.assertEqual(consultado, [], "sin home no se lee ningún par")


if __name__ == "__main__":
    unittest.main()
