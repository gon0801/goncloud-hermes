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
from contextlib import redirect_stderr, redirect_stdout
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

    def instalar_permisos(self, hogar=None, datos=None):
        raices = Path(hogar or self.home) / "raices"
        raices.mkdir(parents=True, exist_ok=True)
        (raices / "permisos.json").write_text(
            json.dumps(self.PERMISOS if datos is None else datos), encoding="utf-8"
        )

    def dejar_intencion_pendiente(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
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
        return tienda

    def tarjeta_en_el_kanban(self, base=None):
        kanban = importlib.import_module("raices.kanban")
        puerto = kanban.KanbanHermes(Path(base or self.home) / "kanban.db")
        self.addCleanup(puerto.cerrar)
        return puerto.leer_estado(f"sa:{RAIZ}:a:1")

    def test_una_vuelta_despacha_la_intencion_pendiente_y_sale_silencioso(self):
        tienda = self.dejar_intencion_pendiente()
        self.instalar_permisos()
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")

        tarjeta = self.tarjeta_en_el_kanban()
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "pendiente")

    def test_sin_permisos_json_la_vuelta_no_despacha_y_falla_cerrado(self):
        tienda = self.dejar_intencion_pendiente()
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")

        self.assertIsNone(self.tarjeta_en_el_kanban())
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        vista = tienda.leer_vista(RAIZ, {})
        causas = [
            e["subject"]["cause"]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "attempt"
        ]
        self.assertEqual(causas, ["permiso-h:sin-permisos"])

    def test_herramientas_y_el_cron_leen_los_mismos_permisos(self):
        tienda = self.dejar_intencion_pendiente()
        tienda.cerrar()
        # Permisos que NO cubren el perfil codex: si el cron y las herramientas
        # leen el mismo archivo, el gate de arranque niega con la causa propia.
        self.instalar_permisos(datos={"acciones": ["read"]})

        herramientas = importlib.import_module("raices.herramientas")
        hogar = importlib.import_module("raices.hogar")
        with mock.patch.dict("os.environ", {"HERMES_HOME": str(self.home)}):
            tienda_de_herramientas = herramientas.abrir_tienda()
            self.addCleanup(tienda_de_herramientas.cerrar)
            self.assertEqual(
                tienda_de_herramientas.permisos_h_datos,
                hogar.leer_permisos(str(self.home)),
            )
            self.assertEqual(
                tienda_de_herramientas.permisos_h_datos, {"acciones": ["read"]}
            )
            cron = importlib.import_module("raices.cron_ticker")
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertIsNone(self.tarjeta_en_el_kanban())
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        vista = tienda.leer_vista(RAIZ, {})
        causas = [
            e["subject"]["cause"]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "attempt"
        ]
        self.assertEqual(causas, ["permiso-h:perfil-sin-toolset"])

    def test_permisos_con_tipos_rotos_fallan_cerrado(self):
        hogar = importlib.import_module("raices.hogar")
        rotos = ({"toolsets": []}, {"acciones": 3}, {"comandos": "git"}, [])
        for datos in rotos:
            with self.subTest(datos=datos):
                self.instalar_permisos(datos=datos)
                self.assertIsNone(hogar.leer_permisos(str(self.home)))

    def test_permisos_rotos_el_cron_no_revienta_y_no_despacha(self):
        tienda = self.dejar_intencion_pendiente()
        self.instalar_permisos(datos={"acciones": 3})
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida = StringIO()
        with redirect_stdout(salida):
            codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")
        self.assertIsNone(self.tarjeta_en_el_kanban())

    def test_sin_hermes_home_sale_en_error(self):
        # Sin hermes_constants: en la PC el real existiria y get_hermes_home
        # caeria al default de la plataforma; lo probado aqui es la guarda del
        # fallback sin home definido.
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.dict(sys.modules, {"hermes_constants": None}),
        ):
            cron = importlib.import_module("raices.cron_ticker")
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.main()
        self.assertEqual(codigo, 1)
        self.assertIn("ERROR ticker raices:", salida.getvalue())

    def test_la_ruta_cerrada_no_corre_y_no_abre_nada(self):
        cron = importlib.import_module("raices.cron_ticker")
        salida, errores = StringIO(), StringIO()
        with redirect_stdout(salida), redirect_stderr(errores):
            codigo = cron.correr(ruta_abierta=lambda: (False, "version-no-certificada"))
        self.assertEqual(codigo, 0)
        self.assertEqual(
            salida.getvalue(),
            "",
            "el stdout del no_agent se entrega: la ruta cerrada no es ruido",
        )
        self.assertIn(
            "ruta de raices cerrada (version-no-certificada)", errores.getvalue()
        )
        self.assertFalse((self.home / "sistema-agentes.db").exists())

    def test_un_fallo_de_la_vuelta_sale_en_error(self):
        cron = importlib.import_module("raices.cron_ticker")
        (self.home / "sistema-agentes.db").mkdir()
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


