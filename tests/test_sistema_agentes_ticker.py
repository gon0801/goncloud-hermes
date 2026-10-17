#!/usr/bin/env python3
"""El ticker de raíces: conciliar, decidir y despachar por la bandeja de salida.

Aquí viven I03 (una sola C con dos tickers y el ACK perdido, con caída inyectada
en cada punto de la bandeja) e I04 (caídas entre commit/admisión/consumo) y la
parte de I12 que fija de dónde sale la política.
"""

import importlib
import sys
import tempfile
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

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

    def setUp(self):
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
        return ticker.vuelta(tienda, kanban, {RAIZ: hechos or hechos_de_raiz()})

    def terminar_a_y_b(self, tienda, kanban, hechos=None):
        self.vuelta(tienda, kanban, hechos)
        for unit_id in ("a", "b"):
            tarjeta = kanban.por_clave(clave_de_tarjeta(RAIZ, unit_id, 1))
            kanban.crear_hecha(tarjeta.clave, resultado_aprobado())
        return self.vuelta(tienda, kanban, hechos)


class I03Ticker(Escenario):
    def test_dos_tickers_y_el_ack_perdido_crea_exactamente_una_c(self):
        tienda, kanban = self.frescar()
        self.terminar_a_y_b(tienda, kanban)
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
                self.terminar_a_y_b(tienda, kanban)
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
        self.terminar_a_y_b(tienda, kanban)
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
        self.assertEqual(vista["resultados"], [])

    def test_tras_las_caidas_el_plan_termina_y_sigue(self):
        tienda, kanban = self.frescar()
        self.terminar_a_y_b(tienda, kanban)
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
