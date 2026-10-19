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
        self.assertIn(
            "bateria_resto.py",
            self.texto,
            "el workflow debe correr el corredor de la mitad resto: si la"
            " seleccion existe pero nadie la invoca, la cobertura es mentira",
        )
        self.assertIn(
            "fake_hermes_cli",
            self.texto,
            "la mitad resto necesita el doble de hermes_cli en PYTHONPATH",
        )
        self.assertIn(
            "OPENCODE_CONFIG_CONTENT",
            self.texto,
            "la mitad resto debe limpiar OPENCODE_CONFIG_CONTENT (env -u):"
            " el router no hereda configuracion real del runner",
        )


if __name__ == "__main__":
    unittest.main()
