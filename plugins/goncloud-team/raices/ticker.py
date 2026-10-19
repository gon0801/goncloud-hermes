"""El ticker de las raíces: una vuelta = conciliar, decidir y despachar (SUA-07 H1).

Puede haber dos tickers a la vez: la exclusividad la dan las transacciones
(BEGIN IMMEDIATE en sistema-agentes.db y write_txn en el kanban), no la memoria.
Cada paso es idempotente por filas: una vuelta interrumpida en cualquier punto la
completa la siguiente (I04). El despacho pasa por la bandeja de salida: la intención
en sistema-agentes.db, buscar-o-crear la tarjeta en UN write_txn del kanban y, recién
entonces, marcar la intención cumplida (I03). Las raíces con control != active no
despachan: sus intenciones quedan cercadas. Con home y sin datos de permisos
de H, el despacho exige el paquete poteto del perfil (H4b2): sin él se levanta
NECESITA_DAVID con causa poteto:<perfil> y no se despacha.
"""

from __future__ import annotations

import importlib
from typing import NamedTuple

compartido = importlib.import_module("raices.compartido")
decidir = importlib.import_module("raices.decidir")
kanban_port = importlib.import_module("raices.kanban_port")
store_module = importlib.import_module("raices.store")
autoridad = importlib.import_module("raices.autoridad")
flota = importlib.import_module("raices.flota")
poteto = importlib.import_module("raices.poteto")

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


