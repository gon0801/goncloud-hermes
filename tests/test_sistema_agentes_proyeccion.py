#!/usr/bin/env python3
"""La proyección de H (I14) y sus casos hostiles (I16): del store sale un
snapshot con la forma de managedTasks.root.read de R, lo deriva `derivar` de
la copia fijada y lo escribe atómico en <dir>/proyecciones/<rootId>.json."""

import importlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

store = importlib.import_module("raices.store")
proyeccion = importlib.import_module("raices.proyeccion")
compartido = importlib.import_module("raices.compartido")

derivar = compartido.derivar.derivar

RAIZ = "root-proy"
AHORA = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
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


def meta_de(unidades, root_id=RAIZ):
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
            "destinations": ["dest-a"],
            "deadline": None,
            "mode": "implement",
        },
        "budget": presupuesto(),
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


def unidad(unit_id, criterio, rol="verifier"):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": [],
        "criteria": [
            {"criterionId": criterio, "text": f"Criterio {criterio}", "verifier": rol}
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
    }


def binding(unit_id):
    return {"unitId": unit_id, "assignment": asignacion(), "writers": []}


def decision(bindings=(), efectos=()):
    return {
        "expected_plan_revision": 1,
        "expected_control_epoch": 1,
        "consumidos": [],
        "claims": [],
        "outcomes": [],
        "bindings": list(bindings),
        "efectos": list(efectos),
        "contadores": [],
        "excepciones": [],
        "respuestas_aplicadas": [],
    }


def hechos_nulos():
    return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}


class Escenario(unittest.TestCase):
    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio)
        self.tienda = store.Store(Path(self.directorio.name) / "db.sqlite")
        self.addCleanup(self.tienda.cerrar)
        self.destino = Path(self.directorio.name) / "raices"

    def abrir(self, unidades, root_id=RAIZ):
        self.tienda.abrir_raiz(meta_de(unidades, root_id))

    def ligar_y_arrancar(self, unit_id, root_id=RAIZ, task_id=None):
        self.tienda.aplicar(
            root_id, decision(bindings=[binding(unit_id)]), f"dec-{root_id}-{unit_id}"
        )
        self.tienda.reclamar_intencion(root_id, unit_id, 1)
        self.tienda.admitir_intento(root_id, unit_id, 1, task_id or f"t-{unit_id}")
        self.tienda.transicionar_intento(
            root_id, unit_id, 1, "running", task_id or f"t-{unit_id}"
        )

    def proyectar(self, root_id=RAIZ):
        return proyeccion.proyectar(
            self.tienda,
            root_id,
            ahora=AHORA,
            dir_destino=self.destino,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )


