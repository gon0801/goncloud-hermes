"""El controlador de una raíz: decidir(vista) -> propuesta (SUA-07 H1).

Pura y total: toda la información que usa entra en la vista (plan, intentos,
resultados, incidencias, efectos, excepciones, contadores, política, hechos y el
reloj `ahora`). Sin I/O y sin reloj propio. La autoridad es un parámetro inyectable;
hoy admite todo y H3 la intersectará con los permisos de H. La semántica transcribe
managed-root.decide.ts de R con la política quality-kit.regla4 (5+5, el relevo es la
ronda 6, a la 10 sin converger NECESITA_DAVID).
"""

from __future__ import annotations

import hashlib
import importlib
import json
import re
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, TypedDict

compartido = importlib.import_module("raices.compartido")
contrato = compartido.contrato

_FECHA = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")

TERMINALES = frozenset({"accepted", "failed", "cancelled"})
REINTENTOS_AUTOMATICOS = 1
EPISODIOS_DE_REINTENTO = frozenset(
    {"launch-failed", "session-closed", "delivery-unaccepted"}
)

TIPOS_DE_EXCEPCION = (
    "SIN_REVISOR",
    "SIN_CUOTA",
    "AUTH_VENCIDA",
    "CI_ROJO",
    "BOT_CALLADO",
    "CONFIRMAR_PLAN",
    "NECESITA_DAVID",
    "BLOQUEANTE_REPETIDO",
    "DEPLOY_FALLO",
    "CIERRE_ROJO",
    "INESPERADO",
    "EVIDENCIA_INCIERTA",
    "CAPACIDAD_INCIERTA",
)

CLASES_DE_EXCEPCION: dict = {
    "SIN_REVISOR": ("seguir", "relevo", "cancelar"),
    "SIN_CUOTA": ("seguir", "relevo", "cancelar"),
    "AUTH_VENCIDA": ("listo", "cancelar"),
    "CI_ROJO": ("seguir", "omitir", "cancelar"),
    "BOT_CALLADO": ("seguir", "cancelar"),
    "CONFIRMAR_PLAN": ("si", "cancelar"),
    "NECESITA_DAVID": (
        "autorizar",
        "listo",
        "omitir",
        "cancelar",
        "aceptar",
        "rechazar",
        "seguir",
    ),
    "BLOQUEANTE_REPETIDO": ("omitir", "cancelar"),
    "DEPLOY_FALLO": ("seguir", "listo", "cancelar"),
    "CIERRE_ROJO": ("seguir", "cancelar"),
    "INESPERADO": ("seguir", "omitir", "cancelar"),
    "EVIDENCIA_INCIERTA": ("conciliar", "omitir", "cancelar"),
    "CAPACIDAD_INCIERTA": ("seguir", "relevo", "cancelar"),
}

TEXTO_DE_OPCION = {
    "seguir": "Seguir.",
    "relevo": "Pasarlo a otro implementador.",
    "cancelar": "Cancelar.",
    "listo": "Ya está hecho.",
    "omitir": "Omitirlo sin aceptarlo.",
    "si": "Sí.",
    "autorizar": "Autorizar solo este efecto.",
    "aceptar": "Aceptar el criterio.",
    "rechazar": "Rechazar el criterio.",
    "conciliar": "Conciliar y asentar lo que pasó.",
}

RENOTICE_MS = 3_600_000


class PoliticaCalidad(TypedDict):
    version: str
    roundsPerImplementer: int
    relays: int
    revisorExcluyeAutores: bool
    segundoModelo: bool


class Hechos(TypedDict):
    principal: list
    asignaciones: list
    claims: list
    efectos: list


class Vista(TypedDict):
    meta: dict
    unidades: list
    sin_ligar: list
    evidencia: list
    efectos: list
    resultados: list
    incidencias: list
    excepciones: list
    contadores: dict
    escritores: list
    hallazgos: list
    plazas: dict
    hechos: Hechos
    politica: PoliticaCalidad
    ahora: str


class NecesidadAsignacion(NamedTuple):
    unit_id: str
    razon: str
    findings_ref: str | None
    findings_refs: list


class NecesidadVerificacion(NamedTuple):
    unit_id: str
    attempt: int
    criterios: list


