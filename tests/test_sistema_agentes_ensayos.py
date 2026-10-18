#!/usr/bin/env python3
"""El recorrido del bloque SUA-07 de H sobre tests/ensayos-h.json.

El manifiesto de ensayos dice qué distingue cada ensayo; este archivo exige
que cada cláusula tenga su prueba antes de cerrar su rebanada.
"""

import functools
import importlib.util
import json
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENSAYOS_H = REPO / "tests/ensayos-h.json"

BLOQUE = [
    "I01",
    "I02",
    "I03",
    "I04",
    "I05",
    "I06",
    "I07",
    "I08",
    "I09",
    "I10",
    "I11",
    "I12",
    "I13",
    "I14",
    "I16",
    "I18",
    "I20",
]

CLAUSULAS_ESPERADAS = {
    "I01": ["I01.1", "I01.2", "I01.3"],
    "I02": ["I02.1", "I02.2", "I02.3", "I02.4"],
    "I03": ["I03.1", "I03.2"],
    "I04": ["I04.1", "I04.2", "I04.3"],
    "I05": ["I05.1", "I05.2", "I05.3", "I05.4", "I05.5"],
    "I06": ["I06.1", "I06.2"],
    "I07": ["I07.1", "I07.2", "I07.3", "I07.4"],
    "I08": ["I08.1", "I08.2", "I08.3"],
    "I09": ["I09.1", "I09.2", "I09.3", "I09.4", "I09.5"],
    "I10": ["I10.1", "I10.2", "I10.3"],
    "I11": ["I11.1", "I11.2", "I11.3"],
    "I12": [
        "I12.1",
        "I12.2",
        "I12.3",
        "I12.4",
        "I12.5",
        "I12.6",
        "I12.7",
    ],
    "I13": ["I13.1", "I13.2", "I13.3", "I13.4", "I13.5"],
    "I14": ["I14.1", "I14.2", "I14.3"],
    "I16": ["I16.1", "I16.2", "I16.3", "I16.4"],
    "I18": ["I18.1", "I18.2"],
    "I20": ["I20.1", "I20.2", "I20.3", "I20.4", "I20.5"],
}

_especificacion = importlib.util.spec_from_file_location(
    "compartido_ensayos", REPO / "plugins/goncloud-team/raices/compartido.py"
)
compartido = importlib.util.module_from_spec(_especificacion)
_especificacion.loader.exec_module(compartido)

DOCUMENTO = json.loads(ENSAYOS_H.read_text(encoding="utf-8"))
MANIFIESTO = compartido.contrato.cargar_json(
    compartido.G_DIR / "scripts/tests/fixtures/sistema-agentes/manifiesto-ensayos.json"
)


def _aplanar(suite):
    for prueba in suite:
        if isinstance(prueba, unittest.TestSuite):
            yield from _aplanar(prueba)
        else:
            yield prueba


@functools.cache
def _ids_de_pruebas():
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(REPO / "tests"), pattern="test_sistema_agentes_*.py"
    )
    return frozenset(prueba.id() for prueba in _aplanar(suite))


# F7: un `no-aplica` de rebanada cerrada solo cubre su cláusula si la razón
# nombra dónde queda cubierta: una rebanada (H4, H0b), un bloque+ensayo
# (SUA-09H) o un ensayo del bloque (I20). Una razón de relleno no cubre nada.
DESTINO_DE_COBERTURA = re.compile(r"\b(?:H\d+[a-z]?|SUA-\d+[A-Z]?|I\d+)\b")


def razon_nombra_destino(razon):
    return bool(DESTINO_DE_COBERTURA.search(razon or ""))


class Bloque(unittest.TestCase):
    maxDiff = None

    def test_el_bloque_de_h_es_el_del_diseno(self):
        self.assertEqual(DOCUMENTO["bloque"], "SUA-07")
        self.assertEqual(DOCUMENTO["ensayos"], BLOQUE)
        ids_del_manifiesto = {ensayo["id"] for ensayo in MANIFIESTO}
        self.assertLessEqual(set(BLOQUE), ids_del_manifiesto)

    def test_cada_clausula_del_bloque_tiene_su_fila(self):
        for ensayo in BLOQUE:
            clausulas = [
                fila["clausula"]
                for fila in DOCUMENTO["filas"]
                if fila["ensayo"] == ensayo
            ]
            with self.subTest(ensayo=ensayo):
                self.assertEqual(clausulas, CLAUSULAS_ESPERADAS[ensayo])


class Recorrido(unittest.TestCase):
    maxDiff = None

    def test_cada_prueba_citada_existe(self):
        for fila in DOCUMENTO["filas"]:
            if fila["prueba"] in (None, "no-aplica"):
                continue
            with self.subTest(clausula=fila["clausula"]):
                self.assertIn(fila["prueba"], _ids_de_pruebas())

    def test_cada_no_aplica_trae_su_razon(self):
        for fila in DOCUMENTO["filas"]:
            if fila["prueba"] != "no-aplica":
                continue
            razon = fila.get("razon")
            with self.subTest(clausula=fila["clausula"]):
                self.assertIsInstance(razon, str)
                self.assertTrue(razon.strip())

    def test_una_rebanada_cerrada_no_deja_clausulas_sin_prueba(self):
        """El diseño (§4) admite `no-aplica` con su razón como fila de una
        rebanada cerrada: la cláusula queda cubierta por la razón SOLO si la
        razón nombra la rebanada o el ensayo donde se cubre (F7)."""
        cerradas = set(DOCUMENTO["rebanadasCerradas"])
        for fila in DOCUMENTO["filas"]:
            if fila["rebanada"] not in cerradas:
                continue
            with self.subTest(clausula=fila["clausula"]):
                if fila["prueba"] == "no-aplica":
                    razon = fila.get("razon") or ""
                    self.assertTrue(razon.strip())
                    self.assertTrue(
                        razon_nombra_destino(razon),
                        f"{fila['clausula']}: la razón debe nombrar la rebanada"
                        " o el ensayo donde se cubre (p. ej. H4/H5/SUA-09H/I20)",
                    )
                else:
                    self.assertIsNotNone(fila["prueba"])

    def test_una_razon_de_relleno_no_cubre_una_clausula_cerrada(self):
        for razon in (
            "pendiente",
            "luego lo vemos",
            "no aplica por ahora",
            "  ",
            "la cubre otra cosa",
        ):
            with self.subTest(razon=razon):
                self.assertFalse(razon_nombra_destino(razon))
        for razon in ("se cubre en H4", "llega en SUA-09H", "ya probada en I20"):
            with self.subTest(razon=razon):
                self.assertTrue(razon_nombra_destino(razon))


if __name__ == "__main__":
    unittest.main()
