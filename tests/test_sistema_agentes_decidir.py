#!/usr/bin/env python3
"""El controlador puro decidir contra la semántica fijada por R y la copia de G.

El bucle de rondas calcaca el test de R (managed-root.decide.test.ts:184-244):
rondas 1-5 de corrección, la 6 de relevo, y a la 10 sin converger NECESITA_DAVID.
"""

import importlib.util
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugins/goncloud-team"))

decidir = importlib.import_module("raices.decidir")

_especificacion = importlib.util.spec_from_file_location(
    "compartido_decidir", REPO / "plugins/goncloud-team/raices/compartido.py"
)
compartido = importlib.util.module_from_spec(_especificacion)
_especificacion.loader.exec_module(compartido)

RAIZ = "root-1"
AHORA = "2026-10-17T12:00:00Z"
DIGEST = "sha256:" + "a" * 64
FIXTURES = compartido.G_DIR / "scripts/tests/fixtures/sistema-agentes"


def politica(**cambios):
    base = {
        "version": "quality-kit.regla4",
        "roundsPerImplementer": 5,
        "relays": 1,
        "revisorExcluyeAutores": True,
        "segundoModelo": False,
    }
    base.update(cambios)
    return base


def unidad(unit_id, dependencias=(), roles=("verifier",)):
    return {
        "unitId": unit_id,
        "title": f"Unidad {unit_id}",
        "dependsOn": list(dependencias),
        "criteria": [
            {
                "criterionId": f"{unit_id}{i + 1}",
                "text": f"Criterio {i + 1}",
                "verifier": rol,
            }
            for i, rol in enumerate(roles)
        ],
        "scope": {"repos": ["owner-01/repo-a"], "mode": "implement"},
    }


def asignacion(etiqueta="a-1", destino=None):
    return {
        "target": destino or {"kind": "cli", "hostId": "host-01", "adapterId": "codex"},
        "instructionRef": {"ref": f"artifact:{etiqueta}", "digest": DIGEST},
        "resultContract": "review.v1",
        "continuation": {"kind": "requester"},
    }


def intento(unit_id, n, estado, destino=None, assignment=None):
    return {
        "schema": "sistema-agentes.unidad.v1",
        "rootId": RAIZ,
        "planRevision": 1,
        "unitId": unit_id,
        "attempt": n,
        "taskId": f"task-{unit_id}-{n}",
        "assignment": assignment or asignacion(f"{unit_id}-{n}", destino),
        "state": estado,
        "controlEpoch": 1,
    }


def claim(unit_id, n, criterio, desenlace, expira=None, principal=None, rol="verifier"):
    return {
        "schema": "sistema-agentes.evidencia.v1",
        "evidenceId": f"evid-{criterio}-{n}",
        "rootId": RAIZ,
        "unitId": unit_id,
        "attempt": n,
        "criterionId": criterio,
        "planRevision": 1,
        "artifactRef": {"ref": f"artifact:check-{criterio}", "digest": DIGEST},
        "environment": {"host": "host-01", "versions": {}},
        "verifier": {"principal": principal or ["verif-1"], "role": rol},
        "procedure": "pytest",
        "outcome": desenlace,
        "at": "2026-10-17T11:00:00Z",
        "expiresAt": expira,
    }


def resultado(
    unit_id, n, contrato="review.v1", veredicto="changes", ref="artifact:findings"
):
    return {
        "taskId": f"task-{unit_id}-{n}",
        "contrato": contrato,
        "verdict": veredicto if contrato == "review.v1" else None,
        "kind": "produced",
        "ref": ref,
    }


def vista(unidades_de_plan, **cambios):
    intentos = cambios.pop("intentos", [])
    base = {
        "meta": {
            "schema": "sistema-agentes.meta.v1",
            "rootId": RAIZ,
            "requester": {"principal": ["david"], "agentId": "main"},
            "authority": {},
            "budget": {},
            "plan": {
                "revision": 1,
                "planDigest": DIGEST,
                "requestDigest": DIGEST,
                "units": unidades_de_plan,
                "assumptions": [],
            },
            "controlEpoch": 1,
            "control": {"state": "active", "epoch": 1, "reason": None},
            "qualityPolicy": {"version": "quality-kit.regla4"},
            "createdAt": "2026-10-17T00:00:00Z",
        },
        "unidades": intentos,
        "sin_ligar": [
            {"unitId": u["unitId"], "state": "ready", "missing": []}
            for u in unidades_de_plan
            if not any(i["unitId"] == u["unitId"] for i in intentos)
        ],
        "evidencia": [],
        "efectos": [],
        "resultados": [],
        "incidencias": [],
        "excepciones": [],
        "contadores": {},
        "escritores": [],
        "hallazgos": [],
        "plazas": {"raiz": 8, "global": 8, "por_perfil": {"codex": 8, "claude": 8}},
        "hechos": {
            "principal": ["david"],
            "asignaciones": [],
            "claims": [],
            "efectos": [],
        },
        "politica": politica(),
        "ahora": AHORA,
    }
    base.update(cambios)
    return base


