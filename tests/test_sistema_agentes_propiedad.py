#!/usr/bin/env python3
"""Propiedad: toda decisión que decidir propone la acepta store.aplicar.

Por cada escenario de la batería (cadena feliz, corrección con claims vencidos
y vigentes, dueño, cierre rojo con seguir, evidencia incierta) la vuelta corre
de punta a punta contra un store real y el doble de kanban: si aplicar
rechaza la propuesta, o por forma o por reglas, la prueba cae. En un solo
hilo no hay carreras, así que un conflicto aquí es una divergencia
decidir/guarda: el defecto que el bot del PR #20 encontró cuatro veces (F9,
F10, F12, CR2) y que así sale en la Mac antes que el bot.
"""

import importlib
import inspect
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))
sys.path.insert(0, str(REPO / "tests"))
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

store = importlib.import_module("raices.store")
decidir = importlib.import_module("raices.decidir")
instrucciones = importlib.import_module("raices.instrucciones")
ticker = importlib.import_module("raices.ticker")
kanban_port = importlib.import_module("raices.kanban_port")

RAIZ = "root-prop"
DIGEST = "sha256:" + "a" * 64
REF = {"ref": "artifact:x", "digest": DIGEST}

PERMISOS_DE_PRUEBA = {
    "acciones": ["read", "implement", "review"],
    "toolsets": {"codex": ["read", "write"]},
    "comandos": ["git", "python3"],
}

# Las ramas sin gate de H no aceptan permisos_h; con gate, falla cerrado
# sin ellos y ningún escenario despacharía.
ARGUMENTOS_DE_VUELTA = (
    {"permisos_h": PERMISOS_DE_PRUEBA}
    if "permisos_h" in inspect.signature(ticker.vuelta).parameters
    else {}
)


def asignacion(dir_instrucciones):
    """El instructionRef apunta a una instrucción REAL del escenario (H6a):
    el ticker despacha con el texto verificado, no con la cadena del ref."""
    ref, digest = instrucciones.guardar(dir_instrucciones, "brief de codex")
    return {
        "target": {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
        "instructionRef": {"ref": ref, "digest": digest},
        "resultContract": "review.v1",
        "inputRevision": {
            "kind": "code",
            "repository": "owner-01/repo-a",
            "sha": "0" * 40,
        },
        "continuation": {"kind": "requester"},
    }


def unidad(unit_id, rol="owner", dependencias=()):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": list(dependencias),
        "criteria": [
            {
                "criterionId": f"{unit_id}1",
                "text": f"Criterio {unit_id}",
                "verifier": rol,
            }
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
    }


def meta(unidades):
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
            "units": unidades,
            "assumptions": [],
        },
        "controlEpoch": 1,
        "control": {"state": "active", "epoch": 1, "reason": None},
        "qualityPolicy": {"version": "quality-kit.regla4"},
        "createdAt": "2026-10-17T00:00:00Z",
    }


def hechos(dir_instrucciones, perfiles=("a", "b", "c"), claims=()):
    return {
        "principal": ["david"],
        "asignaciones": [
            {
                "unitId": u,
                "assignment": asignacion(dir_instrucciones),
                "relay": False,
            }
            for u in perfiles
        ],
        "claims": list(claims),
        "efectos": [],
    }


def claim_de_hecho(unit_id, attempt, criterio, outcome="passed", expira=None):
    return {
        "rootId": RAIZ,
        "unitId": unit_id,
        "attempt": attempt,
        "criterionId": criterio,
        "planRevision": 1,
        "artifactRef": {"ref": f"artifact:check-{criterio}", "digest": DIGEST},
        "environment": {"host": "mac-local", "versions": {}},
        "role": "verifier",
        "procedure": "pytest",
        "outcome": outcome,
        "at": "2026-10-17T11:00:00Z",
        "expiresAt": expira,
    }


def carga_aprobada(unit_id, attempt):
    return {
        "resultContract": "review.v1",
        "verdict": "approved",
        "evidenceRef": REF,
        "resultDigest": DIGEST,
    }


def carga_con_cambios(unit_id, attempt):
    return {
        "resultContract": "review.v1",
        "verdict": "changes",
        "findingsRef": REF,
        "resultDigest": DIGEST,
    }


def politica_dueno(fila):
    if fila["type"] == "NECESITA_DAVID" and str(
        fila["subject"].get("cause", "")
    ).startswith("owner:"):
        return "aceptar"
    return None


