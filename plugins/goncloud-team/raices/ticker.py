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
autoridad = importlib.import_module("raices.autoridad")

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
    "control",
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


def vuelta(tienda, puerto, hechos_por_raiz=None, permisos_h=None) -> Informe:
    """Una vuelta completa. Una CaidaInyectada del doble kanban se propaga: simula la
    muerte del proceso, y la vuelta siguiente recupera desde las filas.
    `permisos_h` es el dict de permisos de H (I07): None deja solo el contrato."""
    hechos_por_raiz = hechos_por_raiz or {}
    permisos = autoridad.PermisosH(permisos_h) if permisos_h is not None else None
    conciliadas = 0
    aplicadas = 0
    conflictos: list = []
    creadas_antes = len(puerto.creaciones)
    estado: str | None = None
    vistas: dict = {}

    for bloqueo in tienda.bloqueos_pendientes():
        with puerto.write_txn() as escritura:
            escritura.bloquear(bloqueo["cardId"], bloqueo["motivo"])
        tienda.bloqueo_hecho(bloqueo["rootId"], bloqueo["cardId"])

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
        vistas[root_id] = vista
        propuesta = decidir.decidir(vista)
        decision = propuesta.decision
        resultado = None
        if any(len(decision[llave]) > 0 for llave in LLAVES_DE_DECISION):
            resultado = tienda.aplicar(
                root_id, decision, _decision_id(vista, propuesta)
            )
            if hasattr(resultado, "codigo"):
                conflictos.append((root_id, resultado.codigo))
            else:
                aplicadas += 1
        # Con aplicar en conflicto, las excepciones nuevas no están en la base (F13).
        avisar = propuesta.avisar
        if hasattr(resultado, "codigo"):
            nuevas = {e["exceptionId"] for e in decision["excepciones"]}
            avisar = [x for x in avisar if x not in nuevas]
        if avisar:
            aviso = tienda.notificar(root_id, avisar)
            if hasattr(aviso, "codigo"):
                conflictos.append((root_id, aviso.codigo))
        if propuesta.estado == "done":
            estado = "done"

    for intencion in tienda.intenciones_pendientes():
        vista_de = vistas.get(intencion.root_id)
        if vista_de is not None:
            razon = _negar_arranque(vista_de, permisos, intencion)
            if razon is not None:
                _levantar_arranque(tienda, vista_de, intencion, razon)
                continue
        reclamo = tienda.reclamar_intencion(
            intencion.root_id, intencion.unit_id, intencion.attempt
        )
        if hasattr(reclamo, "codigo"):
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


def _negar_arranque(vista, permisos, intencion) -> str | None:
    """Punto 1 de la intersección (I07): la unidad no arranca si el contrato o
    H lo niegan."""
    unidad_plan = next(
        (u for u in vista["meta"]["plan"]["units"] if u["unitId"] == intencion.unit_id),
        None,
    )
    return autoridad.negar_arranque(
        vista["meta"]["authority"],
        permisos,
        vista["ahora"],
        intencion.assignment,
        unidad_plan,
    )


def _levantar_arranque(tienda, vista, intencion, razon) -> None:
    borrador = decidir.borrador_excepcion(
        intencion.root_id,
        "NECESITA_DAVID",
        {
            "kind": "attempt",
            "unitId": intencion.unit_id,
            "attempt": intencion.attempt,
            "cause": autoridad.causa_de(razon),
        },
        ["autorizar", "listo", "omitir", "cancelar"],
    )
    decision = decidir._decision_vacia(
        vista["meta"]["plan"]["revision"], vista["meta"]["controlEpoch"]
    )
    decision["excepciones"] = [borrador]
    tienda.aplicar(
        intencion.root_id,
        decision,
        f"dec-arranque:{borrador['exceptionId']}",
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
        _reportar(tienda, intento, tarjeta, ahora)
        return True
    if tarjeta.estado == "bloqueada" and intento["state"] in ("admitted", "running"):
        _transporte_caido(tienda, intento, tarjeta)
        return True
    if tarjeta.estado == "archivada" and intento["state"] in ("admitted", "running"):
        _transporte_caido(tienda, intento, tarjeta)
        return True
    return False


def _transporte_caido(tienda, intento, tarjeta) -> None:
    tienda.registrar_incidencia(
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
        f"tarjeta:{tarjeta.id}",
        {"kind": "recovery-transport-failure", "evidenceRef": tarjeta.id},
    )


def _reportar(tienda, intento, tarjeta, ahora) -> None:
    carga = tarjeta.resultado or {}
    documento = _documento_de_resultado(carga, intento, tarjeta, ahora)
    if documento is None:
        _incidencia_invalida(tienda, intento, tarjeta)
        return
    respuesta = tienda.reportar_resultado(documento)
    if hasattr(respuesta, "codigo"):
        _incidencia_invalida(tienda, intento, tarjeta)


def _documento_de_resultado(carga, intento, tarjeta, ahora):
    contrato_resultado = carga.get("resultContract")
    digest = carga.get("resultDigest")
    if not isinstance(contrato_resultado, str) or not contrato_resultado:
        return None
    if not isinstance(digest, str) or not digest:
        return None
    documento = {
        "rootId": intento["rootId"],
        "unitId": intento["unitId"],
        "attempt": intento["attempt"],
        "resultContract": contrato_resultado,
        "resultDigest": digest,
        "reportedAt": ahora,
        "reportedBy": {"kind": "native", "taskId": tarjeta.id},
    }
    if contrato_resultado == "review.v1":
        veredicto = carga.get("verdict")
        if veredicto == "changes":
            hallazgo = carga.get("findingsRef") or carga.get("evidenceRef")
            if not hallazgo:
                return None
            documento["payload"] = {"verdict": "changes", "findingsRef": hallazgo}
        elif veredicto == "approved":
            if not carga.get("evidenceRef"):
                return None
            documento["payload"] = {
                "verdict": "approved",
                "evidenceRef": carga["evidenceRef"],
            }
        else:
            return None
    else:
        if not carga.get("evidenceRef"):
            return None
        documento["payload"] = {"evidenceRef": carga["evidenceRef"]}
    return documento


def _incidencia_invalida(tienda, intento, tarjeta) -> None:
    tienda.registrar_incidencia(
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
        f"invalid-result:{tarjeta.id}",
        {"kind": "invalid-result", "evidenceRef": tarjeta.id},
    )
