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
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

store = importlib.import_module("raices.store")
ticker = importlib.import_module("raices.ticker")
instrucciones = importlib.import_module("raices.instrucciones")
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


def asignacion(tienda, adapter="codex", repo="owner-01/repo-a"):
    ref, digest = instrucciones.guardar(
        tienda.dir_de_instrucciones, f"brief de {adapter}"
    )
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": adapter},
        "instructionRef": {"ref": ref, "digest": digest},
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


def binding(tienda, unit_id):
    return {"unitId": unit_id, "assignment": asignacion(tienda), "writers": []}


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
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
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
                {"unitId": "a", "assignment": asignacion(self.tienda), "relay": False}
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
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
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


class I07UnAvisoPorEfecto(TiendaCerrada, unittest.TestCase):
    """F1: un efecto preparado que H niega deja UNA excepción NECESITA_DAVID
    abierta, con la causa permiso-h:* — no dos con sujetos distintos."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    def abiertas(self, vista):
        return [
            e
            for e in vista["excepciones"]
            if e["type"] == "NECESITA_DAVID" and e["response"] is None
        ]

    def test_solo_la_vuelta_deja_una_excepcion_abierta_con_la_causa(self):
        # F1 (r5): nadie llama a iniciar_efecto en producción; la vuelta del
        # ticker es quien pregunta. Sin iniciar_efecto tiene que quedar
        # EXACTAMENTE UNA NECESITA_DAVID abierta, con la causa permiso-h:*.
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        ticker.vuelta(self.tienda, kanban_port.DobleKanban(), {})
        vista = self.tienda.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        abiertas = self.abiertas(vista)
        self.assertEqual(len(abiertas), 1)
        self.assertEqual(
            abiertas[0]["subject"].get("cause"), "permiso-h:accion-preguntar"
        )

    def test_un_efecto_negado_pregunta_una_sola_vez_con_la_causa(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        ticker.vuelta(self.tienda, kanban_port.DobleKanban(), {})
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        vista = self.tienda.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        abiertas = self.abiertas(vista)
        self.assertEqual(len(abiertas), 1)
        self.assertEqual(
            abiertas[0]["subject"].get("cause"), "permiso-h:accion-preguntar"
        )

    def test_y_sigue_preguntando_una_sola_vez_en_cada_vuelta(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        for _ in range(3):
            ticker.vuelta(self.tienda, kanban_port.DobleKanban(), {})
            self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        vista = self.tienda.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        self.assertEqual(len(self.abiertas(vista)), 1)

    def test_la_vuelta_y_luego_la_reanudacion_dejan_una(self):
        permisosa = store.Store(
            self.ruta.parent / "reanudacion.db", permisos_h=permisos()
        )
        self.addCleanup(permisosa.cerrar)
        permisosa.abrir_raiz(meta_de([unidad("a", "a1")]))
        self.assertIsInstance(
            permisosa.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"),
            store.Ok,
        )
        self.assertIsInstance(
            permisosa.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        permisosa.cerrar()

        endurecida = store.Store(
            self.ruta.parent / "reanudacion.db",
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(endurecida.cerrar)
        ticker.vuelta(endurecida, kanban_port.DobleKanban(), {})
        self.assertIsInstance(
            endurecida.control(comando("resume", "cmd-02", 2)), store.Ok
        )
        vista = endurecida.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        abiertas = self.abiertas(vista)
        self.assertEqual(len(abiertas), 1)
        self.assertEqual(
            abiertas[0]["subject"].get("cause"), "permiso-h:accion-preguntar"
        )


class EfectoMalFormado(TiendaCerrada, unittest.TestCase):
    """F3: un intento de efecto sin action/repo/mode (el front door solo
    exige effectId) se niega con razón propia en vez de reventar con KeyError
    el arranque, la reanudación o la vuelta del ticker."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    def test_un_efecto_sin_forma_se_niega_con_razon_propia(self):
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[{"effectId": "effect-roto"}]), "dec-roto"
            ),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-roto", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")
        self.assertEqual(recibo.detalle["reason"], "efecto-sin-forma")
        vista = self.tienda.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        causas = [
            e["subject"].get("cause")
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "effect"
        ]
        self.assertEqual(causas, ["autoridad:efecto-sin-forma"])

    def test_interseccion_niega_formas_rotas_sin_keyerror(self):
        negar = autoridad.interseccion(
            {}, autoridad.PermisosH(permisos()), "2026-10-17T11:00:00Z"
        )
        self.assertEqual(negar(efecto()), "autoridad-ilegible")
        valida = meta_de([unidad("a", "a1")])["authority"]
        sin_datos = autoridad.interseccion(valida, None, "2026-10-17T11:00:00Z")
        self.assertEqual(sin_datos({"effectId": "effect-roto"}), "efecto-sin-forma")
        self.assertEqual(sin_datos(efecto()), "sin-permisos")

    def test_la_reanudacion_sobrevive_a_un_efecto_sin_forma(self):
        permisosa = store.Store(
            self.ruta.parent / "permisosa.db", permisos_h=permisos()
        )
        self.addCleanup(permisosa.cerrar)
        permisosa.abrir_raiz(meta_de([unidad("a", "a1")]))
        self.assertIsInstance(
            permisosa.aplicar(
                RAIZ,
                decision(efectos=[efecto(), {"effectId": "effect-roto"}]),
                "dec-mixta",
            ),
            store.Ok,
        )
        self.assertIsInstance(
            permisosa.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        permisosa.cerrar()

        endurecida = store.Store(
            self.ruta.parent / "permisosa.db",
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(endurecida.cerrar)
        recibo = endurecida.control(comando("resume", "cmd-02", 2))
        self.assertIsInstance(recibo, store.Ok)
        vista = endurecida.leer_vista(RAIZ, I07Efecto.hechos_nulos())
        causas = {
            e["subject"].get("effectId"): e["subject"].get("cause")
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "effect"
        }
        self.assertEqual(
            causas,
            {
                "effect-push": "permiso-h:accion-preguntar",
                "effect-roto": "autoridad:efecto-sin-forma",
            },
        )

    def test_el_tick_sigue_con_las_demas_raices(self):
        raiz_b = meta_de([unidad("b", "b1")])
        raiz_b["rootId"] = "root-b"
        raiz_b["plan"]["planDigest"] = "sha256:" + "b" * 64
        self.assertIsInstance(self.tienda.abrir_raiz(raiz_b), store.Ok)
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[{"effectId": "effect-roto"}]), "dec-roto"
            ),
            store.Ok,
        )
        hechos_b = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "b", "assignment": asignacion(self.tienda), "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        informe = ticker.vuelta(
            self.tienda,
            kanban_port.DobleKanban(),
            {RAIZ: {}, "root-b": hechos_b},
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.assertEqual(informe.tarjetas_creadas, 1)
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-roto", "start-1", 1)
        self.assertEqual(recibo.detalle["reason"], "efecto-sin-forma")