def necesitaciones(propuesta):
    return [
        n.razon
        if isinstance(n, decidir.NecesidadAsignacion)
        else ("verify", tuple(n.criterios))
        for n in propuesta.necesidades
    ]


def sujeto_primera_excepcion(propuesta):
    return propuesta.decision["excepciones"][0]["subject"]


class I12Rondas(unittest.TestCase):
    maxDiff = None

    def test_el_bucle_de_diez_rondas(self):
        pedidas = []
        for rondas in range(11):
            propuesta = decidir.decidir(
                vista(
                    [unidad("a")],
                    intentos=[intento("a", rondas + 1, "reported")],
                    resultados=[resultado("a", rondas + 1)],
                    contadores={"rondas:a": rondas},
                )
            )
            if propuesta.necesidades:
                pedidas.append(necesitaciones(propuesta)[0])
            else:
                pedidas.append(sujeto_primera_excepcion(propuesta))
        self.assertEqual(
            pedidas,
            ["correction"] * 5
            + ["relay"] * 5
            + [{"kind": "rounds", "unitId": "a", "block": 0}],
        )

    def test_un_relevo_exige_hecho_relay_y_sube_el_contador(self):
        mismo = {
            "unitId": "a",
            "assignment": asignacion("a-7"),
            "relay": False,
        }
        fracasada = vista(
            [unidad("a")],
            intentos=[intento("a", 6, "failed")],
            contadores={"rondas:a": 5},
            hallazgos=[{"unitId": "a", "attempt": 5, "findingsRef": "artifact:f5"}],
            hechos={
                "principal": ["david"],
                "asignaciones": [mismo],
                "claims": [],
                "efectos": [],
            },
        )
        self.assertEqual(decidir.decidir(fracasada).decision["bindings"], [])
        con_relevo = vista(
            [unidad("a")],
            intentos=[intento("a", 6, "failed")],
            contadores={"rondas:a": 5},
            hallazgos=[{"unitId": "a", "attempt": 5, "findingsRef": "artifact:f5"}],
            hechos={
                "principal": ["david"],
                "asignaciones": [{**mismo, "relay": True}],
                "claims": [],
                "efectos": [],
            },
        )
        ligada = decidir.decidir(con_relevo)
        self.assertEqual(
            ligada.decision["bindings"],
            [{"unitId": "a", "assignment": asignacion("a-7"), "writers": []}],
        )
        self.assertEqual(
            ligada.decision["contadores"], [{"counter": "rondas:a", "expected": 5}]
        )

    def test_al_tope_nada_se_liga_aun_con_hecho(self):
        topada = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 11, "failed")],
                contadores={"rondas:a": 10},
                hechos={
                    "principal": ["david"],
                    "asignaciones": [
                        {"unitId": "a", "assignment": asignacion("a-12"), "relay": True}
                    ],
                    "claims": [],
                    "efectos": [],
                },
            )
        )
        self.assertEqual(topada.decision["bindings"], [])
        self.assertEqual(
            [e["type"] for e in topada.decision["excepciones"]], ["NECESITA_DAVID"]
        )
        self.assertEqual(
            sujeto_primera_excepcion(topada),
            {"kind": "rounds", "unitId": "a", "block": 0},
        )


class I12Linaje(unittest.TestCase):
    def test_hallazgos_nuevos_no_reinician_el_contador(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 3, "reported")],
                resultados=[resultado("a", 3, ref="artifact:f3")],
                contadores={"rondas:a": 3},
                hallazgos=[
                    {"unitId": "a", "attempt": 1, "findingsRef": "artifact:f1"},
                    {"unitId": "a", "attempt": 2, "findingsRef": "artifact:f2"},
                ],
            )
        )
        necesidad = propuesta.necesidades[0]
        self.assertEqual(necesidad.razon, "correction")
        self.assertEqual(necesidad.findings_ref, "artifact:f3")
        self.assertEqual(
            necesidad.findings_refs, ["artifact:f1", "artifact:f2", "artifact:f3"]
        )
        ligada = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 3, "failed")],
                contadores={"rondas:a": 3},
                hechos={
                    "principal": ["david"],
                    "asignaciones": [
                        {"unitId": "a", "assignment": asignacion("a-4"), "relay": False}
                    ],
                    "claims": [],
                    "efectos": [],
                },
            )
        )
        self.assertEqual(
            ligada.decision["contadores"], [{"counter": "rondas:a", "expected": 3}]
        )