class I14Proyeccion(Escenario):
    def test_la_de_h_coincide_con_derivar_sobre_el_mismo_snapshot(self):
        self.abrir([unidad("a", "a1")])
        self.ligar_y_arrancar("a")
        snapshot = self.tienda.snapshot_raiz(RAIZ)
        esperada = derivar(
            snapshot,
            AHORA,
            300000,
            300000,
            {},
            host_gateway="mac-local",
        )
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        self.assertEqual(escrita, esperada)
        self.assertEqual(escrita["schema"], "sistema-agentes.proyeccion.v1")
        ruta = self.destino / "proyecciones" / f"{RAIZ}.json"
        self.assertEqual(json.loads(ruta.read_text()), esperada)

    def test_dos_raices_y_unidades_sin_reporte_se_representan(self):
        self.abrir([unidad("a", "a1"), unidad("b", "b1", rol="ci")])
        self.abrir([unidad("x", "x1")], root_id="root-otra")
        self.ligar_y_arrancar("a")
        self.ligar_y_arrancar("x", root_id="root-otra", task_id="t-x")
        primera, errores = self.proyectar()
        segunda, errores = self.proyectar("root-otra")
        self.assertEqual(errores, [])
        self.assertTrue((self.destino / "proyecciones" / f"{RAIZ}.json").exists())
        self.assertTrue((self.destino / "proyecciones" / "root-otra.json").exists())
        estados_a = {f["unitId"]: f["state"] for f in primera["units"]}
        self.assertEqual(estados_a, {"a": "running", "b": "ready"})
        fila_b = next(f for f in primera["units"] if f["unitId"] == "b")
        self.assertEqual(fila_b["executor"], "sin-asignar")
        fila_a = next(f for f in primera["units"] if f["unitId"] == "a")
        self.assertEqual(fila_a["executor"], "cli:codex")
        self.assertEqual(fila_a["modelId"], None)
        self.assertEqual(fila_a["error"], None)

    def test_ci_en_espera_no_cuenta_como_observado(self):
        self.abrir([unidad("a", "a1", rol="verifier"), unidad("c", "c1", rol="ci")])
        self.ligar_y_arrancar("a")
        self.ligar_y_arrancar("c")
        self.tienda.reportar_resultado(
            {
                "rootId": RAIZ,
                "unitId": "c",
                "attempt": 1,
                "resultContract": "review.v1",
                "resultDigest": DIGEST,
                "reportedAt": "2026-10-17T11:00:00Z",
                "reportedBy": {"kind": "native", "taskId": "t-c"},
                "payload": {"verdict": "approved", "evidenceRef": REF},
            }
        )
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        self.assertEqual(escrita["progress"]["declared"], 50)
        self.assertEqual(escrita["progress"]["observed"], 0)
        fila_c = next(f for f in escrita["units"] if f["unitId"] == "c")
        self.assertEqual(fila_c["state"], "reported")

    def test_la_escritura_es_atomica(self):
        self.abrir([unidad("a", "a1")])
        self.ligar_y_arrancar("a")
        primera, errores = self.proyectar()
        self.assertEqual(errores, [])
        ruta = self.destino / "proyecciones" / f"{RAIZ}.json"
        previo = ruta.read_text(encoding="utf-8")
        # La segunda proyección difiere (el intento pasó a reported): si
        # sobrevive el archivo previo no es porque nadie reescribiera.
        self.tienda.transicionar_intento(RAIZ, "a", 1, "reported", "t-a")
        with mock.patch(
            "raices.proyeccion.os.replace", side_effect=OSError("disco lleno")
        ):
            segunda, errores = self.proyectar()
        self.assertIsNone(segunda)
        self.assertEqual(len(errores), 1)
        self.assertIn("raíz", errores[0])
        self.assertEqual(
            ruta.read_text(encoding="utf-8"),
            previo,
            "el archivo previo sobrevive intacto",
        )
        self.assertEqual(
            sorted(p.name for p in ruta.parent.iterdir()),
            [f"{RAIZ}.json"],
            "no quedan temporales de la escritura fallida",
        )

    def test_un_rootid_que_no_es_nombre_de_archivo_no_se_escribe(self):
        # F12: el rootId va al nombre del archivo; el store es una base y
        # puede traer filas hostiles. Sólo [A-Za-z0-9._-]{1,128} y ni "." ni
        # ".." pasan; el resto es error por raíz, sin archivo y sin tumbar
        # la pasada.
        for malo in ("../x", "a/b", "a\\b", "..", "."):
            with self.subTest(root_id=malo):
                self.insertar_raiz_cruda(malo)
                documento, errores = self.proyectar(malo)
                self.assertIsNone(documento)
                self.assertEqual(len(errores), 1)
                self.assertIn(malo, errores[0])
        self.assertEqual(
            sorted(p.name for p in (self.destino / "proyecciones").glob("*")),
            [],
            "ningún nombre hostil llegó a disco",
        )
        # y las demás raíces siguen proyectándose en la misma pasada
        self.abrir([unidad("a", "a1")])
        informe = proyeccion.pasada(
            self.tienda,
            ahora=AHORA,
            dir_destino=self.destino,
            enviar=lambda _texto: 1,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )
        self.assertEqual(informe["raices"], 1)
        self.assertTrue((self.destino / "proyecciones" / f"{RAIZ}.json").exists())

    def insertar_raiz_cruda(self, root_id):
        meta = meta_de([unidad("a", "a1")])
        self.tienda._conexion.execute(
            "INSERT INTO raices VALUES (?,?,?,?,?,?,?)",
            (
                root_id,
                json.dumps(meta["requester"]),
                json.dumps(meta["authority"]),
                json.dumps(meta["budget"]),
                meta["qualityPolicy"]["version"],
                json.dumps({"version": "quality-kit.regla4"}),
                "2026-10-17T00:00:00Z",
            ),
        )
        self.tienda._conexion.execute(
            "INSERT INTO revisiones VALUES (?,?,?,?,?)",
            (
                root_id,
                1,
                meta["plan"]["planDigest"],
                meta["plan"]["requestDigest"],
                "[]",
            ),
        )
        self.tienda._conexion.execute(
            "INSERT INTO controles VALUES (?,?,?,?,?)",
            (root_id, 1, "active", None, None),
        )
        self.tienda._conexion.commit()


