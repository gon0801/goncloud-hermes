#!/usr/bin/env python3
"""Hechos de asignación e instrucciones en H (SUA-07 H6a): el store de
instrucciones content-addressed, el reconcile con recibo-por-contenido, el
consumo atómico en aplicar y el despacho con el texto del brief.

El texto de una instrucción NUNCA va dentro de la asignación (fixture G):
siempre por referencia artifact:<hex>, como el artifact store de R.
"""

import hashlib
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))

from conexiones import rastrear_conexiones  # noqa: E402

instrucciones = importlib.import_module("raices.instrucciones")
store = importlib.import_module("raices.store")
decidir = importlib.import_module("raices.decidir")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")

DobleKanban = kanban_port.DobleKanban
PuntoDeCaida = kanban_port.PuntoDeCaida
clave_de_tarjeta = kanban_port.clave_de_tarjeta


class DobleConSpecs(DobleKanban):
    """El doble no guarda el spec de la tarjeta; este lo registra para
    asertar el cuerpo (el texto del brief) con el que se despachó."""

    def __init__(self):
        super().__init__()
        self.specs: dict = {}

    def _crear(self, clave, spec):
        self.specs[clave] = spec
        return super()._crear(clave, spec)


RAIZ = "root-h6a"


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


class Escenario(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)

    def abrir(self, unidades=("a",)):
        tienda = store.Store(Path(self.directorio.name) / "sistema-agentes.db")
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(tienda.abrir_raiz(meta_de(unidades)), store.Ok)
        return tienda

    @property
    def dir_de_instrucciones(self):
        return Path(self.directorio.name) / "raices" / "instrucciones"

    def asignacion_de(self, texto="brief de prueba", unit_id="a", relay=False):
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
            "relay": relay,
        }


class Instrucciones(unittest.TestCase):
    def setUp(self):
        rastrear_conexiones(self)
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(self.directorio.cleanup)
        self.dir = Path(self.directorio.name) / "instrucciones"

    def test_guardar_devuelve_ref_y_digest_y_resolver_lo_trae(self):
        ref, digest = instrucciones.guardar(self.dir, "brief del dueno")
        self.assertEqual(ref, f"artifact:{digest[len('sha256:') :]}")
        self.assertTrue(ref.startswith("artifact:"))
        self.assertTrue(digest.startswith("sha256:"))
        texto, motivo = instrucciones.resolver(self.dir, ref, digest)
        self.assertEqual((texto, motivo), ("brief del dueno", None))
        self.assertEqual((self.dir / ref[len("artifact:") :]).read_text(), texto)

    def test_el_mismo_texto_es_el_mismo_ref_y_no_deja_pendientes(self):
        primero = instrucciones.guardar(self.dir, "mismo brief")
        segundo = instrucciones.guardar(self.dir, "mismo brief")
        self.assertEqual(primero, segundo)
        sobrantes = [p.name for p in self.dir.iterdir() if p.name.startswith(".")]
        self.assertEqual(sobrantes, [])
        self.assertEqual(len(list(self.dir.iterdir())), 1)

    def test_el_archivo_queda_de_solo_lectura(self):
        ref, _digest = instrucciones.guardar(self.dir, "brief")
        modo = (self.dir / ref[len("artifact:") :]).stat().st_mode & 0o777
        self.assertEqual(oct(modo), oct(0o444))

    def test_resolver_distingue_ref_invalida_ausente_y_digest_distinto(self):
        ref, digest = instrucciones.guardar(self.dir, "brief")
        self.assertEqual(
            instrucciones.resolver(self.dir, "artifact:no-hex", digest),
            (None, "ref-invalida"),
        )
        self.assertEqual(
            instrucciones.resolver(self.dir, "artifact:" + "b" * 64, digest),
            (None, "instruccion-ausente"),
        )
        otro_digest = "sha256:" + "c" * 64
        self.assertEqual(
            instrucciones.resolver(self.dir, ref, otro_digest),
            (None, "digest-distinto"),
        )

    def test_fines_de_linea_y_no_ascii_no_rompen_el_ref_y_no_utf8_da_motivo(self):
        for texto in ["a\r\nb", "linea\rsola", "café — texto no ASCII con acentos"]:
            with self.subTest(texto=repr(texto)):
                ref, digest = instrucciones.guardar(self.dir, texto)
                hex64 = hashlib.sha256(texto.encode("utf-8")).hexdigest()
                self.assertEqual(ref, f"artifact:{hex64}")
                self.assertEqual(digest, f"sha256:{hex64}")
                self.assertEqual((self.dir / hex64).read_bytes(), texto.encode("utf-8"))
                self.assertEqual(
                    hashlib.sha256((self.dir / hex64).read_bytes()).hexdigest(), hex64
                )
                resuelto, motivo = instrucciones.resolver(self.dir, ref, digest)
                self.assertEqual((resuelto, motivo), (texto, None))
        crudo = b"\xff\xfe"
        hex64 = hashlib.sha256(crudo).hexdigest()
        (self.dir / hex64).write_bytes(crudo)
        os.chmod(self.dir / hex64, 0o644)
        self.assertEqual(
            instrucciones.resolver(self.dir, f"artifact:{hex64}", f"sha256:{hex64}"),
            (None, "no-utf8"),
        )