class I12Politica(unittest.TestCase):
    def test_la_tabla_de_clases_coincide_con_la_copia_fijada(self):
        fijada = compartido.contrato.cargar_json(FIXTURES / "excepcion/clases.json")
        self.assertEqual(
            decidir.CLASES_DE_EXCEPCION,
            {fila["type"]: tuple(fila["options"]) for fila in fijada},
        )

    def test_ningun_paso_pide_revision_a_un_segundo_modelo(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                resultados=[resultado("a", 1)],
            )
        )
        for necesidad in propuesta.necesidades:
            self.assertIsInstance(
                necesidad, (decidir.NecesidadAsignacion, decidir.NecesidadVerificacion)
            )
            self.assertFalse(hasattr(necesidad, "revisor_segundo"))
        razones = [
            n.razon
            for n in propuesta.necesidades
            if isinstance(n, decidir.NecesidadAsignacion)
        ]
        self.assertEqual(razones, ["correction"])
        verificadas = [
            tuple(n.criterios)
            for n in propuesta.necesidades
            if isinstance(n, decidir.NecesidadVerificacion)
        ]
        self.assertEqual(verificadas, [])
        self.assertEqual(
            set(propuesta.decision),
            {
                "expected_plan_revision",
                "expected_control_epoch",
                "consumidos",
                "claims",
                "outcomes",
                "bindings",
                "efectos",
                "contadores",
                "excepciones",
                "respuestas_aplicadas",
                "control",
            },
        )

    def test_el_revisor_excluye_a_los_autores(self):
        destino = {"kind": "agent", "agentId": "impl-1"}
        con_autor = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "reported", destino)],
                evidencia=[
                    claim("a", 1, "a1", "passed", principal=["impl-1"], rol="reviewer")
                ],
            )
        )
        tipos = [e["type"] for e in con_autor.decision["excepciones"]]
        self.assertIn("SIN_REVISOR", tipos)
        self.assertEqual(con_autor.decision["outcomes"], [])

        con_otro = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "reported", destino)],
                evidencia=[
                    claim("a", 1, "a1", "passed", principal=["rev-1"], rol="reviewer")
                ],
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
            )
        )
        self.assertEqual(
            con_otro.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )

        sin_regla = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "reported", destino)],
                evidencia=[
                    claim("a", 1, "a1", "passed", principal=["impl-1"], rol="reviewer")
                ],
                politica=politica(revisorExcluyeAutores=False),
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
            )
        )
        self.assertEqual(
            sin_regla.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )

        relevo = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[
                    intento("a", 5, "failed", {"kind": "agent", "agentId": "impl-1"}),
                    intento("a", 6, "reported", {"kind": "agent", "agentId": "impl-2"}),
                ],
                evidencia=[
                    claim("a", 6, "a1", "passed", principal=["impl-1"], rol="reviewer")
                ],
                resultados=[resultado("a", 6, veredicto="approved", ref="artifact:ok")],
            )
        )
        tipos_de_relevo = [e["type"] for e in relevo.decision["excepciones"]]
        self.assertIn("SIN_REVISOR", tipos_de_relevo)
        self.assertEqual(relevo.decision["outcomes"], [])


class I13Aceptacion(unittest.TestCase):
    def test_review_approved_con_claims_vigentes_acepta(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("verifier", "ci"))],
                intentos=[intento("a", 1, "reported")],
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
                evidencia=[
                    claim("a", 1, "a1", "passed"),
                    claim("a", 1, "a2", "passed"),
                ],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )
        self.assertEqual(propuesta.estado, "done")

    def test_un_claim_verifier_no_cierra_un_criterio_owner(self):
        # F2 (hermes#34): el claim solo cuenta si su rol es compatible con el
        # verifier declarado del criterio; owner pide claim owner, así que el
        # criterio sigue abierto y sale la pregunta que corresponde.
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("owner",))],
                intentos=[intento("a", 1, "reported")],
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
                evidencia=[claim("a", 1, "a1", "passed", rol="verifier")],
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(
            [e["type"] for e in propuesta.decision["excepciones"]],
            ["NECESITA_DAVID"],
        )
        self.assertEqual(
            propuesta.decision["excepciones"][0]["subject"],
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "owner:a1"},
        )
        self.assertEqual(
            propuesta.decision["excepciones"][0]["options"],
            ["aceptar", "rechazar"],
        )

    def test_un_claim_owner_cierra_un_criterio_owner(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("owner",))],
                intentos=[intento("a", 1, "reported")],
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
                evidencia=[
                    claim("a", 1, "a1", "passed", rol="owner", principal=["david"])
                ],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )

    def test_un_claim_verifier_cierra_un_criterio_reviewer(self):
        # F5 (hermes#34, decisión del coordinador): el revisor de H es un
        # perfil que corre y claw verifica su resultado, así que un claim
        # verifier passed acredita un criterio declarado reviewer.
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "reported")],
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
                evidencia=[claim("a", 1, "a1", "passed", rol="verifier")],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )

    def test_ci_sin_claim_pide_verificacion_y_vencido_no_cuenta(self):
        sin_claim = decidir.decidir(
            vista([unidad("a", roles=("ci",))], intentos=[intento("a", 1, "reported")])
        )
        self.assertEqual(necesitaciones(sin_claim), [("verify", ("a1",))])
        vencido = decidir.decidir(
            vista(
                [unidad("a", roles=("ci",))],
                intentos=[intento("a", 1, "reported")],
                evidencia=[
                    claim("a", 1, "a1", "passed", expira="2026-10-17T11:00:00Z")
                ],
            )
        )
        self.assertEqual(necesitaciones(vencido), [("verify", ("a1",))])

    def test_un_criterio_borrado_no_acepta(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                evidencia=[claim("a", 1, "a0", "passed")],
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])

    def test_un_listo_no_fabrica_exito(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                resultados=[
                    resultado(
                        "a", 1, contrato="ready.v1", veredicto=None, ref="artifact:evi"
                    )
                ],
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])

    def test_un_reported_sin_resultado_no_se_acepta(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                evidencia=[claim("a", 1, "a1", "passed")],
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(propuesta.necesidades, [])

    def test_claims_de_otro_intento_no_cubren(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported"), intento("a", 2, "reported")],
                evidencia=[claim("a", 1, "a1", "passed")],
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])
        self.assertEqual(propuesta.necesidades[0].attempt, 2)

    def test_omitir_una_unidad_sin_lanzar_la_cancela(self):
        excepcion = {
            "exceptionId": "e1",
            "rootId": RAIZ,
            "unitId": "a",
            "type": "INESPERADO",
            "subject": {
                "kind": "attempt",
                "unitId": "a",
                "attempt": 1,
                "cause": "incident:x",
            },
            "options": ["seguir", "omitir", "cancelar"],
            "evidenceRef": None,
            "raisedAt": "2026-10-17T11:00:00Z",
            "noticedAt": None,
            "renoticeAfterMs": 3600000,
            "response": {
                "optionId": "omitir",
                "by": ["david"],
                "at": "2026-10-17T11:30:00Z",
            },
            "appliedBy": None,
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "ready")],
                excepciones=[excepcion],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "cancelled"}],
        )


