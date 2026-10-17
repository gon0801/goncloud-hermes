#!/usr/bin/env python3
"""La autoridad real (I07): efecto_admisible de la copia fijada intersectado
con los permisos de H, en los tres puntos: arranque de unidad, reanudación y
antes de cada efecto. La autoridad admite y H niega no ejecuta nada."""

import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

store = importlib.import_module("raices.store")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")
decidir = importlib.import_module("raices.decidir")
autoridad = importlib.import_module("raices.autoridad")

compartido_decidir = importlib.import_module("raices.compartido")
FIXTURES = compartido_decidir.G_DIR / "scripts/tests/fixtures/sistema-agentes"

RAIZ = "root-i07"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}


def permisos(**cambios):
    base = {
        "acciones": ["read", "implement", "review"],
        "toolsets": {"codex": ["read", "write"]},
        "comandos": ["git", "python3"],
    }
    base.update(cambios)
    return base


class PermisosDePrueba(autoridad.PermisosH):
    """Permite todo lo que el contrato ya admita. Solo para pruebas: el None
    de producción niega (fail-closed), así que quien quiera "solo el contrato"
    en un test pasa esta clase, nunca None."""

    def __init__(self):
        super().__init__({})

    def niega_accion(self, accion):
        return None

    def niega_perfil(self, perfil, modo):
        return None

    def niega_comando(self, comando):
        return None


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


def meta_de(unidades, autoridad_acciones=("read", "implement", "review", "push")):
    return {
        "schema": "sistema-agentes.meta.v1",
        "rootId": RAIZ,
        "requester": {"principal": ["david"], "agentId": "main"},
        "authority": {
            "schema": "sistema-agentes.autoridad.v1",
            "authorityRef": "run:turn-one",
            "grantedBy": "david",
            "grantedAt": "2026-10-17T00:00:00Z",
            "actions": list(autoridad_acciones),
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


def unidad(unit_id, criterio, modo="implement"):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": [],
        "criteria": [
            {
                "criterionId": criterio,
                "text": f"Criterio {criterio}",
                "verifier": "verifier",
            }
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": modo},
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


def efecto(effect_id="effect-push", destination="dest-a"):
    return {
        "effectId": effect_id,
        "action": "push",
        "repo": "owner-01/repo-a",
        "destination": destination,
        "mode": "implement",
        "cancellable": False,
        "unitId": None,
        "resourceRef": None,
    }


def comando(command, command_id, epoch):
    return {
        "schema": "sistema-agentes.control.v1",
        "rootId": RAIZ,
        "command": command,
        "commandId": command_id,
        "expectedControlEpoch": epoch,
        "reason": None,
        "issuedAt": "2026-10-17T11:00:00Z",
    }


class I07Contrato(unittest.TestCase):
    def test_cada_caso_del_fixture_da_la_misma_razon(self):
        casos = json.loads((FIXTURES / "autoridad/efectos.json").read_text())
        self.assertTrue(casos)
        for caso in casos:
            with self.subTest(nombre=caso["nombre"]):
                autoridad_doc = json.loads((FIXTURES / caso["autoridad"]).read_text())
                negar = autoridad.interseccion(
                    autoridad_doc, PermisosDePrueba(), caso["ahora"]
                )
                esperado = None if caso["esperado"] == "admisible" else caso["esperado"]
                self.assertEqual(negar(caso["efecto"]), esperado)


class TiendaCerrada:
    """Sonda del arreglo de Windows (WinError 32): al terminar la prueba, la
    conexión del Store quedó cerrada antes de que se borre el temporal. El
    chequeo se registra primero para que corra al final (LIFO), después del
    cierre de la tienda y del borrado del directorio."""

    def _verificar_cierre(self):
        with self.assertRaises(sqlite3.ProgrammingError):
            self.tienda._conexion.execute("SELECT 1")


class I07SinPermisos(TiendaCerrada, unittest.TestCase):
    """Sin permisos de H (None, el valor por omisión) el gate falla cerrado:
    acción, perfil y comando se niegan con la razón propia sin-permisos."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(self.ruta)
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(self.directorio.cleanup)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    def test_none_niega_todo_con_razon_propia(self):
        sin_datos = autoridad.PermisosH(None)
        self.assertEqual(sin_datos.niega_accion("push"), "sin-permisos")
        self.assertEqual(sin_datos.niega_perfil("codex", "implement"), "sin-permisos")
        self.assertEqual(sin_datos.niega_comando("git"), "sin-permisos")
        vacio = autoridad.PermisosH({})
        self.assertEqual(vacio.niega_accion("push"), "accion-preguntar")
        self.assertEqual(autoridad.causa_de("sin-permisos"), "permiso-h:sin-permisos")

    def test_sin_permisos_de_h_el_efecto_no_corre(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")
        self.assertEqual(recibo.detalle["reason"], "sin-permisos")
        vista = self.tienda.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["prepared"])
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("permiso-h:sin-permisos", causas)

    def test_sin_permisos_de_h_el_arranque_no_despacha(self):
        kanban = kanban_port.DobleKanban()
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "a", "assignment": asignacion(), "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        informe = ticker.vuelta(self.tienda, kanban, {RAIZ: hechos})
        self.assertEqual(informe.tarjetas_creadas, 0)
        self.assertEqual(len(self.tienda.intenciones_pendientes()), 1)
        vista = self.tienda.leer_vista(RAIZ, hechos)
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("permiso-h:sin-permisos", causas)


class I07Efecto(TiendaCerrada, unittest.TestCase):
    """Punto 3: antes de cada efecto en iniciar_efecto."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(self.directorio.cleanup)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    def test_la_autoridad_admite_y_h_niega_el_efecto_no_corre(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")
        self.assertEqual(recibo.detalle["reason"], "accion-preguntar")
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["prepared"])
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("permiso-h:accion-preguntar", causas)

    def test_con_h_permisente_el_efecto_corre(self):
        tienda2 = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review", "push"]),
        )
        self.addCleanup(tienda2.cerrar)
        self.assertIsInstance(
            tienda2.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"), store.Ok
        )
        recibo = tienda2.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(recibo.valor.state, "started")

    def test_el_contrato_niega_antes_que_h(self):
        vencida = {
            "schema": "sistema-agentes.autoridad.v1",
            "authorityRef": "P9",
            "grantedBy": "david",
            "grantedAt": "2026-10-17T00:00:00Z",
            "actions": ["push"],
            "repos": ["owner-01/repo-a"],
            "accounts": [],
            "destinations": ["dest-a"],
            "deadline": "2026-10-16T00:00:00Z",
            "mode": "implement",
        }
        tienda = store.Store(self.ruta.parent / "otra.db", permisos_h=permisos())
        self.addCleanup(tienda.cerrar)
        meta = meta_de([unidad("a", "a1")])
        meta["rootId"] = "root-vencida"
        meta["authority"] = vencida
        meta["plan"]["planDigest"] = "sha256:" + "b" * 64
        tienda.abrir_raiz(meta)
        tienda.aplicar(
            "root-vencida",
            {
                **decision(efectos=[efecto()]),
                "expected_plan_revision": 1,
                "expected_control_epoch": 1,
            },
            "dec-ef",
        )
        recibo = tienda.iniciar_efecto("root-vencida", "effect-push", "start-1", 1)
        self.assertEqual(recibo.detalle["reason"], "autoridad-vencida")
        vista = tienda.leer_vista("root-vencida", self.hechos_nulos())
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("autoridad:autoridad-vencida", causas)

    @staticmethod
    def hechos_nulos():
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}


