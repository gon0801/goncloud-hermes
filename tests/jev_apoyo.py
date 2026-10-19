"""Dobles compartidos de las pruebas de JEV (ruteo y cierre).

Un kanban.db con el ESQUEMA de referencia (el del doble
tests/fake_hermes_cli, no tablas inventadas) y las cadenas creadas con el
CODIGO REAL del doble: create_task + decompose_triage_task dejan el grafo
que arma el workflow de verdad (raiz triage con la clave goncloud-workflow:,
etapas iniciales encadenadas entre si y sin idempotency_key, evento
"decomposed" con child_ids en la raiz) y complete_task/block_task dejan los
eventos y corridas reales (payload_json, metadata). Un JEV falso que
contesta lo que la prueba le programa (sin red) y la carga del plugin por su
ruta. Nada importa hermes_cli real ni yaml: corre en el CI de Ubuntu.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
PLUGIN = RAIZ / "plugins/goncloud-team"
if str(PLUGIN) not in sys.path:
    sys.path.insert(0, str(PLUGIN))
DOBLE = RAIZ / "tests/fake_hermes_cli"
if str(DOBLE) not in sys.path:
    sys.path.insert(0, str(DOBLE))

from hermes_cli import kanban_db, kanban_db_graph  # noqa: E402
from hermes_cli.kanban_db_connect import connect as _conectar  # noqa: E402


class Kanban:
    """Un tablero de prueba sobre el esquema y el codigo reales del doble."""

    def __init__(self, ruta: Path):
        self.ruta = Path(ruta)
        # El tablero existe desde el principio, como en un deploy real: sin
        # esto el primer turno no halla kanban y se pierde el cursor.
        conn = self._conn()
        conn.close()

    def _conn(self):
        return _conectar(self.ruta)

    def tarea(self, id, assignee, status="running", title="encargo", at=None, **extra):
        """Una tarjeta suelta (fuera de cadena) con id fijo, en el esquema real."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at,"
                " idempotency_key) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    id,
                    title,
                    assignee,
                    status,
                    int(at if at is not None else time.time()),
                    extra.get("clave"),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def cadena(
        self, raiz="t_raiz", etapas=(("scout", "done"), ("implementer", "running"))
    ):
        """Una cadena Goncloud creada por el codigo real del doble.

        Raiz triage con la clave goncloud-workflow:<raiz>; etapas iniciales
        encadenadas entre si (cada una hija de la anterior, sin
        idempotency_key); el parentesco raiz->etapas solo queda en el evento
        "decomposed" (child_ids). Cada etapa queda en el estado pedido por la
        via real (claim/completar/bloquear), respetando la promocion por
        dependencias. Devuelve {rol: id} mas "raiz".
        """
        conn = self._conn()
        try:
            raiz_id = kanban_db.create_task(
                conn,
                title=f"Engineering: {raiz}",
                assignee="claw",
                created_by="claw",
                triage=True,
                idempotency_key=f"goncloud-workflow:{raiz}",
            )
            hijos = kanban_db_graph.decompose_triage_task(
                conn,
                raiz_id,
                root_assignee="claw",
                children=[
                    {
                        "title": f"[{rol}] cadena",
                        "assignee": rol,
                        "parents": [] if i == 0 else [i - 1],
                    }
                    for i, (rol, _estado) in enumerate(etapas)
                ],
                author="claw",
            )
            ids = dict(zip((rol for rol, _ in etapas), hijos))
            for rol, estado in etapas:
                if estado == "done":
                    kanban_db.claim_task(conn, ids[rol], claimer=rol)
                    kanban_db.complete_task(conn, ids[rol], result="hecho")
                elif estado == "running":
                    kanban_db.claim_task(conn, ids[rol], claimer=rol)
                elif estado == "blocked":
                    kanban_db.block_task(conn, ids[rol], reason="prueba")
            ids["raiz"] = raiz_id
            return ids
        finally:
            conn.close()

    def evento(self, task_id, kind, at=None, payload=None):
        conn = self._conn()
        try:
            cursor = conn.execute(
                "INSERT INTO task_events (task_id, kind, payload_json, created_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    task_id,
                    kind,
                    json.dumps(payload, sort_keys=True)
                    if payload is not None
                    else None,
                    int(at if at is not None else time.time()),
                ),
            )
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def corrida(self, task_id, summary, at=None):
        """Una corrida cerrada con el terminal en su metadata, como el runtime."""
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO task_runs (task_id, status, started_at, metadata, ended_at)"
                " VALUES (?, 'done', ?, ?, ?)",
                (
                    task_id,
                    int(at if at is not None else time.time()),
                    json.dumps({"goncloud_local": {"terminal": summary}}),
                    int(at if at is not None else time.time()),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def estado(self, task_id, status):
        conn = self._conn()
        try:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
            conn.commit()
        finally:
            conn.close()

    def bloquear(self, task_id, motivo):
        """El bloqueo real: evento 'blocked' con payload_json {"reason": ...}."""
        conn = self._conn()
        try:
            return kanban_db.block_task(conn, task_id, reason=motivo)
        finally:
            conn.close()

    def completar(self, task_id, resultado=None, metadata=None):
        """El cierre real: corrida con metadata y evento 'completed'."""
        conn = self._conn()
        try:
            return kanban_db.complete_task(
                conn, task_id, result=resultado, metadata=metadata
            )
        finally:
            conn.close()


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
        if len(preguntas) == 1:
            nombre = next(iter(preguntas))
            opcion, confianza = self.respuestas[nombre]
            return {
                "answers": {
                    nombre: {"type": "choice", "choice": opcion, "confidence": confianza}
                }
            }
        return {
            "answers": {
                nombre: {
                    "type": "choice",
                    "choice": self.respuestas[nombre][0],
                    "confidence": self.respuestas[nombre][1],
                }
                for nombre in preguntas
                if nombre in self.respuestas
            }
        }

    def preguntas(self):
        return [next(iter(p)) for _, p, _ in self.llamadas]