class ElSeguirSobreCierreRojoRespetaLasPlazas(unittest.TestCase):
    """F9: 'seguir' sobre CIERRE_ROJO reabre las cerradas; las ligaduras pasan
    por las plazas como cualquier corrección, no se ligan en bloque."""

    @staticmethod
    def cierre_rojo_seguido():
        return {
            "exceptionId": "e-cierre",
            "rootId": RAIZ,
            "unitId": None,
            "type": "CIERRE_ROJO",
            "subject": {"kind": "root", "cause": "cierre:2"},
            "options": ["seguir", "cancelar"],
            "evidenceRef": None,
            "raisedAt": "2026-10-17T11:00:00Z",
            "noticedAt": "2026-10-17T11:00:00Z",
            "renoticeAfterMs": 3600000,
            "response": {
                "optionId": "seguir",
                "by": ["david"],
                "at": "2026-10-17T11:30:00Z",
            },
            "appliedBy": None,
        }

    def test_no_liga_por_encima_de_las_plazas_y_reabre_las_cerradas(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a"), unidad("b")],
                intentos=[intento("a", 1, "failed"), intento("b", 1, "failed")],
                excepciones=[self.cierre_rojo_seguido()],
                contadores={"cerradas:a": 1, "cerradas:b": 1},
                plazas={
                    "raiz": 1,
                    "global": 1,
                    "por_perfil": {"codex": 8, "claude": 8},
                },
                hechos={
                    "principal": ["david"],
                    "asignaciones": [
                        {
                            "unitId": "a",
                            "assignment": asignacion("a-2"),
                            "relay": False,
                        },
                        {
                            "unitId": "b",
                            "assignment": asignacion("b-2"),
                            "relay": False,
                        },
                    ],
                    "claims": [],
                    "efectos": [],
                },
            )
        )
        self.assertLessEqual(len(propuesta.decision["bindings"]), 1)
        self.assertEqual(
            [c["counter"] for c in propuesta.decision["contadores"]],
            ["reabiertas:a", "reabiertas:b", "rondas:a"],
        )
        self.assertEqual(propuesta.decision["respuestas_aplicadas"], ["e-cierre"])


class HechosYRoles(unittest.TestCase):
    @staticmethod
    def hechos_con(claim_hecho):
        return {
            "principal": ["claw"],
            "asignaciones": [],
            "claims": [claim_hecho],
            "efectos": [],
        }

    @staticmethod
    def claim_de_hecho(rol, principal):
        claim_hecho = claim("a", 1, "a1", "passed", principal=principal, rol=rol)
        claim_hecho.pop("verifier")
        claim_hecho.pop("evidenceId")
        claim_hecho.pop("schema")
        claim_hecho["role"] = rol
        return claim_hecho

    def test_un_claim_owner_del_modelo_no_cubre(self):
        hecho = self.claim_de_hecho("owner", ["claw"])
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("owner",))],
                intentos=[intento("a", 1, "reported")],
                hechos=self.hechos_con(hecho),
            )
        )
        self.assertEqual(propuesta.decision["outcomes"], [])
        tipos = [e["type"] for e in propuesta.decision["excepciones"]]
        self.assertIn("NECESITA_DAVID", tipos)

    def test_un_claim_verifier_del_modelo_sigue_cubriendo(self):
        hecho = self.claim_de_hecho("verifier", ["verif-1"])
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos=self.hechos_con(hecho),
                resultados=[resultado("a", 1, veredicto="approved", ref="artifact:ok")],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )


