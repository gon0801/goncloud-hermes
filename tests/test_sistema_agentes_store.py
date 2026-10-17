#!/usr/bin/env python3
"""El store de sistema-agentes.db: control con recibos, efectos, cupos y linaje.

Las cláusulas de I05, I06, I20 y la parte de I12 e I13 que vive en el estado
durable se fijan aquí contra la semántica de control.v1 del contrato.
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

store = importlib.import_module("raices.store")
decidir = importlib.import_module("raices.decidir")

RAIZ = "root-03"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}


def asignacion(adapter="codex", repo="owner-01/repo-a"):
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": adapter},
        "instructionRef": {"ref": f"artifact:inst-{adapter}", "digest": DIGEST},
        "resultContract": "review.v1",
        "inputRevision": {"kind": "code", "repository": repo, "sha": "0" * 40},
        "continuation": {"kind": "requester"},
    }


def presupuesto(max_concurrentes=8):
    return {
        "maxConcurrentTasks": max_concurrentes,
        "maxDepth": 8,
        "maxChildren": 32,
        "maxModelCalls": 320,
        "maxInputTokens": 1000000,
        "maxOutputTokens": 100000,
        "maxCacheReadTokens": 1000000,
        "maxContextTokens": 100000,
        "maxTreeTokens": 1100000,
        "maxAutomaticRecoveryCalls": 8,
    }


def meta_de(unidades, max_concurrentes=8):
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": RAIZ,
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
        "budget": presupuesto(max_concurrentes),
        "plan": {
            "revision": 1,
            "planDigest": DIGEST,
            "requestDigest": DIGEST,
            "units": unidades,
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def unidad(unit_id, criterio, dependencias=()):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": list(dependencias),
        "criteria": [
            {
                "criterionId": criterio,
                "text": f"Criterio {criterio}",
                "verifier": "verifier",
            }
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
    }


def claim(unit_id, n, criterio, desenlace="passed", rol="verifier", principal=None):
    return {
        "schema": "sistema-agentes.evidencia.v1",
        "evidenceId": f"evid-{criterio}-{n}",
        "rootId": RAIZ,
        "unitId": unit_id,
        "attempt": n,
        "criterionId": criterio,
        "planRevision": 1,
        "artifactRef": REF,
        "environment": {"host": "mac-local", "versions": {"python": "3.9.6"}},
        "verifier": {"principal": principal or ["verif-1"], "role": rol},
        "procedure": "pytest",
        "outcome": desenlace,
        "at": "2026-10-17T11:00:00Z",
        "expiresAt": None,
    }


def comando(command, command_id, epoch, reason=None):
    return {
        "schema": "sistema-agentes.control.v1",
        "rootId": RAIZ,
        "command": command,
        "commandId": command_id,
        "expectedControlEpoch": epoch,
        "reason": reason,
        "issuedAt": "2026-10-17T11:00:00Z",
    }


def decision(bindings=(), outcomes=(), claims=(), contadores=(), efectos=()):
    return {
        "expected_plan_revision": 1,
        "expected_control_epoch": 1,
        "consumidos": [],
        "claims": list(claims),
        "outcomes": list(outcomes),
        "bindings": list(bindings),
        "efectos": list(efectos),
        "contadores": list(contadores),
        "excepciones": [],
        "respuestas_aplicadas": [],
    }


def binding(unit_id, adapter="codex", repo="owner-01/repo-a"):
    return {"unitId": unit_id, "assignment": asignacion(adapter, repo), "writers": []}


def efecto(effect_id="effect-01", destination="dest-a", unit_id=None):
    return {
        "effectId": effect_id,
        "action": "push",
        "repo": "owner-01/repo-a",
        "destination": destination,
        "mode": "implement",
        "cancellable": False,
        "unitId": unit_id,
        "resourceRef": None,
    }


def hechos_vacios():
    return {
        "principal": ["david"],
        "asignaciones": [],
        "claims": [],
        "efectos": [],
    }


def politica_regla4(**cambios):
    base = {
        "version": "quality-kit.regla4",
        "roundsPerImplementer": 5,
        "relays": 1,
        "revisorExcluyeAutores": True,
        "segundoModelo": False,
    }
    base.update(cambios)
    return base


class AlmacenDePrueba(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(self.ruta)
        self.tienda.abrir_raiz(
            meta_de([unidad("a", "a1"), unidad("c", "c1", ["a"]), unidad("d", "d1")])
        )

    def preparar_efecto(self, unit_id=None):
        self.tienda.aplicar(
            RAIZ,
            decision(efectos=[efecto(unit_id=unit_id)]),
            "dec-efecto",
        )
        self.tienda.iniciar_efecto(RAIZ, "effect-01", "start-1", 1)


class I05Control(AlmacenDePrueba):
    def test_la_pausa_enumerara_los_efectos_comprometidos(self):
        self.tienda.aplicar(
            RAIZ,
            decision(efectos=[efecto(), efecto("effect-02")]),
            "dec-efectos",
        )
        self.tienda.iniciar_efecto(RAIZ, "effect-01", "start-1", 1)
        recibo = self.tienda.control(comando("pause", "cmd-01", 1, "detener")).valor
        self.assertEqual(recibo.state, "paused")
        self.assertEqual(recibo.committed_effects[0]["effectId"], "effect-01")
        self.assertEqual(recibo.committed_effects[0]["state"], "started")
        self.assertFalse(recibo.committed_effects[0]["cancellable"])
        denegado = self.tienda.iniciar_efecto(RAIZ, "effect-02", "start-2", 2)
        self.assertIsInstance(denegado, store.Conflicto)
        self.assertEqual(denegado.codigo, "root-not-active")
        self.assertEqual(denegado.detalle["state"], "paused")

    def test_el_cancel_enumerara_los_efectos_comprometidos_y_no_arranca_nuevos(self):
        self.tienda.aplicar(
            RAIZ,
            decision(efectos=[efecto(), efecto("effect-02")]),
            "dec-efectos",
        )
        self.tienda.iniciar_efecto(RAIZ, "effect-01", "start-1", 1)
        recibo = self.tienda.control(comando("cancel", "cmd-01", 1)).valor
        self.assertEqual(recibo.state, "cancelled")
        self.assertEqual(
            [e["effectId"] for e in recibo.committed_effects], ["effect-01"]
        )
        denegado = self.tienda.iniciar_efecto(RAIZ, "effect-02", "start-2", 2)
        self.assertIsInstance(denegado, store.Conflicto)
        self.assertEqual(denegado.codigo, "root-not-active")
        self.assertEqual(denegado.detalle["state"], "cancelled")

    def test_envio_posterior_al_ack_sigue_y_el_recibo_queda_congelado(self):
        self.preparar_efecto()
        recibo_pausa = self.tienda.control(comando("pause", "cmd-01", 1)).valor
        liquidado = self.tienda.liquidar_efecto(
            RAIZ,
            "effect-01",
            "start-1",
            {"kind": "confirmed", "occurred": True, "evidenceRef": "artifact:ev"},
        )
        self.assertEqual(liquidado.valor.state, "confirmed")
        recibo_repetido = self.tienda.control(comando("pause", "cmd-01", 1))
        self.assertTrue(recibo_repetido.replayed)
        self.assertEqual(recibo_repetido.valor._replace(replayed=False), recibo_pausa)
        distinto = self.tienda.control(
            {**comando("pause", "cmd-01", 1), "reason": "otro"}
        )
        self.assertEqual(distinto.codigo, "operation-conflict")

    def test_replay_da_el_recibo_original(self):
        recibo = self.tienda.control(comando("pause", "cmd-01", 1)).valor
        otra_vez = self.tienda.control(comando("pause", "cmd-01", 1))
        self.assertTrue(otra_vez.replayed)
        self.assertEqual(otra_vez.valor._replace(replayed=False), recibo)
        distinto = self.tienda.control(
            {**comando("pause", "cmd-01", 1), "reason": "otro"}
        )
        self.assertEqual(distinto.codigo, "operation-conflict")

    def test_resume_con_epoca_vieja_se_rechaza(self):
        self.tienda.control(comando("pause", "cmd-01", 1))
        viejo = self.tienda.control(comando("resume", "cmd-02", 1))
        self.assertEqual(viejo.codigo, "stale-control-epoch")
        self.assertEqual(viejo.detalle["actual"], 2)
        bueno = self.tienda.control(comando("resume", "cmd-03", 2))
        self.assertEqual(bueno.valor.state, "active")

    def test_cancel_es_terminal_y_un_evento_tardio_no_resucita(self):
        self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-1")
        recibo = self.tienda.control(comando("cancel", "cmd-01", 1)).valor
        self.assertEqual(recibo.state, "cancelled")
        tarde = self.tienda.control(comando("pause", "cmd-02", 2))
        self.assertEqual(tarde.codigo, "transition-refused")
        estado = self.tienda.transicionar_intento(RAIZ, "a", 1, "running", "tarde")
        self.assertIsInstance(estado, store.Conflicto)
        self.assertEqual(self.tienda.intenciones_pendientes(), [])
        vista = self.tienda.leer_vista(RAIZ, EntradasDelMundo.hechos_nulos())
        self.assertEqual(sorted(d["state"] for d in vista["unidades"]), ["cancelled"])

    def test_el_documento_de_control_se_valida_en_la_frontera(self):
        malo = self.tienda.control(
            {
                "schema": "sistema-agentes.control.v1",
                "rootId": RAIZ,
                "command": "reiniciar",
                "commandId": "cmd-x",
                "expectedControlEpoch": 1,
                "reason": None,
                "issuedAt": "2026-10-17T11:00:00Z",
            }
        )
        self.assertEqual(malo.codigo, "documento-invalido")


class CarrerasDeUnaTransaccion(AlmacenDePrueba):
    def test_el_cancel_de_otro_proceso_cierra_la_ventana_de_aplicar(self):
        otro = store.Store(self.ruta)
        self.addCleanup(otro.cerrar)
        original = self.tienda._begin

        def begin_con_cancel_intercalado():
            otro.control(comando("cancel", "cmd-x", 1))
            self.tienda._begin = original
            original()

        self.tienda._begin = begin_con_cancel_intercalado
        resultado = self.tienda.aplicar(
            RAIZ, decision(bindings=[binding("a")]), "dec-1"
        )
        self.assertIsInstance(resultado, store.Conflicto)
        self.assertEqual(resultado.codigo, "stale-control-epoch")
        self.assertEqual(resultado.detalle["actual"], 2)
        self.assertEqual(self.tienda.ocupacion()["global"], 0)
        self.assertEqual(self.tienda.intenciones_pendientes(), [])
        escritores = self.tienda._conexion.execute(
            "SELECT COUNT(*) AS n FROM escritores"
        ).fetchone()["n"]
        self.assertEqual(escritores, 0)

    def test_el_replay_concurrente_da_el_recibo_original(self):
        otro = store.Store(self.ruta)
        self.addCleanup(otro.cerrar)
        original = self.tienda._begin
        visto = []

        def begin_con_pausa_intercalada():
            visto.append(otro.control(comando("pause", "cmd-01", 1)).valor.state)
            self.tienda._begin = original
            original()

        self.tienda._begin = begin_con_pausa_intercalada
        recibo = self.tienda.control(comando("pause", "cmd-01", 1))
        self.assertEqual(visto, ["paused"])
        self.assertIsInstance(recibo, store.Ok)
        self.assertTrue(recibo.replayed)
        self.assertEqual(recibo.valor.state, "paused")


class I06Efectos(AlmacenDePrueba):
    def test_un_confirmado_no_se_repite(self):
        self.preparar_efecto()
        primero = self.tienda.liquidar_efecto(
            RAIZ,
            "effect-01",
            "start-1",
            {"kind": "confirmed", "occurred": True, "evidenceRef": "artifact:ev"},
        )
        self.assertEqual(primero.valor.state, "confirmed")
        repetido = self.tienda.liquidar_efecto(
            RAIZ,
            "effect-01",
            "start-1",
            {"kind": "confirmed", "occurred": True, "evidenceRef": "artifact:ev"},
        )
        self.assertEqual(repetido.codigo, "effect-refused")
        vista = self.tienda.leer_vista(RAIZ, hechos_vacios())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["confirmed"])

    def test_un_incierto_conserva_su_estado_y_exige_conciliacion(self):
        self.preparar_efecto()
        self.tienda.liquidar_efecto(
            RAIZ, "effect-01", "start-1", {"kind": "uncertain", "note": "no sé"}
        )
        vista = self.tienda.leer_vista(RAIZ, hechos_vacios())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["uncertain"])
        propuesta = decidir.decidir(vista)
        tipos = [e["type"] for e in propuesta.decision["excepciones"]]
        self.assertIn("EVIDENCIA_INCIERTA", tipos)


class I20Cupos(AlmacenDePrueba):
    def test_el_presupuesto_de_la_raiz(self):
        tienda = store.Store(self.ruta)
        tienda.abrir_raiz(
            {**meta_de([unidad("x", "x1")], max_concurrentes=1), "rootId": "root-chica"}
        )
        self.assertIsInstance(
            tienda.aplicar("root-chica", decision(bindings=[binding("x")]), "dec-1"),
            store.Ok,
        )
        lleno = tienda.aplicar("root-chica", decision(bindings=[binding("x")]), "dec-2")
        self.assertEqual(lleno.codigo, "capacity")

    def test_el_cupo_global(self):
        tienda = store.Store(self.ruta, cupos=store.Cupos(global_max=0))
        conflicto = tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-1")
        self.assertEqual(conflicto.codigo, "capacity")
        self.assertEqual(tienda.ocupacion()["global"], 0)

    def test_el_cupo_global_y_por_perfil(self):
        sin_cupo = store.Store(self.ruta, cupos=store.Cupos(global_max=0))
        conflicto = sin_cupo.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-1")
        self.assertEqual(conflicto.codigo, "capacity")
        self.assertEqual(sin_cupo.ocupacion()["global"], 0)
        primero = store.Store(self.ruta, cupos=store.Cupos(por_perfil={"codex": 1}))
        self.assertIsInstance(
            primero.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-1"), store.Ok
        )
        segundo = store.Store(self.ruta, cupos=store.Cupos(por_perfil={"codex": 1}))
        negado = segundo.aplicar(RAIZ, decision(bindings=[binding("c")]), "dec-2")
        self.assertEqual(negado.codigo, "capacity")
        holgado = store.Store(self.ruta, cupos=store.Cupos(por_perfil={"codex": 2}))
        self.assertIsInstance(
            holgado.aplicar(RAIZ, decision(bindings=[binding("c")]), "dec-3"), store.Ok
        )
        otro_perfil = store.Store(
            self.ruta, cupos=store.Cupos(por_perfil={"claude": 1})
        )
        self.assertIsInstance(
            otro_perfil.aplicar(
                RAIZ,
                decision(bindings=[binding("d", adapter="claude")]),
                "dec-4",
            ),
            store.Ok,
        )

    def test_el_escritor_exclusivo(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-1"),
            store.Ok,
        )
        self.tienda.abrir_raiz({**meta_de([unidad("b", "b1")]), "rootId": "root-b"})
        choque = self.tienda.aplicar(
            "root-b", decision(bindings=[binding("b")]), "dec-2"
        )
        self.assertEqual(choque.codigo, "writer-held")
        libre = self.tienda.aplicar(
            "root-b",
            decision(bindings=[binding("b", repo="owner-01/repo-b")]),
            "dec-3",
        )
        self.assertIsInstance(libre, store.Ok)

    def test_el_par_atomico_con_dos_y_tres_plazas_ocupadas(self):
        par = store.Store(self.ruta, cupos=store.Cupos(global_max=2))
        self.assertIsInstance(
            par.aplicar(
                RAIZ,
                decision(bindings=[binding("a"), binding("c")]),
                "dec-1",
            ),
            store.Ok,
        )
        self.assertEqual(par.ocupacion()["global"], 2)
        par.abrir_raiz({**meta_de([unidad("b", "b1")]), "rootId": "root-b"})
        tercera = par.aplicar("root-b", decision(bindings=[binding("b")]), "dec-2")
        self.assertEqual(tercera.codigo, "capacity")
        self.assertEqual(par.ocupacion()["global"], 2)
        holgado = store.Store(self.ruta, cupos=store.Cupos(global_max=3))
        self.assertIsInstance(
            holgado.aplicar(
                "root-b",
                decision(bindings=[binding("b", repo="owner-01/repo-b")]),
                "dec-3",
            ),
            store.Ok,
        )
        self.assertEqual(holgado.ocupacion()["global"], 3)
        par.abrir_raiz(
            {
                **meta_de([unidad("p", "p1"), unidad("q", "q1")]),
                "rootId": "root-pq",
            }
        )
        par_de_tres = store.Store(self.ruta, cupos=store.Cupos(global_max=3))
        negado = par_de_tres.aplicar(
            "root-pq",
            decision(
                bindings=[
                    binding("p", repo="owner-01/repo-p"),
                    binding("q", repo="owner-01/repo-q"),
                ]
            ),
            "dec-4",
        )
        self.assertEqual(negado.codigo, "capacity")
        self.assertEqual(par_de_tres.ocupacion()["global"], 3)


class I12LinajeEnElStore(AlmacenDePrueba):
    def test_el_contador_de_rondas_sube_por_linaje_y_no_se_reinicia(self):
        ligadura = decision(bindings=[binding("a")])
        self.assertIsInstance(self.tienda.aplicar(RAIZ, ligadura, "dec-0"), store.Ok)
        primera = decision(
            outcomes=[{"unitId": "a", "attempt": 1, "outcome": "failed"}],
            bindings=[binding("a")],
            contadores=[{"counter": "rondas:a", "expected": 0}],
        )
        self.assertIsInstance(self.tienda.aplicar(RAIZ, primera, "dec-1"), store.Ok)
        segunda = decision(
            outcomes=[{"unitId": "a", "attempt": 2, "outcome": "failed"}],
            bindings=[binding("a")],
            contadores=[{"counter": "rondas:a", "expected": 1}],
        )
        self.assertIsInstance(self.tienda.aplicar(RAIZ, segunda, "dec-2"), store.Ok)
        tercera = decision(
            outcomes=[{"unitId": "a", "attempt": 3, "outcome": "failed"}],
            contadores=[{"counter": "rondas:a", "expected": 1}],
        )
        recargada = self.tienda.aplicar(RAIZ, tercera, "dec-3")
        self.assertEqual(recargada.codigo, "counter-mismatch")
        self.assertEqual(recargada.detalle["current"], 2)

    def test_una_raiz_conserva_la_politica_con_la_que_abrio(self):
        tendera = politica_regla4(roundsPerImplementer=2)
        tienda = store.Store(self.ruta)
        tienda.abrir_raiz(
            {**meta_de([unidad("l", "l1")]), "rootId": "root-lenta"},
            politicas={"quality-kit.regla4": tendera},
        )
        vista = tienda.leer_vista("root-lenta", hechos_vacios())
        self.assertEqual(vista["politica"]["roundsPerImplementer"], 2)
        vista_de_set_up = self.tienda.leer_vista(RAIZ, hechos_vacios())
        self.assertEqual(vista_de_set_up["politica"]["roundsPerImplementer"], 5)
        self.assertTrue(vista_de_set_up["politica"]["revisorExcluyeAutores"])
        asignacion_relevo = {
            "target": {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
            "instructionRef": {"ref": "artifact:inst-codex", "digest": DIGEST},
            "resultContract": "review.v1",
            "continuation": {"kind": "requester"},
        }
        vista_lenta = tienda.leer_vista(
            "root-lenta",
            {
                "principal": ["david"],
                "asignaciones": [
                    {"unitId": "l", "assignment": asignacion_relevo, "relay": True}
                ],
                "claims": [],
                "efectos": [],
            },
        )
        vista_lenta["unidades"] = [
            {
                "schema": "sistema-agentes.unidad.v1",
                "rootId": "root-lenta",
                "planRevision": 1,
                "unitId": "l",
                "attempt": 3,
                "taskId": "task-l-3",
                "assignment": asignacion_relevo,
                "state": "failed",
                "controlEpoch": 1,
            }
        ]
        vista_lenta["contadores"] = {"rondas:l": 2}
        propuesta = decidir.decidir(vista_lenta)
        self.assertEqual(len(propuesta.decision["bindings"]), 1)
        self.assertEqual(
            propuesta.decision["bindings"][0]["assignment"], asignacion_relevo
        )
        self.assertEqual(
            propuesta.decision["contadores"],
            [{"counter": "rondas:l", "expected": 2}],
        )

    def test_abrir_raiz_rechaza_una_politica_que_no_esta_en_el_pin(self):
        malo = self.tienda.abrir_raiz(
            {
                **meta_de([unidad("z", "z1")]),
                "rootId": "root-z",
                "qualityPolicy": {"version": "otra-politica"},
            }
        )
        self.assertEqual(malo.codigo, "documento-invalido")


class I13AceptacionEnElStore(AlmacenDePrueba):
    def test_una_aceptacion_sin_claims_no_entra(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-0"),
            store.Ok,
        )
        intento = decision(
            outcomes=[{"unitId": "a", "attempt": 1, "outcome": "accepted"}]
        )
        negada = self.tienda.aplicar(RAIZ, intento, "dec-1")
        self.assertEqual(negada.codigo, "criteria-unproven")
        vista = self.tienda.leer_vista(RAIZ, hechos_vacios())
        self.assertEqual(sorted(d["state"] for d in vista["unidades"]), ["ready"])

    def test_una_aceptacion_con_claims_vigentes_entra(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-0"),
            store.Ok,
        )
        intento = decision(
            claims=[claim("a", 1, "a1")],
            outcomes=[{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )
        self.assertIsInstance(self.tienda.aplicar(RAIZ, intento, "dec-1"), store.Ok)
        vista = self.tienda.leer_vista(RAIZ, hechos_vacios())
        self.assertEqual(sorted(d["state"] for d in vista["unidades"]), ["accepted"])


class EntradasDelMundo(AlmacenDePrueba):
    def resultado(self, digest):
        return {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "resultContract": "ready.v1",
            "resultDigest": digest,
            "reportedAt": "2026-10-17T11:00:00Z",
            "reportedBy": {"kind": "native", "taskId": "t-1"},
            "payload": {"evidenceRef": REF},
        }

    def excepcion_abierta(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {"kind": "rounds", "unitId": "a", "block": 0},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        return borrador

    def test_la_respuesta_del_dueno_queda_y_se_reconoce(self):
        borrador = self.excepcion_abierta()
        primera = self.tienda.responder_excepcion(
            RAIZ, borrador["exceptionId"], "seguir", ["david"]
        )
        self.assertIsInstance(primera, store.Ok)
        repetida = self.tienda.responder_excepcion(
            RAIZ, borrador["exceptionId"], "seguir", ["david"]
        )
        self.assertEqual(repetida.codigo, "exception-answered")
        otra_opcion = self.tienda.responder_excepcion(
            RAIZ, "exception-desconocida", "seguir", ["david"]
        )
        self.assertEqual(otra_opcion.codigo, "exception-unknown")
        vista = self.tienda.leer_vista(RAIZ, EntradasDelMundo.hechos_nulos())
        self.assertEqual(vista["excepciones"][0]["response"]["optionId"], "seguir")

    def test_una_opcion_no_ofrecida_se_rechaza(self):
        borrador = self.excepcion_abierta()
        negada = self.tienda.responder_excepcion(
            RAIZ, borrador["exceptionId"], "autorizar", ["david"]
        )
        self.assertEqual(negada.codigo, "option-not-offered")

    def _intento_reportado(self):
        self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-0")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "admitted", "t-1")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "running", "t-1")
        self.assertIsInstance(
            self.tienda.reportar_resultado(self.resultado(DIGEST)), store.Ok
        )

    def test_un_claim_owner_respaldado_por_la_respuesta_del_dueno_acepta(self):
        self._intento_reportado()
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {
                "kind": "attempt",
                "unitId": "a",
                "attempt": 1,
                "cause": "owner:a1",
            },
            ["aceptar", "rechazar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        self.assertIsInstance(
            self.tienda.responder_excepcion(
                RAIZ, borrador["exceptionId"], "aceptar", ["david"]
            ),
            store.Ok,
        )
        claim_dueno = claim("a", 1, "a1")
        claim_dueno["verifier"] = {"principal": ["david"], "role": "owner"}
        claim_dueno["artifactRef"] = {
            "ref": f"exception:{borrador['exceptionId']}",
            "digest": DIGEST,
        }
        aceptacion = decision(
            claims=[claim_dueno],
            outcomes=[{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )
        self.assertIsInstance(self.tienda.aplicar(RAIZ, aceptacion, "dec-ok"), store.Ok)

    def test_un_claim_owner_sin_respuesta_del_dueno_no_acepta(self):
        self._intento_reportado()
        claim_falso = claim("a", 1, "a1")
        claim_falso["verifier"] = {"principal": ["claw"], "role": "owner"}
        negada = self.tienda.aplicar(
            RAIZ,
            decision(
                claims=[claim_falso],
                outcomes=[{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
            ),
            "dec-falsa",
        )
        self.assertEqual(negada.codigo, "criteria-unproven")
        vista = self.tienda.leer_vista(RAIZ, EntradasDelMundo.hechos_nulos())
        self.assertEqual(vista["unidades"][0]["state"], "reported")

    def test_reportar_resultado_pone_reported_en_su_misma_transaccion(self):
        self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-0")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "admitted", "t-1")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "running", "t-1")
        recibo = self.tienda.reportar_resultado(self.resultado(DIGEST))
        self.assertIsInstance(recibo, store.Ok)
        vista = self.tienda.leer_vista(RAIZ, EntradasDelMundo.hechos_nulos())
        self.assertEqual(vista["unidades"][0]["state"], "reported")

    def test_un_reintento_con_otro_digest_es_conflicto(self):
        self.tienda.aplicar(RAIZ, decision(bindings=[binding("a")]), "dec-0")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "admitted", "t-1")
        self.tienda.transicionar_intento(RAIZ, "a", 1, "running", "t-1")
        self.assertIsInstance(
            self.tienda.reportar_resultado(self.resultado(DIGEST)), store.Ok
        )
        repetido = self.tienda.reportar_resultado(self.resultado(DIGEST))
        self.assertEqual(repetido.valor, True)
        self.assertTrue(repetido.replayed)
        distinto = self.tienda.reportar_resultado(self.resultado("sha256:" + "b" * 64))
        self.assertEqual(distinto.codigo, "operation-conflict")

    @staticmethod
    def hechos_nulos():
        return {
            "principal": [],
            "asignaciones": [],
            "claims": [],
            "efectos": [],
        }


if __name__ == "__main__":
    unittest.main()
