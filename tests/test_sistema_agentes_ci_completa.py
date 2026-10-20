#!/usr/bin/env python3
"""Contrato de cobertura de la bateria de CI (H-ci-completa).

El rojo de test_subscription_tools (10 fallas por la tabla v6) entro a la
PC sin que nadie lo viera: el patron de la bateria
(`test_sistema_agentes_*.py`) dejaba 28 archivos test_*.py fuera de CI y
solo corrian a mano. Orden del dueno 2026-10-19: la bateria es TODOS los
tests/test_*.py. La union la hacen los patrones `-p` del workflow y la
seleccion de .github/scripts/bateria_resto.py; este contrato lee el
workflow y falla si algun archivo queda fuera, si las mitades se solapan
(un archivo corriendo dos veces, o el sistema corriendo con el doble que
engana a las guardias de hermes_cli real) o si el workflow deja de
invocar al corredor.
"""

import fnmatch
import importlib.util
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "sistema-agentes.yml"
CORREDOR = REPO / ".github" / "scripts" / "bateria_resto.py"
PATRON_P = re.compile(r'-p\s+"([^"]+)"')


def patrones_de_la_bateria() -> list:
    """Los patrones -p de las lineas `unittest discover` del workflow."""
    return [
        patron
        for linea in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if "unittest discover" in linea
        for patron in PATRON_P.findall(linea)
    ]


_INVOCACION_CORREDOR = re.compile(
    r"env\s+-u\s+OPENCODE_CONFIG_CONTENT\s+"
    r"PYTHONPATH=\S*fake_hermes_cli\S*\s+"
    r"python3\s+\.github/scripts/bateria_resto\.py"
)


def _sin_comentario_shell(linea: str) -> str:
    """La linea sin su comentario de shell (`#` fuera de comillas)."""
    en_simple = en_doble = False
    for i, caracter in enumerate(linea):
        if caracter == "'" and not en_doble:
            en_simple = not en_simple
        elif caracter == '"' and not en_simple:
            en_doble = not en_doble
        elif (
            caracter == "#"
            and not en_simple
            and not en_doble
            and (i == 0 or linea[i - 1] in " \t")
        ):
            return linea[:i]
    return linea


def invoca_el_corredor_con_el_doble(texto: str) -> bool:
    """La invocacion real del corredor, en una sola linea logica.

    Quita los comentarios de shell, une las continuaciones con barra y
    evalua cada comando logico por separado: exige en el mismo comando
    el `env -u OPENCODE_CONFIG_CONTENT`, el doble de hermes_cli en
    PYTHONPATH y el corredor. Ni un comentario que nombre las tres cosas
    ni las piezas repartidas en comandos distintos cuentan como correr
    la mitad resto (F5 de #47).
    """
    logicas: list = []
    pendiente = ""
    for fisica in texto.splitlines():
        pendiente += _sin_comentario_shell(fisica)
        if pendiente.rstrip().endswith("\\"):
            pendiente = pendiente.rstrip()[:-1] + " "
            continue
        logicas.append(pendiente)
        pendiente = ""
    if pendiente:
        logicas.append(pendiente)
    return any(_INVOCACION_CORREDOR.search(logica) for logica in logicas)


class ContratoBateriaCompleta(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"falta el workflow: {WORKFLOW}")
        self.assertTrue(CORREDOR.is_file(), f"falta el corredor: {CORREDOR}")
        spec = importlib.util.spec_from_file_location("bateria_resto", CORREDOR)
        self.corredor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.corredor)
        self.texto = WORKFLOW.read_text(encoding="utf-8")
        self.patrones = patrones_de_la_bateria()

    def test_ningun_test_queda_fuera_de_la_bateria(self):
        """Todo tests/test_*.py corre en CI: patron del workflow o resto."""
        fuera = [
            ruta.name
            for ruta in sorted((REPO / "tests").glob("test_*.py"))
            if not any(fnmatch.fnmatch(ruta.name, patron) for patron in self.patrones)
            and ruta.name[: -len(".py")] not in self.corredor.resto()
        ]
        self.assertEqual(
            fuera,
            [],
            "archivos test_*.py que la bateria de CI deja fuera: ni un patron"
            " -p del workflow ni la seleccion de bateria_resto.py los cubre",
        )

    def test_las_dos_mitades_no_se_solapan(self):
        """Un archivo corre UNA vez: en el patron o en el resto, no en ambos.

        El sistema con el doble en PYTHONPATH tambien enganaria las
        guardias skipUnless de hermes_cli real (kanban_real): el doble no
        sustituye esa certificacion.
        """
        solapados = sorted(
            ruta.name
            for ruta in (REPO / "tests").glob("test_*.py")
            if any(fnmatch.fnmatch(ruta.name, patron) for patron in self.patrones)
            and ruta.name[: -len(".py")] in self.corredor.resto()
        )
        self.assertEqual(
            solapados,
            [],
            "archivos que correrian dos veces (el segundo con el doble de"
            " hermes_cli en PYTHONPATH)",
        )

    def test_el_workflow_declara_patrones_y_corre_el_corredor_con_el_doble(self):
        self.assertTrue(
            self.patrones,
            "el workflow debe declarar el patron -p de la mitad sistema",
        )
        self.assertTrue(
            invoca_el_corredor_con_el_doble(self.texto),
            "el workflow debe invocar el corredor de la mitad resto en un"
            " solo comando con env -u OPENCODE_CONFIG_CONTENT y el doble"
            " de hermes_cli en PYTHONPATH: si la seleccion existe pero"
            " nadie la invoca asi, la cobertura es mentira",
        )

    def test_menciones_sueltas_no_cuentan_como_invocacion(self):
        # F5 de #47: el contrato anterior buscaba tres substrings en todo
        # el workflow; un comentario que las nombre pasaba sin correr nada.
        mutantes = [
            "# bateria_resto.py con fake_hermes_cli y OPENCODE_CONFIG_CONTENT\n",
            "PYTHONPATH=tests/fake_hermes_cli python3"
            " .github/scripts/bateria_resto.py\n",
            "env -u OPENCODE_CONFIG_CONTENT python3 .github/scripts/bateria_resto.py\n",
            "env -u OPENCODE_CONFIG_CONTENT PYTHONPATH=tests/fake_hermes_cli"
            " python3 otro_corredor.py\n",
            # La invocacion completa pero comentada no corre nada.
            "# env -u OPENCODE_CONFIG_CONTENT"
            " PYTHONPATH=tests/fake_hermes_cli"
            " python3 .github/scripts/bateria_resto.py\n",
            # Las tres piezas en comandos distintos tampoco la forman:
            # el regex no debe cruzar saltos de linea entre comandos.
            "env -u OPENCODE_CONFIG_CONTENT\n"
            "PYTHONPATH=tests/fake_hermes_cli\n"
            "python3 .github/scripts/bateria_resto.py\n",
        ]
        for mutante in mutantes:
            with self.subTest(mutante=mutante.strip()):
                self.assertFalse(invoca_el_corredor_con_el_doble(mutante))
        # El comando real del workflow (con continuacion) si cuenta.
        real = (
            "env -u OPENCODE_CONFIG_CONTENT"
            " PYTHONPATH=tests/fake_hermes_cli \\\n"
            "  python3 .github/scripts/bateria_resto.py\n"
        )
        self.assertTrue(invoca_el_corredor_con_el_doble(real))
        self.assertTrue(invoca_el_corredor_con_el_doble(self.texto))


if __name__ == "__main__":
    unittest.main()