class RespuestasSobreEfectos(unittest.TestCase):
    def test_el_cancel_de_un_efecto_alcanza_a_su_unidad(self):
        excepcion = {
            "exceptionId": "e1",
            "rootId": RAIZ,
            "unitId": None,
            "type": "EVIDENCIA_INCIERTA",
            "subject": {"kind": "effect", "effectId": "effect-01"},
            "options": ["conciliar", "omitir", "cancelar"],
            "evidenceRef": None,
            "raisedAt": "2026-10-17T11:00:00Z",
            "noticedAt": "2026-10-17T11:00:00Z",
            "renoticeAfterMs": 3600000,
            "response": {
                "optionId": "cancelar",
                "by": ["david"],
                "at": "2026-10-17T11:30:00Z",
            },
            "appliedBy": None,
        }
        efecto = {
            "effectId": "effect-01",
            "state": "uncertain",
            "startId": "s1",
            "startedEpoch": 1,
            "intent": {
                "effectId": "effect-01",
                "action": "push",
                "unitId": "a",
                "destination": "d",
                "cancellable": False,
            },
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "running")],
                excepciones=[excepcion],
                efectos=[efecto],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "cancelled"}],
        )
        self.assertEqual(
            propuesta.decision["contadores"],
            [{"counter": "cerradas:a", "expected": 0}],
        )
        self.assertEqual(propuesta.decision["respuestas_aplicadas"], ["e1"])


class LosHechosMalFormadosNoTumbanLaVuelta(unittest.TestCase):
    """F8: decidir es total con hechos mal formados; el hecho inválido se
    aísla y la vuelta sigue."""

    def test_una_asignacion_sin_target_en_la_unidad_no_reventa(self):
        sin_target = {
            "instructionRef": {"ref": "artifact:a-1", "digest": DIGEST},
            "resultContract": "review.v1",
            "continuation": {"kind": "requester"},
        }
        documento = intento("a", 1, "reported")
        documento["assignment"] = sin_target
        propuesta = decidir.decidir(vista([unidad("a")], intentos=[documento]))
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])

    def test_un_hecho_de_claim_sin_artifact_ref_se_aisla(self):
        malo = {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "outcome": "passed",
            "expiresAt": None,
        }
        bueno = {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "planRevision": 1,
            "artifactRef": {"ref": "artifact:check", "digest": DIGEST},
            "environment": {"host": "host-01", "versions": {}},
            "role": "verifier",
            "procedure": "pytest",
            "outcome": "passed",
            "at": "2026-10-17T11:00:00Z",
            "expiresAt": None,
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [malo, bueno],
                    "efectos": [],
                },
            )
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(len(propuesta.decision["claims"]), 1)
        self.assertIsNotNone(propuesta.decision["claims"][0].get("evidenceId"))

    def test_un_hecho_de_asignacion_sin_target_no_se_liga(self):
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {
                    "unitId": "a",
                    "assignment": {"resultContract": "review.v1"},
                    "relay": False,
                }
            ],
            "claims": [],
            "efectos": [],
        }
        propuesta = decidir.decidir(vista([unidad("a")], hechos=hechos))
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(propuesta.decision["bindings"], [])
        self.assertEqual(necesitaciones(propuesta), ["first"])

    def test_un_hecho_de_asignacion_mal_formado_deja_pasar_al_valido(self):
        hechos = {
            "principal": ["david"],
            "asignaciones": [
                {
                    "unitId": "a",
                    "assignment": {"resultContract": "review.v1"},
                    "relay": False,
                },
                {"unitId": "a", "assignment": asignacion("a-1"), "relay": False},
            ],
            "claims": [],
            "efectos": [],
        }
        propuesta = decidir.decidir(vista([unidad("a")], hechos=hechos))
        self.assertEqual(len(propuesta.decision["bindings"]), 1)
        self.assertEqual(
            propuesta.decision["bindings"][0]["assignment"], asignacion("a-1")
        )

    def test_un_hecho_de_claim_sin_plan_revision_se_aisla(self):
        incompleto = {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "artifactRef": {"ref": "artifact:check", "digest": DIGEST},
            "role": "verifier",
            "procedure": "pytest",
            "outcome": "passed",
            "at": "2026-10-17T11:00:00Z",
            "expiresAt": None,
        }
        bueno = {
            **incompleto,
            "planRevision": 1,
            "environment": {"host": "host-01", "versions": {}},
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [incompleto, bueno],
                    "efectos": [],
                },
            )
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(len(propuesta.decision["claims"]), 1)
        self.assertEqual(propuesta.decision["claims"][0].get("planRevision"), 1)

    def test_un_claim_de_otra_raiz_no_se_siembra(self):
        ajeno = {
            "rootId": "root-otra",
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "planRevision": 1,
            "artifactRef": {"ref": "artifact:check", "digest": DIGEST},
            "environment": {"host": "host-01", "versions": {}},
            "role": "verifier",
            "procedure": "pytest",
            "outcome": "passed",
            "at": "2026-10-17T11:00:00Z",
            "expiresAt": None,
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [ajeno],
                    "efectos": [],
                },
            )
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(propuesta.decision["claims"], [])
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])

    def test_un_claim_de_otra_revision_de_plan_no_se_siembra(self):
        obsoleto = {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "planRevision": 2,
            "artifactRef": {"ref": "artifact:check", "digest": DIGEST},
            "environment": {"host": "host-01", "versions": {}},
            "role": "verifier",
            "procedure": "pytest",
            "outcome": "passed",
            "at": "2026-10-17T11:00:00Z",
            "expiresAt": None,
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [obsoleto],
                    "efectos": [],
                },
            )
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(propuesta.decision["claims"], [])
        self.assertEqual(propuesta.decision["outcomes"], [])
        self.assertEqual(necesitaciones(propuesta), [("verify", ("a1",))])

    def test_un_hecho_de_efecto_sin_effect_id_se_aisla(self):
        efectos = [
            {"action": "push", "destination": "dest-a"},
            "efecto que no es mapa",
            {
                "effectId": "effect-01",
                "action": "push",
                "destination": "dest-a",
                "cancellable": False,
            },
        ]

        def admite(_intent):
            return None

        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [],
                    "efectos": efectos,
                },
            ),
            admite,
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(len(propuesta.decision["efectos"]), 1)
        self.assertEqual(propuesta.decision["efectos"][0]["effectId"], "effect-01")

    def test_un_hecho_de_claim_con_outcome_fuera_del_enum_se_aisla(self):
        raro = {
            "rootId": RAIZ,
            "unitId": "a",
            "attempt": 1,
            "criterionId": "a1",
            "planRevision": 1,
            "artifactRef": {"ref": "artifact:check", "digest": DIGEST},
            "environment": {"host": "host-01", "versions": {}},
            "role": "verifier",
            "procedure": "pytest",
            "outcome": "casi",
            "at": "2026-10-17T11:00:00Z",
            "expiresAt": None,
        }
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "reported")],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [raro],
                    "efectos": [],
                },
            )
        )
        self.assertIsInstance(propuesta, decidir.Propuesta)
        self.assertEqual(propuesta.decision["claims"], [])


