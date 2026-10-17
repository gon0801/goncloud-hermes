#!/usr/bin/env python3
"""Las herramientas goncloud_root_*: solo claw, campos del contrato, recibos.

Corren en la Mac con el doble de hermes_cli instalado: el plugin carga igual
que en la PC (workflow importa hermes_cli) y los manejadores tocan el store y
la base del kanban igual que en produccion.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

import hermes_falso  # noqa: E402
from conexiones import rastrear_conexiones  # noqa: E402

PLUGIN = REPO / "plugins/goncloud-team"

RAIZ = "root-tool"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}


def meta_de(root_id=RAIZ):
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": root_id,
        "requester": {"principal": ["david"], "agentId": "main"},
        "authority": {
            "schema": "sistema-agentes.autoridad.v1",
            "authorityRef": "run:turn-one",
            "grantedBy": "david",
            "grantedAt": "2026-10-17T00:00:00Z",
            "actions": ["read", "implement", "review", "push"],
            "repos": ["owner-01/repo-a"],
            "accounts": [],
            "destinations": ["github.com/owner-01/repo-a"],
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


class CtxFalso:
    def __init__(self, perfil="claw"):
        self.profile_name = perfil
        self.herramientas = {}
        self.ganchos = []

    def register_tool(self, **kwargs):
        self.herramientas[kwargs["name"]] = kwargs

    def register_hook(self, nombre, funcion):
        self.ganchos.append(nombre)


def cargar_plugin():
    hermes_falso.instalar()
    sys.path.insert(0, str(PLUGIN))
    spec = importlib.util.spec_from_file_location(
        "goncloud_team_plugin",
        PLUGIN / "__init__.py",
        submodule_search_locations=[str(PLUGIN)],
    )
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    spec.loader.exec_module(modulo)
    return modulo


class Base(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.plugin = cargar_plugin()
        self.addCleanup(hermes_falso.desinstalar)
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        parche = mock.patch.dict("os.environ", {"HERMES_HOME": self.temporal.name})
        parche.start()
        self.addCleanup(parche.stop)
        # La puerta real de H4a cierra en la Mac (no hay gateway vivo): las
        # pruebas de comportamiento la inyectan abierta y la clase de la
        # puerta la cierra a propósito.
        puerta = mock.patch("raices.capacidades.abrir_ruta", return_value=(True, None))
        puerta.start()
        self.addCleanup(puerta.stop)
        self.ctx = CtxFalso()
        self.plugin.register(self.ctx)

    def llamar(self, nombre, argumentos, perfil="claw"):
        respuesta = self.ctx.herramientas[nombre]["handler"](dict(argumentos))
        return json.loads(respuesta)

    def abrir_raiz(self, root_id=RAIZ):
        return self.llamar("goncloud_root_submit", {"plan": meta_de(root_id)})

    def decidir_una_vuelta(self, root_id=RAIZ):
        decidir = importlib.import_module("raices.decidir")
        store = importlib.import_module("raices.store")
        tienda = store.Store(Path(self.temporal.name) / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        hechos = {
            "principal": [],
            "asignaciones": [],
            "claims": [],
            "efectos": [],
        }
        vista = tienda.leer_vista(root_id, hechos)
        propuesta = decidir.decidir(vista)
        return tienda.aplicar(root_id, propuesta.decision, "dec-prueba")


class Registro(Base):
    def test_las_cuatro_herramientas_quedan_registradas_para_claw(self):
        for nombre in (
            "goncloud_root_submit",
            "goncloud_root_read",
            "goncloud_root_control",
            "goncloud_root_resolve",
        ):
            with self.subTest(nombre=nombre):
                self.assertIn(nombre, self.ctx.herramientas)
                self.assertEqual(
                    self.ctx.herramientas[nombre]["toolset"], "goncloud_team"
                )

    def test_plugin_yaml_declara_las_cuatro(self):
        texto = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        for nombre in (
            "goncloud_root_submit",
            "goncloud_root_read",
            "goncloud_root_control",
            "goncloud_root_resolve",
        ):
            self.assertIn(f"- {nombre}", texto)


class SoloClaw(Base):
    def test_otro_perfil_no_abre_nada(self):
        for nombre, argumentos in (
            ("goncloud_root_submit", {"plan": meta_de()}),
            ("goncloud_root_read", {"rootId": RAIZ}),
            (
                "goncloud_root_control",
                {
                    "rootId": RAIZ,
                    "command": "pause",
                    "commandId": "cmd-01",
                    "expectedControlEpoch": 1,
                },
            ),
            (
                "goncloud_root_resolve",
                {"exceptionId": "ex-1", "optionId": "listo", "by": ["david"]},
            ),
        ):
            with self.subTest(nombre=nombre):
                self.ctx.profile_name = "scout"
                respuesta = self.llamar(nombre, argumentos)
                self.assertIn("error", respuesta, nombre)
                self.assertIn("claw", respuesta["error"], nombre)


class Submit(Base):
    def test_abre_la_raiz_y_el_reintento_devuelve_el_mismo_recibo(self):
        primera = self.abrir_raiz()
        self.assertEqual(primera["rootId"], RAIZ)
        self.assertEqual(primera["planRevision"], 1)
        self.assertEqual(primera["controlEpoch"], 1)
        self.assertIs(primera["replayed"], False)
        segunda = self.abrir_raiz()
        self.assertEqual(segunda, {**primera, "replayed": True})

    def test_otro_plan_con_la_misma_raiz_es_conflicto(self):
        self.abrir_raiz()
        plan = meta_de()
        plan["plan"] = {**plan["plan"], "planDigest": "sha256:" + "b" * 64}
        respuesta = self.llamar("goncloud_root_submit", {"plan": plan})
        self.assertEqual(respuesta["error"], "operation-conflict")

    def test_un_plan_invalido_no_abre_nada(self):
        respuesta = self.llamar("goncloud_root_submit", {"plan": {"schema": "otro"}})
        self.assertEqual(respuesta["error"], "documento-invalido")


class Control(Base):
    def test_pausa_da_recibo_y_el_replay_devuelve_el_original(self):
        self.abrir_raiz()
        orden = {
            "rootId": RAIZ,
            "command": "pause",
            "commandId": "cmd-01",
            "expectedControlEpoch": 1,
        }
        recibo = self.llamar("goncloud_root_control", orden)
        self.assertEqual(recibo["controlEpoch"], 2)
        self.assertEqual(recibo["state"], "paused")
        self.assertIs(recibo["replayed"], False)
        self.assertEqual(recibo["committedEffects"], [])
        repetido = self.llamar("goncloud_root_control", orden)
        self.assertEqual(repetido, {**recibo, "replayed": True})

    def test_resume_con_epoca_vieja_se_rechaza(self):
        self.abrir_raiz()
        pausa = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
            },
        )
        respuesta = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "resume",
                "commandId": "cmd-02",
                "expectedControlEpoch": 1,
            },
        )
        self.assertEqual(respuesta["error"], "stale-control-epoch")
        self.assertEqual(respuesta["detalle"]["actual"], 2)
        activo = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "resume",
                "commandId": "cmd-03",
                "expectedControlEpoch": pausa["controlEpoch"],
            },
        )
        self.assertEqual(activo["state"], "active")

    def test_un_comando_fuera_del_contrato_no_pasa(self):
        self.abrir_raiz()
        respuesta = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "reiniciar-todo",
                "commandId": "cmd-09",
                "expectedControlEpoch": 1,
            },
        )
        self.assertEqual(respuesta["error"], "documento-invalido")


class Lectura(Base):
    def test_el_snapshot_trae_meta_unidades_y_control_con_nombres_del_contrato(self):
        self.abrir_raiz()
        snapshot = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        self.assertEqual(snapshot["meta"]["schema"], "sistema-agentes.meta.v1")
        self.assertEqual(snapshot["meta"]["rootId"], RAIZ)
        self.assertEqual(snapshot["meta"]["controlEpoch"], 1)
        self.assertEqual(snapshot["meta"]["plan"]["revision"], 1)
        self.assertEqual(
            [u["unitId"] for u in snapshot["unidades"]],
            [],
        )
        self.assertEqual(snapshot["excepciones"], [])

    def test_una_raiz_desconocida_da_error(self):
        respuesta = self.llamar("goncloud_root_read", {"rootId": "root-nadie"})
        self.assertEqual(respuesta["error"], "raiz-desconocida")


class Resolucion(Base):
    def preparar_excepcion(self):
        ticker = importlib.import_module("raices.ticker")
        kanban = importlib.import_module("raices.kanban")
        store = importlib.import_module("raices.store")
        tienda = store.Store(Path(self.temporal.name) / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta_de())
        tienda.registrar_incidencia(
            RAIZ, "a", 1, "permiso:efecto", {"kind": "permission-required"}
        )
        puerto = kanban.KanbanHermes(Path(self.temporal.name) / "kanban.db")
        self.addCleanup(puerto.cerrar)
        hechos = {
            "principal": [],
            "asignaciones": [
                {
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
                    "relay": False,
                }
            ],
            "claims": [],
            "efectos": [],
        }
        permisos = {
            "acciones": ["read", "implement", "review"],
            "toolsets": {"codex": ["read", "write"]},
            "comandos": ["git", "python3"],
        }
        informe = ticker.vuelta(tienda, puerto, {RAIZ: hechos}, permisos_h=permisos)
        informe = ticker.vuelta(tienda, puerto, {RAIZ: hechos}, permisos_h=permisos)
        self.assertEqual(informe.aplicadas, 1)
        snapshot = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        self.assertEqual(len(snapshot["excepciones"]), 1)
        return snapshot["excepciones"][0]["exceptionId"]

    def test_la_respuesta_del_dueno_queda_en_la_excepcion(self):
        exception_id = self.preparar_excepcion()
        respuesta = self.llamar(
            "goncloud_root_resolve",
            {
                "rootId": RAIZ,
                "exceptionId": exception_id,
                "optionId": "listo",
                "by": ["david"],
            },
        )
        self.assertNotIn("error", respuesta)
        snapshot = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        guerdada = snapshot["excepciones"][0]
        self.assertEqual(guerdada["response"]["optionId"], "listo")
        self.assertEqual(guerdada["response"]["by"], ["david"])

    def test_sin_root_id_no_responde(self):
        exception_id = self.preparar_excepcion()
        respuesta = self.llamar(
            "goncloud_root_resolve",
            {"exceptionId": exception_id, "optionId": "listo", "by": ["david"]},
        )
        self.assertEqual(respuesta["error"], "root-id-requerido")
        snapshot = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        self.assertIsNone(snapshot["excepciones"][0]["response"])

    def test_una_opcion_no_ofrecida_se_rechaza(self):
        exception_id = self.preparar_excepcion()
        respuesta = self.llamar(
            "goncloud_root_resolve",
            {
                "rootId": RAIZ,
                "exceptionId": exception_id,
                "optionId": "otra",
                "by": ["david"],
            },
        )
        self.assertEqual(respuesta["error"], "option-not-offered")

    def test_dos_respuestas_no_caben(self):
        exception_id = self.preparar_excepcion()
        primera = self.llamar(
            "goncloud_root_resolve",
            {
                "rootId": RAIZ,
                "exceptionId": exception_id,
                "optionId": "listo",
                "by": ["david"],
            },
        )
        self.assertNotIn("error", primera)
        segunda = self.llamar(
            "goncloud_root_resolve",
            {
                "rootId": RAIZ,
                "exceptionId": exception_id,
                "optionId": "omitir",
                "by": ["david"],
            },
        )
        self.assertEqual(segunda["error"], "exception-answered")

    def test_una_excepcion_desconocida_da_error(self):
        respuesta = self.llamar(
            "goncloud_root_resolve",
            {
                "rootId": RAIZ,
                "exceptionId": "ex-nadie",
                "optionId": "listo",
                "by": ["david"],
            },
        )
        self.assertEqual(respuesta["error"], "exception-unknown")


class PuertaDeCapacidades(Base):
    def test_cerrada_rechaza_el_submit_con_la_causa_y_sin_abrir_tienda(self):
        with mock.patch(
            "raices.capacidades.abrir_ruta",
            return_value=(False, "version-no-certificada"),
        ):
            respuesta = self.llamar("goncloud_root_submit", {"plan": meta_de()})
        self.assertEqual(
            respuesta,
            {"error": "ruta-cerrada", "detalle": {"causa": "version-no-certificada"}},
        )
        self.assertFalse((Path(self.temporal.name) / "sistema-agentes.db").exists())

    def test_abierta_inyectada_el_submit_fluye_normal(self):
        herramientas = importlib.import_module("raices.herramientas")
        respuesta = herramientas.enviar_raiz(
            {"plan": meta_de()},
            "claw",
            home=self.temporal.name,
            ruta_abierta=lambda: (True, None),
        )
        self.assertEqual(respuesta["rootId"], RAIZ)
        self.assertFalse(respuesta["replayed"])

    def test_la_lectura_no_consulta_la_puerta(self):
        puerta = mock.MagicMock(return_value=(False, "version-no-certificada"))
        with mock.patch("raices.capacidades.abrir_ruta", puerta):
            respuesta = self.llamar("goncloud_root_read", {"rootId": "root-nadie"})
        self.assertEqual(respuesta["error"], "raiz-desconocida")
        puerta.assert_not_called()


if __name__ == "__main__":
    unittest.main()
