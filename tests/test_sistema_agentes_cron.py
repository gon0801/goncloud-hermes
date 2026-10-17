#!/usr/bin/env python3
"""El script del cron del ticker: una vuelta, salida 0/1, ruta cerrada no corre.

El subprocess prueba el arranque tal como lo vera el cron de Hermes (PYTHONPATH
con el doble de hermes_cli y HERMES_HOME temporal); los caminos inyectables
(ruta cerrada, error) se prueban en el proceso.
"""

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

import hermes_falso  # noqa: E402
from conexiones import rastrear_conexiones  # noqa: E402

SCRIPT = REPO / "plugins/goncloud-team/raices/cron_ticker.py"
PLUGIN = REPO / "plugins/goncloud-team"
DOBLE = REPO / "tests/fake_hermes_cli"

RAIZ = "root-cron"
DIGEST = "sha256:" + "a" * 64

META = {
    "schema": "sistema-agentes.meta.v1",
    "rootId": RAIZ,
    "requester": {"principal": ["david"], "agentId": "main"},
    "authority": {
        "schema": "sistema-agentes.autoridad.v1",
        "authorityRef": "run:turn-one",
        "grantedBy": "david",
        "grantedAt": "2026-10-17T00:00:00Z",
        "actions": ["read", "implement"],
        "repos": ["owner-01/repo-a"],
        "accounts": [],
        "destinations": [],
        "deadline": None,
        "mode": "implement",
    },
    "budget": {
        "maxConcurrentTasks": 8,
        "maxDepth": 8,
        "maxChildren": 32,
        "maxModelCalls": 320,
        "maxInputTokens": 1000000,
        "maxOutputTokens": 100000,
        "maxCacheReadTokens": 1000000,
        "maxContextTokens": 100000,
        "maxTreeTokens": 1100000,
        "maxAutomaticRecoveryCalls": 8,
    },
    "plan": {
        "revision": 1,
        "planDigest": DIGEST,
        "requestDigest": DIGEST,
        "units": [
            {
                "unitId": "a",
                "title": "Unidad a",
                "dependsOn": [],
                "criteria": [
                    {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                ],
                "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
            }
        ],
        "assumptions": [],
    },
    "controlEpoch": 1,
    "control": {"state": "active", "epoch": 1, "reason": None},
    "qualityPolicy": {"version": "quality-kit.regla4"},
    "createdAt": "2026-10-17T00:00:00Z",
}

VINCULO = {
    "unitId": "a",
    "assignment": {
        "target": {"kind": "cli", "adapterId": "codex"},
        "instructionRef": {"ref": "artifact:inst", "digest": DIGEST},
        "resultContract": "review.v1",
        "inputRevision": {
            "kind": "code",
            "repository": "owner-01/repo-a",
            "sha": "0" * 40,
        },
        "continuation": {"kind": "requester"},
    },
    "writers": [],
}


def decidir_dictamen(tienda, root_id, hecho):
    decidir = importlib.import_module("raices.decidir")
    vista = tienda.leer_vista(root_id, hecho)
    propuesta = decidir.decidir(vista)
    return tienda.aplicar(root_id, propuesta.decision, "dec-prueba")


class Base(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.home = Path(self.temporal.name) / "hermes"
        self.home.mkdir()
        parche = mock.patch.dict("os.environ", {"HERMES_HOME": str(self.home)})
        parche.start()
        self.addCleanup(parche.stop)


class Script(Base):
    PERMISOS = {
        "acciones": ["read", "implement", "review"],
        "toolsets": {"codex": ["read", "write"]},
        "comandos": ["git", "python3"],
    }

    def instalar_permisos(self):
        raices = self.home / "raices"
        raices.mkdir(exist_ok=True)
        (raices / "permisos.json").write_text(
            json.dumps(self.PERMISOS), encoding="utf-8"
        )

    def test_una_vuelta_despacha_la_intencion_pendiente_y_sale_silencioso(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        self.instalar_permisos()
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.home / "sistema-agentes.db")
        tienda.abrir_raiz(META)
        hecho = {
            "principal": [],
            "asignaciones": [
                {"unitId": "a", "assignment": VINCULO["assignment"], "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        dictamen = decidir_dictamen(tienda, RAIZ, hecho)
        self.assertFalse(hasattr(dictamen, "codigo"))
        intenciones = tienda.intenciones_pendientes()
        self.assertEqual([(i.unit_id, i.attempt) for i in intenciones], [("a", 1)])
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            # La puerta real cierra en la Mac (no hay gateway vivo): la
            # inyección deja la vuelta como era antes de H4a.
            codigo = cron.correr(ruta_abierta=lambda: (True, None))
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")

        kanban = importlib.import_module("raices.kanban")
        puerto = kanban.KanbanHermes(self.home / "kanban.db")
        self.addCleanup(puerto.cerrar)
        tarjeta = puerto.leer_estado(f"sa:{RAIZ}:a:1")
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "pendiente")

    def test_sin_hermes_home_sale_en_error(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            cron = importlib.import_module("raices.cron_ticker")
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.main()
        self.assertEqual(codigo, 1)
        self.assertIn("ERROR ticker raices:", salida.getvalue())

    def test_la_ruta_cerrada_no_corre_y_no_abre_nada(self):
        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            codigo = cron.correr(
                ruta_abierta=lambda: (False, "capacidad-faltante:write_txn")
            )
        self.assertEqual(codigo, 0)
        texto = salida.getvalue()
        self.assertIn("ruta de raices cerrada (capacidad-faltante:write_txn)", texto)
        self.assertIn("CAPACIDAD_INCIERTA (capacidad-faltante:write_txn)", texto)
        self.assertFalse((self.home / "sistema-agentes.db").exists())

    def test_la_ruta_cerrada_por_version_concilia_sin_despachar(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        self.instalar_permisos()
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.home / "sistema-agentes.db")
        tienda.abrir_raiz(META)
        hecho = {
            "principal": [],
            "asignaciones": [
                {"unitId": "a", "assignment": VINCULO["assignment"], "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        dictamen = decidir_dictamen(tienda, RAIZ, hecho)
        self.assertFalse(hasattr(dictamen, "codigo"))
        intenciones = tienda.intenciones_pendientes()
        self.assertEqual([(i.unit_id, i.attempt) for i in intenciones], [("a", 1)])
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            codigo = cron.correr(ruta_abierta=lambda: (False, "version-no-certificada"))
        self.assertEqual(codigo, 0)
        texto = salida.getvalue()
        self.assertIn("CAPACIDAD_INCIERTA (version-no-certificada)", texto)
        self.assertIn("ruta de raices cerrada (version-no-certificada)", texto)
        self.assertTrue((self.home / "sistema-agentes.db").exists())

        kanban = importlib.import_module("raices.kanban")
        puerto = kanban.KanbanHermes(self.home / "kanban.db")
        self.addCleanup(puerto.cerrar)
        self.assertIsNone(puerto.leer_estado(f"sa:{RAIZ}:a:1"))
        tienda2 = store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(tienda2.cerrar)
        self.assertEqual(
            [(i.unit_id, i.attempt) for i in tienda2.intenciones_pendientes()],
            [("a", 1)],
        )

    def test_un_fallo_de_la_vuelta_sale_en_error(self):
        cron = importlib.import_module("raices.cron_ticker")
        (self.home / "sistema-agentes.db").mkdir()
        # La puerta real cerraría antes de tocar el store: con la puerta
        # abierta la vuelta llega al store roto y main lo reporta.
        with mock.patch.object(
            cron, "ruta_de_raices_abierta", return_value=(True, None)
        ):
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.main()
        self.assertEqual(codigo, 1)
        self.assertIn("ERROR ticker raices:", salida.getvalue())

    def test_el_kanban_respeta_hermes_kanban_db(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        otra = self.home / "otro" / "kanban.db"
        with mock.patch.dict("os.environ", {"HERMES_KANBAN_DB": str(otra)}):
            cron = importlib.import_module("raices.cron_ticker")
            self.assertEqual(cron.ruta_del_kanban(self.home), otra)
            self.assertEqual(
                cron.ruta_del_kanban(self.home), Path(os.environ["HERMES_KANBAN_DB"])
            )


class Arranque(Base):
    def probar_subprocess(self, extra_env=None):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HERMES_HOME": str(self.home),
            "PYTHONPATH": f"{DOBLE}:{PLUGIN}",
            "PYTHONUTF8": "1",
        }
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_el_script_arranca_solo_con_el_doble_en_el_pythonpath(self):
        # Sin gateway vivo la puerta real cierra por par-incierto: el script
        # igual recorre todo (capacidades del doble, aviso, store, vuelta
        # cerrada) y sale 0 sin crear tarjetas.
        proceso = self.probar_subprocess()
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertIn("CAPACIDAD_INCIERTA (par-incierto)", proceso.stdout)
        self.assertIn("ruta de raices cerrada (par-incierto)", proceso.stdout)
        self.assertTrue((self.home / "sistema-agentes.db").exists())

    def test_el_script_sin_hermes_home_dice_error_y_sale_1(self):
        proceso = self.probar_subprocess({"HERMES_HOME": ""})
        self.assertEqual(proceso.returncode, 1, proceso.stderr)
        self.assertIn("ERROR ticker raices:", proceso.stdout)


if __name__ == "__main__":
    unittest.main()