class ElEscritorAjenoDifiereElLigado(unittest.TestCase):
    """F18: el repo que otra raíz tiene tomado no se liga; decide lo mismo
    que el store juzga con writer-held."""

    @staticmethod
    def con_repo(etiqueta):
        return {
            **asignacion(etiqueta),
            "inputRevision": {
                "kind": "code",
                "repository": "owner-01/repo-a",
                "sha": "0" * 40,
            },
        }

    @staticmethod
    def escritores(root_id):
        return [
            {
                "resourceRef": "repo:owner-01/repo-a",
                "unitId": "z",
                "rootId": root_id,
            }
        ]

    @staticmethod
    def hechos(assignment):
        return {
            "principal": ["david"],
            "asignaciones": [{"unitId": "a", "assignment": assignment, "relay": False}],
            "claims": [],
            "efectos": [],
        }

    def test_no_liga_un_recurso_que_otra_raiz_tiene_tomado(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                hechos=self.hechos(self.con_repo("a-1")),
                escritores=self.escritores("root-otra"),
            )
        )
        self.assertEqual(propuesta.decision["bindings"], [])
        self.assertEqual(necesitaciones(propuesta), ["first"])

    def test_la_correccion_tampoco_liga_recurso_ajeno(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                intentos=[intento("a", 1, "failed")],
                hechos=self.hechos(self.con_repo("a-2")),
                escritores=self.escritores("root-otra"),
            )
        )
        self.assertEqual(propuesta.decision["bindings"], [])
        self.assertEqual(necesitaciones(propuesta), ["correction"])

    def test_el_escritor_de_la_propia_raiz_no_bloquea(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a")],
                hechos=self.hechos(self.con_repo("a-1")),
                escritores=self.escritores(RAIZ),
            )
        )
        self.assertEqual(len(propuesta.decision["bindings"]), 1)


class I01Borradores(unittest.TestCase):
    def test_el_id_es_determinista_y_las_opciones_siguen_el_orden_de_la_clase(self):
        sujeto = {"kind": "rounds", "unitId": "a", "block": 0}
        primero = decidir.borrador_excepcion(RAIZ, "NECESITA_DAVID", sujeto)
        segundo = decidir.borrador_excepcion(RAIZ, "NECESITA_DAVID", sujeto)
        self.assertEqual(primero["exceptionId"], segundo["exceptionId"])
        recortado = decidir.borrador_excepcion(
            RAIZ, "NECESITA_DAVID", sujeto, opciones=["seguir", "omitir"]
        )
        self.assertEqual(recortado["options"], ["omitir", "seguir"])
        con_todo = decidir.borrador_excepcion(RAIZ, "NECESITA_DAVID", sujeto)
        self.assertEqual(
            con_todo["options"],
            [
                "autorizar",
                "listo",
                "omitir",
                "cancelar",
                "aceptar",
                "rechazar",
                "seguir",
            ],
        )
        with self.assertRaises(ValueError):
            decidir.borrador_excepcion(RAIZ, "CI_ROJO", sujeto, opciones=["relevo"])


def asignacion_con_repo(etiqueta="a-1", repo="owner-01/repo-a"):
    base = asignacion(etiqueta)
    base["inputRevision"] = {"kind": "code", "repository": repo, "sha": "0" * 40}
    return base


def hecho_de(unit_id, assignment, relevo=False):
    return {"unitId": unit_id, "assignment": assignment, "relay": relevo}


def incidencia_de_reintento(unit_id, attempt=1):
    return {
        "unitId": unit_id,
        "attempt": attempt,
        "incidentKey": f"inc-{unit_id}",
        "incident": {
            "kind": "transport-unavailable",
            "episodeId": "session-closed",
        },
    }


