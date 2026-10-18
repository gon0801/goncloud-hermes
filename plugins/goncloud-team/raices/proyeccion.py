"""La proyección de una raíz de H (I14): el snapshot del store pasa por
`derivar` de la copia fijada y se escribe atómica en
<dir>/proyecciones/<rootId>.json. Nunca ordena trabajo.

Los hechos de otro intento no atribuyen (I16): antes de derivar, los
incidentes se recortan a los del último intento de cada unidad, que es el que
la fila de la proyección representa. El reaviso (I14) usa el mismo estado: los
`notados` salen del store, no de un checkpoint aparte.
"""

from __future__ import annotations

import importlib
import json
import os
from datetime import timedelta
from pathlib import Path

compartido = importlib.import_module("raices.compartido")
modulo_derivar = compartido.derivar
decidir = importlib.import_module("raices.decidir")

instante = decidir.instante


def _atomico(ruta: Path, documento: dict) -> None:
    """Escritura atómica: temporal en el mismo directorio y os.replace."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
    temporal.write_text(
        json.dumps(documento, ensure_ascii=False, sort_keys=True, indent=1),
        encoding="utf-8",
    )
    os.replace(temporal, ruta)


def notados_de(tienda, root_id: str) -> dict:
    """Cuándo se avisó por última vez cada excepción abierta de la raíz."""
    filas = tienda._conexion.execute(
        "SELECT exception_id, noticed_at FROM excepciones"
        " WHERE root_id = ? AND response_json IS NULL AND noticed_at IS NOT NULL",
        (root_id,),
    ).fetchall()
    return {f["exception_id"]: instante(f["noticed_at"]) for f in filas}


def _incidentes_del_ultimo_intento(snapshot: dict) -> list:
    """El intento que la fila representa es el último de cada unidad: sus
    incidentes atribuyen; los de intentos anteriores, no (I16)."""
    ultimo: dict = {}
    for unidad in snapshot.get("units") or []:
        unit_id = unidad.get("unitId")
        if not isinstance(unit_id, str):
            continue
        actual = ultimo.get(unit_id)
        if actual is None or unidad["attempt"] >= actual:
            ultimo[unit_id] = unidad["attempt"]
    return [
        incidente
        for incidente in snapshot.get("incidents") or []
        if incidente["attempt"] == ultimo.get(incidente.get("unitId"))
    ]


def proyectar(
    tienda,
    root_id: str,
    *,
    ahora,
    dir_destino,
    stale_after_ms: int,
    intervalo_ms: int,
    host_gateway,
    notados: dict | None = None,
) -> tuple[dict, list]:
    """Deriva y escribe la proyección de una raíz. Devuelve (proyección, errores);
    los errores son de snapshot roto y no tumban a las demás raíces."""
    errores: list = []
    try:
        snapshot = tienda.snapshot_raiz(root_id)
        if snapshot is None:
            return None, [f"raíz desconocida: {root_id}"]
        vista = {
            **snapshot,
            "incidents": _incidentes_del_ultimo_intento(snapshot),
        }
        if notados is None:
            notados = notados_de(tienda, root_id)
        documento = modulo_derivar.derivar(
            vista,
            ahora,
            stale_after_ms,
            intervalo_ms,
            notados,
            host_gateway=host_gateway,
        )
    except (ValueError, KeyError) as fallo:
        return None, errores + [f"raíz {root_id}: {fallo}"]
    _atomico(Path(dir_destino) / "proyecciones" / f"{root_id}.json", documento)
    return documento, errores


def _recortar(lineas: list, tope: int = 3500) -> str:
    texto = "\n".join(lineas)
    if len(texto) <= tope:
        return texto
    resultado = [lineas[0]]
    largo = len(lineas[0])
    colocadas = 0
    for linea in lineas[1:]:
        if largo + len(linea) + len("… y 999 más") + 2 > tope:
            break
        resultado.append(linea)
        largo += len(linea) + 1
        colocadas += 1
    omitidas = len(lineas) - 1 - colocadas
    if omitidas:
        resultado.append(f"… y {omitidas} más")
    return "\n".join(resultado)


def _excepcion_avisable(excepcion: dict) -> str | None:
    """Por qué el mensaje no puede formatearla, o None si puede. Una excepción
    malformada se salta sola: si llegara al mensaje, lanzaría y callaría los
    avisos de todas las raíces en cada pasada."""
    unidad = excepcion.get("unitId")
    if unidad is not None and not isinstance(unidad, str):
        return "unitId no es un texto"
    if not isinstance(excepcion.get("type"), str):
        return "type no es un texto"
    try:
        instante(excepcion.get("raisedAt"))
    except (ValueError, TypeError) as fallo:
        return str(fallo)
    opciones = excepcion.get("options")
    if opciones is not None and not isinstance(opciones, list):
        return "options no es una lista"
    for opcion in opciones or []:
        if not (
            isinstance(opcion, dict)
            and isinstance(opcion.get("optionId"), str)
            and isinstance(opcion.get("text"), str)
        ):
            return "una opción no trae optionId y text de texto"
    return None


def _mensaje_excepciones(debidas: list) -> str:
    lineas = [f"⚠️ *Excepciones sin respuesta* ({len(debidas)})"]
    for excepcion in debidas[:8]:
        unidad = excepcion.get("unitId") or "(raíz)"
        desde = instante(excepcion["raisedAt"]).strftime("%H:%MZ")
        lineas.append("")
        lineas.append(
            f"*{excepcion['rootId']}/{unidad} · {excepcion['type']}"
            f" ({excepcion['exceptionId']}) · desde {desde}"
        )
        opciones = " | ".join(
            f"{opcion['optionId']}={opcion['text']}"
            for opcion in excepcion.get("options") or []
        )
        if opciones:
            lineas.append(f"responder: {opciones}.")
    if len(debidas) > 8:
        lineas.append("")
        lineas.append(f"… y {len(debidas) - 8} más")
    return _recortar(lineas)


def avisar_excepciones(tienda, *, ahora, enviar) -> dict:
    """Una pasada del aviso de excepciones (§3.5): las abiertas que nunca se
    avisaron o cuyo renoticeAfterMs venció salen por `enviar(texto) ->
    messageId|None`; el checkpoint (noticed_at) solo avanza con un messageId
    entero positivo. Una malformada se salta con su error y no calla las demás."""
    filas = tienda._conexion.execute(
        "SELECT exception_id, root_id, unit_id, type, options_json, raised_at,"
        " renotice_after_ms, noticed_at FROM excepciones"
        " WHERE response_json IS NULL ORDER BY root_id, raised_at, exception_id"
    ).fetchall()
    errores: list = []
    debidas: list = []
    for f in filas:
        try:
            opciones = json.loads(f["options_json"])
        except ValueError:
            opciones = None
        fila = {
            "exceptionId": f["exception_id"],
            "rootId": f["root_id"],
            "unitId": f["unit_id"],
            "type": f["type"],
            "raisedAt": f["raised_at"],
            "options": opciones,
        }
        motivo = _excepcion_avisable(fila)
        if motivo is not None:
            errores.append(f"excepción {f['exception_id']}: {motivo}")
            continue
        if f["noticed_at"] is not None:
            vence = instante(f["noticed_at"]) + timedelta(
                milliseconds=f["renotice_after_ms"]
            )
            if ahora < vence:
                continue
        debidas.append(fila)
    debidas.sort(
        key=lambda e: (
            e["rootId"],
            e["unitId"] is None,
            e["unitId"] or "",
            e["exceptionId"],
        )
    )
    informe: dict = {"avisadas": [], "messageId": None, "errores": errores}
    if not debidas:
        return informe
    message_id = enviar(_mensaje_excepciones(debidas))
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        errores.append("el aviso no se confirmó")
        informe["errores"] = errores
        return informe
    por_raiz: dict = {}
    for excepcion in debidas:
        por_raiz.setdefault(excepcion["rootId"], []).append(excepcion["exceptionId"])
    # F6 (r5): el checkpoint de una raíz en conflicto NO descarta a las demás:
    # su aviso ya salió en el mismo mensaje. Se recorren todas; las que
    # fallan van a errores, `avisadas` lista sólo las excepciones de raíces
    # cuyo checkpoint avanzó (el resto se reavisa en la pasada siguiente) y
    # messageId se informa si al menos una avanzó.
    avisadas: list = []
    avanzaron = 0
    for root_id, ids in por_raiz.items():
        respuesta = tienda.notificar(root_id, ids)
        if hasattr(respuesta, "codigo"):
            errores.append(
                f"checkpoint {root_id}: {respuesta.codigo}"
                f" ({respuesta.detalle.get('exceptionId')})"
            )
            continue
        avanzaron += 1
        avisadas.extend(ids)
    informe["errores"] = errores
    if avanzaron:
        informe["avisadas"] = avisadas
        informe["messageId"] = message_id
    return informe


def pasada(
    tienda,
    *,
    ahora,
    dir_destino,
    enviar,
    stale_after_ms: int,
    intervalo_ms: int,
    host_gateway,
) -> dict:
    """Una vuelta del emisor (I14): proyecta cada raíz y avisa las excepciones
    desde el MISMO estado; el checkpoint del aviso alimenta el nextCheckAt de la
    pasada siguiente."""
    proyecciones: dict = {}
    errores: list = []
    for raiz in tienda.raices():
        root_id = raiz["rootId"]
        try:
            documento, fallos = proyectar(
                tienda,
                root_id,
                ahora=ahora,
                dir_destino=dir_destino,
                stale_after_ms=stale_after_ms,
                intervalo_ms=intervalo_ms,
                host_gateway=host_gateway,
            )
        except (ValueError, KeyError) as fallo:
            errores.append(f"raíz {root_id}: {fallo}")
            continue
        errores.extend(fallos)
        if documento is not None:
            proyecciones[root_id] = documento
    aviso = avisar_excepciones(tienda, ahora=ahora, enviar=enviar)
    errores.extend(aviso["errores"])
    return {
        "raices": len(proyecciones),
        "proyecciones": proyecciones,
        "aviso": aviso,
        "errores": errores,
    }
