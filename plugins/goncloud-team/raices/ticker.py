"""El ticker de las raíces: una vuelta = conciliar, decidir y despachar (SUA-07 H1).

Puede haber dos tickers a la vez: la exclusividad la dan las transacciones
(BEGIN IMMEDIATE en sistema-agentes.db y write_txn en el kanban), no la memoria.
Cada paso es idempotente por filas: una vuelta interrumpida en cualquier punto la
completa la siguiente (I04). El despacho pasa por la bandeja de salida: la intención
en sistema-agentes.db, buscar-o-crear la tarjeta en UN write_txn del kanban y, recién
entonces, marcar la intención cumplida (I03). Las raíces con control != active no
despachan: sus intenciones quedan cercadas.
"""

from __future__ import annotations

import importlib
from typing import NamedTuple

compartido = importlib.import_module("raices.compartido")
decidir = importlib.import_module("raices.decidir")
kanban_port = importlib.import_module("raices.kanban_port")
store_module = importlib.import_module("raices.store")

CaidaInyectada = kanban_port.CaidaInyectada
SpecDeTarjeta = kanban_port.SpecDeTarjeta
digesto = decidir.digesto

LLAVES_DE_DECISION = (
    "consumidos",
    "claims",
    "outcomes",
    "bindings",
    "efectos",
    "contadores",
    "excepciones",
    "respuestas_aplicadas",
)


class Informe(NamedTuple):
    conciliadas: int
    aplicadas: int
    conflictos: tuple
    tarjetas_creadas: int
    estado: str | None


def cargar_politicas() -> dict:
    """Las versiones de politica-calidad.json de la copia fijada. La I/O vive aquí
    para que decidir siga pura; el store congela la versión de cada raíz al abrirla."""
    return store_module.leer_politicas()


def vuelta(tienda, puerto, hechos_por_raiz=None) -> Informe:
    """Una vuelta completa. Una CaidaInyectada del doble kanban se propaga: simula la
    muerte del proceso, y la vuelta siguiente recupera desde las filas."""
    hechos_por_raiz = hechos_por_raiz or {}
    conciliadas = 0
    aplicadas = 0
    conflictos: list = []
    creadas_antes = len(puerto.creaciones)
    estado: str | None = None

    for intento in tienda.intentos_con_tarjeta():
        if _conciliar_uno(tienda, puerto, intento):
            conciliadas += 1

    for raiz in tienda.raices():
        root_id = raiz["rootId"]
        vista = tienda.leer_vista(
            root_id, hechos_por_raiz.get(root_id) or _hechos_vacios()
        )
        if vista is None:
            continue
        propuesta = decidir.decidir(vista)
        decision = propuesta.decision
        if any(len(decision[llave]) > 0 for llave in LLAVES_DE_DECISION):
            resultado = tienda.aplicar(
                root_id, decision, _decision_id(vista, propuesta)
            )
            if hasattr(resultado, "codigo"):
                conflictos.append((root_id, resultado.codigo))
            else:
                aplicadas += 1
        if propuesta.avisar:
            tienda.notificar(root_id, propuesta.avisar)
        if propuesta.estado == "done":
            estado = "done"

    for intencion in tienda.intenciones_pendientes():
        if tienda.estado_de_control(intencion.root_id) != "active":
            continue
        target = intencion.assignment.get("target") or {}
        spec = SpecDeTarjeta(
            f"{intencion.root_id}/{intencion.unit_id}#{intencion.attempt}",
            str(intencion.assignment.get("instructionRef", {}).get("ref", "")),
            str(target.get("agentId") or target.get("adapterId") or "sin-perfil"),
        )
        tarjeta = kanban_port.asegurar_tarjeta(puerto, intencion.idempotency_key, spec)
        tienda.admitir_intento(
            intencion.root_id,
            intencion.unit_id,
            intencion.attempt,
            tarjeta.id,
        )

    return Informe(
        conciliadas,
        aplicadas,
        tuple(conflictos),
        len(puerto.creaciones) - creadas_antes,
        estado,
    )


def _hechos_vacios() -> dict:
    return {
        "principal": [],
        "asignaciones": [],
        "claims": [],
        "efectos": [],
    }


def _decision_id(vista, propuesta) -> str:
    canonico = [
        vista["meta"]["rootId"],
        vista["meta"]["plan"]["revision"],
        vista["meta"]["controlEpoch"],
        propuesta.decision,
    ]
    return "dec-" + digesto(canonico)[:24]


def _conciliar_uno(tienda, puerto, intento) -> bool:
    clave = kanban_port.clave_de_tarjeta(
        intento["rootId"], intento["unitId"], intento["attempt"]
    )
    tarjeta = puerto.leer_estado(clave)
    if tarjeta is None:
        return False
    ahora = tienda.reloj()
    root_id, unit_id, attempt = (
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
    )
    if tarjeta.estado == "corriendo":
        if intento["state"] == "admitted":
            tienda.transicionar_intento(
                root_id, unit_id, attempt, "running", tarjeta.id
            )
            return True
        return False
    if tarjeta.estado == "completada":
        if intento["state"] not in ("admitted", "running"):
            return False
        if intento["state"] == "admitted":
            tienda.transicionar_intento(
                root_id, unit_id, attempt, "running", tarjeta.id
            )
        tienda.transicionar_intento(root_id, unit_id, attempt, "reported", tarjeta.id)
        _reportar(tienda, intento, tarjeta, ahora)
        return True
    if tarjeta.estado == "bloqueada" and intento["state"] in ("admitted", "running"):
        tienda.registrar_incidencia(
            root_id,
            unit_id,
            attempt,
            f"tarjeta:{tarjeta.id}",
            {"kind": "recovery-transport-failure", "evidenceRef": tarjeta.id},
        )
        return True
    return False


def _reportar(tienda, intento, tarjeta, ahora) -> None:
    carga = tarjeta.resultado or {}
    contrato_resultado = carga.get("resultContract", "review.v1")
    documento = {
        "rootId": intento["rootId"],
        "unitId": intento["unitId"],
        "attempt": intento["attempt"],
        "resultContract": contrato_resultado,
        "resultDigest": carga.get("resultDigest", "sha256:" + "0" * 64),
        "reportedAt": ahora,
        "reportedBy": {"kind": "native", "taskId": tarjeta.id},
    }
    if contrato_resultado == "review.v1":
        veredicto = carga.get("verdict", "approved")
        if veredicto == "changes":
            documento["payload"] = {
                "verdict": "changes",
                "findingsRef": carga.get("findingsRef") or carga.get("evidenceRef"),
            }
        else:
            documento["payload"] = {
                "verdict": "approved",
                "evidenceRef": carga.get("evidenceRef"),
            }
    else:
        documento["payload"] = {"evidenceRef": carga.get("evidenceRef")}
    tienda.reportar_resultado(documento)