class F18EscritorTomado(unittest.TestCase):
    """F18: ningún sitio liga sin mirar plazas y el recurso tomado por otra
    raíz. Si no se puede ligar, la unidad queda para la siguiente vuelta, la
    respuesta del dueño no se consume y el resto de la decisión avanza."""

    def vista_con_recurso_ajeno(self, hechos=None, **cambios):
        return vista(
            [unidad("a"), unidad("c")],
            escritores=[
                {
                    "schema": "sistema-agentes.escritor.v1",
                    "rootId": "root-B",
                    "resourceRef": "repo:owner-01/repo-a",
                    "unitId": "x",
                }
            ],
            hechos=hechos
            or {
                "principal": ["david"],
                "asignaciones": [
                    hecho_de("c", asignacion_con_repo("c-1", "owner-01/repo-b"))
                ],
                "claims": [],
                "efectos": [],
            },
            **cambios,
        )

    def test_un_reintento_con_recurso_ajeno_queda_para_la_siguiente(self):
        propuesta = decidir.decidir(
            self.vista_con_recurso_ajeno(
                intentos=[
                    intento("a", 1, "running", assignment=asignacion_con_repo("a-1"))
                ],
                incidencias=[incidencia_de_reintento("a")],
                contadores={"reintentos:a": 0},
            )
        )
        ligadas = [b["unitId"] for b in propuesta.decision["bindings"]]
        self.assertEqual(ligadas, ["c"], "a queda para la siguiente vuelta")
        self.assertEqual(
            propuesta.decision["contadores"],
            [],
            "el tope de reintentos no se gasta mientras espera",
        )

    def test_el_seguir_del_dueno_con_recurso_ajeno_no_se_consume(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "INESPERADO",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "incident:x"},
            ["seguir", "omitir", "cancelar"],
        )
        propuesta = decidir.decidir(
            self.vista_con_recurso_ajeno(
                intentos=[
                    intento("a", 1, "reported", assignment=asignacion_con_repo("a-1"))
                ],
                resultados=[resultado("a", 1)],
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "seguir",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
            )
        )
        ligadas = [b["unitId"] for b in propuesta.decision["bindings"]]
        self.assertEqual(ligadas, ["c"])
        self.assertEqual(
            propuesta.decision["respuestas_aplicadas"],
            [],
            "la respuesta queda para cuando el recurso se libere",
        )

    def test_un_relevo_con_recurso_ajeno_no_se_consume(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "SIN_REVISOR",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "relay:1"},
            ["relevo", "cancelar"],
        )
        propuesta = decidir.decidir(
            self.vista_con_recurso_ajeno(
                intentos=[
                    intento("a", 1, "reported", assignment=asignacion_con_repo("a-1"))
                ],
                resultados=[resultado("a", 1)],
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "relevo",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
                hechos={
                    "principal": ["david"],
                    "asignaciones": [
                        hecho_de(
                            "a",
                            asignacion_con_repo("a-relevo", "owner-01/repo-a"),
                            relevo=True,
                        )
                    ],
                    "claims": [],
                    "efectos": [],
                },
            )
        )
        self.assertEqual(
            propuesta.decision["bindings"], [], "no liga con el recurso ajeno"
        )
        self.assertEqual(propuesta.decision["respuestas_aplicadas"], [])
        self.assertIn("relay", necesitaciones(propuesta), "sigue pidiendo el relevo")


class F10LasPlazasDescuentanLoLiberado(unittest.TestCase):
    """F10: las plazas descuentan lo que la propia decision libera. Cada
    outcome sobre un intento vivo libera su plaza de raiz, una global y una
    del perfil; y el cupo sigue mandando para lo que la decision no libera."""

    def test_el_outcome_de_la_respuesta_libera_la_plaza_para_la_ligadura(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "NECESITA_DAVID",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "run:x"},
            ["cancelar"],
        )
        propuesta = decidir.decidir(
            vista(
                [unidad("a"), unidad("c")],
                intentos=[intento("a", 1, "running")],
                plazas={"raiz": 0, "global": 0, "por_perfil": {"codex": 0}},
                hechos={
                    "principal": ["david"],
                    "asignaciones": [hecho_de("c", asignacion("c-1"))],
                    "claims": [],
                    "efectos": [],
                },
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "cancelar",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
            )
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "cancelled"}],
        )
        self.assertEqual(
            [b["unitId"] for b in propuesta.decision["bindings"]],
            ["c"],
            "la plaza que libera el outcome de esta decision cabe para c",
        )

    def test_el_cupo_sigue_mandando_para_lo_que_la_decision_no_libera(self):
        propuesta = decidir.decidir(
            vista(
                [unidad("a"), unidad("b")],
                intentos=[intento("a", 1, "failed"), intento("b", 1, "running")],
                plazas={"raiz": 0, "global": 8, "por_perfil": {"codex": 8}},
                hechos={
                    "principal": ["david"],
                    "asignaciones": [hecho_de("a", asignacion("a-2"))],
                    "claims": [],
                    "efectos": [],
                },
            )
        )
        self.assertEqual(
            propuesta.decision["bindings"],
            [],
            "la unica plaza la ocupa el intento vivo de b y nada lo libera",
        )
        self.assertEqual(necesitaciones(propuesta), ["correction"])


