#!/usr/bin/env python3
"""Contrato del adaptador del kanban real sobre el doble de hermes_cli (SUA-07 H2).

Las clausulas que la referencia 0.21.5 fija y el adaptador debe respetar:
buscar-o-crear dentro de UN write_txn con la busqueda incluidas las archivadas
(I03), ACK perdido deja la tarjeta (I04), exclusividad de dos escritores, y el
mapeo de estados y del resultado al documento que H1 consume. Las pruebas contra
el kanban real de la PC viven en test_sistema_agentes_kanban_real.py.
"""

import importlib
import sys
from unittest import mock
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

import hermes_falso  # noqa: E402
from conexiones import rastrear_conexiones  # noqa: E402

kanban_port = importlib.import_module("raices.kanban_port")
SpecDeTarjeta = kanban_port.SpecDeTarjeta
asegurar_tarjeta = kanban_port.asegurar_tarjeta
clave_de_tarjeta = kanban_port.clave_de_tarjeta


def con_hermes_falso(clase):
    def prueba(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        clase(self)

    return prueba


SPEC = SpecDeTarjeta("root/unit#1", "instrucciones", "codex")


class BaseDeAdaptador(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        import raices.kanban as kanban

        self.kanban = kanban
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.temporal)
        self.ruta_db = Path(self.temporal.name) / "kanban.db"

    def adaptador(self):
        puerto = self.kanban.KanbanHermes(self.ruta_db)
        self.addCleanup(puerto.cerrar)
        return puerto

    def filas_por_clave(self, clave):
        from hermes_cli.kanban_db_connect import connect

        with closing(connect(self.ruta_db)) as conexion:
            return conexion.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (clave,)
            ).fetchall()

    def fijar_estado(self, clave, estado, resultado=None):
        from hermes_cli.kanban_db_connect import connect

        with closing(connect(self.ruta_db)) as conexion:
            conexion.execute(
                "UPDATE tasks SET status = ?, result = ? WHERE idempotency_key = ?",
                (estado, resultado, clave),
            )


