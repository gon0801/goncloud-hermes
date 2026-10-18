#!/usr/bin/env python3
"""El ticker de raíces: conciliar, decidir y despachar por la bandeja de salida.

Aquí viven I03 (una sola C con dos tickers y el ACK perdido, con caída inyectada
en cada punto de la bandeja) e I04 (caídas entre commit/admisión/consumo) y la
parte de I12 que fija de dónde sale la política.
"""

import importlib
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

from conexiones import rastrear_conexiones  # noqa: E402

store = importlib.import_module("raices.store")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")
decidir = importlib.import_module("raices.decidir")

DobleKanban = kanban_port.DobleKanban
PuntoDeCaida = kanban_port.PuntoDeCaida
clave_de_tarjeta = kanban_port.clave_de_tarjeta

RAIZ = "root-abc"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}


def permisos_de_prueba():
    """Los permisos de H del escenario, explícitos: el None de producción
    niega (fail-closed), así que ningún test depende del omisión."""
    return {
        "acciones": ["read", "implement", "review"],
        "toolsets": {"codex": ["read", "write"]},
        "comandos": ["git", "python3"],
    }


def asignacion(adapter="codex", repo="owner-01/repo-a"):
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": adapter},
        "instructionRef": {"ref": f"artifact:inst-{adapter}", "digest": DIGEST},
        "resultContract": "review.v1",
        "inputRevision": {"kind": "code", "repository": repo, "sha": "0" * 40},
        "continuation": {"kind": "requester"},
    }


