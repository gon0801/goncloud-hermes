#!/usr/bin/env python3
"""Claims de verificación por reconcile (SUA-07 H6b): el espejo de
managedRootFactsSchema de R — evidenceClaim sin verifier ni evidenceId, más
role — validado con la misma vara con la que decidir siembra, persistido con
clave estable y consumido en aplicar igual que las asignaciones de H6a.

El artifactRef del claim resuelve en el MISMO store content-addressed de los
briefs: la evidencia sube con goncloud_root_instruction_put.
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from conexiones import rastrear_conexiones  # noqa: E402

instrucciones = importlib.import_module("raices.instrucciones")
store = importlib.import_module("raices.store")
decidir = importlib.import_module("raices.decidir")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")

DobleKanban = kanban_port.DobleKanban
clave_de_tarjeta = kanban_port.clave_de_tarjeta

RAIZ = "root-h6b"


def permisos_de_prueba():
    return {
        "acciones": ["read", "implement", "review"],
        "toolsets": {"codex": ["read", "write"]},
        "comandos": ["git", "python3"],
    }


def unidad_de(unit_id):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": [],
        "criteria": [
            {
                "criterionId": f"{unit_id}1",
                "text": f"{unit_id}1",
                "verifier": "verifier",
            }
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
    }


def meta_de(unidades=("a",)):
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
            "planDigest": "sha256:" + "a" * 64,
            "requestDigest": "sha256:" + "a" * 64,
            "units": [unidad_de(u) for u in unidades],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def resultado_aprobado(digest):
    return {
        "resultContract": "review.v1",
        "verdict": "approved",
        "evidenceRef": {"ref": "artifact:x", "digest": digest},
        "resultDigest": digest,
    }


class Escenario(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio)

    def abrir(self, unidades=("a",)):
        tienda = store.Store(Path(self.directorio.name) / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(tienda.abrir_raiz(meta_de(unidades)), store.Ok)
        return tienda

    @property
    def dir_de_instrucciones(self):
        return Path(self.directorio.name) / "raices" / "instrucciones"

    def evidencia_de(self, texto="corrida de pytest, todo verde"):
        return instrucciones.guardar(self.dir_de_instrucciones, texto)

    def claim_de(
        self,
        unit_id="a",
        attempt=1,
        criterio="a1",
        role="verifier",
        outcome="passed",
        texto="corrida de pytest, todo verde",
    ):
        ref, digest = self.evidencia_de(texto)
        return {
            "unitId": unit_id,
            "attempt": attempt,
            "criterionId": criterio,
            "artifactRef": {"ref": ref, "digest": digest},
            "environment": {"host": "mac-local", "versions": {}},
            "procedure": "python3 -m unittest",
            "outcome": outcome,
            "at": "2026-10-18T11:00:00Z",
            "expiresAt": None,
            "role": role,
        }

    def asignacion_de(self, texto="brief de la unidad a", unit_id="a"):
        ref, digest = instrucciones.guardar(self.dir_de_instrucciones, texto)
        return {
            "unitId": unit_id,
            "assignment": {
                "target": {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
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

    def hechos_vacios(self):
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}


class ReconcileDeClaims(Escenario):
    def reconciliar(self, tienda, claims, asignaciones=()):
        return tienda.reconciliar(RAIZ, list(asignaciones), list(claims))

    def test_persiste_el_claim_y_lo_transporta_hechos_por_raiz(self):
        tienda = self.abrir()
        claim = self.claim_de()
        recibo = self.reconciliar(tienda, [claim])
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(recibo.valor.root_id, RAIZ)
        self.assertEqual(recibo.valor.plan_revision, 1)
        self.assertEqual(
            recibo.valor.claims,
            [{"unitId": "a", "criterionId": "a1", "replayed": False}],
        )
        hechos = tienda.hechos_por_raiz()[RAIZ]
        self.assertEqual(hechos["principal"], ["claw"])
        self.assertEqual(len(hechos["claims"]), 1)
        guardado = hechos["claims"][0]
        self.assertEqual(guardado["rootId"], RAIZ)
        self.assertEqual(guardado["planRevision"], 1)
        self.assertEqual(guardado["role"], "verifier")
        self.assertNotIn("verifier", guardado)
        self.assertNotIn("evidenceId", guardado)
        self.assertEqual(guardado["artifactRef"], claim["artifactRef"])

    def test_el_artifact_ausente_rechaza_sin_filas(self):
        tienda = self.abrir()
        claim = self.claim_de()
        os_path = (
            self.dir_de_instrucciones / claim["artifactRef"]["ref"][len("artifact:") :]
        )
        # El artefacto queda 0o444; Windows no borra un read-only: quitar el
        # modo antes, como en las pruebas que lo adulteran.
        os_path.chmod(0o644)
        os_path.unlink()
        recibo = self.reconciliar(tienda, [claim])
        self.assertEqual(recibo.codigo, "instruccion-invalida")
        self.assertEqual(recibo.detalle["unitId"], "a")
        self.assertEqual(recibo.detalle["criterionId"], "a1")
        self.assertEqual(recibo.detalle["motivo"], "instruccion-ausente")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_el_digest_falso_rechaza_sin_filas(self):
        tienda = self.abrir()
        claim = self.claim_de()
        claim["artifactRef"]["digest"] = "sha256:" + "c" * 64
        recibo = self.reconciliar(tienda, [claim])
        self.assertEqual(recibo.codigo, "instruccion-invalida")
        self.assertEqual(recibo.detalle["motivo"], "digest-distinto")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_el_role_reviewer_es_rechazado(self):
        tienda = self.abrir()
        recibo = self.reconciliar(tienda, [self.claim_de(role="reviewer")])
        self.assertEqual(recibo.codigo, "documento-invalido")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_un_claim_que_el_contrato_rechaza_no_se_guarda(self):
        tienda = self.abrir()
        roto = self.claim_de()
        roto["outcome"] = "tal-vez"
        self.assertEqual(self.reconciliar(tienda, [roto]).codigo, "documento-invalido")
        sin_fecha = self.claim_de()
        sin_fecha["at"] = "18/10/2026"
        self.assertEqual(
            self.reconciliar(tienda, [sin_fecha]).codigo, "documento-invalido"
        )
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_el_reintento_exacto_es_replay_y_no_duplica(self):
        tienda = self.abrir()
        claim = self.claim_de()
        self.assertIsInstance(self.reconciliar(tienda, [claim]), store.Ok)
        repetido = self.reconciliar(tienda, [claim])
        self.assertIsInstance(repetido, store.Ok)
        self.assertEqual(
            repetido.valor.claims,
            [{"unitId": "a", "criterionId": "a1", "replayed": True}],
        )
        self.assertEqual(len(tienda.hechos_por_raiz()[RAIZ]["claims"]), 1)

    def test_otro_claim_con_la_fila_viva_es_operation_conflict(self):
        tienda = self.abrir()
        primero = self.claim_de(texto="primera corrida")
        self.assertIsInstance(self.reconciliar(tienda, [primero]), store.Ok)
        recibo = self.reconciliar(tienda, [self.claim_de(texto="segunda corrida")])
        self.assertEqual(recibo.codigo, "operation-conflict")
        self.assertEqual(recibo.detalle, {"unitId": "a", "criterionId": "a1"})
        vivos = tienda.hechos_por_raiz()[RAIZ]["claims"]
        self.assertEqual(
            vivos[0]["artifactRef"],
            primero["artifactRef"],
            "la fila viva conserva el primer claim",
        )

    def test_la_fila_consumida_con_otro_contenido_abre_un_ciclo_nuevo(self):
        tienda = self.abrir()
        claim = self.claim_de()
        self.assertIsInstance(self.reconciliar(tienda, [claim]), store.Ok)
        self.consumir_claims(tienda)
        self.assertEqual(tienda.hechos_por_raiz(), {})
        nuevo = self.claim_de(outcome="failed", texto="corrida roja")
        recibo = self.reconciliar(tienda, [nuevo])
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(
            recibo.valor.claims,
            [{"unitId": "a", "criterionId": "a1", "replayed": False}],
        )

    def consumir_claims(self, tienda):
        """Una decisión con los hechos del store consume las filas de claims
        en la MISMA transacción que persiste la evidencia."""
        vista = tienda.leer_vista(RAIZ, tienda.hechos_por_raiz()[RAIZ])
        propuesta = decidir.decidir(vista)
        self.assertTrue(propuesta.decision["claims"])
        respuesta = tienda.aplicar(RAIZ, propuesta.decision, "dec-claims-1")
        self.assertIsInstance(respuesta, store.Ok)

    def test_el_consumo_ocurre_en_aplicar_como_el_de_asignaciones(self):
        tienda = self.abrir()
        self.assertIsInstance(self.reconciliar(tienda, [self.claim_de()]), store.Ok)
        self.consumir_claims(tienda)
        self.assertEqual(
            tienda.hechos_por_raiz(),
            {},
            "la fila del claim se consumió al persistir la evidencia",
        )
        vista = tienda.leer_vista(RAIZ, self.hechos_vacios())
        self.assertEqual(len(vista["evidencia"]), 1)
        documento = vista["evidencia"][0]
        self.assertEqual(documento["verifier"]["principal"], ["claw"])
        self.assertEqual(documento["verifier"]["role"], "verifier")
        self.assertEqual(documento["unitId"], "a")
        self.assertEqual(documento["criterionId"], "a1")

    def test_un_claim_con_schema_se_siembra_y_se_consumo_igual(self):
        # F4 (hermes#34): el modelo puede imitar el contrato y mandar "schema"
        # en el claim. El digesto de la fila y el del consumo tienen que
        # calcularse sobre el MISMO documento o la fila queda viva para
        # siempre (operation-conflict en la corrección).
        tienda = self.abrir()
        claim = self.claim_de()
        claim["schema"] = "sistema-agentes.evidencia.v1"
        self.assertIsInstance(self.reconciliar(tienda, [claim]), store.Ok)
        self.consumir_claims(tienda)
        self.assertEqual(
            tienda.hechos_por_raiz(),
            {},
            "la fila del claim con schema se consumió en aplicar",
        )
        # Un claim corregido para la misma clave ya no da operation-conflict.
        corregido = self.claim_de(outcome="failed", texto="corrida roja corregida")
        recibo = self.reconciliar(tienda, [corregido])
        self.assertIsInstance(recibo, store.Ok, getattr(recibo, "codigo", None))
        self.assertEqual(
            recibo.valor.claims,
            [{"unitId": "a", "criterionId": "a1", "replayed": False}],
        )

    def test_una_raiz_desconocida_se_rechaza(self):
        tienda = self.abrir()
        recibo = tienda.reconciliar("root-nadie", [], [self.claim_de()])
        self.assertEqual(recibo.codigo, "raiz-desconocida")

    def test_un_claim_de_una_unidad_fuera_del_plan_se_rechaza_sin_filas(self):
        tienda = self.abrir(unidades=("a",))
        recibo = self.reconciliar(tienda, [self.claim_de(unit_id="nadie")])
        self.assertEqual(recibo.codigo, "unidad-desconocida")
        self.assertEqual(recibo.detalle, {"unitId": "nadie"})
        self.assertEqual(tienda.hechos_por_raiz(), {}, "cero filas")

    def test_un_claim_de_una_raiz_no_activa_se_rechaza_sin_filas(self):
        tienda = self.abrir()
        self.assertIsInstance(
            tienda.control(
                {
                    "schema": "sistema-agentes.control.v1",
                    "rootId": RAIZ,
                    "command": "pause",
                    "commandId": "cmd-pausa",
                    "expectedControlEpoch": 1,
                    "reason": "prueba",
                    "issuedAt": "2026-10-18T12:00:00Z",
                }
            ),
            store.Ok,
        )
        recibo = self.reconciliar(tienda, [self.claim_de()])
        self.assertEqual(recibo.codigo, "raiz-no-activa")
        self.assertEqual(recibo.detalle, {"state": "paused"})
        self.assertEqual(tienda.hechos_por_raiz(), {}, "cero filas")

    def test_un_claim_de_una_revision_anterior_no_aparece_en_hechos(self):
        tienda = self.abrir()
        self.assertIsInstance(self.reconciliar(tienda, [self.claim_de()]), store.Ok)
        # Una revisión nueva insertada a mano deja la fila del claim en la
        # revisión 1, fuera de la vigente: hechos_por_raiz solo entrega la
        # vigente (igual que las asignaciones desde H6a r3).
        tienda._conexion.execute(
            "INSERT INTO revisiones VALUES (?,?,?,?,?)",
            (RAIZ, 2, "sha256:" + "b" * 64, "sha256:" + "b" * 64, "[]"),
        )
        self.assertEqual(tienda.hechos_por_raiz(), {})


class AceptacionPorClaim(Escenario):
    """claim passed + resultado review.v1 => la unidad queda aceptada y la
    raíz terminada, sin inyectar hechos a mano: todo por el store."""

    def vuelta(self, tienda, kanban):
        return ticker.vuelta(
            tienda,
            kanban,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos_de_prueba(),
        )

    def test_el_claim_passed_con_resultado_acepta_la_unidad(self):
        tienda = self.abrir()
        self.assertIsInstance(
            tienda.reconciliar(RAIZ, [self.asignacion_de()], []), store.Ok
        )
        kanban = DobleKanban()
        self.assertEqual(self.vuelta(tienda, kanban).tarjetas_creadas, 1)

        clave = clave_de_tarjeta(RAIZ, "a", 1)
        kanban.crear_corriendo(clave)
        kanban.crear_hecha(clave, resultado_aprobado("sha256:" + "d" * 64))
        self.assertEqual(self.vuelta(tienda, kanban).conciliadas >= 1, True)
        vista = tienda.leer_vista(RAIZ, self.hechos_vacios())
        self.assertEqual(vista["unidades"][0]["state"], "reported")
        necesidades = tienda.necesidades_de(RAIZ)
        self.assertEqual(
            [n["kind"] for n in necesidades["items"]],
            ["verify"],
            "sin claim hay necesidad de verificación",
        )

        self.assertIsInstance(tienda.reconciliar(RAIZ, [], [self.claim_de()]), store.Ok)
        informe = self.vuelta(tienda, kanban)
        self.assertEqual(informe.estado, "done")
        vista = tienda.leer_vista(RAIZ, self.hechos_vacios())
        self.assertEqual(vista["unidades"][0]["state"], "accepted")
        self.assertEqual(tienda.hechos_por_raiz(), {})
        self.assertEqual(tienda.necesidades_de(RAIZ)["items"], [])


if __name__ == "__main__":
    unittest.main()
