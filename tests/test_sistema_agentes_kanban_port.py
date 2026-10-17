#!/usr/bin/env python3
"""El doble kanban: write_txn exclusivo, buscar_o_crear y caídas inyectables.

La estructura ejercita la garantía I03: crear sin buscar no existe en la API, y una
caída tras el commit (ACK perdido) deja la tarjeta.
"""

import importlib
import sys
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

kanban_port = importlib.import_module("raices.kanban_port")
CaidaInyectada = kanban_port.CaidaInyectada
DobleKanban = kanban_port.DobleKanban
PuntoDeCaida = kanban_port.PuntoDeCaida
SpecDeTarjeta = kanban_port.SpecDeTarjeta
asegurar_tarjeta = kanban_port.asegurar_tarjeta
clave_de_tarjeta = kanban_port.clave_de_tarjeta

SPEC = SpecDeTarjeta("root/unit#1", "instrucciones", "codex")


class Clave(unittest.TestCase):
    def test_la_clave_es_funcion_pura_del_binding(self):
        self.assertEqual(clave_de_tarjeta("root-1", "unit-c", 2), "sa:root-1:unit-c:2")


class Exclusividad(unittest.TestCase):
    def test_write_txn_es_exclusivo(self):
        kanban = DobleKanban()
        dentro_a = threading.Event()
        libera_a = threading.Event()
        salio_a = threading.Event()
        entro_b = threading.Event()

        def corrida_a():
            with kanban.write_txn() as escritura:
                dentro_a.set()
                escritura.buscar_o_crear("k-a", SPEC)
                self.assertTrue(libera_a.wait(2))
            salio_a.set()

        def corrida_b():
            with kanban.write_txn():
                entro_b.set()

        hilo_a = threading.Thread(target=corrida_a)
        hilo_b = threading.Thread(target=corrida_b)
        hilo_a.start()
        self.assertTrue(dentro_a.wait(1))
        hilo_b.start()
        self.assertFalse(entro_b.wait(0.2), "b entró mientras a tenía la txn")
        libera_a.set()
        self.assertTrue(salio_a.wait(1))
        hilo_a.join(1)
        hilo_b.join(1)
        self.assertTrue(entro_b.is_set(), "b nunca entró")


class BuscarOCrear(unittest.TestCase):
    def test_dentro_de_la_misma_txn_busca_y_crea_una_sola_vez(self):
        kanban = DobleKanban()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        primera = asegurar_tarjeta(kanban, clave, SPEC)
        segunda = asegurar_tarjeta(kanban, clave, SPEC)
        self.assertEqual(primera, segunda)
        self.assertEqual(kanban.creaciones, [clave])

    def test_dos_txn_consecutivas_no_duplican(self):
        kanban = DobleKanban()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        asegurar_tarjeta(kanban, clave, SPEC)
        hallada = asegurar_tarjeta(kanban, clave, SPEC)
        self.assertIsNotNone(hallada)
        self.assertEqual(len(kanban.creaciones), 1)

    def test_dos_claves_dan_dos_tarjetas(self):
        kanban = DobleKanban()
        asegurar_tarjeta(kanban, clave_de_tarjeta("r", "a", 1), SPEC)
        asegurar_tarjeta(kanban, clave_de_tarjeta("r", "b", 1), SPEC)
        self.assertEqual(len(kanban.creaciones), 2)


class Caidas(unittest.TestCase):
    def test_cada_punto_interno_no_deja_tarjeta(self):
        for punto in (
            PuntoDeCaida.ABRIR_TXN,
            PuntoDeCaida.ANTES_DE_BUSCAR,
            PuntoDeCaida.DESPUES_DE_CREAR,
        ):
            with self.subTest(punto=punto):
                kanban = DobleKanban()
                kanban.caer_en(punto)
                clave = clave_de_tarjeta("root-1", "unit-c", 1)
                with self.assertRaises(CaidaInyectada) as contexto:
                    asegurar_tarjeta(kanban, clave, SPEC)
                self.assertEqual(contexto.exception.punto, punto)
                self.assertIsNone(kanban.por_clave(clave))

    def test_ack_perdido_deja_la_tarjeta(self):
        kanban = DobleKanban()
        kanban.caer_en(PuntoDeCaida.DESPUES_DE_TXN)
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        with self.assertRaises(CaidaInyectada):
            asegurar_tarjeta(kanban, clave, SPEC)
        self.assertIsNotNone(kanban.por_clave(clave))
        self.assertEqual(kanban.creaciones, [clave])
        kanban.quitar_caida(PuntoDeCaida.DESPUES_DE_TXN)
        hallada = asegurar_tarjeta(kanban, clave, SPEC)
        self.assertIsNotNone(hallada)
        self.assertEqual(len(kanban.creaciones), 1)


class Vinculos(unittest.TestCase):
    def test_vincular_completar_y_bloquear_dentro_de_txn(self):
        kanban = DobleKanban()
        clave_a = clave_de_tarjeta("r", "a", 1)
        clave_b = clave_de_tarjeta("r", "b", 1)
        tarjeta_a = asegurar_tarjeta(kanban, clave_a, SPEC)
        tarjeta_b = asegurar_tarjeta(kanban, clave_b, SPEC)
        with kanban.write_txn() as escritura:
            escritura.vincular(tarjeta_b.id, tarjeta_a.id)
            escritura.completar(
                tarjeta_b.id, {"contrato": "review.v1", "verdict": "approved"}
            )
            escritura.bloquear(tarjeta_a.id, "raíz cancelada")
        self.assertEqual(kanban.por_clave(clave_b).estado, "completada")
        self.assertEqual(
            kanban.por_clave(clave_b).resultado,
            {"contrato": "review.v1", "verdict": "approved"},
        )
        self.assertEqual(kanban.por_clave(clave_a).estado, "bloqueada")

    def test_una_excepcion_dentro_de_la_txn_deshace_todo(self):
        kanban = DobleKanban()
        clave = clave_de_tarjeta("r", "a", 1)

        with self.assertRaises(RuntimeError), kanban.write_txn() as escritura:
            escritura.buscar_o_crear(clave, SPEC)
            raise RuntimeError("falló el despacho")

        self.assertIsNone(kanban.por_clave(clave))
        self.assertEqual(kanban.creaciones, [])

    def test_los_ayudantes_de_prueba_avanzan_estados(self):
        kanban = DobleKanban()
        clave = clave_de_tarjeta("r", "a", 1)
        asegurar_tarjeta(kanban, clave, SPEC)
        kanban.crear_corriendo(clave)
        self.assertEqual(kanban.por_clave(clave).estado, "corriendo")
        kanban.crear_hecha(clave, {"contrato": "ready.v1"})
        self.assertEqual(kanban.por_clave(clave).estado, "completada")
        kanban.crear_bloqueada(clave, "bloqueo")
        self.assertEqual(kanban.por_clave(clave).estado, "bloqueada")


if __name__ == "__main__":
    unittest.main()
