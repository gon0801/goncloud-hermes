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
