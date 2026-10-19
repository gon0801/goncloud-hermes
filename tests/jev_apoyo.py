"""Dobles compartidos de las pruebas de JEV (ruteo, cierre y vigilante).

Un kanban.db minimo con las columnas que lee jev_estado, un JEV falso que
contesta lo que la prueba le programa (sin red) y la carga del plugin por su
ruta. Nada importa hermes_cli ni yaml: corre en el CI de Ubuntu.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PLUGIN = RAIZ / "plugins/goncloud-team"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))

ESQUEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
    created_at INTEGER, started_at INTEGER, completed_at INTEGER,
    idempotency_key TEXT, last_heartbeat_at INTEGER, result TEXT);
CREATE TABLE task_links (parent_id TEXT, child_id TEXT);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, run_id INTEGER,
    kind TEXT, payload TEXT, created_at INTEGER);
CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, status TEXT,
    last_heartbeat_at INTEGER, started_at INTEGER, ended_at INTEGER,
    summary TEXT, error TEXT, outcome TEXT);
"""


class Kanban:
    """Un tablero de prueba; escribe como lo haria el kernel de Hermes."""

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)
        conn = sqlite3.connect(self.ruta)
        conn.executescript(ESQUEMA)
        conn.commit()
        conn.close()

    def _ejecutar(self, sql, params=()):
        conn = sqlite3.connect(self.ruta)
        try:
            cursor = conn.execute(sql, params)
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def tarea(self, id, assignee, status="running", title="encargo", at=None, **extra):
        at = int(at if at is not None else time.time())
        self._ejecutar(
            "INSERT INTO tasks (id, title, assignee, status, created_at, idempotency_key,"
            " started_at, last_heartbeat_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                id,
                title,
                assignee,
                status,
                at,
                extra.get("clave"),
                extra.get("inicio"),
                extra.get("latido"),
            ),
        )

    def cadena(
        self,
        raiz="t_raiz",
        etapas=(("scout", "done"), ("implementer", "running")),
        at=None,
    ):
        """Una cadena Goncloud: raiz con la clave goncloud-workflow y sus etapas."""
        self.tarea(
            raiz, "claw", "running", "cadena", at=at, clave="goncloud-workflow:pedido-1"
        )
        for assignee, status in etapas:
            id = f"{raiz}_{assignee}"
            self.tarea(id, assignee, status, f"[{assignee}] cadena", at=at)
            self._ejecutar("INSERT INTO task_links VALUES (?, ?)", (raiz, id))

    def evento(self, task_id, kind, at=None, payload=None):
        return self._ejecutar(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (task_id, kind, payload, int(at if at is not None else time.time())),
        )

    def corrida(self, task_id, summary, at=None):
        self._ejecutar(
            "INSERT INTO task_runs (task_id, status, started_at, summary) VALUES (?, 'done', ?, ?)",
            (task_id, int(at if at is not None else time.time()), summary),
        )

    def estado(self, task_id, status):
        self._ejecutar("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


class JevFalso:
    """El `llamar` del Decisor: contesta lo programado por pregunta, sin red."""

    def __init__(self, respuestas=None, falla=None):
        self.respuestas = respuestas or {}  # pregunta -> (opcion, confianza)
        self.falla = falla  # Exception o None
        self.llamadas = []  # (state, preguntas, timeout)

    def __call__(self, state, preguntas, timeout):
        self.llamadas.append((state, preguntas, timeout))
        if self.falla is not None:
            raise self.falla
        nombre = next(iter(preguntas))
        opcion, confianza = self.respuestas[nombre]
        return {
            "answers": {
                nombre: {"type": "choice", "choice": opcion, "confidence": confianza}
            }
        }

    def preguntas(self):
        return [next(iter(p)) for _, p, _ in self.llamadas]