def politica_de_cierre(fila):
    if fila["type"] == "CIERRE_ROJO":
        return "seguir"
    causa = str(fila["subject"].get("cause", ""))
    if fila["type"] == "NECESITA_DAVID" and causa.startswith("incident:"):
        return "omitir"
    return politica_dueno(fila)


def politica_de_conciliacion(fila):
    if fila["type"] == "EVIDENCIA_INCIERTA":
        return "conciliar"
    return None


def politica_relevo(fila):
    if fila["type"] == "SIN_REVISOR":
        return "relevo"
    return None


def hechos_relevo(dir_instrucciones):
    return {
        "principal": ["david"],
        "asignaciones": [
            {
                "unitId": "a",
                "assignment": asignacion(dir_instrucciones),
                "relay": False,
            },
            {"unitId": "a", "assignment": asignacion(dir_instrucciones), "relay": True},
        ],
        "claims": [],
        "efectos": [],
    }


def registrar_incidencias(vistas, unidades=("a", "b")):
    def prepara(tienda, kanban):
        for intento in tienda.intentos_con_tarjeta():
            llave = (intento["unitId"], intento["attempt"])
            if (
                llave in vistas
                or intento["attempt"] != 1
                or intento["unitId"] not in unidades
            ):
                continue
            vistas.add(llave)
            tienda.registrar_incidencia(
                RAIZ,
                intento["unitId"],
                intento["attempt"],
                f"inc-{intento['unitId']}-{intento['attempt']}",
                {"kind": "permission-required", "evidenceRef": "run:x"},
            )

    return prepara


def archivar_la_tarjeta(attempt=1):
    """F10: la tarjeta del intento vive archivada con el intento aun vivo."""

    def prepara(tienda, kanban):
        for intento in tienda.intentos_con_tarjeta():
            if intento["attempt"] != attempt:
                continue
            clave = kanban_port.clave_de_tarjeta(
                intento["rootId"], intento["unitId"], intento["attempt"]
            )
            if kanban.por_clave(clave) is not None:
                kanban.crear_archivada(clave)

    return prepara


