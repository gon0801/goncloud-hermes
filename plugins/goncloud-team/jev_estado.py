"""Lo que JEV necesita saber del trabajo en curso, sin gastar un token.

Dos fuentes, las dos de solo lectura salvo el archivo de seguimiento:

  kanban.db         tarjetas de especialista y etapas de cadenas Goncloud:
                    cierres nuevos (completed/blocked) y candidatos a atorado.
  seguimiento.json  lo que los ganchos de claw anotan: mensajes de David sin
                    respuesta y promesas "te aviso". Hermes no guarda el
                    historial de Telegram ni "lo prometido", asi que lo anotan
                    pre_llm_call (entra el mensaje) y post_llm_call (sale la
                    respuesta); un turno que muere a medias deja su mensaje sin
                    cerrar y de ahi lo ve el vigilante.

El vigilante (raices/jev_vigilante.py) y el cierre (jev_auto) usan estas
mismas funciones. Nada abre el kanban para escribir.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path

try:
    from .jev_decisiones import ESPECIALISTAS
except ImportError:  # carga suelta (pruebas): el plugin esta en sys.path
    from jev_decisiones import ESPECIALISTAS

PREFIJO_CADENA = "goncloud-workflow:"
SIN_AVANCE_S = 600
PROMESA_S = 1800
TERMINALES = ("done", "archived")
EN_CURSO = ("ready", "running", "blocked")
PROMESA = re.compile(
    r"\bte\s+(?:aviso|avisar[eéaá]|"
    r"(?:escribo|confirmo|cuento|informo)\s+(?:cuando|en\s+cuanto|apenas))",
    re.IGNORECASE,
)
_candado = threading.Lock()


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
    if str(tarea["idempotency_key"] or "").startswith(PREFIJO_CADENA):
        return True
    return (
        conn.execute(
            "SELECT 1 FROM task_links l JOIN tasks p ON p.id = l.parent_id "
            "WHERE l.child_id = ? AND p.idempotency_key LIKE ? LIMIT 1",
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


def _avance(conn, tarea_id: str) -> int:
    """El ultimo instante con senal de vida: evento, latido, inicio o alta."""
    fila = conn.execute(
        "SELECT MAX(x) FROM ("
        " SELECT created_at x FROM tasks WHERE id = ?1"
        " UNION ALL SELECT started_at FROM tasks WHERE id = ?1"
        " UNION ALL SELECT last_heartbeat_at FROM tasks WHERE id = ?1"
        " UNION ALL SELECT MAX(created_at) FROM task_events WHERE task_id = ?1"
        " UNION ALL SELECT MAX(last_heartbeat_at) FROM task_runs WHERE task_id = ?1"
        " UNION ALL SELECT MAX(started_at) FROM task_runs WHERE task_id = ?1)",
        (tarea_id,),
    ).fetchone()
    return int(fila[0] or 0)


def _cadenas_abiertas(conn):
    marcas = ",".join("?" for _ in TERMINALES)
    return conn.execute(
        "SELECT id, title, status FROM tasks WHERE idempotency_key LIKE ? "
        f"AND idempotency_key NOT LIKE ? AND status NOT IN ({marcas})",
        (PREFIJO_CADENA + "%", PREFIJO_CADENA + "%:round2:%", *TERMINALES),
    ).fetchall()


def ingenieria_abierta(conn) -> bool:
    """Hay una cadena de ingenieria sin terminar (rompe el silencio nocturno)."""
    return bool(_cadenas_abiertas(conn))


def _etapas(conn, raiz_id: str):
    return conn.execute(
        "SELECT t.id, t.title, t.assignee, t.status FROM tasks t WHERE t.id IN "
        "(SELECT child_id FROM task_links WHERE parent_id = ?1) "
        "OR t.idempotency_key LIKE ?2",
        (raiz_id, f"{PREFIJO_CADENA}{raiz_id}:round2:%"),
    ).fetchall()


def candidatos_del_kanban(conn, ahora: int, sin_avance_s: int = SIN_AVANCE_S):
    """Cadenas con etapa blocked o sin avance y tarjetas de especialista sin avance."""
    candidatos = []
    for raiz in _cadenas_abiertas(conn):
        etapas = _etapas(conn, raiz["id"])
        bloqueadas = [e for e in etapas if e["status"] == "blocked"]
        if bloqueadas:
            etapa = bloqueadas[0]
            candidatos.append(
                {
                    "clave": f"cadena:{raiz['id']}:blocked",
                    "tipo": "etapa bloqueada",
                    "ficha": (
                        f"Cadena de ingenieria {raiz['id']} ({_acotar(raiz['title'], 80)}): "
                        f"la etapa {etapa['assignee']} ({etapa['id']}) esta bloqueada. "
                        f"Reporte: {_reporte(conn, etapa['id'], None) or 'sin reporte'}"
                    ),
                }
            )
            continue
        vivas = [e for e in etapas if e["status"] in ("ready", "running")]
        if not vivas:
            continue
        ultimo = max(_avance(conn, e["id"]) for e in vivas)
        if ahora - ultimo > sin_avance_s:
            candidatos.append(
                {
                    "clave": f"cadena:{raiz['id']}:sin-avance",
                    "tipo": "cadena sin avance",
                    "ficha": (
                        f"Cadena de ingenieria {raiz['id']} ({_acotar(raiz['title'], 80)}): "
                        f"etapas {', '.join(e['assignee'] for e in vivas)} sin avance "
                        f"desde hace {(ahora - ultimo) // 60} min."
                    ),
                }
            )
    marcas = ",".join("?" for _ in ESPECIALISTAS)
    tarjetas = conn.execute(
        f"SELECT id, title, assignee, status FROM tasks WHERE assignee IN ({marcas}) "
        f"AND status IN ({','.join('?' for _ in EN_CURSO)})",
        (*ESPECIALISTAS, *EN_CURSO),
    ).fetchall()
    for tarjeta in tarjetas:
        quieta = ahora - _avance(conn, tarjeta["id"])
        if quieta > sin_avance_s:
            candidatos.append(
                {
                    "clave": f"tarjeta:{tarjeta['id']}",
                    "tipo": "tarjeta sin avance",
                    "ficha": (
                        f"Tarjeta {tarjeta['id']} de {tarjeta['assignee']} "
                        f"({_acotar(tarjeta['title'], 80)}): estado {tarjeta['status']}, "
                        f"sin avance desde hace {quieta // 60} min."
                    ),
                }
            )
    return candidatos


# --- seguimiento.json (lo anotan los ganchos de claw) --------------------------


def _ruta_seguimiento(hogar) -> Path:
    return Path(hogar) / "jev" / "seguimiento.json"


def leer_seguimiento(hogar) -> dict:
    try:
        datos = json.loads(_ruta_seguimiento(hogar).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        datos = {}
    if not isinstance(datos, dict):
        datos = {}
    for clave in ("mensajes", "promesas"):
        if not isinstance(datos.get(clave), dict):
            datos[clave] = {}
    return datos


def _escribir_seguimiento(hogar, datos: dict) -> None:
    for clave in ("mensajes", "promesas"):
        while len(datos[clave]) > 20:
            datos[clave].pop(next(iter(datos[clave])))
    ruta = _ruta_seguimiento(hogar)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, sort_keys=True), encoding="utf-8")
    os.replace(temporal, ruta)


def anotar_mensaje(hogar, llave: str, ahora: int) -> None:
    """Entro un mensaje de David al turno `llave`; sin cerrar hasta que salga la respuesta."""
    try:
        with _candado:
            datos = leer_seguimiento(hogar)
            datos["mensajes"].setdefault(llave, {"at": ahora})
            _escribir_seguimiento(hogar, datos)
    except Exception:
        pass  # el seguimiento jamas rompe el turno


def cerrar_turno(hogar, llave: str, respuesta, ahora: int) -> None:
    """Salio la respuesta del turno: el mensaje se atendio y una promesa vieja se da por atendida.

    Si la respuesta misma promete un aviso, esa promesa queda anotada; es
    heuristica: un turno posterior de claw (el aviso de la tarjeta que
    termino, por ejemplo) la cierra.
    """
    try:
        with _candado:
            datos = leer_seguimiento(hogar)
            datos["mensajes"].pop(llave, None)
            datos["promesas"] = {}
            if isinstance(respuesta, str) and PROMESA.search(respuesta):
                datos["promesas"][llave] = {"at": ahora}
            _escribir_seguimiento(hogar, datos)
    except Exception:
        pass