def meta_de():
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
            "units": [
                {
                    "unitId": "a",
                    "title": "Unidad a",
                    "dependsOn": [],
                    "criteria": [
                        {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                },
                {
                    "unitId": "b",
                    "title": "Unidad b",
                    "dependsOn": [],
                    "criteria": [
                        {"criterionId": "b1", "text": "b1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                },
                {
                    "unitId": "c",
                    "title": "Unidad c",
                    "dependsOn": ["a", "b"],
                    "criteria": [
                        {"criterionId": "c1", "text": "c1", "verifier": "verifier"}
                    ],
                    "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
                },
            ],
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def hechos_de_raiz():
    claims = []
    for unit_id, criterio in (("a", "a1"), ("b", "b1"), ("c", "c1")):
        claims.append(
            {
                "rootId": RAIZ,
                "unitId": unit_id,
                "attempt": 1,
                "criterionId": criterio,
                "planRevision": 1,
                "artifactRef": REF,
                "environment": {"host": "mac-local", "versions": {}},
                "role": "verifier",
                "procedure": "pytest",
                "outcome": "passed",
                "at": "2026-10-17T11:00:00Z",
                "expiresAt": None,
            }
        )
    return {
        "principal": ["david"],
        "asignaciones": [
            {"unitId": "a", "assignment": asignacion(), "relay": False},
            {"unitId": "b", "assignment": asignacion(), "relay": False},
            {"unitId": "c", "assignment": asignacion(), "relay": False},
        ],
        "claims": claims,
        "efectos": [],
    }


def resultado_aprobado():
    return {
        "resultContract": "review.v1",
        "verdict": "approved",
        "evidenceRef": REF,
        "resultDigest": DIGEST,
    }


class Escenario(unittest.TestCase):
    """A+B→C: dos vueltas deuran a y b, la tercera liga y despacha a c."""

    @staticmethod
    def completar_a_y_b(kanban):
        for unit_id in ("a", "b"):
            tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, unit_id, 1))
            kanban.crear_hecha(tarjeta.clave, resultado_aprobado())

    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.escenarios = 0

    def frescar(self):
        """Un escenario nuevo: base propia y kanban propio."""
        self.escenarios += 1
        tienda = store.Store(self.directorio.name + f"/{self.escenarios}.db")
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(meta_de())
        kanban = DobleKanban()
        return tienda, kanban

    def vuelta(self, tienda, kanban, hechos=None):
        return ticker.vuelta(
            tienda,
            kanban,
            {RAIZ: hechos or hechos_de_raiz()},
            permisos_h=permisos_de_prueba(),
        )

    def terminar_a_y_b(self, tienda, kanban, hechos=None):
        self.vuelta(tienda, kanban, hechos)
        for unit_id in ("a", "b"):
            tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, unit_id, 1))
            kanban.crear_hecha(tarjeta.clave, resultado_aprobado())
        return self.vuelta(tienda, kanban, hechos)


class I03Ticker(Escenario):
    def test_dos_tickers_y_el_ack_perdido_crea_exactamente_una_c(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        self.completar_a_y_b(kanban)
        clave_c = clave_de_tarjeta(RAIZ, "c", 1)
        kanban.caer_en(PuntoDeCaida.DESPUES_DE_TXN)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        self.assertIsNotNone(kanban.por_clave(clave_c))
        kanban.quitar_caida(PuntoDeCaida.DESPUES_DE_TXN)
        tienda2 = store.Store(tienda.ruta)
        self.addCleanup(tienda2.cerrar)
        self.vuelta(tienda2, kanban)
        self.assertEqual(len(kanban.creaciones), 3)
        self.assertEqual(len(kanban.creaciones_llamadas), 3)
        self.assertEqual(kanban.creaciones.count(clave_c), 1)
        self.assertEqual(tienda.intenciones_pendientes(), [])
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        estado_c = [d["state"] for d in vista["unidades"] if d["unitId"] == "c"]
        self.assertEqual(estado_c, ["admitted"])

    def test_dos_tickers_en_vivo_no_duplican_tarjetas(self):
        tienda, kanban = self.frescar()
        tienda2 = store.Store(tienda.ruta)
        self.addCleanup(tienda2.cerrar)
        informes = []
        cerrojo = threading.Lock()

        def correr(almacen):
            informe = self.vuelta(almacen, kanban)
            with cerrojo:
                informes.append(informe)

        hilos = [threading.Thread(target=correr, args=(t,)) for t in (tienda, tienda2)]
        for hilo in hilos:
            hilo.start()
        for hilo in hilos:
            hilo.join(10)
        self.assertEqual(len(informes), 2)
        for unit_id in ("a", "b"):
            clave = clave_de_tarjeta(RAIZ, unit_id, 1)
            self.assertEqual(kanban.creaciones.count(clave), 1)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        ligadas = sorted(d["unitId"] for d in vista["unidades"])
        self.assertEqual(ligadas, ["a", "b"])
        self.assertEqual(tienda.intenciones_pendientes(), [])
        estados = sorted(d["state"] for d in vista["unidades"])
        self.assertEqual(estados, ["admitted", "admitted"])

    def test_caida_en_cada_punto_de_la_bandeja_deja_una_sola_c(self):
        for punto in (
            PuntoDeCaida.ABRIR_TXN,
            PuntoDeCaida.ANTES_DE_BUSCAR,
            PuntoDeCaida.DESPUES_DE_CREAR,
            PuntoDeCaida.DESPUES_DE_TXN,
        ):
            with self.subTest(punto=punto):
                tienda, kanban = self.frescar()
                self.vuelta(tienda, kanban)
                self.completar_a_y_b(kanban)
                clave_c = clave_de_tarjeta(RAIZ, "c", 1)
                self.assertIsNone(kanban.por_clave(clave_c))
                kanban.caer_en(punto)
                with self.assertRaises(kanban_port.CaidaInyectada):
                    self.vuelta(tienda, kanban)
                kanban.quitar_caida(punto)
                self.vuelta(tienda, kanban)
                self.assertIsNotNone(kanban.por_clave(clave_c))
                self.assertEqual(kanban.creaciones.count(clave_c), 1)
                self.assertEqual(tienda.intenciones_pendientes(), [])


class I04Ticker(Escenario):
    def test_caida_entre_el_commit_y_la_admision(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        self.completar_a_y_b(kanban)
        clave_c = clave_de_tarjeta(RAIZ, "c", 1)
        kanban.caer_en(PuntoDeCaida.ABRIR_TXN)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        kanban.quitar_caida(PuntoDeCaida.ABRIR_TXN)
        self.assertIsNone(kanban.por_clave(clave_c))
        self.vuelta(tienda, kanban)
        self.assertIsNotNone(kanban.por_clave(clave_c))
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        estado_c = [d["state"] for d in vista["unidades"] if d["unitId"] == "c"]
        self.assertEqual(estado_c, ["admitted"])

    def test_caida_entre_la_admision_y_el_consumo(self):
        tienda, kanban = self.frescar()
        self.terminar_a_y_b(tienda, kanban)
        self.vuelta(tienda, kanban)
        clave_c = clave_de_tarjeta(RAIZ, "c", 1)
        kanban.crear_hecha(clave_c, resultado_aprobado())
        original = tienda.reportar_resultado

        def muere(documento):
            raise RuntimeError("el ticker muere entre la admisión y el consumo")

        tienda.reportar_resultado = muere
        with self.assertRaises(RuntimeError):
            self.vuelta(tienda, kanban)
        estado_tras_caida = [
            d["state"]
            for d in tienda.leer_vista(RAIZ, hechos_de_raiz())["unidades"]
            if d["unitId"] == "c"
        ]
        self.assertEqual(estado_tras_caida, ["running"])
        tienda.reportar_resultado = original
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(
            [d["state"] for d in vista["unidades"] if d["unitId"] == "c"],
            ["accepted"],
        )
        self.assertEqual(vista["resultados"], [])
        informe_de_mas = self.vuelta(tienda, kanban)
        vista_de_mas = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(
            [d["state"] for d in vista_de_mas["unidades"] if d["unitId"] == "c"],
            ["accepted"],
        )
        self.assertEqual(informe_de_mas.estado, "done")

    def test_un_resultado_invalido_registra_la_incidencia_y_no_fabrica_exito(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        kanban.crear_hecha(
            tarjeta.clave, {"resultContract": "review.v1", "verdict": "changes"}
        )
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        estados = [d["state"] for d in vista["unidades"] if d["unitId"] == "a"]
        self.assertEqual(estados[0], "failed")
        self.assertEqual(
            [(i["unitId"], i["incidentKey"]) for i in vista["incidencias"]],
            [("a", f"invalid-result:{tarjeta.id}")],
        )
        episodio = vista["incidencias"][0]["incident"]["episodeId"]
        identidad = decidir.digesto(["schema-invalid", "structured-result"])
        self.assertEqual(
            episodio,
            "invalid-result:" + decidir.digesto([tarjeta.id, identidad]),
        )
        self.assertNotEqual(episodio, tarjeta.id)
        self.assertEqual(vista["resultados"], [])

    def test_una_tarjeta_archivada_es_un_intento_muerto_con_incidencia(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        kanban.crear_archivada(tarjeta.clave)
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        unidad = [d for d in vista["unidades"] if d["unitId"] == "a"][0]
        self.assertEqual(unidad["state"], "cancelled")
        self.assertEqual(
            [(i["unitId"], i["incident"]) for i in vista["incidencias"]],
            [
                (
                    "a",
                    {
                        "kind": "recovery-transport-failure",
                        "evidenceRef": tarjeta.id,
                    },
                )
            ],
        )
        self.assertEqual(vista["resultados"], [])
        clave_dos = clave_de_tarjeta(RAIZ, "a", 2)
        self.assertIn(clave_dos, kanban.creaciones)
        self.assertIsNotNone(kanban.por_clave(clave_dos))

    def test_el_episodio_invalido_distingue_cargas_distintas(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        tarjeta_a = kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        tarjeta_b = kanban.por_clave(clave_de_tarjeta(RAIZ, "b", 1))
        kanban.crear_hecha(
            tarjeta_a.clave, {"resultContract": "review.v1", "verdict": "changes"}
        )
        kanban.crear_hecha(
            tarjeta_b.clave,
            {
                "resultContract": "review.v1",
                "verdict": "changes",
                "resultDigest": DIGEST,
            },
        )
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        episodios = {
            i["unitId"]: i["incident"]["episodeId"] for i in vista["incidencias"]
        }
        self.assertEqual(
            episodios["b"],
            "invalid-result:" + decidir.digesto([tarjeta_b.id, DIGEST]),
        )
        self.assertNotEqual(episodios["a"], episodios["b"])

    def test_tras_las_caidas_el_plan_termina_y_sigue(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        self.completar_a_y_b(kanban)
        clave_c = clave_de_tarjeta(RAIZ, "c", 1)
        kanban.caer_en(PuntoDeCaida.DESPUES_DE_CREAR)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        kanban.quitar_caida(PuntoDeCaida.DESPUES_DE_CREAR)
        self.vuelta(tienda, kanban)
        kanban.crear_hecha(clave_c, resultado_aprobado())
        informe = self.vuelta(tienda, kanban)
        self.assertEqual(informe.estado, "done")
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(
            sorted(d["state"] for d in vista["unidades"]),
            ["accepted", "accepted", "accepted"],
        )

    def test_las_plazas_por_perfil_sin_tope_no_salen_negativas(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, "a", 1))
        kanban.crear_hecha(
            tarjeta.clave,
            {
                "resultContract": "review.v1",
                "verdict": "changes",
                "findingsRef": REF,
                "resultDigest": DIGEST,
            },
        )
        informe = self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(vista["plazas"]["por_perfil"], {})
        self.assertEqual(informe.tarjetas_creadas, 1)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        estados = [d["state"] for d in vista["unidades"] if d["unitId"] == "a"]
        self.assertEqual(estados, ["failed", "admitted"])

    def test_una_raiz_en_pausa_no_despacha(self):
        tienda, kanban = self.frescar()
        kanban.caer_en(PuntoDeCaida.ABRIR_TXN)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        kanban.quitar_caida(PuntoDeCaida.ABRIR_TXN)
        tienda.control(
            {
                "schema": "sistema-agentes.control.v1",
                "rootId": RAIZ,
                "command": "pause",
                "commandId": "cmd-01",
                "expectedControlEpoch": 1,
                "reason": None,
                "issuedAt": "2026-10-17T11:00:00Z",
            }
        )
        informe = self.vuelta(tienda, kanban)
        self.assertEqual(informe.tarjetas_creadas, 0)
        self.assertEqual(len(tienda.intenciones_pendientes()), 2)


class ElCancelYLaBandeja(Escenario):
    @staticmethod
    def comando_cancel(command_id, epoch):
        return {
            "schema": "sistema-agentes.control.v1",
            "rootId": RAIZ,
            "command": "cancel",
            "commandId": command_id,
            "expectedControlEpoch": epoch,
            "reason": None,
            "issuedAt": "2026-10-17T11:00:00Z",
        }

    def test_el_cancel_bloquea_las_tarjetas_vivas_en_el_kanban(self):
        tienda, kanban = self.frescar()
        self.vuelta(tienda, kanban)
        kanban.crear_corriendo(clave_de_tarjeta(RAIZ, "a", 1))
        self.vuelta(tienda, kanban)
        recibo = tienda.control(self.comando_cancel("c1", 1)).valor
        self.assertEqual(recibo.state, "cancelled")
        self.vuelta(tienda, kanban)
        for unit_id in ("a", "b"):
            tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, unit_id, 1))
            self.assertIsNotNone(tarjeta)
            self.assertEqual(tarjeta.estado, "bloqueada")

    def test_el_despacho_en_carrera_con_el_cancel_no_deja_tarjeta_viva(self):
        tienda, kanban = self.frescar()
        otro = store.Store(tienda.ruta)
        self.addCleanup(otro.cerrar)
        original = kanban_port.asegurar_tarjeta

        def con_cancel(puerto, clave, spec):
            if tienda.estado_de_control(RAIZ) == "active":
                otro.control(self.comando_cancel("c1", 1))
            return original(puerto, clave, spec)

        kanban_port.asegurar_tarjeta = con_cancel
        try:
            self.vuelta(tienda, kanban)
        finally:
            kanban_port.asegurar_tarjeta = original
        self.assertEqual(tienda.estado_de_control(RAIZ), "cancelled")
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(
            sorted(d["state"] for d in vista["unidades"]),
            ["cancelled", "cancelled"],
        )
        self.vuelta(tienda, kanban)
        for clave in kanban.creaciones:
            self.assertEqual(kanban.por_clave(clave).estado, "bloqueada")

    def test_terminar_un_intento_anula_su_intencion(self):
        tienda, kanban = self.frescar()
        kanban.caer_en(PuntoDeCaida.ABRIR_TXN)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        kanban.quitar_caida(PuntoDeCaida.ABRIR_TXN)
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {
                "kind": "attempt",
                "unitId": "a",
                "attempt": 1,
                "cause": "incident:x",
            },
            ["listo", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        tienda._conexion.execute(
            "UPDATE excepciones SET response_json = ? WHERE exception_id = ?",
            (
                json.dumps(
                    {
                        "optionId": "cancelar",
                        "by": ["david"],
                        "at": "2026-10-17T11:00:00Z",
                    }
                ),
                borrador["exceptionId"],
            ),
        )
        self.vuelta(tienda, kanban)
        self.assertEqual(kanban.creaciones, [clave_de_tarjeta(RAIZ, "b", 1)])
        self.assertEqual(tienda.intenciones_pendientes(), [])


class ElCierreRojoYElCancelDelDueno(Escenario):
    """B15: el cancelar del dueño sobre CIERRE_ROJO cancela la raíz."""

    @staticmethod
    def meta_de_cierre():
        meta = meta_de()
        meta["plan"]["units"] = [
            {
                "unitId": "a",
                "title": "Unidad a",
                "dependsOn": [],
                "criteria": [
                    {"criterionId": "a1", "text": "a1", "verifier": "verifier"}
                ],
                "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
            },
            {
                "unitId": "b",
                "title": "Unidad b",
                "dependsOn": ["a"],
                "criteria": [
                    {"criterionId": "b1", "text": "b1", "verifier": "verifier"}
                ],
                "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
            },
        ]
        return meta

    def frescar_cierre(self):
        self.escenarios += 1
        tienda = store.Store(self.directorio.name + f"/{self.escenarios}.db")
        self.addCleanup(tienda.cerrar)
        tienda.abrir_raiz(self.meta_de_cierre())
        return tienda, DobleKanban()

    def test_cancelar_sobre_cierre_rojo_cancela_la_raiz(self):
        tienda, kanban = self.frescar_cierre()
        self.vuelta(tienda, kanban)
        kanban.crear_corriendo(clave_de_tarjeta(RAIZ, "a", 1))
        self.vuelta(tienda, kanban)
        bloqueante = decidir.borrador_excepcion(
            RAIZ,
            "INESPERADO",
            {
                "kind": "attempt",
                "unitId": "a",
                "attempt": 1,
                "cause": "incident:x",
            },
            ["seguir", "omitir", "cancelar"],
        )
        pregunta = decidir._decision_vacia(1, 1)
        pregunta["excepciones"] = [bloqueante]
        self.assertIsInstance(tienda.aplicar(RAIZ, pregunta, "dec-bloq"), store.Ok)
        self.assertIsInstance(
            tienda.responder_excepcion(
                RAIZ, bloqueante["exceptionId"], "omitir", ["david"]
            ),
            store.Ok,
        )
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        cierre = [
            e
            for e in vista["excepciones"]
            if e["type"] == "CIERRE_ROJO" and e["subject"]["kind"] == "root"
        ]
        self.assertEqual(len(cierre), 1)
        self.assertIsNone(cierre[0]["appliedBy"])
        self.assertIsInstance(
            tienda.responder_excepcion(
                RAIZ, cierre[0]["exceptionId"], "cancelar", ["david"]
            ),
            store.Ok,
        )
        self.vuelta(tienda, kanban)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        cierre = [
            e
            for e in vista["excepciones"]
            if e["type"] == "CIERRE_ROJO" and e["subject"]["kind"] == "root"
        ]
        self.assertIsNotNone(cierre[0]["appliedBy"])
        self.assertEqual(tienda.estado_de_control(RAIZ), "cancelled")
        tranquilidad = self.vuelta(tienda, kanban)
        self.assertEqual(tranquilidad.aplicadas, 0)
        self.assertEqual(tranquilidad.tarjetas_creadas, 0)
        vista = tienda.leer_vista(RAIZ, hechos_de_raiz())
        self.assertEqual(
            [e["type"] for e in vista["excepciones"]].count("CIERRE_ROJO"), 1
        )
        self.assertEqual(tienda.estado_de_control(RAIZ), "cancelled")


class LosHechosMalFormadosNoTumbanLaVuelta(Escenario):
    """F8: un hecho inválido se aísla; la vuelta entera no cae."""

    def test_la_vuelta_sobrevive_a_un_hecho_mal_formado(self):
        tienda, kanban = self.frescar()
        hechos = hechos_de_raiz()
        hechos["claims"].append(
            {
                "rootId": RAIZ,
                "unitId": "a",
                "attempt": 1,
                "criterionId": "a1",
                "outcome": "passed",
                "expiresAt": None,
            }
        )
        hechos["asignaciones"].insert(
            0,
            {
                "unitId": "a",
                "assignment": {"resultContract": "review.v1"},
                "relay": False,
            },
        )
        informe = self.vuelta(tienda, kanban, hechos)
        self.assertEqual(informe.aplicadas, 1)
        self.assertEqual(
            kanban.creaciones,
            [clave_de_tarjeta(RAIZ, "a", 1), clave_de_tarjeta(RAIZ, "b", 1)],
        )
        vista = tienda.leer_vista(RAIZ, hechos)
        ligada_a = [d["assignment"] for d in vista["unidades"] if d["unitId"] == "a"]
        self.assertEqual(ligada_a, [asignacion()])
        otra = self.vuelta(tienda, kanban, hechos)
        self.assertEqual(otra.tarjetas_creadas, 0)


class ElConflictoDeAplicarNoTumbaLosAvisos(Escenario):
    """F13: si aplicar devuelve conflicto, la vuelta sigue y los avisos de
    excepciones YA guardadas igual salen: la vuelta ya no marca noticed_at
    (F9: el checkpoint solo avanza cuando el emisor de la pasada del cron
    confirmó un messageId), así que la excepción sigue debiéndose y
    avisar_excepciones la manda pese al conflicto de aplicar."""

    def test_un_conflicto_de_aplicar_no_bloquea_el_renotice(self):
        tienda, kanban = self.frescar()
        # El reloj se fija desde el arranque: la primera notificación marca
        # noticed_at con la hora de la tienda y el renotice vence a la hora
        # simulada, no contra el reloj de pared (que caduca el caso).
        tienda.reloj = lambda: "2026-10-17T10:00:00Z"
        self.vuelta(tienda, kanban)
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "INESPERADO",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "incident:x"},
            ["seguir", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(tienda.aplicar(RAIZ, dec, "dec-exc"), store.Ok)
        self.assertIsInstance(
            tienda.notificar(RAIZ, [borrador["exceptionId"]]), store.Ok
        )
        tienda.reloj = lambda: "2026-10-18T00:00:00Z"
        tienda.registrar_incidencia(
            RAIZ,
            "a",
            1,
            "inc-capacidad",
            {"kind": "transport-unavailable", "episodeId": "launch-refused"},
        )
        original = tienda.aplicar

        def aplicar_en_conflicto(root_id, decision, decision_id):
            tienda.aplicar = original
            return store.Conflicto("root-not-active", {"state": "cancelled"})

        tienda.aplicar = aplicar_en_conflicto
        informe = self.vuelta(tienda, kanban)
        self.assertIn((RAIZ, "root-not-active"), informe.conflictos)
        fila = tienda._una_fila(
            "SELECT noticed_at FROM excepciones WHERE exception_id = ?",
            (borrador["exceptionId"],),
        )
        self.assertEqual(
            fila["noticed_at"],
            "2026-10-17T10:00:00Z",
            "la vuelta ya no avisa: el checkpoint es de la pasada del cron",
        )
        proyeccion = importlib.import_module("raices.proyeccion")
        desde = datetime(2026, 10, 18, 0, 0, 0, tzinfo=timezone.utc)
        informe_aviso = proyeccion.avisar_excepciones(
            tienda, ahora=desde, enviar=lambda _texto: 1
        )
        self.assertIn(borrador["exceptionId"], informe_aviso["avisadas"])


class I12PoliticaDelTicker(unittest.TestCase):
    def test_la_politica_sale_de_la_copia_fijada_con_la_decision_de_david(self):
        politicas = ticker.cargar_politicas()
        self.assertIn("quality-kit.regla4", politicas)
        regla4 = politicas["quality-kit.regla4"]
        self.assertEqual(regla4["roundsPerImplementer"], 5)
        self.assertEqual(regla4["relays"], 1)
        self.assertTrue(regla4["revisorExcluyeAutores"])
        self.assertFalse(regla4["segundoModelo"])
        self.assertEqual(regla4["decision"]["by"], "David")


if __name__ == "__main__":
    unittest.main()
