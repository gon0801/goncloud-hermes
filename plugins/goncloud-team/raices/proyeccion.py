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
import re
from datetime import datetime, timedelta
from pathlib import Path

compartido = importlib.import_module("raices.compartido")
modulo_derivar = compartido.derivar
decidir = importlib.import_module("raices.decidir")

instante = decidir.instante

# F12: el rootId va crudo al nombre del archivo. Sólo esta forma y ni "." ni
# ".." cuentan como nombre de archivo de una proyección.
_NOMBRE_DE_ARCHIVO = re.compile(r"[A-Za-z0-9._-]{1,128}")


def _sirve_como_nombre(root_id) -> bool:
    return (
        isinstance(root_id, str)
        and root_id not in (".", "..")
        and _NOMBRE_DE_ARCHIVO.fullmatch(root_id) is not None
    )


def _atomico(ruta: Path, documento: dict) -> None:
    """Escritura atómica: temporal en el mismo directorio y os.replace. Si
    algo revienta a mitad, el archivo previo queda intacto y no sobrevive
    ningún temporal (F10)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
    try:
        temporal.write_text(
            json.dumps(documento, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8",
        )
        os.replace(temporal, ruta)
    except OSError:
        temporal.unlink(missing_ok=True)
        raise


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
    if not _sirve_como_nombre(root_id):
        return None, [f"raíz {root_id}: rootId no válido como nombre de archivo"]
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
    try:
        _atomico(Path(dir_destino) / "proyecciones" / f"{root_id}.json", documento)
    except OSError as fallo:
        return None, errores + [f"raíz {root_id}: escritura: {fallo}"]
    return documento, errores


# Los DOS recortes del mensaje de excepciones: hasta este número de
# excepciones detalladas (CodeRabbit r6d) y nada más que este largo total.
# El checkpoint del aviso cubre EXACTAMENTE las excepciones cuyo detalle
# completo quedó en el mensaje final, contando los dos recortes: lo que el
# dueño no vio no se marca como avisado.
DETALLE_DEL_MENSAJE = 8
TOPE_DE_TEXTO = 3500
_RESERVA_DEL_SUFIJO = len("… y 999 más") + 2


def _mensaje_excepciones(debidas: list) -> tuple:
    """El texto del aviso y los exceptionId cuyo detalle completo quedó en
    él. Un bloque (su línea en blanco, su título y cómo responderle) entra
    entero o no entra: una excepción con el texto cortado a mitad no cuenta
    como mostrada — el dueño no habría sabido responderle."""
    lineas = [f"⚠️ *Excepciones sin respuesta* ({len(debidas)})"]
    llevamos = len(lineas[0])
    mostradas: list = []
    for excepcion in debidas[:DETALLE_DEL_MENSAJE]:
        unidad = excepcion.get("unitId") or "(raíz)"
        desde = instante(excepcion["raisedAt"]).strftime("%H:%MZ")
        titulo = (
            f"*{excepcion['rootId']}/{unidad} · {excepcion['type']}"
            f" ({excepcion['exceptionId']}) · desde {desde}"
        )
        opciones = " | ".join(
            f"{opcion['optionId']}={opcion['text']}"
            for opcion in excepcion.get("options") or []
        )
        bloque = ["", titulo]
        if opciones:
            bloque.append(f"responder: {opciones}.")
        costo = sum(len(linea) + 1 for linea in bloque)
        if llevamos + costo + _RESERVA_DEL_SUFIJO > TOPE_DE_TEXTO:
            break
        lineas.extend(bloque)
        llevamos += costo
        mostradas.append(excepcion["exceptionId"])
    omitidas = len(debidas) - len(mostradas)
    if omitidas:
        lineas.append("")
        lineas.append(f"… y {omitidas} más")
    return "\n".join(lineas), mostradas


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
    if not isinstance(opciones, list):
        return "options no es una lista"
    for opcion in opciones or []:
        if not (
            isinstance(opcion, dict)
            and isinstance(opcion.get("optionId"), str)
            and isinstance(opcion.get("text"), str)
        ):
            return "una opción no trae optionId y text de texto"
    return None


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
        # C3: un options_json que no parsea se salta con su error, igual que
        # un raisedAt malformado: un aviso sin cómo responder no sale ni
        # recibe checkpoint.
        try:
            opciones = json.loads(f["options_json"])
        except ValueError:
            errores.append(f"excepción {f['exception_id']}: options no es JSON")
            continue
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
    texto, mostradas = _mensaje_excepciones(debidas)
    # C4: si el emisor lanza (no falla: LANZA), no sube: el aviso no se
    # confirmó y ningún checkpoint avanza; la pasada no pierde lo suyo.
    try:
        message_id = enviar(texto)
    except Exception as fallo:  # noqa: BLE001 - el emisor es frontera
        errores.append(f"el aviso no se confirmó: {type(fallo).__name__}")
        informe["errores"] = errores
        return informe
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id <= 0
    ):
        errores.append("el aviso no se confirmó")
        informe["errores"] = errores
        return informe
    # El checkpoint cubre SOLO las mostradas (CodeRabbit r6d): las que el
    # mensaje recortó — por cantidad o por largo — quedan sin noticed_at y
    # salen en la pasada siguiente. El dueño no pudo responder lo que no vio.
    raiz_de = {e["exceptionId"]: e["rootId"] for e in debidas}
    por_raiz: dict = {}
    for exception_id in mostradas:
        por_raiz.setdefault(raiz_de[exception_id], []).append(exception_id)
    # F6 (r5): el checkpoint de una raíz en conflicto NO descarta a las demás:
    # su aviso ya salió en el mismo mensaje. Se recorren todas; las que
    # fallan van a errores, `avisadas` lista sólo las excepciones de raíces
    # cuyo checkpoint avanzó (el resto se reavisa en la pasada siguiente) y
    # messageId se informa si al menos una avanzó.
    avisadas: list = []
    avanzaron = 0
    for root_id, ids in por_raiz.items():
        # C5: un notificar que LANZA (p. ej. SQLite locked) no corta el ciclo
        # de raíces: la suya queda con error y las demás siguen (F6).
        try:
            respuesta = tienda.notificar(root_id, ids)
        except Exception as fallo:  # noqa: BLE001 - frontera de la base
            errores.append(f"checkpoint {root_id}: {type(fallo).__name__}: {fallo}")
            continue
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


def _mensaje_capacidad_incierta(causa: str) -> str:
    lineas = [
        f"⚠️ *CAPACIDAD_INCIERTA · {causa}*",
        "",
        "La ruta de raíces está cerrada: el ticker no decide ni despacha.",
    ]
    return "\n".join(lineas)


def avisar_capacidad_incierta(ruta_estado, causa, ahora, enviar) -> dict:
    """El aviso de ruta cerrada (H4a): una vez por cambio de causa. El
    checkpoint es el estado en disco y solo avanza con un messageId entero
    positivo, así la vuelta siguiente reintenta hasta que el aviso salga.
    No crea directorios: en producción %HERMES_HOME%/raices ya existe, y un
    home sin raices/ no es asunto de esta pasada."""
    try:
        estado = json.loads(Path(ruta_estado).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        estado = {}
    if not isinstance(estado, dict):
        estado = {}
    if estado.get("causa") == causa:
        return {"avisada": False}
    mensaje_id = enviar(_mensaje_capacidad_incierta(causa))
    if (
        not isinstance(mensaje_id, int)
        or isinstance(mensaje_id, bool)
        or mensaje_id <= 0
    ):
        return {"avisada": False}
    avisada_en = ahora
    if isinstance(ahora, datetime):
        avisada_en = ahora.strftime("%Y-%m-%dT%H:%M:%SZ")
    documento = {
        "causa": causa,
        "avisadaEn": avisada_en,
        "messageId": mensaje_id,
    }
    try:
        ruta = Path(ruta_estado)
        temporal = ruta.with_name(f".{ruta.name}.{os.getpid()}.tmp")
        temporal.write_text(
            json.dumps(documento, ensure_ascii=False, sort_keys=True, indent=1),
            encoding="utf-8",
        )
        os.replace(temporal, ruta)
    except OSError:
        return {"avisada": False}
    return {"avisada": True, "messageId": mensaje_id}


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
