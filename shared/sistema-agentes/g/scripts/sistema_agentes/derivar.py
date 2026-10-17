#!/usr/bin/env python3
"""El derivador de proyecciones (SUA-06, 4.1): snapshot de una raíz -> proyeccion.v1.

`derivar` es una función pura: ni el reloj ni el mapa de notados salen del snapshot, los
pasa quien llama. Solo biblioteca estándar, Python 3.9, sin fcntl, subprocess ni socket:
Hermes (Windows) copia este módulo tal cual (SUA-07, H0a). El CLI y la pasada del emisor
viven en scripts/agent-work/proyeccion.py, que lo reexporta.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

SCHEMA = "sistema-agentes.proyeccion.v1"
ID_RE = re.compile(r"[A-Za-z0-9._-]{1,80}")
SECRET_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]|github_pat_[A-Za-z0-9_]|sk-[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16}"
)
FECHA_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?Z")
REPORTADOS = frozenset({"reported", "accepted"})


def _fecha(momento: datetime) -> str:
    return momento.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _instante(texto: object, nombre: str) -> datetime:
    partes = FECHA_RE.fullmatch(texto) if isinstance(texto, str) else None
    if partes is None:
        raise ValueError(f"{nombre} no es una fecha ISO 8601 en UTC con Z: {texto!r}")
    *enteros, fraccion = partes.groups()
    micros = int((fraccion or "")[:6].ljust(6, "0"))
    return datetime(*(int(parte) for parte in enteros), micros, tzinfo=timezone.utc)


def _con_zona(momento: object, nombre: str) -> datetime:
    if not isinstance(momento, datetime) or momento.tzinfo is None:
        raise ValueError(f"{nombre} debe ser un datetime con zona horaria")
    return momento


def _entero_positivo(valor: object, nombre: str) -> int:
    if not isinstance(valor, int) or isinstance(valor, bool) or valor <= 0:
        raise ValueError(f"{nombre} debe ser un entero positivo")
    return valor


def _como(condicion: bool, detalle: str) -> None:
    if not condicion:
        raise ValueError(f"snapshot malformado: {detalle}")


def _identificador(valor: object, nombre: str) -> str:
    if not isinstance(valor, str) or ID_RE.fullmatch(valor) is None:
        raise ValueError(f"{nombre} fuera de la allowlist [A-Za-z0-9._-]{{1,80}}: {valor!r}")
    if SECRET_RE.search(valor):
        raise ValueError(f"{nombre} parece un secreto y no puede entrar a la proyección")
    return valor


def _lista(snapshot: dict, clave: str) -> list:
    valor = snapshot.get(clave, [])
    _como(isinstance(valor, list), f"{clave} no es una lista")
    return valor


def derivar(
    snapshot: object,
    ahora: datetime,
    stale_after_ms: int,
    intervalo_ms: int,
    notados: Mapping[str, datetime],
    host_gateway: str | None = None,
) -> dict:
    """La proyección de una raíz (sistema-agentes.proyeccion.v1). Nunca ordena trabajo."""
    _con_zona(ahora, "ahora")
    _entero_positivo(stale_after_ms, "stale_after_ms")
    _entero_positivo(intervalo_ms, "intervalo_ms")
    for exception_id, momento in notados.items():
        _con_zona(momento, f"notados[{exception_id!r}]")

    def gateway() -> str:
        if host_gateway is None:
            raise ValueError("host_gateway es requerido: la fila necesita el host del gateway")
        return _identificador(host_gateway, "host_gateway")

    _como(isinstance(snapshot, dict), "la raíz no es un objeto")
    meta = snapshot.get("meta")
    _como(isinstance(meta, dict), "meta no es un objeto")
    plan = meta.get("plan")
    _como(isinstance(plan, dict), "meta.plan no es un objeto")
    revision = plan.get("revision")
    _como(
        isinstance(revision, int) and not isinstance(revision, bool) and revision >= 1,
        "meta.plan.revision no es un entero >= 1",
    )
    unidades = plan.get("units")
    _como(isinstance(unidades, list), "meta.plan.units no es una lista")
    epoch = meta.get("controlEpoch")
    _como(
        isinstance(epoch, int) and not isinstance(epoch, bool) and epoch >= 1,
        "meta.controlEpoch no es un entero >= 1",
    )
    root_id = _identificador(meta.get("rootId"), "meta.rootId")

    deadline = None
    autoridad = meta.get("authority")
    if autoridad is not None:
        _como(isinstance(autoridad, dict), "meta.authority no es un objeto")
        if autoridad.get("deadline") is not None:
            deadline = _instante(autoridad["deadline"], "meta.authority.deadline")

    por_unidad: dict[str, list[dict]] = {}
    for doc in _lista(snapshot, "units"):
        _como(isinstance(doc, dict), "units[] no es un objeto")
        if doc.get("planRevision") != revision:
            continue
        _como(
            isinstance(doc.get("attempt"), int) and not isinstance(doc["attempt"], bool),
            f"units[] (unitId {doc.get('unitId')!r}) attempt no es un entero",
        )
        _como(
            isinstance(doc.get("state"), str),
            f"units[] (unitId {doc.get('unitId')!r}) state no es un texto",
        )
        unit_id = doc.get("unitId")
        _como(isinstance(unit_id, str), "units[].unitId no es un texto")
        por_unidad.setdefault(unit_id, []).append(doc)

    pasados: dict[str, set[str]] = {}
    for evidencia in _lista(snapshot, "evidence"):
        _como(isinstance(evidencia, dict), "evidence[] no es un objeto")
        if evidencia.get("planRevision") != revision or evidencia.get("outcome") != "passed":
            continue
        unit_id, criterio = evidencia.get("unitId"), evidencia.get("criterionId")
        if isinstance(unit_id, str) and isinstance(criterio, str):
            pasados.setdefault(unit_id, set()).add(criterio)

    ultimo_incidente: dict[str, object] = {}
    for incidente in _lista(snapshot, "incidents"):
        _como(isinstance(incidente, dict), "incidents[] no es un objeto")
        registro = incidente.get("incident")
        _como(isinstance(registro, dict), "incidents[].incident no es un objeto")
        unit_id = incidente.get("unitId")
        if isinstance(unit_id, str):
            ultimo_incidente[unit_id] = registro.get("kind")

    abiertas: dict[str, list[tuple[str, int]]] = {}
    for excepcion in _lista(snapshot, "exceptions"):
        _como(isinstance(excepcion, dict), "exceptions[] no es un objeto")
        if excepcion.get("response") is not None:
            continue
        unit_id = excepcion.get("unitId")
        if not isinstance(unit_id, str):
            continue
        exception_id = excepcion.get("exceptionId")
        _como(isinstance(exception_id, str), "exceptions[].exceptionId no es un texto")
        renotice = excepcion.get("renoticeAfterMs")
        _como(
            isinstance(renotice, int) and not isinstance(renotice, bool),
            f"exceptions[] (exceptionId {exception_id!r}) renoticeAfterMs no es un entero",
        )
        abiertas.setdefault(unit_id, []).append((exception_id, renotice))

    sin_bound: dict[str, object] = {}
    for suelta in _lista(snapshot, "unbound"):
        _como(isinstance(suelta, dict), "unbound[] no es un objeto")
        unit_id = suelta.get("unitId")
        if isinstance(unit_id, str):
            sin_bound[unit_id] = suelta.get("state")

    filas = []
    validas = declarados = observados = aceptados = 0
    for indice, unidad in enumerate(unidades):
        _como(isinstance(unidad, dict), f"meta.plan.units[{indice}] no es un objeto")
        unit_id = _identificador(unidad.get("unitId"), f"meta.plan.units[{indice}].unitId")
        criterios = unidad.get("criteria")
        _como(isinstance(criterios, list), f"meta.plan.units[{indice}].criteria no es una lista")
        ids_de_criterio = set()
        for criterio in criterios:
            _como(
                isinstance(criterio, dict),
                f"meta.plan.units[{indice}].criteria[] no es un objeto",
            )
            identificador = criterio.get("criterionId")
            _como(
                isinstance(identificador, str),
                f"meta.plan.units[{indice}].criteria[].criterionId no es un texto",
            )
            ids_de_criterio.add(identificador)

        docs_de = por_unidad.get(unit_id, [])
        ultimo = None
        for doc in docs_de:
            if ultimo is None or doc["attempt"] >= ultimo["attempt"]:
                ultimo = doc

        if ultimo is not None:
            estado = ultimo["state"]
            asignacion = ultimo.get("assignment")
            _como(
                isinstance(asignacion, dict),
                f"units[] (unitId {unit_id}) assignment no es un objeto",
            )
            destino = asignacion.get("target")
            _como(
                isinstance(destino, dict),
                f"units[] (unitId {unit_id}) assignment.target no es un objeto",
            )
            prefijo = f"units[] (unitId {unit_id}) assignment.target"
            clase = destino.get("kind")
            if clase == "cli":
                executor = "cli:" + _identificador(destino.get("adapterId"), f"{prefijo}.adapterId")
                host = _identificador(destino.get("hostId"), f"{prefijo}.hostId")
            elif clase == "agent":
                executor = "agent:" + _identificador(destino.get("agentId"), f"{prefijo}.agentId")
                host = gateway()
            else:
                raise ValueError(f"snapshot malformado: {prefijo}.kind desconocido: {clase!r}")
        else:
            estado = sin_bound.get(unit_id, "pending")
            _como(isinstance(estado, str), f"unbound[] (unitId {unit_id}) state no es un texto")
            executor, host = "sin-asignar", gateway()

        proximo = None
        for exception_id, renotice in abiertas.get(unit_id, []):
            notado = notados.get(exception_id)
            candidato = notado + timedelta(milliseconds=renotice) if notado is not None else ahora
            if proximo is None or candidato < proximo:
                proximo = candidato
        if proximo is None:
            proximo = ahora + timedelta(milliseconds=intervalo_ms)
        if deadline is not None and deadline < proximo:
            proximo = deadline

        error = ultimo_incidente.get(unit_id)
        if error is not None:
            error = _identificador(error, f"incidents[] (unitId {unit_id}) incident.kind")

        filas.append(
            {
                "unitId": unit_id,
                "state": estado,
                "host": host,
                "executor": executor,
                "modelId": None,
                "nextCheckAt": _fecha(proximo),
                "error": error,
            }
        )

        if ids_de_criterio:
            validas += 1
            if any(doc["state"] in REPORTADOS for doc in docs_de):
                declarados += 1
            if ids_de_criterio <= pasados.get(unit_id, set()):
                observados += 1
            if ultimo is not None and ultimo["state"] == "accepted":
                aceptados += 1

    def porcentaje(numero: int) -> int | None:
        return None if validas == 0 else (100 * numero) // validas

    return {
        "schema": SCHEMA,
        "rootId": root_id,
        "planRevision": revision,
        "controlEpoch": epoch,
        "generatedAt": _fecha(ahora),
        "progress": {
            "declared": porcentaje(declarados),
            "observed": porcentaje(observados),
            "accepted": porcentaje(aceptados),
        },
        "units": filas,
        "freshness": {"staleAfterMs": stale_after_ms},
    }