class I07Arranque(TiendaCerrada, unittest.TestCase):
    """Punto 1: al arrancar una unidad, en el despacho del ticker."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.tienda = store.Store(Path(self.directorio.name) / "db.sqlite")
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1"), unidad("b", "b1")]))
        self.kanban = kanban_port.DobleKanban()
        self.hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "a", "assignment": asignacion(self.tienda), "relay": False},
                {"unitId": "b", "assignment": asignacion(self.tienda), "relay": False},
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
        # Con "publish" entre las acciones, la publish niega por RANGO de
        # modo (publish > implement), igual que antes de F11.
        tienda = store.Store(
            Path(self.directorio.name) / "modo.db",
            permisos_h=permisos(),
        )
        self.addCleanup(tienda.cerrar)
        meta = meta_de(
            [unidad("p", "p1", modo="publish")],
            autoridad_acciones=("read", "implement", "review", "push", "publish"),
        )
        meta["rootId"] = "root-modo"
        tienda.abrir_raiz(meta)
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "p", "assignment": asignacion(tienda), "relay": False}
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
        # Sin "publish" entre las acciones (la forma típica), la misma unidad
        # niega por acción: desde F11 el efecto de arranque ya no se hace
        # pasar por un "implement" que la autoridad sí cubriría.
        sin_publish = store.Store(
            Path(self.directorio.name) / "sin-publish.db",
            permisos_h=permisos(),
        )
        self.addCleanup(sin_publish.cerrar)
        meta_tipica = meta_de([unidad("p", "p1", modo="publish")])
        meta_tipica["rootId"] = "root-modo-tipico"
        sin_publish.abrir_raiz(meta_tipica)
        informe = ticker.vuelta(
            sin_publish, kanban_port.DobleKanban(), {"root-modo-tipico": hechos}
        )
        self.assertEqual(informe.tarjetas_creadas, 0)
        vista = sin_publish.leer_vista("root-modo-tipico", hechos)
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertIn("autoridad:accion-fuera-de-autoridad", causas)


class ConcesionDeEfecto(TiendaCerrada, unittest.TestCase):
    """F8 (SUA-02 «Conceder un efecto», R effectGranted): la respuesta
    `autorizar` del dueño a la NECESITA_DAVID de un efecto concede ESE
    effectId y ningún otro, mientras ninguna restricción posterior la anule.
    La compuerta de arranque (iniciar_efecto) y la reanudación la respetan."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.instante_actual = "2026-10-17T11:00:00Z"
        self.tienda = store.Store(
            self.ruta,
            reloj=lambda: self.instante_actual,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    @staticmethod
    def hechos_nulos():
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}

    def pregunta_del_efecto(self, effect_id="effect-push"):
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        return next(
            e["exceptionId"]
            for e in vista["excepciones"]
            if e["type"] == "NECESITA_DAVID"
            and e["subject"].get("kind") == "effect"
            and e["subject"].get("effectId") == effect_id
        )

    def abiertas_por_efecto(self):
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        return [
            e["subject"].get("effectId")
            for e in vista["excepciones"]
            if e["type"] == "NECESITA_DAVID"
            and e["response"] is None
            and e["subject"].get("kind") == "effect"
        ]

    def negar_y_autorizar(self, effect_id="effect-push"):
        """Efecto negado -> pregunta -> respuesta autorizar del dueño."""
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[efecto(effect_id)]), f"dec-{effect_id}"
            ),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, effect_id, "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")
        self.instante_actual = "2026-10-17T12:00:00Z"
        self.assertIsInstance(
            self.tienda.responder_excepcion(
                RAIZ, self.pregunta_del_efecto(effect_id), "autorizar", ["david"]
            ),
            store.Ok,
        )

    def test_el_autorizar_del_dueno_concede_ese_efecto(self):
        self.negar_y_autorizar()
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(recibo.valor.state, "started")
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        self.assertEqual([e["state"] for e in vista["efectos"]], ["started"])

    def test_otro_effect_id_con_la_misma_accion_vuelve_a_preguntar_y_se_niega(self):
        self.negar_y_autorizar()
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto("push-otro")]), "dec-2"),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "push-otro", "start-2", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")
        self.assertEqual(self.abiertas_por_efecto(), ["push-otro"])

    def test_una_restriccion_posterior_a_la_respuesta_anula_la_concesion(self):
        self.negar_y_autorizar()
        self.instante_actual = "2026-10-17T13:00:00Z"
        # La restricción futura: quien revise la autoridad de la raíz deja su
        # marca (restricciones) junto al authority_json nuevo.
        self.tienda._conexion.execute(
            "INSERT INTO restricciones VALUES (?,?,?)",
            (RAIZ, 2, "2026-10-17T13:30:00Z"),
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.codigo, "authority")

    def test_la_reanudacion_respeta_la_concesion(self):
        self.negar_y_autorizar()
        self.assertIsInstance(
            self.tienda.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        recibo = self.tienda.control(comando("resume", "cmd-02", 2))
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(self.abiertas_por_efecto(), [])
        arrancado = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 3)
        self.assertIsInstance(arrancado, store.Ok)
        self.assertEqual(arrancado.valor.state, "started")

    def test_decidir_no_reabre_la_pregunta_mientras_la_concesion_siga_vigente(self):
        self.negar_y_autorizar()
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        propuesta = decidir.decidir(vista)
        preguntas = [
            b["subject"].get("effectId")
            for b in propuesta.decision["excepciones"]
            if b["subject"].get("kind") == "effect"
        ]
        self.assertEqual(preguntas, [])