class StoreCompartido(Base):
    """F6: el store vive en la RAIZ del home de Hermes, una sola ruta para el
    perfil claw (el gateway, que no define HERMES_HOME: resuelve el home con
    un override de contexto) y el cron del perfil default (que si lo trae)."""

    def setUp(self):
        super().setUp()
        self.raiz = Path(self.temporal.name) / "hermes-raiz"
        self.perfil_claw = self.raiz / "profiles" / "claw"
        self.perfil_claw.mkdir(parents=True)

    def instalar_doble(self):
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        return sys.modules["hermes_constants"]

    def test_un_home_de_perfil_sube_a_la_raiz_sin_hermes_constants(self):
        constants = self.instalar_doble()
        constants.set_override(None)
        with (
            mock.patch.dict(sys.modules, {"hermes_constants": None}),
            mock.patch.dict("os.environ", {"HERMES_HOME": str(self.perfil_claw)}),
        ):
            herramientas = importlib.import_module("raices.herramientas")
            self.assertEqual(
                herramientas.ruta_del_store(),
                self.raiz / "sistema-agentes.db",
            )

    def test_herramientas_de_claw_y_cron_de_la_raiz_abren_el_mismo_store(self):
        constants = self.instalar_doble()
        herramientas = importlib.import_module("raices.herramientas")
        # El gateway sirve a claw: sin HERMES_HOME en el entorno, solo el
        # override de contexto resuelve el home del perfil.
        constants.set_override(str(self.perfil_claw))
        with mock.patch.dict("os.environ", {}, clear=True):
            desde_claw = herramientas.ruta_del_store()
        # El cron del perfil default llega con HERMES_HOME = la raiz.
        constants.set_override(None)
        with mock.patch.dict("os.environ", {"HERMES_HOME": str(self.raiz)}):
            desde_el_cron = herramientas.ruta_del_store()
        self.assertEqual(desde_claw, desde_el_cron)
        self.assertEqual(desde_claw, self.raiz / "sistema-agentes.db")

    def test_submit_de_claw_y_una_vuelta_del_ticker_despachan_la_raiz(self):
        constants = self.instalar_doble()
        # 1) El gateway sirve a claw: solo el override de contexto.
        constants.set_override(str(self.perfil_claw))
        herramientas = importlib.import_module("raices.herramientas")
        with mock.patch.dict("os.environ", {}, clear=True):
            recibo = herramientas.enviar_raiz({"plan": META}, "claw")
        self.assertNotIn("error", recibo)
        # 2) La decision con el hecho de asignacion, como la aplicaria el
        #    gateway: reabrir por la raiz tambien demuestra donde escribio el
        #    submit.
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.raiz / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
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
        # 3) El cron del perfil default: HERMES_HOME = la raiz, sin override.
        #    Los permisos de H (I07) viven en <raiz>/raices/permisos.json.
        constants.set_override(None)
        raices = self.raiz / "raices"
        raices.mkdir()
        (raices / "permisos.json").write_text(
            json.dumps(
                {
                    "acciones": ["read", "implement", "review"],
                    "toolsets": {"codex": ["read", "write"]},
                    "comandos": ["git", "python3"],
                }
            ),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {"HERMES_HOME": str(self.raiz)}):
            cron = importlib.import_module("raices.cron_ticker")
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")
        # 4) La tarjeta quedo en el kanban de la raiz y el perfil queda limpio.
        kanban = importlib.import_module("raices.kanban")
        puerto = kanban.KanbanHermes(self.raiz / "kanban.db")
        self.addCleanup(puerto.cerrar)
        tarjeta = puerto.leer_estado(f"sa:{RAIZ}:a:1")
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "pendiente")
        self.assertFalse((self.perfil_claw / "sistema-agentes.db").exists())

    def test_el_cron_de_un_home_de_perfil_lee_los_permisos_de_la_raiz(self):
        # I07: los permisos viven junto al store, en la RAIZ. Un cron cuyo
        # HERMES_HOME es un home de perfil (nunca HERMES_HOME pelado) sube por
        # hogar.raiz_del_hogar y los halla; si alguien leyera el entorno
        # pelado, perfiles/claw/raices no existe y el gate falla cerrado.
        constants = self.instalar_doble()
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.raiz / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
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
        tienda.cerrar()

        raices = self.raiz / "raices"
        raices.mkdir()
        (raices / "permisos.json").write_text(
            json.dumps(
                {
                    "acciones": ["read", "implement", "review"],
                    "toolsets": {"codex": ["read", "write"]},
                    "comandos": ["git", "python3"],
                }
            ),
            encoding="utf-8",
        )

        constants.set_override(None)
        with mock.patch.dict("os.environ", {"HERMES_HOME": str(self.perfil_claw)}):
            cron = importlib.import_module("raices.cron_ticker")
            salida = StringIO()
            with redirect_stdout(salida):
                codigo = cron.correr()
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")

        kanban = importlib.import_module("raices.kanban")
        puerto = kanban.KanbanHermes(self.raiz / "kanban.db")
        self.addCleanup(puerto.cerrar)
        tarjeta = puerto.leer_estado(f"sa:{RAIZ}:a:1")
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "pendiente")


