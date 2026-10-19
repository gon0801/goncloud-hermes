"""Lo que JEV necesita saber del trabajo en curso, sin gastar un token.

Fuente de solo lectura, kanban.db: tarjetas de especialista y etapas de
cadenas Goncloud que terminaron (completed/blocked, con su ultimo reporte),
para que el cierre (jev_auto) las traiga al turno siguiente. Nada abre el
kanban para escribir.
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
    """HERMES_KANBAN_DB o <raiz>/kanban.db, como el ticker de raices."""
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


def _es_etapa_de_cadena(conn, tarea) -> bool:
    # En el kanban real la etapa es PADRE de la raiz (workflow.py encadena
    # etapa -> ... -> entrega y cada una apunta a la raiz): la raiz es hija.
    if str(tarea["idempotency_key"] or "").startswith(PREFIJO_CADENA):
        return True
    return (
        conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks r ON r.id = l.child_id "
            "WHERE l.parent_id = ? AND r.idempotency_key LIKE ? LIMIT 1",
            (tarea["id"], PREFIJO_CADENA + "%"),
        ).fetchone()
        is not None
    )


def _relevante(conn, tarea) -> str | None:
    """'especialista', 'etapa' o None: lo que JEV cierra y vigila."""
    if tarea["assignee"] in ESPECIALISTAS:
        return "especialista"
    if _es_etapa_de_cadena(conn, tarea):
        return "etapa"
    return None


def _reporte(conn, tarea_id: str, payload) -> str:
    """Lo que la tarjeta dijo al terminar: resumen de su ultima corrida."""
    fila = conn.execute(
        "SELECT summary, error FROM task_runs WHERE task_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (tarea_id,),
    ).fetchone()
    partes = []
    if fila is not None:
        partes += [fila["summary"], fila["error"]]
    if not any(partes):
        partes.append(
            conn.execute(
                "SELECT result FROM tasks WHERE id = ?", (tarea_id,)
            ).fetchone()[0]
        )
    razon = None
    try:
        datos = json.loads(payload) if payload else None
        if isinstance(datos, dict):
            razon = datos.get("reason")
    except ValueError:
        pass
    partes.append(razon)
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
            "SELECT id, title, assignee, status, idempotency_key FROM tasks WHERE id = ?",
            (fila["task_id"],),
        ).fetchone()
        if tarea is None:
            continue
        clase = _relevante(conn, tarea)
        if clase is None:
            continue
        cierres.append(
            {
                "evento": fila["id"],
                "task_id": tarea["id"],
                "clase": clase,
                "assignee": tarea["assignee"],
                "titulo": _acotar(tarea["title"], 100),
                "desenlace": fila["kind"],
                "reporte": _reporte(conn, tarea["id"], fila["payload"]),
            }
        )
    return cierres, cursor


def ficha_de_cierre(cierre: dict) -> str:
    cuerpo = (
        f"Tarjeta {cierre['task_id']} de {cierre['assignee']} "
        f"({cierre['clase']}): {cierre['titulo']}\n"
        f"Desenlace reportado: {'completada' if cierre['desenlace'] == 'completed' else 'bloqueada'}\n"
    )
    if cierre["reporte"]:
        cuerpo += f"Reporte: {cierre['reporte']}\n"
    return cuerpo