class SujetosDeIntentoSinAutorizar(TiendaCerrada, unittest.TestCase):
    """F15: la concesión `autorizar` solo existe para efectos (F8). Un sujeto
    {"kind": "attempt"} — arranque negado o reanudación — ofrece SOLO
    listo/omitir/cancelar (como R, decide.ts:550), y una respuesta
    `autorizar` vieja a un sujeto de intento no queda aplicada: la excepción
    sigue abierta en vez de enterrarse respondida para siempre."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    def hechos(self, asignaciones_para=()):
        return {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": u, "assignment": asignacion(self.tienda), "relay": False}
                for u in asignaciones_para
            ],
            "claims": [],
            "efectos": [],
        }

    @staticmethod
    def hechos_nulos():
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}

    def opciones_del_intento(self):
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        return [
            [o["optionId"] for o in e["options"]]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "attempt"
        ]

    def test_el_arranque_negado_ofrece_solo_listo_omitir_cancelar(self):
        informe = ticker.vuelta(
            self.tienda,
            kanban_port.DobleKanban(),
            {RAIZ: self.hechos(["a"])},
            permisos_h=permisos(toolsets={"otro": ["read", "write"]}),
        )
        self.assertEqual(informe.tarjetas_creadas, 0)
        self.assertEqual(self.opciones_del_intento(), [["listo", "omitir", "cancelar"]])

    def test_la_reanudacion_negada_ofrece_solo_listo_omitir_cancelar(self):
        permisosa = store.Store(
            self.ruta.parent / "permisosa.db", permisos_h=permisos()
        )
        self.addCleanup(permisosa.cerrar)
        permisosa.abrir_raiz(meta_de([unidad("a", "a1")]))
        self.assertIsInstance(
            permisosa.aplicar(
                RAIZ, decision(bindings=[binding(permisosa, "a")]), "dec-liga"
            ),
            store.Ok,
        )
        self.assertIsInstance(
            permisosa.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        permisosa.cerrar()

        endurecida = store.Store(
            self.ruta.parent / "permisosa.db",
            permisos_h=permisos(toolsets={"otro": ["read", "write"]}),
        )
        self.addCleanup(endurecida.cerrar)
        self.assertIsInstance(
            endurecida.control(comando("resume", "cmd-02", 2)), store.Ok
        )
        vista = endurecida.leer_vista(RAIZ, self.hechos_nulos())
        opciones = [
            [o["optionId"] for o in e["options"]]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "attempt"
        ]
        self.assertEqual(opciones, [["listo", "omitir", "cancelar"]])

    def test_un_autorizar_viejo_a_un_sujeto_de_intento_no_queda_aplicado(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {
                "kind": "attempt",
                "unitId": "a",
                "attempt": 1,
                "cause": "permiso-h:accion-preguntar",
            },
            # La forma VIEJA (pre-F15), que ofrecía autorizar por error.
            ["autorizar", "listo", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, dec, "dec-vieja"), store.Ok)
        self.assertIsInstance(
            self.tienda.responder_excepcion(
                RAIZ, borrador["exceptionId"], "autorizar", ["david"]
            ),
            store.Ok,
        )
        ticker.vuelta(self.tienda, kanban_port.DobleKanban(), {})
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        fila = next(
            e
            for e in vista["excepciones"]
            if e["exceptionId"] == borrador["exceptionId"]
        )
        self.assertIsNone(fila["appliedBy"])
        propuesta = decidir.decidir(self.tienda.leer_vista(RAIZ, self.hechos_nulos()))
        self.assertNotIn(
            borrador["exceptionId"], propuesta.decision["respuestas_aplicadas"]
        )


class ModoDeLaUnidadEnElArranque(TiendaCerrada, unittest.TestCase):
    """F11: el efecto sintético de arranque toma la acción del modo de la
    unidad: una unidad scope.mode "read" bajo una autoridad actions ["read"]
    arranca; no se le endilga un "implement" que la autoridad no cubre."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(self.ruta, permisos_h=permisos(acciones=["read"]))
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)

    def test_una_unidad_read_bajo_autoridad_read_arranca(self):
        self.tienda.abrir_raiz(
            meta_de([unidad("r", "r1", modo="read")], autoridad_acciones=("read",))
        )
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {"unitId": "r", "assignment": asignacion(self.tienda), "relay": False}
            ],
            "claims": [],
            "efectos": [],
        }
        informe = ticker.vuelta(
            self.tienda,
            kanban_port.DobleKanban(),
            {RAIZ: hechos},
            permisos_h=permisos(acciones=["read"]),
        )
        self.assertEqual(informe.tarjetas_creadas, 1)
        vista = self.tienda.leer_vista(RAIZ, hechos)
        causas = [e["subject"].get("cause") for e in vista["excepciones"]]
        self.assertEqual(causas, [])