class Arranque(Base):
    def entorno(self, extra=None):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HERMES_HOME": str(self.home),
            # F7: ':' no separa en Windows; el doble debe quedar importable
            # con el separador real de la plataforma.
            "PYTHONPATH": os.pathsep.join([str(DOBLE), str(PLUGIN)]),
            "PYTHONUTF8": "1",
        }
        env.update(extra or {})
        return env

    def probar_subprocess(self, extra_env=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=self.entorno(extra_env),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_el_script_arranca_solo_con_el_doble_en_el_pythonpath(self):
        proceso = self.probar_subprocess()
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertEqual(proceso.stdout, "")
        self.assertTrue((self.home / "sistema-agentes.db").exists())

    def test_el_doble_de_hermes_cli_importa_con_ese_pythonpath(self):
        # F7: con un store vacio la vuelta nunca importa hermes_cli y el
        # arranque pasa aunque el PYTHONPATH no sirva; la sonda si lo prueba.
        proceso = subprocess.run(
            [
                sys.executable,
                "-c",
                "import pathlib, hermes_cli.kanban_db; "
                "print(pathlib.Path(hermes_cli.kanban_db.__file__).parent.parent)",
            ],
            env=self.entorno(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertEqual(Path(proceso.stdout.strip()), DOBLE)

    def test_el_script_sin_hermes_home_dice_error_y_sale_1(self):
        proceso = self.probar_subprocess({"HERMES_HOME": ""})
        self.assertEqual(proceso.returncode, 1, proceso.stderr)
        self.assertIn("ERROR ticker raices:", proceso.stdout)


if __name__ == "__main__":
    unittest.main()
