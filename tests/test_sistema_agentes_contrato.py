#!/usr/bin/env python3
"""Los ensayos I01 e I02 contra la copia fijada de G en shared/sistema-agentes/g.

El paquete padre del espejo lleva guion y no es importable: compartido.py lo
expone cargándolo por ruta. casos.json declara el veredicto de cada fixture y,
para sistema-agentes.meta.v1, los validadores son validar y validar_plan.
"""

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_especificacion = importlib.util.spec_from_file_location(
    "compartido_contrato", REPO / "plugins/goncloud-team/raices/compartido.py"
)
compartido = importlib.util.module_from_spec(_especificacion)
_especificacion.loader.exec_module(compartido)

contrato = compartido.contrato
FIXTURES = compartido.G_DIR / "scripts/tests/fixtures/sistema-agentes"
CASOS = contrato.cargar_json(FIXTURES / "casos.json")
VALIDADORES = [contrato.validar, contrato.validar_plan]


def casos(veredicto, ensayo):
    return [
        caso
        for caso in CASOS
        if caso["veredicto"] == veredicto and caso["ensayo"] == ensayo
    ]


class I01PedidosValidos(unittest.TestCase):
    """Los tres pedidos válidos del ensayo I01 pasan por ambos validadores."""

    maxDiff = None

    def test_cada_pedido_valido_valida_con_ambos_validadores(self):
        validos = casos("valido", "I01")
        self.assertEqual(
            [caso["archivo"] for caso in validos],
            [
                "planes/validos/pedido-chico.json",
                "planes/validos/fila-importada.json",
                "planes/validos/pedido-compuesto.json",
            ],
        )
        for caso in validos:
            documento = contrato.cargar_json(FIXTURES / caso["archivo"])
            with self.subTest(archivo=caso["archivo"]):
                self.assertEqual(documento["schema"], caso["esquema"])
                for validador in VALIDADORES:
                    self.assertEqual(validador(documento), [])


class I02PlanesInvalidos(unittest.TestCase):
    """Cada plan inválido del ensayo I02 da exactamente su regla y su ruta."""

    maxDiff = None

    def test_cada_plan_invalido_da_su_regla_y_su_ruta(self):
        invalidos = casos("invalido", "I02")
        self.assertEqual(
            [caso["archivo"] for caso in invalidos],
            [
                "planes/invalidos/ciclo.json",
                "planes/invalidos/auto-dependencia.json",
                "planes/invalidos/id-duplicado.json",
                "planes/invalidos/criterio-duplicado.json",
                "planes/invalidos/dependencia-faltante.json",
                "planes/invalidos/criterio-faltante.json",
                "planes/invalidos/campo-prohibido.json",
                "planes/invalidos/schema-ajeno.json",
            ],
        )
        for caso in invalidos:
            documento = contrato.cargar_json(FIXTURES / caso["archivo"])
            for validador in VALIDADORES:
                with self.subTest(
                    archivo=caso["archivo"], validador=validador.__name__
                ):
                    self.assertEqual(
                        [
                            (hallazgo.regla, hallazgo.ruta)
                            for hallazgo in validador(documento)
                        ],
                        [(caso["regla"], caso["ruta"])],
                    )


if __name__ == "__main__":
    unittest.main()