class BuscarOCrear(BaseDeAdaptador):
    @con_hermes_falso
    def test_dentro_de_una_sola_txn_busca_y_crea_una_sola_vez(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        primera = asegurar_tarjeta(puerto, clave, SPEC)
        segunda = asegurar_tarjeta(puerto, clave, SPEC)
        self.assertEqual(primera.id, segunda.id)
        self.assertEqual(puerto.creaciones, [clave])
        self.assertEqual(len(self.filas_por_clave(clave)), 1)

    @con_hermes_falso
    def test_la_busqueda_incluye_las_archivadas_y_no_crea_otra(self):
        from hermes_cli import kanban_db
        from hermes_cli.kanban_db_connect import connect

        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        tarjeta = asegurar_tarjeta(puerto, clave, SPEC)
        with closing(connect(self.ruta_db)) as conexion:
            self.assertTrue(kanban_db.archive_task(conexion, tarjeta.id))
        hallada = asegurar_tarjeta(puerto, clave, SPEC)
        self.assertEqual(hallada.id, tarjeta.id)
        self.assertEqual(hallada.estado, "archivada")
        self.assertEqual(puerto.creaciones, [clave])
        self.assertEqual(len(self.filas_por_clave(clave)), 1)

    @con_hermes_falso
    def test_el_contexto_de_upstream_recibe_el_exit_con_la_excepcion(self):
        """F4: cerrar(False) no tira la pila a mano: el write_txn de upstream
        recibe __exit__ con la excepcion viva y hace su rollback y limpieza."""
        import contextlib

        from hermes_cli import kanban_db_connect

        recibidas = []
        original = kanban_db_connect.write_txn

        @contextlib.contextmanager
        def write_txn_espia(conn, **kwargs):
            with original(conn, **kwargs):
                try:
                    yield conn
                except Exception as exc:
                    recibidas.append(exc)
                    raise

        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        with mock.patch.object(kanban_db_connect, "write_txn", write_txn_espia):
            with self.assertRaises(RuntimeError), puerto.write_txn() as escritura:
                escritura.buscar_o_crear(clave, SPEC)
                raise RuntimeError("cayo el proceso antes del commit")
        self.assertEqual(len(recibidas), 1, "upstream recibe __exit__ con la excepcion")
        self.assertIsInstance(recibidas[0], RuntimeError)
        self.assertIsNone(puerto.leer_estado(clave))

    @con_hermes_falso
    def test_una_excepcion_tras_la_creacion_no_deja_tarjeta_ni_cuenta(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        with self.assertRaises(RuntimeError), puerto.write_txn() as escritura:
            escritura.buscar_o_crear(clave, SPEC)
            raise RuntimeError("cayo el proceso antes del commit")
        self.assertIsNone(puerto.leer_estado(clave))
        self.assertEqual(puerto.creaciones, [])
        self.assertEqual(self.filas_por_clave(clave), [])

    @con_hermes_falso
    def test_ack_perdido_deja_la_tarjeta_y_la_vuelta_la_halla(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        tarjeta = asegurar_tarjeta(puerto, clave, SPEC)
        otro = self.adaptador()
        hallada = asegurar_tarjeta(otro, clave, SPEC)
        self.assertEqual(hallada.id, tarjeta.id)
        self.assertEqual(otro.creaciones, [])
        self.assertEqual(len(self.filas_por_clave(clave)), 1)


class Exclusividad(BaseDeAdaptador):
    @con_hermes_falso
    def test_dos_escritores_solo_dejan_una_tarjeta(self):
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        puerto_a = self.adaptador()
        puerto_b = self.adaptador()
        dentro_a = threading.Event()
        libera_a = threading.Event()
        salio_a = threading.Event()
        salio_b = threading.Event()
        errores = []

        def corrida_a():
            try:
                with puerto_a.write_txn() as escritura:
                    escritura.buscar_o_crear(clave, SPEC)
                    dentro_a.set()
                    self.assertTrue(libera_a.wait(5))
            except BaseException as exc:
                errores.append(exc)
            finally:
                # La conexion nace en este hilo y sqlite solo deja cerrarla aqui.
                puerto_a.cerrar()
                salio_a.set()

        def corrida_b():
            try:
                asegurar_tarjeta(puerto_b, clave, SPEC)
            except BaseException as exc:
                errores.append(exc)
            finally:
                puerto_b.cerrar()
                salio_b.set()

        hilo_a = threading.Thread(target=corrida_a)
        hilo_b = threading.Thread(target=corrida_b)
        hilo_a.start()
        self.assertTrue(dentro_a.wait(2))
        hilo_b.start()
        time.sleep(0.3)
        self.assertFalse(salio_b.is_set(), "b termino mientras a tenia la txn")
        libera_a.set()
        self.assertTrue(salio_a.wait(5))
        self.assertTrue(salio_b.wait(5))
        hilo_a.join(2)
        hilo_b.join(2)
        self.assertEqual(errores, [])
        self.assertEqual(len(self.filas_por_clave(clave)), 1)
        self.assertEqual(sorted(puerto_a.creaciones + puerto_b.creaciones), [clave])


class Estados(BaseDeAdaptador):
    @con_hermes_falso
    def test_cada_estado_del_kanban_real_mapea_al_del_puerto(self):
        mapeo = {
            "triage": "pendiente",
            "todo": "pendiente",
            "scheduled": "pendiente",
            "ready": "pendiente",
            "running": "corriendo",
            "review": "corriendo",
            "done": "completada",
            "blocked": "bloqueada",
            "archived": "archivada",
        }
        puerto = self.adaptador()
        for bruto, esperado in mapeo.items():
            with self.subTest(bruto=bruto):
                clave = clave_de_tarjeta("root-1", f"unit-{bruto}", 1)
                asegurar_tarjeta(puerto, clave, SPEC)
                self.fijar_estado(clave, bruto)
                tarjeta = puerto.leer_estado(clave)
                self.assertIsNotNone(tarjeta, bruto)
                self.assertEqual(tarjeta.estado, esperado, bruto)

    @con_hermes_falso
    def test_un_estado_desconocido_no_rompe_la_lectura(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        asegurar_tarjeta(puerto, clave, SPEC)
        self.fijar_estado(clave, "un-estado-futuro")
        tarjeta = puerto.leer_estado(clave)
        self.assertEqual(tarjeta.estado, "pendiente")

    @con_hermes_falso
    def test_una_clave_sin_tarjeta_lee_none(self):
        puerto = self.adaptador()
        self.assertIsNone(puerto.leer_estado(clave_de_tarjeta("root-x", "unit-x", 9)))


class Resultado(BaseDeAdaptador):
    @con_hermes_falso
    def test_el_resultado_de_la_tarjeta_llega_como_documento(self):
        documento = '{"resultContract":"review.v1","resultDigest":"sha256:ab","verdict":"approved","evidenceRef":{"ref":"artifact:x"}}'
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        asegurar_tarjeta(puerto, clave, SPEC)
        self.fijar_estado(clave, "done", documento)
        carga = puerto.leer_estado(clave).resultado
        self.assertEqual(carga["resultContract"], "review.v1")
        self.assertEqual(carga["verdict"], "approved")

    @con_hermes_falso
    def test_un_resultado_que_no_es_documento_sale_none(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        asegurar_tarjeta(puerto, clave, SPEC)
        for bruto in (None, "", "no es json", "[1, 2]", '"texto"'):
            with self.subTest(bruto=bruto):
                self.fijar_estado(clave, "done", bruto)
                self.assertIsNone(puerto.leer_estado(clave).resultado, bruto)


class Escrituras(BaseDeAdaptador):
    @con_hermes_falso
    def test_vincular_completar_y_bloquear_dentro_de_txn_se_aplican(self):
        puerto = self.adaptador()
        clave_a = clave_de_tarjeta("r", "a", 1)
        clave_b = clave_de_tarjeta("r", "b", 1)
        with puerto.write_txn() as escritura:
            tarjeta_a = escritura.buscar_o_crear(clave_a, SPEC)
            tarjeta_b = escritura.buscar_o_crear(clave_b, SPEC)
            escritura.vincular(tarjeta_b.id, tarjeta_a.id)
            escritura.completar(
                tarjeta_a.id, {"contrato": "review.v1", "verdict": "approved"}
            )
            escritura.bloquear(tarjeta_b.id, "raiz cancelada")
        estado_a = self.filas_por_clave(clave_a)[0]
        estado_b = self.filas_por_clave(clave_b)[0]
        self.assertEqual(estado_a["status"], "done")
        self.assertIn("approved", estado_a["result"] or "")
        self.assertEqual(estado_b["status"], "blocked")
        from hermes_cli.kanban_db_connect import connect

        with closing(connect(self.ruta_db)) as conexion:
            vinculo = conexion.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (tarjeta_b.id,)
            ).fetchone()
        self.assertIsNotNone(vinculo)
        self.assertEqual(vinculo["parent_id"], tarjeta_a.id)

    @con_hermes_falso
    def test_una_excepcion_tras_las_escrituras_no_aplica_nada(self):
        puerto = self.adaptador()
        clave_a = clave_de_tarjeta("r", "a", 1)
        clave_b = clave_de_tarjeta("r", "b", 1)
        with self.assertRaises(RuntimeError), puerto.write_txn() as escritura:
            tarjeta_a = escritura.buscar_o_crear(clave_a, SPEC)
            escritura.bloquear(tarjeta_a.id, "no debe quedar")
            escritura.buscar_o_crear(clave_b, SPEC)
            raise RuntimeError("cayo el despacho")
        self.assertEqual(self.filas_por_clave(clave_a), [])
        self.assertEqual(self.filas_por_clave(clave_b), [])
        self.assertEqual(puerto.creaciones, [])

    @con_hermes_falso
    def test_bloquear_sin_txn_propia_se_aplica_en_el_momento(self):
        from hermes_cli.kanban_db_connect import connect

        puerto = self.adaptador()
        clave = clave_de_tarjeta("r", "a", 1)
        tarjeta = asegurar_tarjeta(puerto, clave, SPEC)
        with puerto.write_txn() as escritura:
            escritura.bloquear(tarjeta.id, "dependencia caida")
        with closing(connect(self.ruta_db)) as conexion:
            fila = conexion.execute(
                "SELECT status FROM tasks WHERE id = ?", (tarjeta.id,)
            ).fetchone()
        self.assertEqual(fila["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