def vuelta(
    tienda, puerto, hechos_por_raiz=None, permisos_h=None, ruta_abierta=True, home=None
) -> Informe:
    """Una vuelta completa. Una CaidaInyectada del doble kanban se propaga: simula la
    muerte del proceso, y la vuelta siguiente recupera desde las filas.
    `permisos_h` es el dict de permisos de H (I07): None o ausentes niegan todo.
    `home` es el %HERMES_HOME% de los perfiles: con él el despacho congela el
    documento flota.v1 del intento que admite (I09).
    Con `ruta_abierta=False` (H4a) la puerta de capacidades está cerrada: bloquea
    y concilia igual (un intento abierto conserva su snapshot), pero no decide, no
    despacha ni levanta el gate de arranque."""
    hechos_por_raiz = hechos_por_raiz or {}
    permisos = autoridad.PermisosH(permisos_h)
    conciliadas = 0
    aplicadas = 0
    conflictos: list = []
    creadas_antes = len(puerto.creaciones)
    estado: str | None = None

    for bloqueo in tienda.bloqueos_pendientes():
        with puerto.write_txn() as escritura:
            escritura.bloquear(bloqueo["cardId"], bloqueo["motivo"])
        tienda.bloqueo_hecho(bloqueo["rootId"], bloqueo["cardId"])

    for intento in tienda.intentos_con_tarjeta():
        if _conciliar_uno(tienda, puerto, intento):
            conciliadas += 1

    if ruta_abierta:
        vistas: dict = {}
        for raiz in tienda.raices():
            root_id = raiz["rootId"]
            vista = tienda.leer_vista(
                root_id, hechos_por_raiz.get(root_id) or _hechos_vacios()
            )
            if vista is None:
                continue
            vistas[root_id] = vista
            propuesta = decidir.decidir(vista)
            tienda.asentar_necesidades(root_id, propuesta.necesidades)
            decision = propuesta.decision
            if any(len(decision[llave]) > 0 for llave in LLAVES_DE_DECISION):
                resultado = tienda.aplicar(
                    root_id, decision, _decision_id(vista, propuesta)
                )
                if hasattr(resultado, "codigo"):
                    conflictos.append((root_id, resultado.codigo))
                else:
                    aplicadas += 1
            # El aviso de las excepciones no pasa por aquí: el checkpoint
            # (noticed_at) solo avanza cuando el emisor de la pasada del cron
            # (proyeccion.avisar_excepciones) confirmó un messageId. Si la vuelta
            # marcara sin enviar, la pasada no encontraría nada debido y el aviso
            # nunca saldría (F9).
            if propuesta.estado == "done":
                estado = "done"

        for intencion in tienda.intenciones_pendientes():
            target = intencion.assignment.get("target") or {}
            specialist = str(
                target.get("agentId") or target.get("adapterId") or "sin-perfil"
            )
            vista_de = vistas.get(intencion.root_id)
            if vista_de is not None:
                razon = _negar_arranque(vista_de, permisos, intencion)
                if razon is not None:
                    _levantar_arranque(
                        tienda,
                        vista_de,
                        intencion,
                        autoridad.causa_de(razon),
                        conflictos,
                    )
                    continue
                if (
                    home is not None
                    and poteto.paquete_del_perfil(home, specialist) is None
                ):
                    # H4b2 (I08): sin el paquete poteto del perfil la ruta no
                    # se admite; la causa queda visible en read. El gate de
                    # I07 ya pasó: poteto no concede permisos, exigir el
                    # paquete no los cercena.
                    _levantar_arranque(
                        tienda, vista_de, intencion, f"poteto:{specialist}", conflictos
                    )
                    continue
            referencia = intencion.assignment.get("instructionRef") or {}
            ref = str(referencia.get("ref", ""))
            texto = tienda.texto_de_instruccion(ref, str(referencia.get("digest", "")))
            if _sin_instruccion_verificable(
                tienda, vista_de, intencion, ref, conflictos, texto
            ):
                continue
            reclamo = tienda.reclamar_intencion(
                intencion.root_id, intencion.unit_id, intencion.attempt
            )
            if hasattr(reclamo, "codigo"):
                continue
            spec = SpecDeTarjeta(
                f"{intencion.root_id}/{intencion.unit_id}#{intencion.attempt}",
                texto.valor,
                specialist,
            )
            tarjeta = kanban_port.asegurar_tarjeta(
                puerto, intencion.idempotency_key, spec
            )
            documento = flota.construir(
                intencion.root_id,
                intencion.unit_id,
                intencion.attempt,
                specialist,
                home,
                permisos_h,
            )
            tienda.admitir_intento(
                intencion.root_id,
                intencion.unit_id,
                intencion.attempt,
                tarjeta.id,
                flota=documento,
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


def _levantar_arranque(tienda, vista, intencion, causa, conflictos) -> None:
    """La pregunta por un arranque negado. Un sujeto de INTENTO no ofrece
    `autorizar`: la concesión del dueño solo existe para efectos (F8/F15),
    igual que en R, donde permission-required sale con
    ["listo","omitir","cancelar"] (managed-root.decide.ts:550). Si la
    decisión choca, el Conflicto entra al informe (F17). La decisión NO se
    reaplica (C2): con la id fija `dec-arranque:<exceptionId>` y una época
    que cambió sería un operation-conflict en cada vuelta, y con la raíz en
    pausa un root-not-active; si la pregunta ya está en la base, no hace
    falta, y si la raíz no está active la pregunta sale al reanudar."""
    borrador = decidir.borrador_excepcion(
        intencion.root_id,
        "NECESITA_DAVID",
        {
            "kind": "attempt",
            "unitId": intencion.unit_id,
            "attempt": intencion.attempt,
            "cause": causa,
        },
        ["listo", "omitir", "cancelar"],
    )
    conocidas = {e["exceptionId"] for e in vista.get("excepciones") or ()}
    if (
        borrador["exceptionId"] in conocidas
        or vista["meta"]["control"]["state"] != "active"
    ):
        return
    decision = decidir._decision_vacia(
        vista["meta"]["plan"]["revision"], vista["meta"]["controlEpoch"]
    )
    decision["excepciones"] = [borrador]
    resultado = tienda.aplicar(
        intencion.root_id,
        decision,
        f"dec-arranque:{borrador['exceptionId']}",
    )
    if hasattr(resultado, "codigo"):
        conflictos.append((intencion.root_id, resultado.codigo))


def _sin_instruccion_verificable(
    tienda, vista_de, intencion, ref, conflictos, texto
) -> bool:
    if not hasattr(texto, "codigo"):
        return False
    if vista_de is not None:
        _levantar_arranque(
            tienda, vista_de, intencion, f"instruccion:{ref}", conflictos
        )
    return True


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


_jev_ticker_resuelto = None

_JEV_PREGUNTA_TRANSPORTE = (
    "Is this evidence a transport failure — the worker died, was cut off or "
    "never really answered, so retrying the attempt can work — rather than "
    "an invalid or missing result produced by a worker that did answer?"
)


def _jev_ticker():
    """(módulo, cliente, config) de JEV, o None si no se pudo cargar."""
    global _jev_ticker_resuelto
    if _jev_ticker_resuelto is None:
        try:
            import sys
            from pathlib import Path

            plugin_dir = Path(__file__).resolve().parents[1]
            if str(plugin_dir) not in sys.path:
                sys.path.insert(0, str(plugin_dir))
            modulo = importlib.import_module("jev_cliente")
            config = modulo.config_jev()
            _jev_ticker_resuelto = (modulo, modulo.ClienteJev(config=config), config)
        except Exception:
            _jev_ticker_resuelto = False
    return _jev_ticker_resuelto or None


def _jev_es_transporte(intento, tarjeta, regla: str) -> bool:
    """JEV decide transporte vs inválido con evidencia ambigua; si no manda, la regla.

    `regla` es la lectura de hoy del llamador ("transporte" desde una tarjeta
    bloqueada/archivada, "invalid_result" desde un resultado ilegible).
    """
    resuelto = _jev_ticker()
    if resuelto is None:
        return regla == "transporte"
    modulo, cliente, config = resuelto
    carga = tarjeta.resultado if isinstance(tarjeta.resultado, dict) else {}
    claves = sorted(str(clave) for clave in carga)[:20]
    estado = "\n".join(
        (
            f"card_state: {tarjeta.estado}",
            f"attempt_state: {intento.get('state')}",
            f"rule_reading: {regla}",
            f"result_keys: {', '.join(claves) or '(none)'}",
            f"result_contract: {carga.get('resultContract') if isinstance(carga.get('resultContract'), str) else ''}",
            f"verdict: {carga.get('verdict') if isinstance(carga.get('verdict'), str) else ''}",
            f"has_evidence_ref: {bool(carga.get('evidenceRef') or carga.get('findingsRef'))}",
            f"has_result_digest: {bool(carga.get('resultDigest'))}",
        )
    )
    decision = modulo.confirmar(
        cliente,
        config,
        punto=modulo.PUNTO_TICKER_INCIDENCIA,
        estado=estado,
        pregunta=_JEV_PREGUNTA_TRANSPORTE,
        regla=regla,
        contexto={
            "root": intento["rootId"],
            "unit": intento["unitId"],
            "attempt": intento["attempt"],
            "tarjeta": tarjeta.id,
        },
    )
    if decision is None:
        return regla == "transporte"
    return decision


def _transporte_caido(tienda, intento, tarjeta, *, _consultar_jev=True) -> None:
    if _consultar_jev and not _jev_es_transporte(intento, tarjeta, "transporte"):
        _incidencia_invalida(
            tienda,
            intento,
            tarjeta,
            tarjeta.resultado if isinstance(tarjeta.resultado, dict) else None,
            _consultar_jev=False,
        )
        return
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
        _incidencia_invalida(tienda, intento, tarjeta, carga)
        return
    respuesta = tienda.reportar_resultado(documento)
    if hasattr(respuesta, "codigo"):
        _incidencia_invalida(tienda, intento, tarjeta, carga)


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


def _incidencia_invalida(
    tienda, intento, tarjeta, carga=None, *, _consultar_jev=True
) -> None:
    """El episodeId es el canónico de R (managed-task.worker.ts): el digest del
    resultado si lo trajo, o el de schema-invalid sobre lo que llegó. Una
    tarjeta completada sin resultado alguno es un hijo que murió sin decir
    nada: reintenta con el mismo tope que invalid-result y el kind distingue
    la muerte en la evidencia (I18 sigue: sin resultado válido no hay
    veredicto, aceptación ni despacho de dependientes)."""
    if _consultar_jev and _jev_es_transporte(intento, tarjeta, "invalid_result"):
        _transporte_caido(tienda, intento, tarjeta, _consultar_jev=False)
        return
    if not carga:
        kind = "result-missing"
        identidad = digesto(["schema-invalid", "sin-resultado"])
    else:
        kind = "invalid-result"
        identidad = carga.get("resultDigest") if isinstance(carga, dict) else None
        if not isinstance(identidad, str) or not identidad:
            identidad = digesto(
                [
                    "schema-invalid",
                    carga if isinstance(carga, str) else "structured-result",
                ]
            )
    tienda.registrar_incidencia(
        intento["rootId"],
        intento["unitId"],
        intento["attempt"],
        f"{kind}:{tarjeta.id}",
        {
            "kind": kind,
            "episodeId": f"{kind}:{digesto([tarjeta.id, identidad])}",
            "evidenceRef": tarjeta.id,
        },
    )
