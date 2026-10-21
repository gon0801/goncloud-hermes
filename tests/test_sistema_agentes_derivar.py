#!/usr/bin/env python3
"""El derivador del espejo contra una salida dorada.

La prueba de G compara contra proyeccion.py, que H no copia; el dorado es la
salida de G en el commit del pin con las constantes de abajo.
"""

import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_especificacion = importlib.util.spec_from_file_location(
    "compartido_derivar", REPO / "tests/compartido_g.py"
)
compartido = importlib.util.module_from_spec(_especificacion)
_especificacion.loader.exec_module(compartido)

derivar = compartido.derivar
AHORA = datetime(2026, 10, 16, 12, 0, 0, tzinfo=timezone.utc)
STALE = 300000
INTERVALO = 300000
GATEWAY = "gateway-1"
NOTADOS = {"exc-01": datetime(2026, 10, 16, 11, 30, 0, tzinfo=timezone.utc)}
FIXTURES = (
    compartido.G_DIR / "scripts/tests/fixtures/sistema-agentes/proyeccion-derivar"
)
DORADO = REPO / "tests/fixtures/sistema-agentes-g/salida-derivar.json"


class DerivarContraDorado(unittest.TestCase):
    maxDiff = None

    def test_cada_fixture_deriva_su_salida_fijada(self):
        dorado = compartido.contrato.cargar_json(DORADO)
        self.assertEqual(
            [fixture.name for fixture in sorted(FIXTURES.glob("*.json"))],
            sorted(dorado),
        )
        for fixture in sorted(FIXTURES.glob("*.json")):
            with self.subTest(fixture=fixture.name):
                snapshot = compartido.contrato.cargar_json(fixture)
                notados = NOTADOS if fixture.stem == "excepciones" else {}
                try:
                    obtenida = derivar.derivar(
                        snapshot, AHORA, STALE, INTERVALO, notados, host_gateway=GATEWAY
                    )
                except ValueError as fallo:
                    obtenida = {"ValueError": str(fallo)}
                self.assertEqual(obtenida, dorado[fixture.name])


if __name__ == "__main__":
    unittest.main()