class Propuesta(NamedTuple):
    decision: dict
    necesidades: list
    avisar: list
    estado: str


def ADMITE_TODO(intento: Mapping) -> str | None:
    # TODO(H3): intersectar contrato.efecto_admisible con los permisos de H en los
    # tres puntos (arranque, reanudación y antes del efecto).
    return None


def digesto(partes: object) -> str:
    canonico = json.dumps(
        partes, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonico.encode("utf-8")).hexdigest()


def instante(texto: str) -> datetime:
    partes = _FECHA.fullmatch(texto)
    if partes is None:
        raise ValueError(f"no es una fecha ISO 8601 en UTC con Z: {texto!r}")
    *enteros, fraccion = partes.groups()
    micros = int((fraccion or "")[:6].ljust(6, "0"))
    return datetime(*(int(parte) for parte in enteros), micros, tzinfo=timezone.utc)


def borrador_excepcion(
    root_id: str,
    tipo: str,
    sujeto: dict,
    opciones: list | None = None,
    evidencia_ref: dict | None = None,
) -> dict:
    if tipo not in CLASES_DE_EXCEPCION:
        raise ValueError(f"clase de excepción desconocida: {tipo}")
    clase = CLASES_DE_EXCEPCION[tipo]
    pedidas = list(clase) if opciones is None else list(opciones)
    for opcion in pedidas:
        if opcion not in clase:
            raise ValueError(f"{tipo} no ofrece {opcion}")
    ofrecidas = [opcion for opcion in clase if opcion in pedidas]
    return {
        "exceptionId": digesto([root_id, tipo, sujeto]),
        "type": tipo,
        "subject": sujeto,
        "options": ofrecidas,
        "evidenceRef": evidencia_ref,
    }


def _perfil_de(target: Mapping) -> str:
    identidad = target.get("agentId") or target.get("adapterId")
    if identidad is None:
        identidad = json.dumps(target, sort_keys=True, separators=(",", ":"))
    return str(identidad)


def _target_de(assignment):
    if isinstance(assignment, Mapping):
        target = assignment.get("target")
        if isinstance(target, Mapping):
            return target
    return {}


def _decision_vacia(revision: int, epoch: int) -> dict:
    return {
        "expected_plan_revision": revision,
        "expected_control_epoch": epoch,
        "consumidos": [],
        "claims": [],
        "outcomes": [],
        "bindings": [],
        "efectos": [],
        "contadores": [],
        "excepciones": [],
        "respuestas_aplicadas": [],
        "control": {},
    }


