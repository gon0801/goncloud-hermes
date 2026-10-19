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

herramientas = importlib.import_module("raices.herramientas")
store = importlib.import_module("raices.store")

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
    def test_las_seis_herramientas_quedan_registradas_para_claw(self):
        for nombre in (
            "goncloud_root_submit",
            "goncloud_root_read",
            "goncloud_root_control",
            "goncloud_root_resolve",
            "goncloud_root_instruction_put",
            "goncloud_root_reconcile",
        ):
            with self.subTest(nombre=nombre):
                self.assertIn(nombre, self.ctx.herramientas)
                self.assertEqual(
                    self.ctx.herramientas[nombre]["toolset"], "goncloud_team"
                )

    def test_plugin_yaml_declara_las_seis(self):
        texto = (PLUGIN / "plugin.yaml").read_text(encoding="utf-8")
        for nombre in (
            "goncloud_root_submit",
            "goncloud_root_read",
            "goncloud_root_control",
            "goncloud_root_resolve",
            "goncloud_root_instruction_put",
            "goncloud_root_reconcile",
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
            ("goncloud_root_instruction_put", {"text": "brief"}),
            (
                "goncloud_root_reconcile",
                {"rootId": RAIZ, "asignaciones": []},
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

    def test_el_replay_no_depende_del_reloj(self):
        """El mismo commandId con los mismos campos es replay aunque las dos
        llamadas crucen un segundo: el instante de emisión no es identidad."""
        self.abrir_raiz()
        orden = {
            "rootId": RAIZ,
            "command": "pause",
            "commandId": "cmd-01",
            "expectedControlEpoch": 1,
        }
        with mock.patch(
            "raices.store.ahora_utc",
            side_effect=["2026-10-17T00:00:05Z", "2026-10-17T00:01:35Z"],
        ):
            recibo = self.llamar("goncloud_root_control", orden)
            repetido = self.llamar("goncloud_root_control", orden)
        self.assertEqual(recibo["controlEpoch"], 2)
        self.assertEqual(recibo["state"], "paused")
        self.assertIs(recibo["replayed"], False)
        self.assertEqual(repetido, {**recibo, "replayed": True})

    def test_otro_campo_con_el_mismo_command_id_sigue_en_conflicto(self):
        self.abrir_raiz()
        primera = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
                "reason": "mantenimiento",
            },
        )
        self.assertEqual(primera["state"], "paused")
        respuesta = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
                "reason": "otro-motivo",
            },
        )
        self.assertEqual(respuesta["error"], "operation-conflict")

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

    def test_el_snapshot_expone_las_necesidades_vigentes(self):
        plan = meta_de()
        unidad_b = {
            **plan["plan"]["units"][0],
            "unitId": "b",
            "title": "Unidad b",
            "criteria": [{"criterionId": "b1", "text": "b1", "verifier": "verifier"}],
        }
        plan["plan"] = {**plan["plan"], "units": [plan["plan"]["units"][0], unidad_b]}
        recibo_submit = self.llamar("goncloud_root_submit", {"plan": plan})
        self.assertNotIn("error", recibo_submit)
        guardada = self.llamar("goncloud_root_instruction_put", {"text": "brief a"})
        recibo = self.llamar(
            "goncloud_root_reconcile",
            {
                "rootId": RAIZ,
                "asignaciones": [
                    {
                        "unitId": "a",
                        "assignment": {
                            "target": {
                                "kind": "cli",
                                "hostId": "host-01",
                                "adapterId": "codex",
                            },
                            "instructionRef": guardada,
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
            },
        )
        self.assertNotIn("error", recibo)
        snapshot = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        self.assertEqual(
            snapshot["necesidades"]["items"],
            recibo["necesidades"],
        )


class GuardarInstruccion(Base):
    def test_devuelve_ref_y_digest_y_el_mismo_texto_el_mismo_ref(self):
        primera = self.llamar("goncloud_root_instruction_put", {"text": "brief"})
        self.assertNotIn("error", primera)
        self.assertTrue(primera["ref"].startswith("artifact:"))
        self.assertTrue(primera["digest"].startswith("sha256:"))
        segunda = self.llamar("goncloud_root_instruction_put", {"text": "brief"})
        self.assertEqual(segunda, primera)
        dir_instr = Path(self.temporal.name) / "raices" / "instrucciones"
        self.assertTrue((dir_instr / primera["ref"][len("artifact:") :]).exists())

    def test_el_texto_vacio_o_fuera_del_tope_no_guarda_nada(self):
        vacio = self.llamar("goncloud_root_instruction_put", {"text": ""})
        self.assertEqual(vacio["error"], "documento-invalido")
        largo = self.llamar("goncloud_root_instruction_put", {"text": "x" * 32769})
        self.assertEqual(largo["error"], "documento-invalido")
        dir_instr = Path(self.temporal.name) / "raices" / "instrucciones"
        self.assertFalse(dir_instr.exists())


class ReconcileHerramienta(Base):
    def asignacion(self, guardada, unit_id="a"):
        return {
            "unitId": unit_id,
            "assignment": {
                "target": {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
                "instructionRef": guardada,
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

    def test_devuelve_recibo_necesidades_y_replayed(self):
        self.abrir_raiz()
        guardada = self.llamar("goncloud_root_instruction_put", {"text": "brief a"})
        recibo = self.llamar(
            "goncloud_root_reconcile",
            {
                "rootId": RAIZ,
                "asignaciones": [self.asignacion(guardada)],
            },
        )
        self.assertEqual(recibo["rootId"], RAIZ)
        self.assertEqual(recibo["planRevision"], 1)
        self.assertEqual(
            recibo["asignaciones"],
            [{"unitId": "a", "relay": False, "replayed": False}],
        )
        self.assertIs(recibo["replayed"], False)
        self.assertEqual(recibo["necesidades"], [])
        repetido = self.llamar(
            "goncloud_root_reconcile",
            {
                "rootId": RAIZ,
                "asignaciones": [self.asignacion(guardada)],
            },
        )
        self.assertIs(repetido["replayed"], True)
        self.assertEqual(
            repetido["asignaciones"],
            [{"unitId": "a", "relay": False, "replayed": True}],
        )

    def test_el_conflicto_del_store_llega_como_error_tipado(self):
        self.abrir_raiz()
        guardada = self.llamar("goncloud_root_instruction_put", {"text": "brief a"})
        rota = {**guardada, "digest": "sha256:" + "c" * 64}
        respuesta = self.llamar(
            "goncloud_root_reconcile",
            {"rootId": RAIZ, "asignaciones": [self.asignacion(rota)]},
        )
        self.assertEqual(respuesta["error"], "instruccion-invalida")
        self.assertEqual(respuesta["detalle"]["motivo"], "digest-distinto")

    def test_la_forma_cruda_invalida_es_documento_invalido(self):
        self.abrir_raiz()
        for asignaciones in (
            [{"unitId": "a", "assignment": "en texto", "relay": False}],
            [{"unitId": "", "assignment": {}, "relay": False}],
            [{"unitId": "a", "assignment": {}, "relay": "si"}],
            "no soy una lista",
        ):
            with self.subTest(asignaciones=asignaciones):
                respuesta = self.llamar(
                    "goncloud_root_reconcile",
                    {"rootId": RAIZ, "asignaciones": asignaciones},
                )
                self.assertEqual(respuesta["error"], "documento-invalido")
        sin_raiz = self.llamar(
            "goncloud_root_reconcile", {"rootId": "", "asignaciones": []}
        )
        self.assertEqual(sin_raiz["error"], "documento-invalido")


class ReconcileConClaims(Base):
    """H6b: los claims de verificación entran por la misma herramienta, con
    la evidencia subida antes con goncloud_root_instruction_put."""

    def claim_con_evidencia(self, **sobre):
        evidencia = self.llamar(
            "goncloud_root_instruction_put", {"text": "salida de la corrida"}
        )
        self.assertNotIn("error", evidencia)
        claim = {
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "artifactRef": {
                "ref": evidencia["ref"],
                "digest": evidencia["digest"],
            },
            "environment": {"host": "mac-local", "versions": {}},
            "procedure": "python3 -m unittest",
            "outcome": "passed",
            "at": "2026-10-18T11:00:00Z",
            "expiresAt": None,
            "role": "verifier",
        }
        return {**claim, **sobre}

    def reconciliar(self, claims, asignaciones=None):
        return self.llamar(
            "goncloud_root_reconcile",
            {"rootId": RAIZ, "asignaciones": asignaciones or [], "claims": claims},
        )

    def test_persiste_el_claim_y_el_replay_lo_marca(self):
        self.abrir_raiz()
        recibo = self.reconciliar([self.claim_con_evidencia()])
        self.assertNotIn("error", recibo)
        self.assertEqual(
            recibo["claims"], [{"unitId": "a", "criterionId": "a1", "replayed": False}]
        )
        self.assertIs(recibo["replayed"], False)
        repetido = self.reconciliar([self.claim_con_evidencia()])
        self.assertEqual(
            repetido["claims"], [{"unitId": "a", "criterionId": "a1", "replayed": True}]
        )
        self.assertIs(repetido["replayed"], True)

    def test_el_claim_con_role_reviewer_se_rechaza(self):
        self.abrir_raiz()
        respuesta = self.reconciliar([self.claim_con_evidencia(role="reviewer")])
        self.assertEqual(respuesta["error"], "documento-invalido")

    def test_el_artifactref_irresoluble_es_instruccion_invalida(self):
        self.abrir_raiz()
        claim = self.claim_con_evidencia()
        claim["artifactRef"] = {
            "ref": claim["artifactRef"]["ref"],
            "digest": "sha256:" + "c" * 64,
        }
        respuesta = self.reconciliar([claim])
        self.assertEqual(respuesta["error"], "instruccion-invalida")
        self.assertEqual(respuesta["detalle"]["motivo"], "digest-distinto")

    def test_la_forma_cruda_del_claim_es_documento_invalido(self):
        self.abrir_raiz()
        for claims in (
            [{"unitId": "a"}],
            [{**self.claim_con_evidencia(), "outcome": "tal-vez"}],
            [{**self.claim_con_evidencia(), "attempt": "1"}],
            [{**self.claim_con_evidencia(), "expiresAt": 0}],
            "no soy una lista",
        ):
            with self.subTest(claims=claims):
                respuesta = self.reconciliar(claims)
                self.assertEqual(respuesta["error"], "documento-invalido")


class Resolucion(Base):
    def preparar_excepcion(self):
        ticker = importlib.import_module("raices.ticker")
        kanban = importlib.import_module("raices.kanban")
        instrucciones = importlib.import_module("raices.instrucciones")
        store = importlib.import_module("raices.store")
        tienda = store.Store(Path(self.temporal.name) / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta_de())
        tienda.registrar_incidencia(
            RAIZ, "a", 1, "permiso:efecto", {"kind": "permission-required"}
        )
        puerto = kanban.KanbanHermes(Path(self.temporal.name) / "kanban.db")
        self.addCleanup(puerto.cerrar)
        ref, digest = instrucciones.guardar(
            tienda.dir_de_instrucciones, "brief de la excepcion"
        )
        hechos = {
            "principal": [],
            "asignaciones": [
                {
                    "unitId": "a",
                    "assignment": {
                        "target": {"kind": "cli", "adapterId": "codex"},
                        "instructionRef": {"ref": ref, "digest": digest},
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


class RutaDelStore(unittest.TestCase):
    """F1: sin HERMES_HOME las herramientas fallan con error claro y no
    abren ./sistema-agentes.db en el cwd del gateway."""

    def test_sin_hermes_home_falla_y_no_crea_archivo_en_el_cwd(self):
        herramientas = importlib.import_module("raices.herramientas")
        # Sin hermes_constants: en la PC el real existiria y caeria al default
        # de la plataforma; lo probado aqui es la guarda del fallback (F1).
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch.dict(sys.modules, {"hermes_constants": None}),
        ):
            with self.assertRaises(ValueError):
                herramientas.ruta_del_store(None)
            with self.assertRaises(ValueError):
                herramientas.ruta_del_store("")
            with self.assertRaises(ValueError):
                herramientas.abrir_tienda(None)
        self.assertFalse(
            Path("sistema-agentes.db").exists(),
            "sin HERMES_HOME no se abre un store en el cwd",
        )


class CargaComoPaquete(unittest.TestCase):
    """F2: el plugin cargado como paquete sin el directorio del plugin en
    sys.path igual deja las herramientas raiz operables (no 'No module
    named')."""

    def test_las_herramientas_raiz_responden_sin_path_previo(self):
        import importlib

        temporal = tempfile.TemporaryDirectory()
        self.addCleanup(temporal.cleanup)
        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        # F9: con HERMES_HOME fijado al temporal el manejador abre el store de
        # ahi (y con el doble de hermes_constants, no el real de la PC), no el
        # home vivo del dueno.
        with mock.patch.dict("os.environ", {"HERMES_HOME": temporal.name}):
            for nombre in [m for m in list(sys.modules) if m.split(".")[0] == "raices"]:
                del sys.modules[nombre]
            plugins = str(Path(__file__).resolve().parents[1] / "plugins/goncloud-team")
            sys.path[:] = [
                p
                for p in sys.path
                if str(Path(p).resolve()) != str(Path(plugins).resolve())
            ]
            spec = importlib.util.spec_from_file_location(
                "goncloud_team_paquete",
                Path(__file__).resolve().parents[1]
                / "plugins/goncloud-team/__init__.py",
                submodule_search_locations=[
                    str(Path(__file__).resolve().parents[1] / "plugins/goncloud-team")
                ],
            )
            modulo = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = modulo
            spec.loader.exec_module(modulo)

            class Ctx:
                profile_name = "claw"

                def __init__(self):
                    self.herramientas = {}

                def register_tool(self, **kwargs):
                    self.herramientas[kwargs["name"]] = kwargs

                def register_hook(self, nombre, funcion):
                    pass

            ctx = Ctx()
            modulo.register(ctx)
            respuesta = json.loads(
                ctx.herramientas["goncloud_root_read"]["handler"]({"rootId": "root-x"})
            )
            self.assertNotIn(
                "No module named", json.dumps(respuesta), "las herramientas responden"
            )
            self.assertIn("error", respuesta)
            self.assertTrue(
                (Path(temporal.name) / "sistema-agentes.db").exists(),
                "el store se abre en el temporal, no en el home vivo",
            )


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

    def test_version_sin_certificar_abre_con_aviso_en_el_recibo(self):
        # Encargo H punto 3: la puerta abre con aviso; el submit fluye y el
        # recibo lleva el aviso sin bloquear nada.
        herramientas = importlib.import_module("raices.herramientas")
        respuesta = herramientas.enviar_raiz(
            {"plan": meta_de()},
            "claw",
            home=self.temporal.name,
            ruta_abierta=lambda: (True, "version-no-certificada"),
        )
        self.assertEqual(respuesta["rootId"], RAIZ)
        self.assertEqual(respuesta["aviso"], "version-no-certificada")

    def test_la_lectura_no_consulta_la_puerta(self):
        puerta = mock.MagicMock(return_value=(False, "version-no-certificada"))
        with mock.patch("raices.capacidades.abrir_ruta", puerta):
            respuesta = self.llamar("goncloud_root_read", {"rootId": "root-nadie"})
        self.assertEqual(respuesta["error"], "raiz-desconocida")
        puerta.assert_not_called()

    def test_la_puerta_del_submit_recibe_la_raiz_de_hogar(self):
        # F8: el gateway_state.json vive en la RAIZ. El submit con el home de
        # un perfil le pasa a la puerta la raiz resuelta por hogar, nunca el
        # home pelado (perfiles/claw no tiene gateway_state.json).
        raiz = Path(self.temporal.name) / "hermes-raiz"
        (raiz / "profiles" / "claw").mkdir(parents=True)
        consultada = []
        puerta = mock.MagicMock(return_value=(True, None))

        def capturar(home=None, **kwargs):
            consultada.append(Path(home) if home is not None else None)
            return puerta(home=home, **kwargs)

        herramientas = importlib.import_module("raices.herramientas")
        with mock.patch("raices.capacidades.abrir_ruta", capturar):
            herramientas.enviar_raiz(
                {"plan": meta_de()},
                "claw",
                home=str(raiz / "profiles" / "claw"),
            )
        self.assertEqual(consultada, [raiz])


class Permisos(Base):
    """La tienda de las herramientas abre con los mismos permisos del ticker
    (cron_ticker.leer_permisos sobre %HERMES_HOME%/raices/permisos.json):
    una sola fuente. Sin archivo, falla cerrada."""

    def preparar_efecto(self):
        tienda = herramientas.abrir_tienda(self.temporal.name)
        self.addCleanup(tienda.cerrar)
        decision = {
            "expected_plan_revision": 1,
            "expected_control_epoch": 1,
            "consumidos": [],
            "claims": [],
            "outcomes": [],
            "bindings": [],
            "efectos": [
                {
                    "effectId": "effect-push",
                    "action": "push",
                    "repo": "owner-01/repo-a",
                    "destination": "github.com/owner-01/repo-a",
                    "mode": "implement",
                    "cancellable": False,
                    "unitId": None,
                    "resourceRef": None,
                }
            ],
            "contadores": [],
            "excepciones": [],
            "respuestas_aplicadas": [],
        }
        self.assertIsInstance(tienda.aplicar(RAIZ, decision, "dec-ef"), store.Ok)

    def escribir_permisos(self, acciones):
        raices = Path(self.temporal.name) / "raices"
        raices.mkdir(parents=True, exist_ok=True)
        (raices / "permisos.json").write_text(
            json.dumps(
                {
                    "acciones": acciones,
                    "toolsets": {"codex": ["read", "write"]},
                    "comandos": ["git", "python3"],
                }
            ),
            encoding="utf-8",
        )

    def causa_de_efecto_en_read(self):
        respuesta = self.llamar("goncloud_root_read", {"rootId": RAIZ})
        causas = [
            e["subject"].get("cause", "")
            for e in respuesta["excepciones"]
            if isinstance(e.get("subject"), dict)
        ]
        return [c for c in causas if c.startswith("permiso-h:")]

    def test_la_tienda_de_las_herramientas_lee_la_misma_fuente(self):
        self.escribir_permisos(["push"])
        tienda = herramientas.abrir_tienda(self.temporal.name)
        self.addCleanup(tienda.cerrar)
        self.assertEqual(tienda.permisos_h_datos["acciones"], ["push"])

    def test_sin_archivo_la_tienda_abre_sin_permisos(self):
        tienda = herramientas.abrir_tienda(self.temporal.name)
        self.addCleanup(tienda.cerrar)
        self.assertIsNone(tienda.permisos_h_datos)

    def test_con_permisos_json_que_admite_el_resume_no_niega_por_sin_permisos(self):
        self.abrir_raiz()
        self.escribir_permisos(["push"])
        self.preparar_efecto()
        pausa = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
            },
        )
        self.assertIn("state", pausa)
        reanuda = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "resume",
                "commandId": "cmd-02",
                "expectedControlEpoch": 2,
            },
        )
        self.assertIn("state", reanuda)
        self.assertEqual(self.causa_de_efecto_en_read(), [])

    def test_sin_archivo_sigue_fallando_cerrado(self):
        self.abrir_raiz()
        self.preparar_efecto()
        pausa = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
            },
        )
        self.assertIn("state", pausa)
        reanuda = self.llamar(
            "goncloud_root_control",
            {
                "rootId": RAIZ,
                "command": "resume",
                "commandId": "cmd-02",
                "expectedControlEpoch": 2,
            },
        )
        self.assertIn("state", reanuda)
        self.assertIn("permiso-h:sin-permisos", self.causa_de_efecto_en_read())


if __name__ == "__main__":
    unittest.main()