class LaDecisionDeDecidirLaAceptaElStore(unittest.TestCase):
    maxDiff = None
    TOPE = 30

    def setUp(self):
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio)

    @property
    def dir_de_instrucciones(self):
        return Path(self.directorio.name) / "raices" / "instrucciones"

    def dirigir(
        self,
        nombre,
        unidades,
        esperado,
        carga,
        politica,
        cupos=None,
        hechos_raiz=None,
        prepara=None,
        hasta_done=True,
        ademas=None,
        presupuesto=None,
    ):
        documento = meta(unidades)
        if presupuesto is not None:
            documento["budget"]["maxConcurrentTasks"] = presupuesto
        tienda = store.Store(
            self.directorio.name + f"/{nombre}.db",
            cupos=cupos,
        )
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(tienda.abrir_raiz(documento), store.Ok)
        kanban = kanban_port.DobleKanban()
        hechos_raiz = hechos_raiz or hechos(
            self.dir_de_instrucciones, [u["unitId"] for u in unidades]
        )
        estados = {}
        informe = None
        for _ in range(self.TOPE):
            self.cargar(tienda, kanban, carga)
            self.responder(tienda, politica)
            if prepara is not None:
                prepara(tienda, kanban)
            informe = ticker.vuelta(
                tienda, kanban, {RAIZ: hechos_raiz}, **ARGUMENTOS_DE_VUELTA
            )
            self.assertEqual(
                informe.conflictos,
                (),
                "decidir propuso algo que la guarda del store rechazó",
            )
            estados = {}
            for documento in tienda.leer_vista(RAIZ, hechos_raiz)["unidades"]:
                estados.setdefault(documento["unitId"], []).append(documento["state"])
            if {u: sorted(v) for u, v in estados.items()} == {
                u: sorted(v) for u, v in esperado.items()
            } and (ademas is None or ademas(tienda)):
                if hasta_done:
                    self.assertEqual(informe.estado, "done")
                return informe, tienda
        self.fail(f"no convergió: {estados} != {esperado}")

    def cargar(self, tienda, kanban, carga):
        for intento in tienda.intentos_con_tarjeta():
            clave = kanban_port.clave_de_tarjeta(
                intento["rootId"], intento["unitId"], intento["attempt"]
            )
            tarjeta = kanban.por_clave(clave)
            if tarjeta is not None and tarjeta.estado != "completada":
                kanban.crear_hecha(clave, carga(intento["unitId"], intento["attempt"]))

    def responder(self, tienda, politica):
        vista = tienda.leer_vista(RAIZ, hechos(self.dir_de_instrucciones, ()))
        for fila in vista["excepciones"]:
            if fila["response"] is not None:
                continue
            opcion = politica(fila)
            if opcion is not None:
                self.assertIsInstance(
                    tienda.responder_excepcion(
                        RAIZ, fila["exceptionId"], opcion, ["david"]
                    ),
                    store.Ok,
                )

    def test_la_cadena_dueno_termina_en_done_sin_conflictos(self):
        self.dirigir(
            "cadena",
            [unidad("a"), unidad("b"), unidad("c", dependencias=("a", "b"))],
            {"a": ["accepted"], "b": ["accepted"], "c": ["accepted"]},
            carga_aprobada,
            politica_dueno,
        )

    def test_la_correccion_con_claims_vencidos_y_vigentes_termina_sin_conflictos(self):
        claims = [
            claim_de_hecho("a", 2, "a1", "passed"),
            claim_de_hecho("a", 2, "a1", "unknown", expira="2026-10-16T00:00:00Z"),
        ]

        def carga(unit_id, attempt):
            return (
                carga_con_cambios(unit_id, attempt)
                if attempt == 1
                else carga_aprobada(unit_id, attempt)
            )

        self.dirigir(
            "correccion",
            [unidad("a", rol="verifier")],
            {"a": ["failed", "accepted"]},
            carga,
            politica_dueno,
            hechos_raiz=hechos(self.dir_de_instrucciones, ("a",), claims=claims),
        )

    def test_el_seguir_sobre_cierre_rojo_reabre_en_una_plaza_sin_conflictos(self):
        vistas: set = set()
        self.dirigir(
            "cierre",
            [unidad("a"), unidad("b"), unidad("c", dependencias=("a",))],
            {
                "a": ["failed", "accepted"],
                "b": ["failed", "accepted"],
                "c": ["accepted"],
            },
            carga_aprobada,
            politica_de_cierre,
            cupos=store.Cupos(global_max=1),
            prepara=registrar_incidencias(vistas),
        )

    def test_el_reintento_ocupa_la_plaza_del_intento_que_cancela(self):
        """F10: maxConcurrentTasks=1 y la unica plaza la tiene el intento
        cuya tarjeta se archivo: la decision lo cancela, re-liga la unidad y
        el store (que aplica los outcomes antes de contar cupos) la acepta.
        Sin el descuento, el reintento se difiere para siempre y nunca llega
        a INESPERADO ni al dueno."""
        self.dirigir(
            "reintento-raiz",
            [unidad("a")],
            {"a": ["cancelled", "accepted"]},
            carga_aprobada,
            politica_dueno,
            presupuesto=1,
            prepara=archivar_la_tarjeta(1),
        )

    def test_el_reintento_con_el_cupo_global_lleno_por_ese_intento(self):
        """F10: el cupo global lleno por el propio intento a cancelar tambien
        se descuenta: el store acepta la decision."""
        self.dirigir(
            "reintento-global",
            [unidad("a")],
            {"a": ["cancelled", "accepted"]},
            carga_aprobada,
            politica_dueno,
            cupos=store.Cupos(global_max=1),
            prepara=archivar_la_tarjeta(1),
        )

    def test_el_relevo_al_mismo_perfil_ocupa_la_plaza_del_vivo(self):
        """r9: maxConcurrentTasks=1 y la unica plaza la tiene el intento vivo
        cuyo relevo pidio el dueno: la decision lo cancela, liga el relevo y
        el store la acepta. Sin medir con el cancelado como liberado, el
        relevo se difiere en cada vuelta y la respuesta nunca se consume."""
        self.dirigir(
            "relevo-raiz",
            [unidad("a", rol="reviewer")],
            {"a": ["failed", "admitted"]},
            carga_aprobada,
            politica_relevo,
            presupuesto=1,
            hechos_raiz=hechos_relevo(self.dir_de_instrucciones),
            hasta_done=False,
        )

    def test_el_seguir_tardio_no_liga_de_mas_ni_recibe_capacity(self):
        """F12: el dueno responde seguir cuando el intento de la excepcion ya
        murio (failed) y la unica plaza la ocupa OTRA unidad viva: la unidad
        se difiere y el store acepta la decision. Descontar la plaza del
        intento muerto liga una de mas y store.aplicar rechaza la decision
        ENTERA con capacity."""
        documento = meta([unidad("a"), unidad("b")])
        documento["budget"]["maxConcurrentTasks"] = 1
        tienda = store.Store(self.directorio.name + "/seguir-tardio.db")
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(tienda.abrir_raiz(documento), store.Ok)

        numero = [0]

        def decidir_y_aplicar(hechos_raiz):
            numero[0] += 1
            propuesta = decidir.decidir(tienda.leer_vista(RAIZ, hechos_raiz))
            resultado = tienda.aplicar(RAIZ, propuesta.decision, f"dec-{numero[0]}")
            self.assertFalse(
                hasattr(resultado, "codigo"),
                f"el store rechazo la decision: {getattr(resultado, 'codigo', None)}",
            )
            return propuesta

        # La primera decision liga a1 y despachamos su tarjeta.
        primera = decidir_y_aplicar(hechos(self.dir_de_instrucciones, ("a",)))
        self.assertEqual([b["unitId"] for b in primera.decision["bindings"]], ["a"])
        tienda.reclamar_intencion(RAIZ, "a", 1)
        tienda.admitir_intento(RAIZ, "a", 1, "t-a1")
        tienda.transicionar_intento(RAIZ, "a", 1, "running", "t-a1")
        # Una incidencia inesperada sobre a1 viva y un resultado aprobado.
        tienda.registrar_incidencia(
            RAIZ,
            "a",
            1,
            "transporte-raro",
            {"kind": "transport-unavailable", "episodeId": "raro"},
        )
        self.assertIsInstance(
            tienda.reportar_resultado(
                {
                    "rootId": RAIZ,
                    "unitId": "a",
                    "attempt": 1,
                    "resultContract": "review.v1",
                    "resultDigest": DIGEST,
                    "reportedAt": "2026-10-17T12:00:00Z",
                    "reportedBy": {"kind": "native", "taskId": "t-a1"},
                    "payload": {"verdict": "approved", "evidenceRef": REF},
                }
            ),
            store.Ok,
        )
        # La decision levanta NECESITA_DAVID(owner) e INESPERADO sobre a1.
        segunda = decidir_y_aplicar(hechos(self.dir_de_instrucciones, ()))
        por_tipo = {
            e["type"]: e["exceptionId"] for e in segunda.decision["excepciones"]
        }
        self.assertIn("INESPERADO", por_tipo)
        # El dueno rechaza a1 (muere) y solo entonces responde el seguir.
        self.assertIsInstance(
            tienda.responder_excepcion(
                RAIZ, por_tipo["NECESITA_DAVID"], "rechazar", ["david"]
            ),
            store.Ok,
        )
        decidir_y_aplicar(hechos(self.dir_de_instrucciones, ("b",)))
        estados = {
            d["unitId"]: d["state"]
            for d in tienda.leer_vista(RAIZ, hechos(self.dir_de_instrucciones, ()))[
                "unidades"
            ]
        }
        self.assertEqual(estados.get("a"), "failed")
        self.assertEqual(estados.get("b"), "ready")
        self.assertIsInstance(
            tienda.responder_excepcion(
                RAIZ, por_tipo["INESPERADO"], "seguir", ["david"]
            ),
            store.Ok,
        )
        # El seguir tardio no puede abrir la plaza del intento muerto.
        tardia = decidir_y_aplicar(hechos(self.dir_de_instrucciones, ()))
        self.assertEqual(tardia.decision["bindings"], [])
        vivos = tienda.leer_vista(RAIZ, hechos(self.dir_de_instrucciones, ()))[
            "unidades"
        ]
        self.assertEqual(
            [d["attempt"] for d in vivos if d["unitId"] == "a"],
            [1],
            "no se liga un intento de mas para a",
        )

    def test_el_claim_de_otra_revision_no_bloquea_la_aceptacion(self):
        claims = [
            claim_de_hecho("a", 1, "a1", "passed"),
            {**claim_de_hecho("a", 1, "a1", "passed"), "planRevision": 2},
        ]
        self.dirigir(
            "revision",
            [unidad("a", rol="verifier")],
            {"a": ["accepted"]},
            carga_aprobada,
            politica_dueno,
            hechos_raiz=hechos(self.dir_de_instrucciones, ("a",), claims=claims),
        )

    def test_el_claim_de_otra_raiz_no_fabrica_cobertura(self):
        claims = [
            {
                **claim_de_hecho("a", 1, "a1", "passed"),
                "rootId": "root-otra",
            }
        ]
        informe, tienda = self.dirigir(
            "ajena",
            [unidad("a", rol="verifier")],
            {"a": ["reported"]},
            carga_aprobada,
            politica_dueno,
            hechos_raiz=hechos(self.dir_de_instrucciones, ("a",), claims=claims),
            hasta_done=False,
            ademas=lambda tienda: all(
                c["rootId"] == RAIZ
                for c in tienda.leer_vista(
                    RAIZ, hechos(self.dir_de_instrucciones, ("a",), claims=claims)
                )["evidencia"]
            ),
        )
        self.assertEqual(informe.conflictos, ())
        vista = tienda.leer_vista(
            RAIZ, hechos(self.dir_de_instrucciones, ("a",), claims=claims)
        )
        self.assertEqual(vista["evidencia"], [])

    def test_el_efecto_mal_formado_no_tumba_la_aceptacion(self):
        hechos_raiz = hechos(
            self.dir_de_instrucciones,
            ("a",),
            claims=[claim_de_hecho("a", 1, "a1", "passed")],
        )
        hechos_raiz["efectos"] = [
            {"action": "push", "destination": "dest-a"},
            "efecto que no es mapa",
        ]
        self.dirigir(
            "efecto-malo",
            [unidad("a", rol="verifier")],
            {"a": ["accepted"]},
            carga_aprobada,
            politica_dueno,
            hechos_raiz=hechos_raiz,
        )

    def test_la_asignacion_sin_target_no_se_liga_y_el_valido_sigue(self):
        hechos_raiz = hechos(self.dir_de_instrucciones, ("a",))
        hechos_raiz["asignaciones"].insert(
            0,
            {
                "unitId": "a",
                "assignment": {"resultContract": "review.v1"},
                "relay": False,
            },
        )
        self.dirigir(
            "asignacion-mala",
            [unidad("a", rol="owner")],
            {"a": ["accepted"]},
            carga_aprobada,
            politica_dueno,
            hechos_raiz=hechos_raiz,
        )

    def test_la_evidencia_incierta_concilia_y_la_guarda_no_objeta(self):
        claims = [claim_de_hecho("a", 1, "a1", "unknown")]
        informe, tienda = self.dirigir(
            "incierto",
            [unidad("a", rol="verifier")],
            {"a": ["reported"]},
            carga_aprobada,
            politica_de_conciliacion,
            hechos_raiz=hechos(self.dir_de_instrucciones, ("a",), claims=claims),
            hasta_done=False,
            ademas=lambda tienda: any(
                e["appliedBy"]
                for e in tienda.leer_vista(
                    RAIZ, hechos(self.dir_de_instrucciones, ("a",), claims=claims)
                )["excepciones"]
            ),
        )
        vista = tienda.leer_vista(
            RAIZ, hechos(self.dir_de_instrucciones, ("a",), claims=claims)
        )
        tipos = [e["type"] for e in vista["excepciones"]]
        self.assertIn("EVIDENCIA_INCIERTA", tipos)
        conciliada = [
            e for e in vista["excepciones"] if e["type"] == "EVIDENCIA_INCIERTA"
        ][0]
        self.assertIsNotNone(conciliada["appliedBy"])

    def _unidad_aceptada_con_hecho_relay(self, nombre):
        hechos_raiz = hechos(
            self.dir_de_instrucciones,
            ("a",),
            claims=[claim_de_hecho("a", 1, "a1", "passed")],
        )
        hechos_raiz["asignaciones"] = [
            {
                "unitId": "a",
                "assignment": asignacion(self.dir_de_instrucciones),
                "relay": True,
            },
            {
                "unitId": "a",
                "assignment": asignacion(self.dir_de_instrucciones),
                "relay": False,
            },
        ]
        informe, tienda = self.dirigir(
            nombre,
            [unidad("a", rol="verifier")],
            {"a": ["accepted"]},
            carga_aprobada,
            politica_dueno,
            hechos_raiz=hechos_raiz,
        )
        return informe, tienda, hechos_raiz

    def _responder_tardia(self, tienda, hechos_raiz, tipo, opciones, opcion):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            tipo,
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "incident:x"},
            list(opciones),
        )
        dec = decidir._decision_vacia(1, 1)
        dec["excepciones"] = [borrador]
        aplicada = tienda.aplicar(RAIZ, dec, f"dec-exc-{tipo}-{opcion}")
        self.assertIsInstance(aplicada, store.Ok, getattr(aplicada, "detalle", None))
        self.assertIsInstance(
            tienda.responder_excepcion(
                RAIZ, borrador["exceptionId"], opcion, ["david"]
            ),
            store.Ok,
        )
        kanban = kanban_port.DobleKanban()
        vueltas = []
        for _ in range(3):
            vuelta = ticker.vuelta(
                tienda, kanban, {RAIZ: hechos_raiz}, **ARGUMENTOS_DE_VUELTA
            )
            self.assertEqual(vuelta.conflictos, (), (tipo, opcion))
            self.assertEqual(vuelta.estado, "done", (tipo, opcion))
            vueltas.append(vuelta)
        vista = tienda.leer_vista(RAIZ, hechos_raiz)
        religadas = [
            d for d in vista["unidades"] if d["unitId"] == "a" and d["attempt"] > 1
        ]
        self.assertEqual(religadas, [], (tipo, opcion))
        return vista

    def test_la_respuesta_tardia_sobre_unidad_aceptada_no_religa(self):
        informe, tienda, hechos_raiz = self._unidad_aceptada_con_hecho_relay("tardia")
        vista = self._responder_tardia(
            tienda,
            hechos_raiz,
            "INESPERADO",
            ["seguir", "omitir", "cancelar"],
            "seguir",
        )
        fila = [e for e in vista["excepciones"]][0]
        self.assertIsNotNone(fila["appliedBy"])

    def test_toda_opcion_respondida_sobre_unidad_terminada_no_tumba(self):
        for tipo, opciones in sorted(decidir.CLASES_DE_EXCEPCION.items()):
            for opcion in opciones:
                with self.subTest(tipo=tipo, opcion=opcion):
                    informe, tienda, hechos_raiz = (
                        self._unidad_aceptada_con_hecho_relay(f"sweep-{tipo}-{opcion}")
                    )
                    self._responder_tardia(tienda, hechos_raiz, tipo, opciones, opcion)

    def test_dos_raices_sobre_el_mismo_repo_alternan_sin_conflicto(self):
        RAIZ_B = "root-prop-b"
        self.directorio = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.directorio)
        tienda = store.Store(self.directorio.name + "/dos-raices.db")
        self.addCleanup(tienda.cerrar)
        self.assertIsInstance(
            tienda.abrir_raiz(meta([unidad("a", rol="verifier")])), store.Ok
        )
        meta_b = {**meta([unidad("b", rol="verifier")]), "rootId": RAIZ_B}
        self.assertIsInstance(tienda.abrir_raiz(meta_b), store.Ok)
        hechos_a = hechos(
            self.dir_de_instrucciones,
            ("a",),
            claims=[claim_de_hecho("a", 1, "a1", "passed")],
        )
        hechos_b = hechos(
            self.dir_de_instrucciones,
            ("b",),
            claims=[{**claim_de_hecho("b", 1, "b1", "passed"), "rootId": RAIZ_B}],
        )
        kanbans = (kanban_port.DobleKanban(), kanban_port.DobleKanban())
        estados = {}
        for _ in range(self.TOPE):
            for kanban in kanbans:
                self.cargar(tienda, kanban, carga_aprobada)
                informe = ticker.vuelta(
                    tienda,
                    kanban,
                    {RAIZ: hechos_a, RAIZ_B: hechos_b},
                    **ARGUMENTOS_DE_VUELTA,
                )
                self.assertEqual(
                    informe.conflictos,
                    (),
                    "decidir propuso algo que la guarda del store rechazó",
                )
            estados = {}
            for raiz, hechos_raiz in ((RAIZ, hechos_a), (RAIZ_B, hechos_b)):
                for documento in tienda.leer_vista(raiz, hechos_raiz)["unidades"]:
                    estados.setdefault(raiz, []).append(documento["state"])
            if estados == {RAIZ: ["accepted"], RAIZ_B: ["accepted"]}:
                return
        self.fail(f"no convergió: {estados}")


if __name__ == "__main__":
    unittest.main()
