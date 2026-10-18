#!/usr/bin/env python3
"""El cron despertar-raices (SUA-07 H6b): le avisa a claw qué necesita cada
raíz viva, por la salida del cron no-agent con --deliver bot-chat:claw.

stdout vacío = corrida silenciosa (nada que avisar). El aviso sale si el
needs_digest cambió desde el último aviso o si venció RENOTICE_MS, y el
asiento (avisos_de_necesisidades) ocurre en la MISMA transacción que decide
imprimir. Sin briefs ni secretos en el texto.
"""

import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

from conexiones import rastrear_conexiones  # noqa: E402

instrucciones = importlib.import_module("raices.instrucciones")
store = importlib.import_module("raices.store")
decidir = importlib.import_module("raices.decidir")
despertar = importlib.import_module("raices.despertar")

RAIZ = "root-despertar"
DIGEST = "sha256:" + "a" * 64


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
            "planDigest": DIGEST,
            "requestDigest": DIGEST,
            "units": [unidad_de(u) for u in unidades],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


class Escenario(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.impresos: list[str] = []
        self.reloj = ["2026-10-18T12:00:00Z"]

    def abrir(self, unidades=("a",)):
        tienda = store.Store(
            Path(self.directorio.name) / "sistema-agentes.db",
            reloj=lambda: self.reloj[0],
        )
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(tienda.abrir_raiz(meta_de(unidades)), store.Ok)
        return tienda

    def correr(self):
        """Una corrida del despertar sobre el home del escenario."""
        return despertar.correr(
            home=Path(self.directorio.name),
            escribir=self.impresos.append,
            reloj=lambda: self.reloj[0],
        )

    def asentar(self, tienda, necesidades):
        tienda.asentar_necesidades(RAIZ, necesidades)

    def aviso_de(self, root_id):
        import sqlite3

        conexion = sqlite3.connect(Path(self.directorio.name) / "sistema-agentes.db")
        try:
            fila = conexion.execute(
                "SELECT needs_digest, avisado_at FROM avisos_de_necesidades"
                " WHERE root_id = ?",
                (root_id,),
            ).fetchone()
        finally:
            conexion.close()
        return fila


class Avisos(Escenario):
    def test_el_primer_aviso_sale_y_queda_asentado(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        tienda.cerrar()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(len(self.impresos), 1)
        texto = self.impresos[0]
        self.assertIn(RAIZ, texto)
        self.assertIn("assignment", texto)
        self.assertIn("first", texto)
        self.assertIn("goncloud_root_read", texto)
        self.assertIn("goncloud_root_instruction_put", texto)
        self.assertIn("goncloud_root_reconcile", texto)
        self.assertIn("raices", texto)
        fila = self.aviso_de(RAIZ)
        self.assertIsNotNone(fila)
        self.assertEqual(fila[1], "2026-10-18T12:00:00Z")
        self.assertEqual(
            fila[0],
            decidir.digesto(
                [
                    {
                        "kind": "assignment",
                        "unitId": "a",
                        "reason": "first",
                        "findingsRef": None,
                        "findingsRefs": [],
                    }
                ]
            ),
        )

    def test_sin_cambio_de_digest_es_silencio(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        tienda.cerrar()
        self.correr()
        self.impresos.clear()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_el_cambio_de_digest_vuelve_a_avisar(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        tienda.cerrar()
        self.correr()
        self.impresos.clear()
        tienda = store.Store(
            Path(self.directorio.name) / "sistema-agentes.db",
            reloj=lambda: self.reloj[0],
        )
        self.addCleanup(tienda.cerrar)
        self.asentar(
            tienda,
            [
                decidir.NecesidadVerificacion("a", 1, ["a1"]),
            ],
        )
        tienda.cerrar()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(len(self.impresos), 1)
        self.assertIn("verify", self.impresos[0])
        self.assertIn("a1", self.impresos[0])

    def test_el_renotice_vencido_vuelve_a_avisar(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        tienda.cerrar()
        self.correr()
        self.impresos.clear()
        self.reloj[0] = "2026-10-18T13:00:01Z"
        self.assertEqual(self.correr(), 0)
        self.assertEqual(len(self.impresos), 1)
        fila = self.aviso_de(RAIZ)
        self.assertEqual(fila[1], "2026-10-18T13:00:01Z")

    def test_el_renotice_a_punto_de_vencer_sigue_callado(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        tienda.cerrar()
        self.correr()
        self.impresos.clear()
        self.reloj[0] = "2026-10-18T12:59:59Z"
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_la_raiz_cancelada_es_silencio(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        respuesta = tienda.control(
            {
                "schema": "sistema-agentes.control.v1",
                "rootId": RAIZ,
                "command": "cancel",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
                "reason": None,
                "issuedAt": self.reloj[0],
            }
        )
        self.assertIsInstance(respuesta, store.Ok)
        tienda.cerrar()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_la_raiz_pausada_es_silencio(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadAsignacion("a", "first", None, [])])
        respuesta = tienda.control(
            {
                "schema": "sistema-agentes.control.v1",
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
                "reason": None,
                "issuedAt": self.reloj[0],
            }
        )
        self.assertIsInstance(respuesta, store.Ok)
        tienda.cerrar()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_sin_necesidades_es_silencio(self):
        tienda = self.abrir()
        self.asentar(tienda, [])
        tienda.cerrar()
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_la_necesidad_verify_lleva_criterio_e_intento(self):
        tienda = self.abrir()
        self.asentar(tienda, [decidir.NecesidadVerificacion("a", 2, ["a1", "a2"])])
        tienda.cerrar()
        self.correr()
        texto = self.impresos[0]
        self.assertIn("verify", texto)
        self.assertIn("a1", texto)
        self.assertIn("a2", texto)
        self.assertIn("2", texto)

    def test_el_texto_no_lleva_briefs(self):
        tienda = self.abrir()
        ref, _digest = instrucciones.guardar(
            tienda.dir_de_instrucciones, "SECRETO del brief que no debe salir"
        )
        self.asentar(
            tienda,
            [
                decidir.NecesidadAsignacion(
                    "a", "correction", ref, [ref, "otro:hallazgo"]
                )
            ],
        )
        tienda.cerrar()
        self.correr()
        texto = self.impresos[0]
        self.assertNotIn("SECRETO", texto)
        self.assertNotIn(ref, texto)


class Salida(Escenario):
    def test_sin_store_el_cron_crea_el_home_y_sale_silencioso(self):
        self.assertEqual(self.correr(), 0)
        self.assertEqual(self.impresos, [])

    def test_main_sin_home_imprime_error_y_sale_uno(self):
        import io
        import os
        from contextlib import redirect_stdout

        entorno = {k: v for k, v in os.environ.items() if k != "HERMES_HOME"}
        salida = io.StringIO()
        with (
            unittest.mock.patch.dict(os.environ, entorno, clear=True),
            redirect_stdout(salida),
        ):
            codigo = despertar.main()
        self.assertEqual(codigo, 1)
        self.assertIn("ERROR despertar raices:", salida.getvalue())


class E2eDelBriefH6b(Escenario):
    """El e2e del brief sin inyectar nada a mano: submit -> instruction_put
    -> reconcile -> cron ticker -> tarjeta -> resultado review.v1 del doble
    -> despertar imprime la necesidad verify -> instruction_put (evidencia)
    -> reconcile con claim passed -> ticker -> unidad aceptada y raíz
    terminada."""

    def setUp(self):
        super().setUp()
        import json

        import hermes_falso
        from yaml_doble import YamlDoble, yaml_real

        hermes_falso.instalar()
        self.addCleanup(hermes_falso.desinstalar)
        self.permisos = importlib.import_module("raices.permisos")
        self._mock_yaml = mock.patch.object(
            self.permisos, "yaml", yaml_real or YamlDoble
        )
        self._mock_yaml.start()
        self.addCleanup(self._mock_yaml.stop)
        # Las herramientas resuelven su home del entorno y la puerta real de
        # H4a cierra en la Mac (no hay gateway vivo): abierta inyectada.
        self._mock_home = mock.patch.dict(
            "os.environ", {"HERMES_HOME": str(Path(self.directorio.name))}
        )
        self._mock_home.start()
        self.addCleanup(self._mock_home.stop)
        capacidades = importlib.import_module("raices.capacidades")
        self._mock_puerta = mock.patch.object(
            capacidades, "abrir_ruta", return_value=(True, None)
        )
        self._mock_puerta.start()
        self.addCleanup(self._mock_puerta.stop)
        raices = Path(self.directorio.name) / "raices"
        raices.mkdir(parents=True, exist_ok=True)
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
        perfil = Path(self.directorio.name) / "profiles" / "codex"
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
        (perfil / "config.yaml").write_text(
            "platform_toolsets:\n  cli:\n    - coding\n    - kanban\n",
            encoding="utf-8",
        )

    def cron(self):
        cron = importlib.import_module("raices.cron_ticker")
        with mock.patch.object(self.permisos, "yaml", self.permisos.yaml):
            return cron.correr(
                home=Path(self.directorio.name),
                ruta_abierta=lambda: (True, None),
                enviar=lambda _texto: 42,
            )

    def test_del_submit_a_la_raiz_terminada(self):
        herramientas = importlib.import_module("raices.herramientas")
        kanban_mod = importlib.import_module("raices.kanban")

        recibo = herramientas.enviar_raiz({"plan": meta_de()}, "claw")
        self.assertNotIn("error", recibo)
        brief = herramientas.guardar_instruccion(
            {"text": "brief del e2e H6b: unidad a"}, "claw"
        )
        vinculo = {
            "unitId": "a",
            "assignment": {
                "target": {"kind": "cli", "hostId": "gon-pc", "adapterId": "codex"},
                "instructionRef": brief,
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
        reconciliado = herramientas.reconciliar_raiz(
            {"rootId": RAIZ, "asignaciones": [vinculo]}, "claw"
        )
        self.assertNotIn("error", reconciliado)

        # El ticker despacha la tarjeta con el TEXTO del brief.
        self.assertEqual(self.cron(), 0)
        puerto = kanban_mod.KanbanHermes(Path(self.directorio.name) / "kanban.db")
        self.addCleanup(puerto.cerrar)
        clave = f"sa:{RAIZ}:a:1"
        tarjeta = puerto.leer_estado(clave)
        self.assertIsNotNone(tarjeta)
        # El doble completa la tarjeta con un resultado review.v1 aprobado.
        with puerto.write_txn() as escritura:
            escritura.completar(
                tarjeta.id,
                {
                    "resultContract": "review.v1",
                    "verdict": "approved",
                    "evidenceRef": {"ref": "artifact:x", "digest": DIGEST},
                    "resultDigest": DIGEST,
                },
            )
        puerto.cerrar()

        # La vuelta concilia el resultado: la unidad queda reported y nace la
        # necesidad verify.
        self.assertEqual(self.cron(), 0)
        leido = herramientas.leer_raiz({"rootId": RAIZ}, "claw")
        estados = {u["unitId"]: u["state"] for u in leido["unidades"]}
        self.assertEqual(estados["a"], "reported")
        self.assertEqual(
            [(n["kind"], n.get("criteria")) for n in leido["necesidades"]["items"]],
            [("verify", ["a1"])],
        )

        # El despertar le avisa a claw esa necesidad.
        self.impresos.clear()
        self.assertEqual(
            despertar.correr(
                home=Path(self.directorio.name),
                escribir=self.impresos.append,
                reloj=lambda: self.reloj[0],
            ),
            0,
        )
        self.assertEqual(len(self.impresos), 1)
        self.assertIn("verify", self.impresos[0])
        self.assertIn("a1", self.impresos[0])

        # claw contesta: sube la evidencia y trae el claim passed.
        evidencia = herramientas.guardar_instruccion(
            {"text": "evidencia del e2e H6b: unittest verde en el head"}, "claw"
        )
        claim = {
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "artifactRef": evidencia,
            "environment": {"host": "mac-local", "versions": {}},
            "procedure": "python3 -m unittest",
            "outcome": "passed",
            "at": "2026-10-18T12:00:00Z",
            "expiresAt": None,
            "role": "verifier",
        }
        contestado = herramientas.reconciliar_raiz(
            {"rootId": RAIZ, "asignaciones": [], "claims": [claim]}, "claw"
        )
        self.assertNotIn("error", contestado)
        self.assertEqual(
            contestado["claims"],
            [{"unitId": "a", "criterionId": "a1", "replayed": False}],
        )

        # La última vuelta acepta la unidad y termina la raíz.
        self.assertEqual(self.cron(), 0)
        leido = herramientas.leer_raiz({"rootId": RAIZ}, "claw")
        estados = {u["unitId"]: u["state"] for u in leido["unidades"]}
        self.assertEqual(estados["a"], "accepted")
        self.assertEqual(leido["meta"]["control"]["state"], "active")
        self.assertEqual(leido["necesidades"]["items"], [])
        # Y el despertar ya no tiene nada que decir.
        self.impresos.clear()
        self.assertEqual(
            despertar.correr(
                home=Path(self.directorio.name),
                escribir=self.impresos.append,
                reloj=lambda: self.reloj[0],
            ),
            0,
        )
        self.assertEqual(self.impresos, [])


if __name__ == "__main__":
    unittest.main()
