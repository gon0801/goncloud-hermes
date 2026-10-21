"""Lo que JEV necesita saber del trabajo en curso, sin gastar un token.

Fuente de solo lectura, kanban.db (esquema real de hermes-agent): tarjetas de
especialista y etapas de cadenas Goncloud que terminaron (completed/blocked,
con su ultimo reporte), para que el cierre (jev_auto) las traiga al turno
siguiente. Las consultas siguen el esquema REAL: el payload del evento vive
en task_events.payload, el resultado en tasks.result y el resumen de la
corrida en task_runs.metadata. Nada abre el kanban para escribir.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

try:
    from .jev_decisiones import ESPECIALISTAS
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from jev_decisiones import ESPECIALISTAS

PREFIJO_CADENA = "goncloud-workflow:"


def _acotar(texto, tope: int = 160) -> str:
    return " ".join(str(texto or "").split())[:tope]


# --- kanban.db (solo lectura) ------------------------------------------------


def ruta_del_kanban(raiz) -> Path:
    """HERMES_KANBAN_DB o <raiz>/kanban.db, como el resto del plugin."""
    elegida = os.environ.get("HERMES_KANBAN_DB")
    return Path(elegida) if elegida else Path(raiz) / "kanban.db"


def abrir_kanban(ruta):
    """Conexion de solo lectura, o None si el tablero no existe todavia."""
    ruta = Path(ruta)
    if not ruta.is_file():
        return None
    conn = sqlite3.connect(f"{ruta.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
    conn.row_factory = sqlite3.Row
    return conn


def ultimo_evento(conn) -> int:
    return int(
        conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
    )


def _json_de(bruto):
    """El payload del evento parseado; None si no trae o no es JSON."""
    try:
        return json.loads(bruto) if bruto else None
    except (TypeError, ValueError):
        return None


def _es_etapa_de_cadena(conn, tarea) -> bool:
    """'etapa' de cadena Goncloud: la raiz, una etapa de ronda 2 o una etapa
    inicial.

    La raiz y las de ronda 2 llevan la clave en su idempotency_key
    (goncloud-workflow:...). Las etapas INICIALES no la llevan y NO cuelgan
    de la raiz en task_links: workflow encadena las etapas entre si
    (cada una hija de la anterior) y el parentesco raiz->etapas solo queda
    en el evento "decomposed" de la raiz (payload.child_ids), que es
    donde lo mira workflow._decomposed_child_ids.
    """
    if str(tarea["idempotency_key"] or "").startswith(PREFIJO_CADENA):
        return True
    filas = conn.execute(
        "SELECT e.payload FROM task_events e JOIN tasks r ON r.id = e.task_id "
        "WHERE e.kind = 'decomposed' AND r.idempotency_key LIKE ?",
        (PREFIJO_CADENA + "%",),
    ).fetchall()
    for fila in filas:
        datos = _json_de(fila["payload"])
        hijos = datos.get("child_ids") if isinstance(datos, dict) else None
        if isinstance(hijos, list) and tarea["id"] in hijos:
            return True
    return False


def _relevante(conn, tarea) -> str | None:
    """'especialista', 'etapa' o None: lo que JEV cierra y vigila."""
    if tarea["assignee"] in ESPECIALISTAS:
        return "especialista"
    if _es_etapa_de_cadena(conn, tarea):
        return "etapa"
    return None


def _reporte(conn, tarea_id: str, payload) -> str:
    """Lo que la tarjeta dijo al terminar, segun el kanban real.

    El resumen de goncloud_local vive en task_runs.metadata (el
    "terminal"), aunque el esquema real tambien tenga summary/error en
    task_runs: el resultado queda en tasks.result y el evento trae
    payload ({"result": ...} al completar, {"reason": ..., "kind": ...}
    al bloquear).
    """
    partes = []
    fila = conn.execute("SELECT result FROM tasks WHERE id = ?", (tarea_id,)).fetchone()
    if fila is not None and fila["result"]:
        partes.append(fila["result"])
    corrida = conn.execute(
        "SELECT metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (tarea_id,),
    ).fetchone()
    if corrida is not None:
        datos = _json_de(corrida["metadata"])
        local = datos.get("goncloud_local") if isinstance(datos, dict) else None
        terminal = local.get("terminal") if isinstance(local, dict) else None
        if terminal:
            partes.append(terminal)
    datos = payload if isinstance(payload, dict) else _json_de(payload)
    if isinstance(datos, dict):
        partes.append(datos.get("reason") or datos.get("result"))
    return _acotar(" | ".join(str(p) for p in partes if p), 600)


def cierres_nuevos(conn, desde: int, limite: int = 3):
    """(cierres, cursor): eventos completed/blocked despues de `desde`.

    Un cierre es una tarjeta de especialista o una etapa de cadena Goncloud
    (incluido blocked). Se procesan a lo mucho `limite` por llamada; el
    cursor queda justo despues del ultimo examinado, asi lo que sobra sale en
    el turno siguiente y nada se pierde ni se repite.
    """
    cierres: list[dict] = []
    cursor = desde
    filas = conn.execute(
        "SELECT id, task_id, kind, payload FROM task_events "
        "WHERE id > ? AND kind IN ('completed', 'blocked') ORDER BY id LIMIT 200",
        (desde,),
    ).fetchall()
    for fila in filas:
        if len(cierres) >= limite:
            break
        cursor = fila["id"]
        tarea = conn.execute(
            "SELECT id, title, body, assignee, status, idempotency_key "
            "FROM tasks WHERE id = ?",
            (fila["task_id"],),
        ).fetchone()
        if tarea is None:
            continue
        clase = _relevante(conn, tarea)
        if clase is None:
            continue
        encargo = _acotar(tarea["body"], 600)
        cierres.append(
            {
                "evento": fila["id"],
                "task_id": tarea["id"],
                "clase": clase,
                "assignee": tarea["assignee"],
                "titulo": _acotar(tarea["title"], 100),
                "encargo": encargo,
                "body": encargo,
                "desenlace": fila["kind"],
                "reporte": _reporte(conn, tarea["id"], _json_de(fila["payload"])),
            }
        )
    return cierres, cursor


def ficha_de_cierre(cierre: dict) -> str:
    cuerpo = (
        f"Tarjeta {cierre['task_id']} de {cierre['assignee']} "
        f"({cierre['clase']}): {cierre['titulo']}\n"
        f"Desenlace reportado: {'completada' if cierre['desenlace'] == 'completed' else 'bloqueada'}\n"
    )
    encargo = _acotar(
        cierre.get("encargo") or cierre.get("body") or cierre.get("descripcion"),
        600,
    )
    if encargo:
        cuerpo += f"Encargo: {encargo}\n"
    if cierre.get("reporte"):
        cuerpo += f"Reporte: {cierre['reporte']}\n"
    return cuerpo