class I07Arranque(TiendaCerrada, unittest.TestCase):
    """Punto 1: al arrancar una unidad, en el despacho del ticker."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.tienda = store.Store(Path(self.directorio.name) / "db.sqlite")
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(self.directorio.cleanup)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1"), unidad("b", "b1")]))
        self.kanban = kanban_port.DobleKanban()
        self.hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "a", "assignment": asignacion(), "relay": False},
                {"unitId": "b", "assignment": asignacion(), "relay": False},
            ],
            "claims": [],
            "efectos": [],
        }

    def test_la_autoridad_admite_y_h_niega_no_despacha_nada(self):
        informe = ticker.vuelta(
            self.tienda,
            self.kanban,
            {RAIZ: self.hechos},
            permisos_h=permisos(toolsets={"claude": ["read", "write"]}),
        )
        self.assertEqual(informe.tarjetas_creadas, 0)
        self.assertEqual(len(self.tienda.intenciones_pendientes()), 2)
        vista = self.tienda.leer_vista(RAIZ, self.hechos)
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertEqual(
            causas,
            [
                "permiso-h:perfil-sin-toolset",
                "permiso-h:perfil-sin-toolset",
            ],
        )
        permisos_buenos = permisos()
        ticker.vuelta(
            self.tienda, self.kanban, {RAIZ: self.hechos}, permisos_h=permisos_buenos
        )
        self.assertEqual(len(self.kanban.creaciones), 2)

    def test_el_contrato_niega_el_arranque_por_modo(self):
        tienda = store.Store(Path(self.directorio.name) / "modo.db")
        self.addCleanup(tienda.cerrar)
        meta = meta_de([unidad("p", "p1", modo="publish")])
        meta["rootId"] = "root-modo"
        tienda.abrir_raiz(meta)
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "p", "assignment": asignacion(), "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        kanban = kanban_port.DobleKanban()
        informe = ticker.vuelta(tienda, kanban, {"root-modo": hechos})
        self.assertEqual(informe.tarjetas_creadas, 0)
        vista = tienda.leer_vista("root-modo", hechos)
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("autoridad:modo-fuera-de-autoridad", causas)


class I07Reanudacion(unittest.TestCase):
    """Punto 2: al reanudar tras pause/resume."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        permisosa = store.Store(self.ruta, permisos_h=permisos())
        permisosa.abrir_raiz(meta_de([unidad("a", "a1")]))
        self.assertIsInstance(
            permisosa.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        self.assertIsInstance(
            permisosa.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        permisosa.cerrar()

    def test_al_reanudar_h_niega_y_el_efecto_no_recupera_el_permiso(self):
        endurecida = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(endurecida.cerrar)
        recibo = endurecida.control(comando("resume", "cmd-02", 2))
        self.assertIsInstance(recibo, store.Ok)
        vista = endurecida.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["prepared"])
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("permiso-h:accion-preguntar", causas)
        tarde = endurecida.iniciar_efecto(RAIZ, "effect-push", "start-1", 3)
        self.assertEqual(tarde.codigo, "authority")


if __name__ == "__main__":
    unittest.main()
