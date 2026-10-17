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


def intento(unit_id, n, estado, destino=None):
    return {
        "schema": "sistema-agentes.unidad.v1",
        "rootId": RAIZ,
        "planRevision": 1,
        "unitId": unit_id,
        "attempt": n,
        "taskId": f"task-{unit_id}-{n}",
        "assignment": asignacion(f"{unit_id}-{n}", destino),
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
            )
        )
        self.assertEqual(
            sin_regla.decision["outcomes"],
            [{"unitId": "a", "attempt": 1, "outcome": "accepted"}],
        )


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


if __name__ == "__main__":
    unittest.main()
