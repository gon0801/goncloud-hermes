#!/usr/bin/env python3
"""El adaptador contra el kanban real: pruebas que solo corren en la PC (SUA-07 H2).

Certifican en hermes_cli vivo lo que el diseno fija para el par (version,
code_sha): deduplicacion por idempotency_key con archivadas, exclusion de
write_txn con dos escritores, y link_tasks/complete_task/block_task. En la Mac
quedan saltadas: hermes_cli no esta instalado y el doble no sustituye esta
certificacion.
"""

import importlib.util
import sys
import tempfile
import threading
from contextlib import closing
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
# Como tests/test_workflow.py: hermes_cli importa módulos de la raíz del runtime (hermes_yaml).
sys.path.insert(0, str(Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

kanban_port = importlib.import_module("raices.kanban_port")
SpecDeTarjeta = kanban_port.SpecDeTarjeta
asegurar_tarjeta = kanban_port.asegurar_tarjeta
clave_de_tarjeta = kanban_port.clave_de_tarjeta

SPEC = SpecDeTarjeta("root/unit#1", "instrucciones", "codex")

HAY_HERMES = importlib.util.find_spec("hermes_cli") is not None


@unittest.skipUnless(HAY_HERMES, "hermes_cli solo esta instalado en la PC")
class KanbanReal(unittest.TestCase):
    def setUp(self):
        from hermes_cli.kanban_db_connect import connect

        self.kanban = importlib.import_module("raices.kanban")
        self.kanban_db = importlib.import_module("hermes_cli.kanban_db")
        self.connect = connect
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.temporal.cleanup)
        self.ruta_db = Path(self.temporal.name) / "kanban.db"

    def adaptador(self):
        puerto = self.kanban.KanbanHermes(self.ruta_db)
        self.addCleanup(puerto.cerrar)
        return puerto

    def filas_por_clave(self, clave):
        with closing(self.connect(self.ruta_db)) as conexion:
            return conexion.execute(
                "SELECT * FROM tasks WHERE idempotency_key = ?", (clave,)
            ).fetchall()

    def test_dedupe_con_archivadas_y_una_sola_tarjeta_por_clave(self):
        puerto = self.adaptador()
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        primera = asegurar_tarjeta(puerto, clave, SPEC)
        self.assertEqual(asegurar_tarjeta(puerto, clave, SPEC).id, primera.id)
        self.assertEqual(puerto.creaciones, [clave])
        with closing(self.connect(self.ruta_db)) as conexion:
            self.assertTrue(self.kanban_db.archive_task(conexion, primera.id))
        hallada = asegurar_tarjeta(puerto, clave, SPEC)
        self.assertEqual(hallada.id, primera.id)
        self.assertEqual(hallada.estado, "archivada")
        self.assertEqual(len(self.filas_por_clave(clave)), 1)

    def test_write_txn_excluye_al_segundo_escritor(self):
        clave = clave_de_tarjeta("root-1", "unit-c", 1)
        puerto_a = self.adaptador()
        puerto_b = self.adaptador()
        dentro_a = threading.Event()
        libera_a = threading.Event()
        salieron = threading.Event()
        errores = []

        def corrida_a():
            try:
                with puerto_a.write_txn() as escritura:
                    escritura.buscar_o_crear(clave, SPEC)
                    dentro_a.set()
                    self.assertTrue(libera_a.wait(10))
            except BaseException as exc:
                errores.append(exc)
            finally:
                # La conexión nace en este hilo y sqlite solo deja cerrarla aquí.
                puerto_a.cerrar()
                salieron.set()

        def corrida_b():
            try:
                asegurar_tarjeta(puerto_b, clave, SPEC)
            except BaseException as exc:
                errores.append(exc)
            finally:
                puerto_b.cerrar()
                salieron.set()

        hilo_a = threading.Thread(target=corrida_a)
        hilo_b = threading.Thread(target=corrida_b)
        hilo_a.start()
        self.assertTrue(dentro_a.wait(5))
        hilo_b.start()
        time.sleep(0.3)
        self.assertFalse(salieron.is_set(), "b entro mientras a tenia la txn")
        libera_a.set()
        hilo_a.join(10)
        hilo_b.join(10)
        self.assertEqual(errores, [])
        self.assertEqual(len(self.filas_por_clave(clave)), 1)

    def test_link_complete_y_block_del_puerto_sobre_el_kanban_real(self):
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
        filas = {
            f["idempotency_key"]: f
            for f in self.filas_por_clave(clave_a) + self.filas_por_clave(clave_b)
        }
        self.assertEqual(filas[clave_a]["status"], "done")
        self.assertIn("approved", filas[clave_a]["result"])
        self.assertEqual(filas[clave_b]["status"], "blocked")
        with closing(self.connect(self.ruta_db)) as conexion:
            vinculo = conexion.execute(
                "SELECT parent_id FROM task_links WHERE child_id = ?", (tarjeta_b.id,)
            ).fetchone()
        self.assertIsNotNone(vinculo)
        self.assertEqual(vinculo["parent_id"], tarjeta_a.id)


if __name__ == "__main__":
    unittest.main()