class I16Proyeccion(Escenario):
    def test_un_hecho_de_otro_intento_no_atribuye_error(self):
        self.abrir([unidad("a", "a1")])
        self.ligar_y_arrancar("a", task_id="t-a1")
        self.tienda.registrar_incidencia(
            RAIZ,
            "a",
            1,
            "transport-unavailable:pid-reutilizado",
            {
                "kind": "transport-unavailable",
                "episodeId": "pid-reutilizado",
                "evidenceRef": "t-a1",
            },
        )
        self.tienda.transicionar_intento(RAIZ, "a", 1, "reported", "t-a1")
        reintentada = decision(bindings=[binding("a")])
        reintentada["outcomes"] = [{"unitId": "a", "attempt": 1, "outcome": "failed"}]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, reintentada, "dec-2"), store.Ok)
        self.tienda.reclamar_intencion(RAIZ, "a", 2)
        self.tienda.admitir_intento(RAIZ, "a", 2, "t-a2")
        self.tienda.transicionar_intento(RAIZ, "a", 2, "running", "t-a2")
        snapshot = self.tienda.snapshot_raiz(RAIZ)
        self.assertEqual(len(snapshot["incidents"]), 1)
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        fila = next(u for u in escrita["units"] if u["unitId"] == "a")
        self.assertEqual(fila["error"], None)
        self.assertEqual(fila["state"], "running")

    def test_la_sesion_caida_solo_mancha_su_fila(self):
        self.abrir([unidad("a", "a1"), unidad("b", "b1")])
        self.ligar_y_arrancar("a")
        self.ligar_y_arrancar("b")
        self.tienda.registrar_incidencia(
            RAIZ,
            "a",
            1,
            "session-closed:ep-1",
            {"kind": "session-closed", "episodeId": "ep-1", "evidenceRef": "t-a"},
        )
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        por_unidad = {f["unitId"]: f["error"] for f in escrita["units"]}
        self.assertEqual(por_unidad, {"a": "session-closed", "b": None})

    def test_un_json_roto_no_atribuye_ni_tumba_la_proyeccion(self):
        self.abrir([unidad("a", "a1"), unidad("b", "b1")])
        self.ligar_y_arrancar("a")
        self.ligar_y_arrancar("b")
        self.tienda.registrar_incidencia(RAIZ, "a", 1, "rota:x", {"kind": "x"})
        self.tienda._conexion.execute(
            "UPDATE incidencias SET incident_json = '{roto' WHERE unit_id = 'a'"
        )
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        por_unidad = {f["unitId"]: f["error"] for f in escrita["units"]}
        self.assertEqual(por_unidad, {"a": None, "b": None})

    def test_un_reporteby_malo_no_tumba_la_pasada_y_las_sanas_se_proyectan(self):
        self.abrir([unidad("a", "a1")], root_id="root-a")
        self.abrir([unidad("b", "b1")], root_id="root-b")
        roto = json.dumps(
            {
                "schema": "sistema-agentes.resultado.v1",
                "resultContract": "review.v1",
                "resultDigest": DIGEST,
                "reportedBy": None,
            }
        )
        self.tienda._conexion.execute(
            "INSERT INTO resultados VALUES ('root-a', 'a', 1, ?, ?, NULL, ?)",
            (DIGEST, roto, AHORA),
        )
        self.tienda._conexion.commit()
        informe = proyeccion.pasada(
            self.tienda,
            ahora=AHORA,
            dir_destino=self.destino,
            enviar=lambda _texto: 1,
            stale_after_ms=300000,
            intervalo_ms=300000,
            host_gateway="mac-local",
        )
        self.assertEqual(
            [e for e in informe["errores"] if "root" in e],
            [],
            "ninguna raíz muere por una fila con reportedBy nulo",
        )
        self.assertEqual(informe["raices"], 2)
        self.assertIn("root-b", informe["proyecciones"])
        self.assertEqual(
            self.tienda.snapshot_raiz("root-a")["pendingResults"][0]["taskId"], None
        )
        vista = self.tienda.leer_vista("root-a", hechos_nulos())
        self.assertEqual(vista["resultados"][0]["taskId"], None)

    def test_metricas_ausentes_no_inventan_nada(self):
        self.abrir([unidad("a", "a1")])
        self.ligar_y_arrancar("a")
        escrita, errores = self.proyectar()
        self.assertEqual(errores, [])
        fila = escrita["units"][0]
        self.assertIsNone(fila["modelId"])
        self.assertIsNone(fila["error"])
        self.assertEqual(fila["host"], "host-01")
        self.assertEqual(fila["nextCheckAt"], "2026-10-17T12:05:00Z")


if __name__ == "__main__":
    unittest.main()
