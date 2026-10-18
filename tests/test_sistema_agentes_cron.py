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
from yaml_doble import YamlDoble, yaml_real  # noqa: E402


def yaml_de_prueba():
    """El PyYAML real donde esté; si no, el doble del subset."""
    return yaml_real or YamlDoble


SCRIPT = REPO / "plugins/goncloud-team/raices/cron_ticker.py"
PLUGIN = REPO / "plugins/goncloud-team"
DOBLE = REPO / "tests/fake_hermes_cli"

RAIZ = "root-cron"
DIGEST = "sha256:" + "a" * 64
# El payload que imprime `hermes send --json` para Telegram, medido en la
# fuente de la PC: _success(...) con message_id como TEXTO de dígitos.
PAYLOAD_REAL = (
    '{"success": true, "platform": "telegram", "chat_id": "6470689715",'
    ' "message_id": "8123"}'
)

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
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
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
        # La puerta real de H4a cierra en la Mac (no hay gateway vivo): el
        # comportamiento general se prueba con la puerta inyectada abierta y
        # las clases de la puerta la cierran a propósito.
        puerta = mock.patch("raices.capacidades.abrir_ruta", return_value=(True, None))
        puerta.start()
        self.addCleanup(puerta.stop)
        # La interseccion de toolsets (H4b3) lee el config.yaml con el PyYAML
        # real donde este; en la Mac de pruebas, el doble del subset.
        permisos = importlib.import_module("raices.permisos")
        yaml_parche = mock.patch.object(permisos, "yaml", yaml_de_prueba())
        yaml_parche.start()
        self.addCleanup(yaml_parche.stop)


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
        # El despacho exige el paquete poteto del perfil (H4b2, I08).
        perfil = self.home / "profiles" / "codex"
        perfil.mkdir(parents=True, exist_ok=True)
        (perfil / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 1,
                    "poteto": {"digest": "sha256:" + "b" * 64},
                }
            ),
            encoding="utf-8",
        )
        # Los toolsets efectivos son la intersección con el config real del
        # perfil (H4b3): sin config, codex queda sin toolsets y el gate niega
        # el despacho.
        perfil = self.home / "profiles" / "codex"
        perfil.mkdir(parents=True, exist_ok=True)
        (perfil / "config.yaml").write_text(
            "platform_toolsets:\n  cli:\n    - coding\n    - kanban\n",
            encoding="utf-8",
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
        permisos = importlib.import_module("raices.permisos")
        with redirect_stdout(salida):
            with mock.patch.object(permisos, "yaml", yaml_de_prueba()):
                # La puerta real cierra en la Mac (no hay gateway vivo): la
                # inyección deja la vuelta como era antes de H4a.
                codigo = cron.correr(ruta_abierta=lambda: (True, None))
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
            # Emisor inyectado: la excepción del arranque no sale por Telegram.
            codigo = cron.correr(enviar=lambda _texto: 42)
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
                codigo = cron.correr(enviar=lambda _texto: 42)
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
            codigo = cron.correr(enviar=lambda _texto: 42)
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
            codigo = cron.correr(
                ruta_abierta=lambda: (False, "capacidad-faltante:write_txn")
            )
        self.assertEqual(codigo, 0)
        self.assertEqual(
            salida.getvalue(),
            "",
            "el stdout del no_agent se entrega: la ruta cerrada no es ruido",
        )
        texto = errores.getvalue()
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
        salida, errores = StringIO(), StringIO()
        with redirect_stdout(salida), redirect_stderr(errores):
            codigo = cron.correr(ruta_abierta=lambda: (False, "version-no-certificada"))
        self.assertEqual(codigo, 0)
        self.assertEqual(
            salida.getvalue(),
            "",
            "el stdout del no_agent se entrega: la ruta cerrada no es ruido",
        )
        texto = errores.getvalue()
        self.assertIn("CAPACIDAD_INCIERTA (version-no-certificada)", texto)
        self.assertIn("ruta de raices cerrada (version-no-certificada)", texto)
        self.assertTrue((self.home / "sistema-agentes.db").exists())
        # Con la puerta cerrada la pasada del emisor no corre: ni
        # proyeccion ni aviso, el CAPACIDAD_INCIERTA es el unico mensaje.
        self.assertFalse(
            (self.home / "raices" / "proyecciones" / f"{RAIZ}.json").exists()
        )

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
        # El despacho exige el paquete poteto del perfil (H4b2, I08) y los
        # toolsets efectivos la interseccion con el config real (H4b3); el
        # managed.json y el config viven en profiles bajo la raiz.
        perfil = self.raiz / "profiles" / "codex"
        perfil.mkdir(parents=True)
        (perfil / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 1,
                    "poteto": {"digest": "sha256:" + "b" * 64},
                }
            ),
            encoding="utf-8",
        )
        (perfil / "config.yaml").write_text(
            "platform_toolsets:\n  cli:\n    - coding\n    - kanban\n",
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
        # El despacho exige el paquete poteto del perfil (H4b2, I08) y los
        # toolsets efectivos la interseccion con el config real (H4b3); el
        # managed.json y el config viven en profiles bajo la raiz.
        perfil_codex = self.raiz / "profiles" / "codex"
        perfil_codex.mkdir(parents=True)
        (perfil_codex / "goncloud-managed.json").write_text(
            json.dumps(
                {
                    "source_revision": "abc",
                    "generation": 1,
                    "poteto": {"digest": "sha256:" + "b" * 64},
                }
            ),
            encoding="utf-8",
        )
        (perfil_codex / "config.yaml").write_text(
            "platform_toolsets:\n  cli:\n    - coding\n    - kanban\n",
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

    def test_cron_de_perfil_y_de_raiz_llegan_a_la_misma_puerta_y_permisos(self):
        # La puerta (H4a) y los permisos (I07/H4b3) se resuelven sobre la
        # RAIZ: correr el cron con el home del perfil claw y con la raiz
        # consulta el MISMO gateway_state.json y el MISMO permisos.json. Si
        # alguien leyera el home pelado, perfiles/claw no tiene raices/ y la
        # puerta cerraria y los permisos serian None.
        hogar = importlib.import_module("raices.hogar")
        capacidades = importlib.import_module("raices.capacidades")
        raices = self.raiz / "raices"
        raices.mkdir()
        (raices / "permisos.json").write_text(
            json.dumps(
                {
                    "acciones": ["read", "implement"],
                    "toolsets": {"codex": ["write"]},
                    "comandos": ["git"],
                }
            ),
            encoding="utf-8",
        )
        perfil_codex = self.raiz / "profiles" / "codex"
        perfil_codex.mkdir(parents=True)
        (perfil_codex / "config.yaml").write_text(
            "platform_toolsets:\n  cli:\n    - coding\n",
            encoding="utf-8",
        )
        # gateway_state.json vive en la raiz, nunca en el home del perfil.
        (self.raiz / "gateway_state.json").write_text(
            json.dumps(
                {
                    "kind": "hermes-gateway",
                    "gateway_state": "running",
                    "code_version": "0.21.5",
                    "code_sha": "b" * 40,
                }
            ),
            encoding="utf-8",
        )

        puertas, consultadas, permisos = [], [], []

        def capturar(home=None, **kwargs):
            # La puerta REAL por dentro: hechos_vivos + decidir, sin pasar
            # por el parche del Base.
            consultadas.append(Path(home))
            resultado = capacidades.decidir(capacidades.hechos_vivos(home=home))
            puertas.append(resultado)
            return resultado

        for hermes_home in (str(self.perfil_claw), str(self.raiz)):
            with mock.patch.object(capacidades, "abrir_ruta", capturar):
                cron = importlib.import_module("raices.cron_ticker")
                abierta, _causa = cron.ruta_de_raices_abierta(
                    hogar.raiz_del_hogar(hermes_home)
                )
                self.assertIsInstance(abierta, bool)
            permisos.append(hogar.leer_permisos(hermes_home))

        # La MISMA puerta: ambas corridas consultaron la raiz y la puerta
        # real decidio lo MISMO y cerrada (el gateway_state.json de la raiz
        # es el leido en ambos casos; la causa concreta depende del entorno:
        # capacidad-faltante sin doble, par-incierto con el).
        self.assertEqual(consultadas, [self.raiz, self.raiz])
        self.assertEqual(puertas[0], puertas[1])
        self.assertFalse(puertas[0][0])
        self.assertIsInstance(puertas[0][1], str)
        # Los MISMOS permisos: toolsets efectivos (write declarado y coding
        # real) leidos del permisos.json de la raiz en ambos casos.
        esperados = {
            "acciones": ["read", "implement"],
            "toolsets": {"codex": ["write"]},
            "comandos": ["git"],
        }
        self.assertEqual(permisos[0], esperados)
        self.assertEqual(permisos[0], permisos[1])


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
        # Sin gateway vivo la puerta real cierra por par-incierto: el script
        # igual recorre todo (capacidades del doble, aviso, store, vuelta
        # cerrada) y sale 0 sin crear tarjetas. El aviso va a stderr: el
        # stdout del no_agent se entrega como salida del agente.
        proceso = self.probar_subprocess()
        self.assertEqual(proceso.returncode, 0, proceso.stderr)
        self.assertEqual(proceso.stdout, "")
        self.assertIn("CAPACIDAD_INCIERTA (par-incierto)", proceso.stderr)
        self.assertIn("ruta de raices cerrada (par-incierto)", proceso.stderr)
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


class ProyeccionYAviso(Script):
    """F9: tras las vueltas del ticker, el cron corre la pasada del emisor
    (§3.5/§3.6): proyecta cada raíz en <raíz>/raices/proyecciones y avisa las
    excepciones desde el MISMO estado. El emisor va inyectado: nunca un
    mensaje real. Un fallo del envío o de la proyección deja errores y no
    tumba las vueltas."""

    def abrir_tienda(self):
        store = importlib.import_module("raices.store")
        tienda = store.Store(self.home / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        return tienda

    def test_el_cron_escribe_la_proyeccion_y_llama_al_emisor(self):
        tienda = self.dejar_intencion_pendiente()
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        mensajes = []
        salida, errores = StringIO(), StringIO()
        with redirect_stdout(salida), redirect_stderr(errores):
            codigo = cron.correr(enviar=lambda texto: mensajes.append(texto) or 42)
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")
        self.assertEqual(errores.getvalue(), "")
        ruta = self.home / "raices" / "proyecciones" / f"{RAIZ}.json"
        self.assertTrue(ruta.exists())
        documento = json.loads(ruta.read_text(encoding="utf-8"))
        self.assertEqual(documento["schema"], "sistema-agentes.proyeccion.v1")
        # Sin permisos.json el arranque se negó: esa excepción salió por el
        # emisor y el checkpoint avanzó con el messageId.
        self.assertEqual(len(mensajes), 1)
        self.assertIn("NECESITA_DAVID", mensajes[0])
        tienda = self.abrir_tienda()
        fila = tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE root_id = ?", (RAIZ,)
        )
        self.assertIsNotNone(fila["noticed_at"])

    def test_un_emisor_que_falla_deja_errores_y_el_cron_sigue(self):
        tienda = self.dejar_intencion_pendiente()
        self.instalar_permisos()
        decidir = importlib.import_module("raices.decidir")
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "INESPERADO",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "incident:x"},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        store = importlib.import_module("raices.store")
        self.assertIsInstance(tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        tienda.cerrar()

        cron = importlib.import_module("raices.cron_ticker")
        salida, errores = StringIO(), StringIO()
        with redirect_stdout(salida), redirect_stderr(errores):
            codigo = cron.correr(enviar=lambda _texto: None)
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")
        # La vuelta corrió igual: la tarjeta se despachó y la proyección se
        # escribió; solo el aviso quedó debiéndose.
        tarjeta = self.tarjeta_en_el_kanban()
        self.assertIsNotNone(tarjeta)
        self.assertEqual(tarjeta.estado, "pendiente")
        ruta = self.home / "raices" / "proyecciones" / f"{RAIZ}.json"
        self.assertTrue(ruta.exists())
        self.assertIn("el aviso no se confirmó", errores.getvalue())
        tienda = self.abrir_tienda()
        fila = tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (borrador["exceptionId"],),
        )
        self.assertIsNone(fila["noticed_at"])

    def test_una_proyeccion_que_falla_no_tumba_las_vueltas(self):
        tienda = self.dejar_intencion_pendiente()
        self.instalar_permisos()
        tienda.cerrar()
        # <raíz>/raices/proyecciones como archivo: el mkdir de la escritura
        # atómica revienta para cada raíz.
        raices = self.home / "raices"
        raices.mkdir(parents=True, exist_ok=True)
        (raices / "proyecciones").write_text("no soy un directorio")

        cron = importlib.import_module("raices.cron_ticker")
        salida, errores = StringIO(), StringIO()
        with redirect_stdout(salida), redirect_stderr(errores):
            codigo = cron.correr(enviar=lambda _texto: 42)
        self.assertEqual(codigo, 0)
        self.assertEqual(salida.getvalue(), "")
        self.assertIsNotNone(self.tarjeta_en_el_kanban())
        self.assertTrue(
            any(
                "proyeccion" in e.lower() or "proyección" in e
                for e in errores.getvalue().splitlines()
            ),
            errores.getvalue(),
        )


class EmisorDeTelegram(unittest.TestCase):
    """El emisor de producción (§3.5): `hermes send --to telegram --json` por
    stdin con el HERMES_HOME del perfil claw. Todo por inyección: nunca un
    subprocess real, nunca un mensaje real."""

    class Corrida:
        def __init__(self, returncode=0, stdout=""):
            self.returncode = returncode
            self.stdout = stdout

    def test_manda_por_stdin_con_el_hermes_home_del_perfil_claw(self):
        cron = importlib.import_module("raices.cron_ticker")
        with tempfile.TemporaryDirectory() as temporal:
            base = Path(temporal)
            llamadas = []

            def corredor(*args, **kwargs):
                llamadas.append((args, kwargs))
                return self.Corrida(stdout=PAYLOAD_REAL)

            enviar = cron.emisor_de_telegram(
                base, resolvedor=lambda _nombre: "/bin/hermes", corredor=corredor
            )
            self.assertEqual(enviar("⚠️ hola"), 8123)
        comando, kwargs = llamadas[0]
        self.assertEqual(
            comando[0], ["/bin/hermes", "send", "--to", "telegram", "--json"]
        )
        self.assertEqual(kwargs["input"], "⚠️ hola")
        self.assertEqual(kwargs["env"]["HERMES_HOME"], str(base / "profiles" / "claw"))

    def test_el_payload_real_de_hermes_da_el_message_id(self):
        # Medido en la fuente de Hermes (PC, hermes-agent): send_message_tool
        # imprime _success("telegram", chat_id, warnings,
        # message_id=str(last_msg.message_id)) — el id llega como TEXTO de
        # dígitos (tools/send_message_senders.py:305). Exit 0 si success o
        # skipped sin error; 1 si error.
        cron = importlib.import_module("raices.cron_ticker")
        enviar = cron.emisor_de_telegram(
            Path("/base"),
            resolvedor=lambda _nombre: "/bin/hermes",
            corredor=lambda *_a, **_k: self.Corrida(stdout=PAYLOAD_REAL),
        )
        self.assertEqual(enviar("hola"), 8123)
        por_int = cron.emisor_de_telegram(
            Path("/base"),
            resolvedor=lambda _nombre: "/bin/hermes",
            corredor=lambda *_a, **_k: self.Corrida(
                stdout='{"success": true, "message_id": 77}'
            ),
        )
        self.assertEqual(por_int("hola"), 77)

    def test_sin_hermes_resoluble_no_invoca_nada_y_devuelve_none(self):
        cron = importlib.import_module("raices.cron_ticker")

        def corredor(*_args, **_kwargs):
            raise AssertionError("sin ejecutable no se invoca nada")

        enviar = cron.emisor_de_telegram(
            Path("/base"), resolvedor=lambda _nombre: None, corredor=corredor
        )
        self.assertIsNone(enviar("hola"))

    def test_un_fallo_del_subprocess_devuelve_none(self):
        cron = importlib.import_module("raices.cron_ticker")

        def revienta(*_args, **_kwargs):
            raise OSError("sin gateway")

        enviar = cron.emisor_de_telegram(
            Path("/base"),
            resolvedor=lambda _nombre: "/bin/hermes",
            corredor=revienta,
        )
        self.assertIsNone(enviar("hola"))

        enviar_salida_mala = cron.emisor_de_telegram(
            Path("/base"),
            resolvedor=lambda _nombre: "/bin/hermes",
            corredor=lambda *_a, **_k: self.Corrida(returncode=1),
        )
        self.assertIsNone(enviar_salida_mala("hola"))

    def test_un_payload_sin_exito_o_sin_message_id_valido_devuelve_none(self):
        cron = importlib.import_module("raices.cron_ticker")
        por_payload = (
            "no soy json",
            "[]",
            '{"skipped": true}',
            '{"success": false, "error": "sin chat"}',
            '{"success": true, "error": "algo falló"}',
            '{"success": true, "message_id": "abc"}',
            '{"success": true, "message_id": "0"}',
            '{"success": true}',
            '{"message_id": 8123}',
            '{"success": "yes", "message_id": 8123}',
        )
        for payload in por_payload:
            with self.subTest(payload=payload):
                enviar = cron.emisor_de_telegram(
                    Path("/base"),
                    resolvedor=lambda _nombre: "/bin/hermes",
                    corredor=lambda *_a, **_k: self.Corrida(stdout=payload),
                )
                self.assertIsNone(enviar("hola"))

    def test_el_subprocess_va_con_encoding_utf8_y_errors_replace(self):
        # F16: text=True sin encoding en Windows con code page no UTF-8
        # revienta encodeando el cuerpo ("⚠️ ...") con UnicodeEncodeError
        # (ValueError), que nadie atrapaba.
        cron = importlib.import_module("raices.cron_ticker")
        grabados = {}

        def corredor(*args, **kwargs):
            grabados.update(kwargs)
            return self.Corrida(stdout=PAYLOAD_REAL)

        enviar = cron.emisor_de_telegram(
            Path("/base"), resolvedor=lambda _nombre: "/bin/hermes", corredor=corredor
        )
        self.assertEqual(enviar("⚠️ hola"), 8123)
        self.assertEqual(grabados["encoding"], "utf-8")
        self.assertEqual(grabados["errors"], "replace")

    def test_un_unicodeencodeerror_del_corredor_devuelve_none(self):
        cron = importlib.import_module("raices.cron_ticker")

        def corredor(*_args, **_kwargs):
            raise UnicodeEncodeError("cp1252", "⚠️", 0, 1, "invalid character")

        enviar = cron.emisor_de_telegram(
            Path("/base"),
            resolvedor=lambda _nombre: "/bin/hermes",
            corredor=corredor,
        )
        self.assertIsNone(enviar("⚠️ hola"))


if __name__ == "__main__":
    unittest.main()