class Reconcile(Escenario):
    def reconciliar(self, tienda, *items):
        return tienda.reconciliar_asignaciones(RAIZ, list(items))

    def test_persiste_el_hecho_y_devuelve_el_recibo(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        recibo = self.reconciliar(tienda, hecho)
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(recibo.valor.root_id, RAIZ)
        self.assertEqual(recibo.valor.plan_revision, 1)
        self.assertEqual(
            recibo.valor.asignaciones,
            [{"unitId": "a", "relay": False, "replayed": False}],
        )
        hechos = tienda.hechos_por_raiz()[RAIZ]["asignaciones"]
        self.assertEqual(len(hechos), 1)
        self.assertEqual(hechos[0]["unitId"], "a")
        self.assertIs(hechos[0]["relay"], False)
        self.assertEqual(hechos[0]["assignment"], hecho["assignment"])

    def test_un_digest_falso_en_el_instruction_ref_rechaza_sin_filas(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        hecho["assignment"]["instructionRef"]["digest"] = "sha256:" + "c" * 64
        recibo = self.reconciliar(tienda, hecho)
        self.assertEqual(recibo.codigo, "instruccion-invalida")
        self.assertEqual(recibo.detalle["unitId"], "a")
        self.assertEqual(recibo.detalle["motivo"], "digest-distinto")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_un_ref_ausente_rechaza_sin_filas(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        os.unlink(
            self.dir_de_instrucciones
            / hecho["assignment"]["instructionRef"]["ref"][len("artifact:") :]
        )
        recibo = self.reconciliar(tienda, hecho)
        self.assertEqual(recibo.codigo, "instruccion-invalida")
        self.assertEqual(recibo.detalle["motivo"], "instruccion-ausente")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_el_assignment_en_texto_es_documento_invalido(self):
        tienda = self.abrir()
        recibo = self.reconciliar(
            tienda, {"unitId": "a", "assignment": "no soy un mapa", "relay": False}
        )
        self.assertEqual(recibo.codigo, "documento-invalido")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_un_result_contract_desconocido_es_documento_invalido(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        hecho["assignment"]["resultContract"] = "otra-cosa.v1"
        recibo = self.reconciliar(tienda, hecho)
        self.assertEqual(recibo.codigo, "documento-invalido")
        self.assertEqual(tienda.hechos_por_raiz(), {})

    def test_el_reintento_exacto_es_replay_y_no_duplica(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        self.assertIsInstance(self.reconciliar(tienda, hecho), store.Ok)
        repetido = self.reconciliar(tienda, hecho)
        self.assertIsInstance(repetido, store.Ok)
        self.assertEqual(
            repetido.valor.asignaciones,
            [{"unitId": "a", "relay": False, "replayed": True}],
        )
        self.assertEqual(len(tienda.hechos_por_raiz()[RAIZ]["asignaciones"]), 1)

    def test_otra_asignacion_con_la_fila_viva_es_operation_conflict(self):
        tienda = self.abrir()
        primero = self.asignacion_de(texto="primer brief")
        self.assertIsInstance(self.reconciliar(tienda, primero), store.Ok)
        recibo = self.reconciliar(tienda, self.asignacion_de(texto="segundo brief"))
        self.assertEqual(recibo.codigo, "operation-conflict")
        self.assertEqual(recibo.detalle, {"unitId": "a", "relay": False})
        hechos = tienda.hechos_por_raiz()[RAIZ]["asignaciones"]
        self.assertEqual(
            hechos[0]["assignment"]["instructionRef"]["ref"],
            primero["assignment"]["instructionRef"]["ref"],
            "la fila viva conserva la primera asignación",
        )

    def test_la_fila_consumida_con_otro_digest_abre_un_ciclo_nuevo(self):
        tienda = self.abrir()
        hecho = self.asignacion_de(texto="ciclo uno")
        self.assertIsInstance(self.reconciliar(tienda, hecho), store.Ok)
        vista = tienda.leer_vista(RAIZ, tienda.hechos_por_raiz()[RAIZ])
        propuesta = decidir.decidir(vista)
        self.assertEqual(propuesta.decision["bindings"][0]["unitId"], "a")
        self.assertIsInstance(
            tienda.aplicar(RAIZ, propuesta.decision, "dec-1"), store.Ok
        )
        self.assertEqual(tienda.hechos_por_raiz(), {})
        recibo = self.reconciliar(
            tienda, self.asignacion_de(texto="ciclo dos", relay=True)
        )
        self.assertIsInstance(recibo, store.Ok)
        self.assertEqual(
            recibo.valor.asignaciones,
            [{"unitId": "a", "relay": True, "replayed": False}],
        )
        self.assertEqual(len(tienda.hechos_por_raiz()[RAIZ]["asignaciones"]), 1)

    def test_una_raiz_desconocida_se_rechaza(self):
        tienda = self.abrir()
        recibo = tienda.reconciliar_asignaciones("root-nadie", [self.asignacion_de()])
        self.assertEqual(recibo.codigo, "raiz-desconocida")

    def test_las_necesidades_de_la_decision_quedan_en_el_recibo_y_la_tabla(self):
        tienda = self.abrir(unidades=("a", "b"))
        recibo = self.reconciliar(tienda, self.asignacion_de(unit_id="a"))
        self.assertIsInstance(recibo, store.Ok)
        necesidades = recibo.valor.necesidades
        self.assertEqual(
            necesidades,
            [
                {
                    "kind": "assignment",
                    "unitId": "b",
                    "reason": "first",
                    "findingsRef": None,
                    "findingsRefs": [],
                }
            ],
        )
        guardadas = tienda.necesidades_de(RAIZ)
        self.assertEqual(guardadas["items"], necesidades)
        self.assertEqual(guardadas["digest"], decidir.digesto(necesidades))


class Necesidades(Escenario):
    def test_sin_asentar_no_hay_nada(self):
        tienda = self.abrir()
        self.assertIsNone(tienda.necesidades_de(RAIZ))

    def test_asentar_reemplaza_el_digesto_por_raiz(self):
        tienda = self.abrir()
        primeras = [
            decidir.NecesidadAsignacion("a", "first", None, []),
        ]
        tienda.asentar_necesidades(RAIZ, primeras)
        self.assertEqual(
            tienda.necesidades_de(RAIZ),
            {
                "digest": decidir.digesto(
                    [
                        {
                            "kind": "assignment",
                            "unitId": "a",
                            "reason": "first",
                            "findingsRef": None,
                            "findingsRefs": [],
                        },
                    ]
                ),
                "items": [
                    {
                        "kind": "assignment",
                        "unitId": "a",
                        "reason": "first",
                        "findingsRef": None,
                        "findingsRefs": [],
                    },
                ],
            },
        )
        tienda.asentar_necesidades(RAIZ, [])
        self.assertEqual(
            tienda.necesidades_de(RAIZ), {"digest": decidir.digesto([]), "items": []}
        )


class TextoDeInstruccion(Escenario):
    def test_resuelve_por_la_via_del_store(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        referencia = hecho["assignment"]["instructionRef"]
        texto = tienda.texto_de_instruccion(referencia["ref"], referencia["digest"])
        self.assertIsInstance(texto, store.Ok)
        self.assertEqual(texto.valor, "brief de prueba")

    def test_un_digest_que_no_coincide_es_instruccion_invalida(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        referencia = hecho["assignment"]["instructionRef"]
        respuesta = tienda.texto_de_instruccion(referencia["ref"], "sha256:" + "c" * 64)
        self.assertEqual(respuesta.codigo, "instruccion-invalida")
        self.assertEqual(respuesta.detalle, {"motivo": "digest-distinto"})

    def test_un_archivo_alterado_en_disco_es_digest_distinto(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        referencia = hecho["assignment"]["instructionRef"]
        ruta = self.dir_de_instrucciones / referencia["ref"][len("artifact:") :]
        ruta.chmod(0o644)
        ruta.write_text("texto adulterado", encoding="utf-8")
        respuesta = tienda.texto_de_instruccion(referencia["ref"], referencia["digest"])
        self.assertEqual(respuesta.codigo, "instruccion-invalida")
        self.assertEqual(respuesta.detalle, {"motivo": "digest-distinto"})


class DespacheConTexto(Escenario):
    """La tarjeta lleva el TEXTO del brief leído del store y verificado contra
    el digest; un digest que no coincide no despacha y levanta la pregunta."""

    @staticmethod
    def vuelta(tienda, kanban):
        return ticker.vuelta(
            tienda,
            kanban,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos_de_prueba(),
        )

    def hechos_vacios(self):
        return {"principal": [], "asignaciones": [], "claims": [], "efectos": []}

    def test_digest_alterado_en_disco_no_despacha_y_pregunta_al_dueno(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        referencia = hecho["assignment"]["instructionRef"]
        self.assertIsInstance(tienda.reconciliar_asignaciones(RAIZ, [hecho]), store.Ok)
        ruta = self.dir_de_instrucciones / referencia["ref"][len("artifact:") :]
        ruta.chmod(0o644)
        ruta.write_text("texto adulterado tras el reconcile", encoding="utf-8")
        kanban = DobleKanban()
        informe = self.vuelta(tienda, kanban)
        self.assertEqual(informe.tarjetas_creadas, 0)
        self.assertEqual(
            [(i.unit_id, i.attempt) for i in tienda.intenciones_pendientes()],
            [("a", 1)],
            "la intención sigue pendiente, sin tarjeta",
        )
        vista = tienda.leer_vista(RAIZ, self.hechos_vacios())
        causas = [
            e["subject"].get("cause")
            for e in vista["excepciones"]
            if e["subject"].get("kind") == "attempt"
        ]
        self.assertEqual(causas, [f"instruccion:{referencia['ref']}"])

    def test_el_archivo_reparado_vuelve_a_despachar(self):
        tienda = self.abrir()
        hecho = self.asignacion_de(texto="brief reparado")
        referencia = hecho["assignment"]["instructionRef"]
        self.assertIsInstance(tienda.reconciliar_asignaciones(RAIZ, [hecho]), store.Ok)
        ruta = self.dir_de_instrucciones / referencia["ref"][len("artifact:") :]
        ruta.chmod(0o644)
        ruta.write_text("adulterado", encoding="utf-8")
        kanban = DobleKanban()
        self.assertEqual(self.vuelta(tienda, kanban).tarjetas_creadas, 0)
        # El store es content-addressed: guardar no sobreescribe el archivo
        # corrupto (el link EEXIST se tolera); repararlo es borrarlo y
        # reescribir el texto original.
        ruta.unlink()
        instrucciones.guardar(self.dir_de_instrucciones, "brief reparado")
        self.assertEqual(self.vuelta(tienda, kanban).tarjetas_creadas, 1)
        self.assertEqual(tienda.intenciones_pendientes(), [])


class NecesidadesDeLaVuelta(Escenario):
    def test_sin_reconcile_la_vuelta_asienta_la_necesidad_first(self):
        tienda = self.abrir(unidades=("a", "b"))
        kanban = DobleKanban()
        informe = ticker.vuelta(
            tienda,
            kanban,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos_de_prueba(),
        )
        self.assertEqual(informe.tarjetas_creadas, 0)
        necesidades = tienda.necesidades_de(RAIZ)
        self.assertEqual(
            necesidades["items"],
            [
                {
                    "kind": "assignment",
                    "unitId": "a",
                    "reason": "first",
                    "findingsRef": None,
                    "findingsRefs": [],
                },
                {
                    "kind": "assignment",
                    "unitId": "b",
                    "reason": "first",
                    "findingsRef": None,
                    "findingsRefs": [],
                },
            ],
        )
        self.assertEqual(necesidades["digest"], decidir.digesto(necesidades["items"]))


class Consumo(Escenario):
    """El e2e sin inyectar hechos a mano: submit, guardar la instrucción,
    reconcile y una vuelta del ticker con los hechos del store. El intento
    queda ligado, la bandeja cumplida y la asignación consumida en la MISMA
    transacción que crea el intento."""

    @staticmethod
    def vuelta(tienda, kanban):
        return ticker.vuelta(
            tienda,
            kanban,
            hechos_por_raiz=tienda.hechos_por_raiz(),
            permisos_h=permisos_de_prueba(),
        )

    def test_el_e2e_desde_submit_hasta_intento_admitido(self):
        tienda = self.abrir()
        hecho = self.asignacion_de(texto="brief con el TEXTO del dueno")
        self.assertIsInstance(tienda.reconciliar_asignaciones(RAIZ, [hecho]), store.Ok)
        kanban = DobleConSpecs()
        informe = self.vuelta(tienda, kanban)
        self.assertEqual(informe.aplicadas, 1)
        self.assertEqual(informe.tarjetas_creadas, 1)
        self.assertEqual(tienda.intenciones_pendientes(), [])
        vista = tienda.leer_vista(
            RAIZ, {"principal": [], "asignaciones": [], "claims": [], "efectos": []}
        )
        unidad = vista["unidades"][0]
        self.assertEqual(unidad["state"], "admitted")
        self.assertEqual(unidad["assignment"], hecho["assignment"])
        self.assertEqual(tienda.hechos_por_raiz(), {}, "la asignación se consumió")
        spec = kanban.specs[clave_de_tarjeta(RAIZ, "a", 1)]
        self.assertEqual(spec.cuerpo, "brief con el TEXTO del dueno")
        self.assertEqual(spec.asignado_a, "codex")

    def test_la_caida_tras_el_commit_no_duplica_nada(self):
        tienda = self.abrir()
        hecho = self.asignacion_de()
        self.assertIsInstance(tienda.reconciliar_asignaciones(RAIZ, [hecho]), store.Ok)
        kanban = DobleKanban()
        kanban.caer_en(PuntoDeCaida.DESPUES_DE_TXN)
        with self.assertRaises(kanban_port.CaidaInyectada):
            self.vuelta(tienda, kanban)
        kanban.quitar_caida(PuntoDeCaida.DESPUES_DE_TXN)
        self.vuelta(tienda, kanban)
        clave = clave_de_tarjeta(RAIZ, "a", 1)
        self.assertEqual(kanban.creaciones.count(clave), 1)
        self.assertEqual(tienda.intenciones_pendientes(), [])
        vista = tienda.leer_vista(
            RAIZ, {"principal": [], "asignaciones": [], "claims": [], "efectos": []}
        )
        self.assertEqual(vista["unidades"][0]["state"], "admitted")
        self.assertEqual(tienda.hechos_por_raiz(), {})


if __name__ == "__main__":
    unittest.main()