class R9ElRelevoMideConElIntentoQueCancela(unittest.TestCase):
    """r9: el relevo del dueno mide la plaza contando como liberado el intento
    vivo que cancela; el assignment nuevo se mide con SU perfil (un relevo que
    cambia de perfil necesita plaza en el nuevo; el mismo perfil netea)."""

    CLAUDE = {"kind": "cli", "hostId": "host-01", "adapterId": "claude"}

    def relevo_respondido(self, plazas, assignment):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "SIN_REVISOR",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "reviewer:a1"},
            ["relevo", "cancelar"],
        )
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "reported")],
                plazas=plazas,
                hechos={
                    "principal": ["david"],
                    "asignaciones": [hecho_de("a", assignment, relevo=True)],
                    "claims": [],
                    "efectos": [],
                },
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "relevo",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
            )
        )
        return borrador["exceptionId"], propuesta

    def test_a_otro_perfil_con_raiz_en_tope_por_el_vivo_liga(self):
        _, propuesta = self.relevo_respondido(
            {"raiz": 0, "global": 8, "por_perfil": {"codex": 8, "claude": 8}},
            asignacion("a-relevo", destino=self.CLAUDE),
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "failed"}],
        )
        self.assertEqual(
            [
                b["assignment"]["target"]["adapterId"]
                for b in propuesta.decision["bindings"]
            ],
            ["claude"],
            "la plaza del vivo cancelado cabe para el relevo de otro perfil",
        )

    def test_a_otro_perfil_sin_cupo_en_el_nuevo_se_difiere(self):
        id_excepcion, propuesta = self.relevo_respondido(
            {"raiz": 8, "global": 8, "por_perfil": {"codex": 8, "claude": 0}},
            asignacion("a-relevo", destino=self.CLAUDE),
        )
        self.assertEqual(
            propuesta.decision["bindings"],
            [],
            "el tope del perfil nuevo lo ocupa otra unidad y nada lo libera",
        )
        self.assertEqual(propuesta.decision["respuestas_aplicadas"], [])
        self.assertIn("relay", necesitaciones(propuesta))


class F12LaLiberacionSoloCuentaSiElIntentoVive(unittest.TestCase):
    """F12: la liberacion de se_puede_ligar_liberando solo cuenta si el
    intento a cancelar sigue vivo; terminar no emite outcome sobre un intento
    ya terminal (o terminado en esta decision), y descontarlo abre una plaza
    ficticia: se liga de mas y el store rechaza la decision entera con
    capacity."""

    def seguir_sobre_un_fallado(self, plazas):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "INESPERADO",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "incident:x"},
            ["seguir", "omitir", "cancelar"],
        )
        return decidir.decidir(
            vista(
                [unidad("a"), unidad("b")],
                intentos=[intento("a", 1, "failed"), intento("b", 1, "running")],
                plazas=plazas,
                hechos={
                    "principal": ["david"],
                    "asignaciones": [],
                    "claims": [],
                    "efectos": [],
                },
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "seguir",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
            )
        )

    def test_el_seguir_tardio_no_pasa_por_una_plaza_ficticia(self):
        propuesta = self.seguir_sobre_un_fallado(
            {"raiz": 0, "global": 8, "por_perfil": {"codex": 8}}
        )
        self.assertEqual(
            propuesta.decision["outcomes"],
            [],
            "el intento ya es failed: terminar no emite nada",
        )
        self.assertEqual(
            propuesta.decision["bindings"],
            [],
            "la unica plaza la ocupa el vivo de b: la unidad se difiere",
        )
        self.assertEqual(
            propuesta.decision["respuestas_aplicadas"],
            [],
            "la respuesta queda para cuando haya plaza de verdad",
        )

    def test_el_relevo_tardio_tampoco_pasa_por_una_plaza_ficticia(self):
        borrador = decidir.borrador_excepcion(
            RAIZ,
            "SIN_REVISOR",
            {"kind": "attempt", "unitId": "a", "attempt": 1, "cause": "reviewer:a1"},
            ["relevo", "cancelar"],
        )
        propuesta = decidir.decidir(
            vista(
                [unidad("a", roles=("reviewer",))],
                intentos=[intento("a", 1, "failed")],
                plazas={"raiz": 0, "global": 8, "por_perfil": {"codex": 8}},
                hechos={
                    "principal": ["david"],
                    "asignaciones": [
                        hecho_de("a", asignacion("a-relevo"), relevo=True)
                    ],
                    "claims": [],
                    "efectos": [],
                },
                excepciones=[
                    {
                        **borrador,
                        "response": {
                            "optionId": "relevo",
                            "by": ["david"],
                            "at": AHORA,
                        },
                    }
                ],
            )
        )
        self.assertEqual(propuesta.decision["bindings"], [])
        self.assertEqual(propuesta.decision["respuestas_aplicadas"], [])
        self.assertIn("relay", necesitaciones(propuesta))


if __name__ == "__main__":
    unittest.main()