class LaConcesionNoCallaLasRazonesDeForma(TiendaCerrada, unittest.TestCase):
    """C1 (r6e): la concesión `autorizar` calla lo que el dueño puede
    conceder (autoridad o permisos de H), nunca las razones de FORMA: un
    efecto mal formado no llega a started aunque el dueño lo autorice, y
    decidir sigue preguntando por él."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.instante_actual = "2026-10-17T11:00:00Z"
        self.tienda = store.Store(
            self.ruta,
            reloj=lambda: self.instante_actual,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    @staticmethod
    def hechos_nulos():
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}

    def test_un_efecto_sin_forma_autorizado_sigue_negado(self):
        # La forma VIEJA (pre-r6f) ofrecía autorizar por una razón de forma:
        # esa concesión puede existir en datos viejos y no calla la razón
        # (C1/r6e). Las preguntas nuevas ya no la ofrecen (r6f).
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[{"effectId": "roto-concedido"}]), "dec-roto"
            ),
            store.Ok,
        )
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {
                "kind": "effect",
                "effectId": "roto-concedido",
                "cause": "autoridad:efecto-sin-forma",
            },
            ["autorizar", "listo", "omitir", "cancelar"],
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        self.assertIsInstance(self.tienda.aplicar(RAIZ, dec, "dec-vieja"), store.Ok)
        recibo = self.tienda.iniciar_efecto(RAIZ, "roto-concedido", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.detalle["reason"], "efecto-sin-forma")
        self.instante_actual = "2026-10-17T12:00:00Z"
        self.assertIsInstance(
            self.tienda.responder_excepcion(
                RAIZ, borrador["exceptionId"], "autorizar", ["david"]
            ),
            store.Ok,
        )
        # La concesión quedó vigente pero la razón es de forma: sigue negado.
        recibo = self.tienda.iniciar_efecto(RAIZ, "roto-concedido", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.detalle["reason"], "efecto-sin-forma")
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        self.assertEqual(vista["concesiones"], ["roto-concedido"])

    def test_decidir_pregunta_por_un_efecto_sin_forma_aunque_este_concedido(self):
        # Concesión ganada por accion-preguntar; el intent del efecto se
        # corrompe después: la razón pasa a ser de FORMA y, como la causa
        # cambia, el exceptionId es otro: decidir vuelve a preguntar aunque
        # el efecto figure en las concesiones.
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"), store.Ok
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.detalle["reason"], "accion-preguntar")
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        exception_id = next(
            e["exceptionId"]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "effect"
        )
        self.instante_actual = "2026-10-17T12:00:00Z"
        self.assertIsInstance(
            self.tienda.responder_excepcion(RAIZ, exception_id, "autorizar", ["david"]),
            store.Ok,
        )
        self.tienda._conexion.execute(
            'UPDATE efectos SET intent_json = \'{"effectId": "effect-push"}\''
            " WHERE root_id = ? AND effect_id = 'effect-push'",
            (RAIZ,),
        )
        self.tienda._conexion.commit()
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        self.assertEqual(vista["concesiones"], ["effect-push"])
        propuesta = decidir.decidir(vista)
        preguntas = [
            b["subject"].get("effectId")
            for b in propuesta.decision["excepciones"]
            if b["subject"].get("kind") == "effect"
        ]
        self.assertEqual(preguntas, ["effect-push"])
        causas = [
            b["subject"].get("cause")
            for b in propuesta.decision["excepciones"]
            if b["subject"].get("kind") == "effect"
        ]
        self.assertEqual(causas, ["autoridad:efecto-sin-forma"])


class OpcionesPorRazonDeForma(TiendaCerrada, unittest.TestCase):
    """r6f: cuando la razón es de forma (efecto-sin-forma,
    autoridad-ilegible), la pregunta NECESITA_DAVID por un efecto NO ofrece
    `autorizar` en ninguno de los cuatro caminos (decidir x2, iniciar_efecto
    y reanudación): la concesión no calla una razón de forma (C1/r6e) y la
    pregunta respondida no se reabriría — la raíz quedaría trabada. Por una
    razón de permisos, `autorizar` se sigue ofreciendo."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.ruta = Path(self.directorio.name) / "sistema-agentes.db"
        self.tienda = store.Store(
            self.ruta,
            permisos_h=permisos(acciones=["read", "implement", "review"]),
        )
        self.addCleanup(self._verificar_cierre)
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
        self.addCleanup(self.tienda.cerrar)
        self.tienda.abrir_raiz(meta_de([unidad("a", "a1")]))

    @staticmethod
    def hechos_nulos():
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}

    def opciones_del_efecto(self, effect_id):
        vista = self.tienda.leer_vista(RAIZ, self.hechos_nulos())
        return next(
            [o["optionId"] for o in e["options"]]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "effect"
            and e["subject"].get("effectId") == effect_id
        )

    def opciones_del_borrador(self, propuesta, effect_id):
        return next(
            b["options"]
            for b in propuesta.decision["excepciones"]
            if b["subject"].get("kind") == "effect"
            and b["subject"].get("effectId") == effect_id
        )

    def test_el_inicio_de_un_efecto_sin_forma_no_ofrece_autorizar(self):
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[{"effectId": "roto"}]), "dec-roto"
            ),
            store.Ok,
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "roto", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.detalle["reason"], "efecto-sin-forma")
        self.assertEqual(
            self.opciones_del_efecto("roto"), ["listo", "omitir", "cancelar"]
        )

    def test_la_reanudacion_no_ofrece_autorizar_por_un_efecto_sin_forma(self):
        permisosa = store.Store(
            self.ruta.parent / "permisosa.db", permisos_h=permisos()
        )
        self.addCleanup(permisosa.cerrar)
        permisosa.abrir_raiz(meta_de([unidad("a", "a1")]))
        self.assertIsInstance(
            permisosa.aplicar(
                RAIZ, decision(efectos=[{"effectId": "roto"}]), "dec-roto"
            ),
            store.Ok,
        )
        self.assertIsInstance(
            permisosa.control(comando("pause", "cmd-01", 1)), store.Ok
        )
        permisosa.cerrar()

        endurecida = store.Store(
            self.ruta.parent / "permisosa.db", permisos_h=permisos()
        )
        self.addCleanup(endurecida.cerrar)
        self.assertIsInstance(
            endurecida.control(comando("resume", "cmd-02", 2)), store.Ok
        )
        vista = endurecida.leer_vista(RAIZ, self.hechos_nulos())
        opciones = next(
            [o["optionId"] for o in e["options"]]
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "effect"
        )
        self.assertEqual(opciones, ["listo", "omitir", "cancelar"])

    def test_decidir_preparado_no_ofrece_autorizar_por_forma(self):
        self.assertIsInstance(
            self.tienda.aplicar(
                RAIZ, decision(efectos=[{"effectId": "roto"}]), "dec-roto"
            ),
            store.Ok,
        )
        propuesta = decidir.decidir(self.tienda.leer_vista(RAIZ, self.hechos_nulos()))
        self.assertEqual(
            self.opciones_del_borrador(propuesta, "roto"),
            ["listo", "omitir", "cancelar"],
        )

    def test_decidir_hecho_nuevo_no_ofrece_autorizar_por_forma(self):
        hechos = {**self.hechos_nulos(), "efectos": [{"effectId": "roto-hechos"}]}
        propuesta = decidir.decidir(self.tienda.leer_vista(RAIZ, hechos))
        self.assertEqual(
            self.opciones_del_borrador(propuesta, "roto-hechos"),
            ["listo", "omitir", "cancelar"],
        )

    def test_una_razon_de_permisos_sigue_ofreciendo_autorizar(self):
        self.assertIsInstance(
            self.tienda.aplicar(RAIZ, decision(efectos=[efecto()]), "dec-ef"), store.Ok
        )
        recibo = self.tienda.iniciar_efecto(RAIZ, "effect-push", "start-1", 1)
        self.assertIsInstance(recibo, store.Conflicto)
        self.assertEqual(recibo.detalle["reason"], "accion-preguntar")
        self.assertEqual(
            self.opciones_del_efecto("effect-push"),
            ["autorizar", "listo", "omitir", "cancelar"],
        )


class I07Reanudacion(unittest.TestCase):
    """Punto 2: al reanudar tras pause/resume."""

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio.cleanup)
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