def decidir(
    vista: Vista, autoridad: Callable[[Mapping], str | None] = ADMITE_TODO
) -> Propuesta:
    """El juicio del controlador, en el orden fijo de R. Cada paso ve lo que los
    anteriores pusieron en la propuesta. Las necesidades son NecesidadAsignacion o
    NecesidadVerificacion."""
    meta = vista["meta"]
    root_id = meta["rootId"]
    revision = meta["plan"]["revision"]
    epoch = meta["controlEpoch"]
    ahora = instante(vista["ahora"])
    politica = vista["politica"]
    decision = _decision_vacia(revision, epoch)
    necesidades: list = []
    conocidas = {e["exceptionId"] for e in vista["excepciones"]}

    def levantar(tipo, sujeto, opciones=None, evidencia_ref=None):
        borrador = borrador_excepcion(root_id, tipo, sujeto, opciones, evidencia_ref)
        if borrador["exceptionId"] not in conocidas:
            conocidas.add(borrador["exceptionId"])
            decision["excepciones"].append(borrador)

    def avisados_vencidos():
        avisados = []
        for fila in vista["excepciones"]:
            if fila.get("response") is not None:
                continue
            notado = fila.get("noticedAt")
            if notado is None:
                avisados.append(fila["exceptionId"])
                continue
            if ahora >= instante(notado) + timedelta(
                milliseconds=fila["renoticeAfterMs"]
            ):
                avisados.append(fila["exceptionId"])
        return avisados

    if meta["control"]["state"] == "cancelled":
        return Propuesta(decision, [], [], "stopped")
    if meta["control"]["state"] == "paused":
        return Propuesta(decision, [], avisados_vencidos(), "waiting")

    terminados: dict = {}
    ligadas = set()
    hechos_usados = set()
    fallo_por_resultado: dict = {}
    claims = list(vista["evidencia"])
    parar_raiz = False

    plan_unidades = meta["plan"]["units"]
    plan_por_id = {u["unitId"]: u for u in plan_unidades}

    def intentos_de(unit_id):
        return sorted(
            (d for d in vista["unidades"] if d["unitId"] == unit_id),
            key=lambda d: d["attempt"],
        )

    def estado_de(documento):
        return terminados.get(
            (documento["unitId"], documento["attempt"]), documento["state"]
        )

    def ultimo_de(unit_id):
        documentos = intentos_de(unit_id)
        return documentos[-1] if documentos else None

    def vivo_de(unit_id):
        ultimo = ultimo_de(unit_id)
        if ultimo and estado_de(ultimo) not in TERMINALES:
            return ultimo
        return None

    def hallazgos_de(unit_id):
        return [h["findingsRef"] for h in vista["hallazgos"] if h["unitId"] == unit_id]

    def hallazgos_acumulados(unit_id):
        historial = hallazgos_de(unit_id)
        actual = fallo_por_resultado.get(unit_id)
        if isinstance(actual, str):
            return historial + [actual]
        return historial

    def aceptada(unit_id):
        return any(estado_de(d) == "accepted" for d in intentos_de(unit_id))

    def contador(nombre):
        extraido = vista["contadores"].get(nombre, 0)
        por_subir = any(c["counter"] == nombre for c in decision["contadores"])
        return extraido + (1 if por_subir else 0)

    def subir_contador(nombre):
        if not any(c["counter"] == nombre for c in decision["contadores"]):
            decision["contadores"].append(
                {"counter": nombre, "expected": vista["contadores"].get(nombre, 0)}
            )

    def cerrada(unit_id):
        return contador(f"cerradas:{unit_id}") > contador(f"reabiertas:{unit_id}")

    def terminar(documento, desenlace):
        llave = (documento["unitId"], documento["attempt"])
        if llave in terminados or documento["state"] in TERMINALES:
            return
        if desenlace == "failed" and documento["state"] in (
            "pending",
            "ready",
            "admitted",
        ):
            desenlace = "cancelled"
        terminados[llave] = desenlace
        decision["outcomes"].append(
            {
                "unitId": documento["unitId"],
                "attempt": documento["attempt"],
                "outcome": desenlace,
            }
        )

    def ligar(unit_id, assignment):
        ligadas.add(unit_id)
        decision["bindings"].append(
            {"unitId": unit_id, "assignment": assignment, "writers": []}
        )

    def reintentar(unit_id):
        ultimo = ultimo_de(unit_id)
        if not ultimo or unit_id in ligadas or cerrada(unit_id):
            return
        terminar(ultimo, "failed")
        ligar(unit_id, ultimo["assignment"])

    def _hecho_disponible(unit_id, relevo):
        for indice, hecho in enumerate(vista["hechos"]["asignaciones"]):
            if indice in hechos_usados:
                continue
            if not isinstance(hecho, Mapping) or not isinstance(
                hecho.get("assignment"), Mapping
            ):
                continue
            assignment = hecho["assignment"]
            if not isinstance(assignment.get("target"), Mapping) or not isinstance(
                assignment.get("instructionRef"), Mapping
            ):
                continue
            if hecho.get("unitId") == unit_id and (
                relevo is None or hecho.get("relay") == relevo
            ):
                return indice, hecho
        return None

    def tomar_hecho(unit_id, relevo):
        hallado = _hecho_disponible(unit_id, relevo)
        if hallado is None:
            return None
        indice, hecho = hallado
        hechos_usados.add(indice)
        return hecho

    def soltar_hecho(hecho):
        hechos_usados.discard(vista["hechos"]["asignaciones"].index(hecho))

    def autores_de(unit_id):
        return {
            _perfil_de(_target_de(documento.get("assignment")))
            for documento in intentos_de(unit_id)
        }

    def sembrar_claim(claim_nuevo):
        decision["claims"].append(claim_nuevo)
        claims.append(claim_nuevo)

    def ultimo_claim(unit_id, attempt, criterion_id, excluir=None):
        for fila in reversed(claims):
            if (
                fila["unitId"] != unit_id
                or fila["attempt"] != attempt
                or fila["criterionId"] != criterion_id
            ):
                continue
            if fila["expiresAt"] is not None and instante(fila["expiresAt"]) <= ahora:
                continue
            if (
                excluir
                and fila["verifier"]["role"] == "reviewer"
                and excluir.intersection(fila["verifier"]["principal"])
            ):
                continue
            return fila
        return None

    def falla_el_resultado(pendiente):
        return pendiente["kind"] != "produced" or (
            pendiente["contrato"] == "review.v1" and pendiente["verdict"] == "changes"
        )

    def documento_de_tarea(task_id):
        for documento in vista["unidades"]:
            if documento["taskId"] == task_id:
                return documento
        return None

    # Los claims de los hechos entran primero: los resultados de abajo se juzgan con
    # ellos. Solo prueban verifier o ci (la guarda del store exige lo mismo) y cada
    # uno pasa contrato.validar antes de sembrarse: el inválido se aísla y el store
    # nunca rechaza la decisión entera por su forma (F12).
    for claim_hecho in vista["hechos"]["claims"]:
        if not isinstance(claim_hecho, Mapping):
            continue
        if claim_hecho.get("role", "verifier") not in ("verifier", "ci"):
            continue
        if any(
            clave not in claim_hecho
            for clave in ("unitId", "attempt", "criterionId", "outcome")
        ):
            continue
        artifact_ref = claim_hecho.get("artifactRef")
        if (
            not isinstance(artifact_ref, Mapping)
            or not isinstance(artifact_ref.get("ref"), str)
            or not isinstance(artifact_ref.get("digest"), str)
        ):
            continue
        resto = {k: v for k, v in claim_hecho.items() if k not in ("role",)}
        sembrado = {
            **resto,
            "evidenceId": digesto(
                [
                    root_id,
                    resto["unitId"],
                    resto["attempt"],
                    resto["criterionId"],
                    resto["artifactRef"]["digest"],
                    resto["outcome"],
                ]
            ),
            "verifier": {
                "principal": vista["hechos"]["principal"],
                "role": claim_hecho.get("role", "verifier"),
            },
        }
        if contrato.validar({"schema": contrato.EVIDENCIA, **sembrado}):
            continue
        sembrar_claim(sembrado)

    # Respuestas sin aplicar, cada una sobre su sujeto y nada más.
    for excepcion in vista["excepciones"]:
        respuesta = excepcion.get("response")
        if not respuesta or excepcion.get("appliedBy") is not None:
            continue
        sujeto = excepcion["subject"]
        if sujeto.get("kind") == "effect":
            unidad_sujeta = next(
                (
                    e["intent"].get("unitId")
                    for e in vista["efectos"]
                    if e["effectId"] == sujeto["effectId"]
                ),
                None,
            )
        elif sujeto.get("kind") == "root":
            unidad_sujeta = None
        else:
            unidad_sujeta = sujeto.get("unitId")
        vivo = vivo_de(unidad_sujeta) if unidad_sujeta else None
        opcion = respuesta["optionId"]
        aplicada = True
        if opcion == "cancelar":
            if sujeto.get("kind") == "root":
                parar_raiz = True
            elif unidad_sujeta:
                if vivo:
                    terminar(vivo, "cancelled")
                subir_contador(f"cerradas:{unidad_sujeta}")
        elif opcion == "omitir":
            if unidad_sujeta:
                if vivo:
                    terminar(vivo, "failed")
                subir_contador(f"cerradas:{unidad_sujeta}")
        elif opcion == "seguir":
            if sujeto.get("kind") == "rounds":
                subir_contador(f"bloques:{sujeto['unitId']}")
            elif excepcion["type"] == "CIERRE_ROJO":
                for unidad_plan in plan_unidades:
                    unidad_id = unidad_plan["unitId"]
                    if cerrada(unidad_id) and not aceptada(unidad_id):
                        subir_contador(f"reabiertas:{unidad_id}")
            elif unidad_sujeta and excepcion["type"] in (
                "INESPERADO",
                "CAPACIDAD_INCIERTA",
            ):
                reintentar(unidad_sujeta)
        elif opcion == "listo":
            if (
                unidad_sujeta
                and sujeto.get("kind") == "attempt"
                and excepcion["type"] in ("AUTH_VENCIDA", "NECESITA_DAVID")
            ):
                reintentar(unidad_sujeta)
        elif opcion == "relevo":
            if not unidad_sujeta or cerrada(unidad_sujeta) or unidad_sujeta in ligadas:
                pass
            else:
                hecho = tomar_hecho(unidad_sujeta, True)
                if hecho is None:
                    necesidades.append(
                        NecesidadAsignacion(
                            unidad_sujeta,
                            "relay",
                            None,
                            hallazgos_acumulados(unidad_sujeta),
                        )
                    )
                    aplicada = False
                else:
                    if vivo:
                        terminar(vivo, "failed")
                    ligar(unidad_sujeta, hecho["assignment"])
        elif opcion in ("aceptar", "rechazar"):
            if sujeto.get("kind") == "attempt" and sujeto.get("cause", "").startswith(
                "owner:"
            ):
                criterion_id = sujeto["cause"][len("owner:") :]
                sembrar_claim(
                    {
                        "evidenceId": digesto(
                            [root_id, excepcion["exceptionId"], opcion]
                        ),
                        "rootId": root_id,
                        "planRevision": revision,
                        "unitId": sujeto["unitId"],
                        "attempt": sujeto["attempt"],
                        "criterionId": criterion_id,
                        "artifactRef": {
                            "ref": f"exception:{excepcion['exceptionId']}",
                            "digest": f"sha256:{digesto(respuesta)}",
                        },
                        "environment": {"host": "gateway", "versions": {}},
                        "verifier": {
                            "principal": respuesta.get("by", []),
                            "role": "owner",
                        },
                        "procedure": f"El dueño respondió {opcion}.",
                        "outcome": "passed" if opcion == "aceptar" else "failed",
                        "at": respuesta["at"],
                        "expiresAt": None,
                    }
                )
        elif opcion == "conciliar" and sujeto.get("kind") == "attempt":
            causa = sujeto.get("cause", "")
            if causa.startswith("claim:"):
                necesidades.append(
                    NecesidadVerificacion(
                        sujeto["unitId"],
                        sujeto["attempt"],
                        [causa[len("claim:") :]],
                    )
                )
        if aplicada:
            decision["respuestas_aplicadas"].append(excepcion["exceptionId"])

    if parar_raiz and meta["control"]["state"] != "cancelled":
        decision["control"] = {"command": "cancel", "reason": "owner:cancelar"}

    for pendiente in vista["resultados"]:
        documento = documento_de_tarea(pendiente["taskId"])
        if documento is None or estado_de(documento) in TERMINALES:
            decision["consumidos"].append(pendiente["taskId"])
            continue
        if falla_el_resultado(pendiente):
            decision["consumidos"].append(pendiente["taskId"])
            terminar(documento, "failed")
            fallo_por_resultado[documento["unitId"]] = (
                pendiente["ref"] if pendiente["kind"] == "produced" else None
            )

    resultados_por_tarea = {r["taskId"] for r in vista["resultados"]}

    def juzgar_criterios(unidad_plan, documento):
        sin_cubrir: list = []
        abierto = False
        excluir = (
            autores_de(documento["unitId"])
            if politica["revisorExcluyeAutores"]
            else None
        )
        for criterio in unidad_plan["criteria"]:
            claim_vigente = ultimo_claim(
                documento["unitId"],
                documento["attempt"],
                criterio["criterionId"],
                excluir,
            )
            if claim_vigente is not None and claim_vigente["outcome"] == "failed":
                terminar(documento, "failed")
                fallo_por_resultado[documento["unitId"]] = claim_vigente["artifactRef"][
                    "ref"
                ]
                return
            if claim_vigente is not None and claim_vigente["outcome"] == "passed":
                continue
            abierto = True
            sujeto_base = {
                "kind": "attempt",
                "unitId": documento["unitId"],
                "attempt": documento["attempt"],
            }
            if claim_vigente is not None and claim_vigente["outcome"] == "unknown":
                levantar(
                    "EVIDENCIA_INCIERTA",
                    {**sujeto_base, "cause": f"claim:{criterio['criterionId']}"},
                )
            elif criterio["verifier"] == "owner":
                levantar(
                    "NECESITA_DAVID",
                    {**sujeto_base, "cause": f"owner:{criterio['criterionId']}"},
                    ["aceptar", "rechazar"],
                )
            elif criterio["verifier"] == "reviewer":
                levantar(
                    "SIN_REVISOR",
                    {**sujeto_base, "cause": f"reviewer:{criterio['criterionId']}"},
                )
            else:
                sin_cubrir.append(criterio["criterionId"])
        if sin_cubrir:
            necesidades.append(
                NecesidadVerificacion(
                    documento["unitId"], documento["attempt"], sin_cubrir
                )
            )
        if not abierto:
            if documento["taskId"] in resultados_por_tarea:
                decision["consumidos"].append(documento["taskId"])
                terminar(documento, "accepted")

    for unidad_plan in plan_unidades:
        documento = vivo_de(unidad_plan["unitId"])
        if documento is None or estado_de(documento) != "reported":
            continue
        juzgar_criterios(unidad_plan, documento)

    def clase_de_incidencia(incidencia):
        clase = incidencia.get("kind")
        if clase == "permission-required":
            return "permission"
        if clase in ("deadline-missed", "invalid-result", "recovery-transport-failure"):
            return "retry"
        if clase == "transport-unavailable":
            if incidencia.get("episodeId") == "launch-refused":
                return "capacity"
            return (
                "retry"
                if incidencia.get("episodeId") in EPISODIOS_DE_REINTENTO
                else "unexpected"
            )
        return "unexpected"

    for incidencia in vista["incidencias"]:
        documento = next(
            (
                d
                for d in intentos_de(incidencia["unitId"])
                if d["attempt"] == incidencia["attempt"]
            ),
            None,
        )
        if documento is None or estado_de(documento) in TERMINALES:
            continue
        sujeto = {
            "kind": "attempt",
            "unitId": incidencia["unitId"],
            "attempt": incidencia["attempt"],
            "cause": f"incident:{incidencia['incidentKey']}",
        }
        clase = clase_de_incidencia(incidencia["incident"])
        if clase == "retry":
            if contador(f"reintentos:{incidencia['unitId']}") < REINTENTOS_AUTOMATICOS:
                subir_contador(f"reintentos:{incidencia['unitId']}")
                reintentar(incidencia["unitId"])
            else:
                levantar("INESPERADO", sujeto)
        elif clase == "capacity":
            levantar("CAPACIDAD_INCIERTA", sujeto)
        elif clase == "permission":
            levantar("NECESITA_DAVID", sujeto, ["listo", "omitir", "cancelar"])
        else:
            levantar("INESPERADO", sujeto)

    # Efectos: los nuevos siempre se preparan, así una pregunta nombra un efecto real.
    for efecto in vista["efectos"]:
        if efecto["state"] == "uncertain":
            levantar(
                "EVIDENCIA_INCIERTA",
                {"kind": "effect", "effectId": efecto["effectId"]},
            )
        elif efecto["state"] == "prepared" and autoridad(efecto["intent"]) is not None:
            levantar(
                "NECESITA_DAVID",
                {"kind": "effect", "effectId": efecto["effectId"]},
                ["autorizar", "listo", "omitir", "cancelar"],
            )
    for intent in vista["hechos"]["efectos"]:
        ya_visto = any(
            e["effectId"] == intent["effectId"]
            for e in vista["efectos"] + decision["efectos"]
        )
        if ya_visto:
            continue
        decision["efectos"].append(intent)
        if autoridad(intent) is not None:
            levantar(
                "NECESITA_DAVID",
                {"kind": "effect", "effectId": intent["effectId"]},
                ["autorizar", "listo", "omitir", "cancelar"],
            )

    # Correcciones: `rondas` cuenta por la vida de la unidad; el tope crece un bloque
    # cada vez que el dueño responde `seguir`.
    por_bloque = politica["roundsPerImplementer"]

    def caben_en_plazas(assignment, ya_ligados):
        plazas = vista["plazas"]
        perfil = _perfil_de(_target_de(assignment))
        por_perfil = plazas.get("por_perfil") or {}
        cabe = (
            ya_ligados + 1 <= int(plazas.get("raiz", 0))
            and ya_ligados + 1 <= int(plazas.get("global", 0))
            and (perfil not in por_perfil or ya_ligados + 1 <= int(por_perfil[perfil]))
        )
        return cabe

    for unidad_plan in plan_unidades:
        unit_id = unidad_plan["unitId"]
        ultimo = ultimo_de(unit_id)
        if (
            ultimo is None
            or estado_de(ultimo) != "failed"
            or unit_id in ligadas
            or aceptada(unit_id)
            or cerrada(unit_id)
        ):
            continue
        rondas = contador(f"rondas:{unit_id}")
        bloques = contador(f"bloques:{unit_id}")
        if rondas >= por_bloque * (1 + politica["relays"] + bloques):
            levantar(
                "NECESITA_DAVID",
                {"kind": "rounds", "unitId": unit_id, "block": bloques},
                ["seguir", "omitir", "cancelar"],
            )
            continue
        relevo = rondas >= por_bloque
        hecho = tomar_hecho(unit_id, relevo)
        if hecho is None:
            necesidades.append(
                NecesidadAsignacion(
                    unit_id,
                    "relay" if relevo else "correction",
                    fallo_por_resultado.get(unit_id),
                    hallazgos_acumulados(unit_id),
                )
            )
            continue
        if not caben_en_plazas(hecho["assignment"], len(decision["bindings"])):
            soltar_hecho(hecho)
            necesidades.append(
                NecesidadAsignacion(
                    unit_id,
                    "relay" if relevo else "correction",
                    fallo_por_resultado.get(unit_id),
                    hallazgos_acumulados(unit_id),
                )
            )
            continue
        ligar(unit_id, hecho["assignment"])
        subir_contador(f"rondas:{unit_id}")

    for unidad_plan in plan_unidades:
        unit_id = unidad_plan["unitId"]
        ultimo = ultimo_de(unit_id)
        if (
            unit_id in ligadas
            or aceptada(unit_id)
            or cerrada(unit_id)
            or (ultimo is not None and estado_de(ultimo) != "cancelled")
            or not all(aceptada(d) for d in unidad_plan["dependsOn"])
        ):
            continue
        hallado = _hecho_disponible(unit_id, None)
        if hallado is None:
            necesidades.append(NecesidadAsignacion(unit_id, "first", None, []))
            continue
        indice, hecho = hallado
        if not caben_en_plazas(hecho["assignment"], len(decision["bindings"])):
            necesidades.append(NecesidadAsignacion(unit_id, "first", None, []))
            continue
        hechos_usados.add(indice)
        ligar(unit_id, hecho["assignment"])

    # 9 antes de 8, para que la pregunta de cierre salga en la misma llamada.
    def depende_de(unit_id):
        return plan_por_id[unit_id]["dependsOn"]

    def bloqueada(unit_id, memo):
        if unit_id in memo:
            return memo[unit_id]
        valor = (
            not aceptada(unit_id)
            and vivo_de(unit_id) is None
            and any(
                not aceptada(d) and (cerrada(d) or bloqueada(d, memo))
                for d in depende_de(unit_id)
            )
        )
        memo[unit_id] = valor
        return valor

    memo_bloqueadas: dict = {}
    done = all(aceptada(u["unitId"]) for u in plan_unidades)
    if (
        plan_unidades
        and not done
        and all(
            aceptada(u["unitId"])
            or cerrada(u["unitId"])
            or bloqueada(u["unitId"], memo_bloqueadas)
            for u in plan_unidades
        )
    ):
        cierres = sum(contador(f"cerradas:{u['unitId']}") for u in plan_unidades)
        levantar(
            "CIERRE_ROJO",
            {"kind": "root", "cause": f"cierre:{cierres}"},
            ["seguir", "cancelar"],
        )

    avisar = [e["exceptionId"] for e in decision["excepciones"]] + avisados_vencidos()
    decision_vacia = all(
        len(decision[llave]) == 0
        for llave in (
            "consumidos",
            "claims",
            "outcomes",
            "bindings",
            "efectos",
            "contadores",
            "excepciones",
            "respuestas_aplicadas",
            "control",
        )
    )
    if parar_raiz:
        estado = "stopped"
    elif done:
        estado = "done"
    elif decision_vacia and not necesidades:
        estado = "waiting"
    else:
        estado = "working"
    return Propuesta(decision, necesidades, avisar, estado)
