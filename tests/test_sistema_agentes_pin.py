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
import subprocess
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location(
    "compartido", REPO / "tests/compartido_g.py"
)
compartido = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compartido)

SIN_CONVERSION = (
    "shared/sistema-agentes/**",
    "tests/fixtures/sistema-agentes-g/**",
)


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
            and ruta.name != ".DS_Store"
            # __pycache__ ajeno: un import desde otro arbol no cuenta como
            # contenido del espejo, igual que .DS_Store.
            and "__pycache__" not in ruta.parts
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


class FinDeLinea(unittest.TestCase):
    """La copia fijada y su dorado no pasan por la conversion de fin de linea.

    Un clon en Windows con autocrlf reescribia LF como CRLF y los sha256 del
    pin ya no coincidian. La regla vive en .gitattributes y esta prueba la
    lee con git check-attr para que valga en cualquier arbol que corra la
    bateria.
    """

    def test_los_fijados_por_bytes_quedan_fuera_de_la_conversion(self):
        for regla in SIN_CONVERSION:
            for ruta in (
                "shared/sistema-agentes/pin.json",
                "tests/fixtures/sistema-agentes-g/salida-derivar.json",
            ):
                with self.subTest(regla=regla, ruta=ruta):
                    try:
                        resultado = subprocess.run(
                            ["git", "check-attr", "text", "--", ruta],
                            cwd=REPO,
                            capture_output=True,
                            text=True,
                            timeout=30,
                        )
                    except OSError:
                        self.skipTest("git no esta disponible")
                    self.assertEqual(resultado.returncode, 0, resultado.stderr)
                    self.assertIn("text: unset", resultado.stdout, (regla, ruta))

    def test_un_sh_del_espejo_sigue_sin_conversion_pese_al_sh_global(self):
        """F6: el `*.sh text eol=lf` global no pisa el -text del espejo.

        En .gitattributes la ULTIMA regla que aplica a una ruta gana por
        atributo: puesta al final, `*.sh text eol=lf` daba text: set a un
        .sh del espejo y autocrlf lo reescribiria (los sha256 del pin se
        rompen en Windows). Vive ANTES de las -text, asi que un .sh dentro
        del espejo (aunque hoy no exista: check-attr evalua patrones, no
        el disco) debe salir text: unset. El otro lado del contrato: los
        .sh que SI se ejecutan con bash (.github/scripts) conservan su
        text: set y eol: lf.
        """
        for ruta in (
            "shared/sistema-agentes/fakes/hipotetico.sh",
            "tests/fixtures/sistema-agentes-g/hipotetico.sh",
        ):
            with self.subTest(ruta=ruta):
                salida = self.check_attr("text", ruta)
                self.assertIn("text: unset", salida, ruta)
        salida = self.check_attr("text", "eol", ".github/scripts/reusar_completa.sh")
        self.assertIn("text: set", salida)
        self.assertIn("eol: lf", salida)

    def check_attr(self, *atributos_y_ruta):
        """`git check-attr <atributos> -- <ruta>`; devuelve stdout (salta sin git)."""
        *atributos, ruta = atributos_y_ruta
        try:
            resultado = subprocess.run(
                ["git", "check-attr", *atributos, "--", ruta],
                cwd=REPO,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except OSError:
            self.skipTest("git no esta disponible")
        self.assertEqual(resultado.returncode, 0, resultado.stderr)
        return resultado.stdout


if __name__ == "__main__":
    unittest.main()
