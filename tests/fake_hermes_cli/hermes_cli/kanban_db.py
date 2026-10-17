"""Doble de kanban_db: las funciones que usan las raices, con la regla de la referencia.

Reglas copiadas de la referencia 0.21.5:
- create_task (:1255) deduplica por idempotency_key SOLO contra tareas NO
  archivadas (:1325-1332); el estado inicial sale de initial_task_state
  (triage si triage=True, todo si un padre no esta done, ready en otro caso).
- link_tasks (:1632) rechaza ids desconocidos, auto-enlace y ciclos.
- complete_task (:2731) solo pasa de running/ready/blocked/review a done con
  padres satisfechos; deja el resultado en la columna result.
- block_task (:3216) solo pasa de running/ready a blocked.
- unblock_task (:3652) devuelve blocked a ready.
"""

from __future__ import annotations

import json
import time

from hermes_cli.kanban_db_connect import write_txn

VALID_STATUSES = {
    "triage",
    "todo",
    "scheduled",
    "ready",
    "running",
    "blocked",
    "review",
    "done",
    "archived",
}
VALID_INITIAL_STATUSES = {"running", "blocked"}
VALID_BLOCK_KINDS = {"dependency", "needs_input", "capability", "transient"}

_CONTADORES = {"id": 0}


def _append_event(conn, task_id, kind, payload):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload_json, created_at)"
        " VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, sort_keys=True), int(time.time())),
    )


def _task_row(conn, task_id):
    return conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()


def initial_task_state(conn, parents, initial_status, triage, tenant):
    """Referencia kanban_db_graph: el estado de entrada de una tarjeta."""
    del initial_status, tenant
    if triage:
        return "triage", None
    for pid in parents:
        fila = _task_row(conn, pid)
        if fila is None:
            raise ValueError(f"unknown parent task: {pid}")
        if fila["status"] not in ("done", "archived"):
            return "todo", None
    return "ready", None


def create_task(
    conn,
    *,
    title,
    body=None,
    assignee=None,
    created_by=None,
    workspace_kind=None,
    workspace_path=None,
    branch_name=None,
    tenant=None,
    priority=0,
    parents=(),
    triage=False,
    idempotency_key=None,
    max_runtime_seconds=None,
    skills=None,
    max_retries=None,
    model_override=None,
    provider_override=None,
    reasoning_effort=None,
    goal_mode=False,
    goal_max_turns=None,
    initial_status="running",
    session_id=None,
    board=None,
    project_id=None,
    project_source_task_id=None,
    creator_task_id=None,
    completion_contract=None,
):
    """Referencia :1255: crea la tarjeta y devuelve su id; deduplica SOLO no archivadas."""
    del (
        workspace_kind,
        workspace_path,
        branch_name,
        priority,
        max_runtime_seconds,
        skills,
        max_retries,
        model_override,
        provider_override,
        reasoning_effort,
        goal_mode,
        goal_max_turns,
        session_id,
        board,
        project_id,
        project_source_task_id,
        creator_task_id,
        completion_contract,
    )
    if not title or not title.strip():
        raise ValueError("title is required")
    if initial_status not in VALID_INITIAL_STATUSES:
        raise ValueError(
            f"initial_status must be one of {sorted(VALID_INITIAL_STATUSES)}"
        )
    parents = tuple(p for p in parents if p)
    if idempotency_key:
        fila = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if fila:
            return fila["id"]
    ahora = int(time.time())
    _CONTADORES["id"] += 1
    task_id = f"t_fake_{_CONTADORES['id']:06d}"
    with write_txn(conn, allow_nested=True):
        estado, tenant = initial_task_state(
            conn, parents, initial_status, triage, tenant
        )
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, priority,"
            " created_by, created_at, idempotency_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title.strip(),
                body,
                assignee,
                estado,
                0,
                created_by,
                ahora,
                idempotency_key,
            ),
        )
        for pid in parents:
            _link(conn, pid, task_id)
        _append_event(
            conn,
            task_id,
            "created",
            {"assignee": assignee, "status": estado, "parents": list(parents)},
        )
    return task_id


def _link(conn, parent_id, child_id):
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )


def get_task(conn, task_id):
    fila = _task_row(conn, task_id)
    return dict(fila) if fila is not None else None


def list_tasks(
    conn,
    *,
    assignee=None,
    status=None,
    tenant=None,
    session_id=None,
    include_archived=False,
    limit=None,
    order_by=None,
    workflow_template_id=None,
    current_step_key=None,
):
    del tenant, session_id, order_by, workflow_template_id, current_step_key
    consulta = "SELECT * FROM tasks WHERE 1=1"
    parametros = []
    for columna, valor in (("assignee", assignee), ("status", status)):
        if valor is not None:
            consulta += f" AND {columna} = ?"
            parametros.append(valor)
    if not include_archived and status != "archived":
        consulta += " AND status != 'archived'"
    consulta += " ORDER BY created_at ASC"
    if limit:
        consulta += f" LIMIT {int(limit)}"
    return [dict(f) for f in conn.execute(consulta, parametros).fetchall()]


def _parents_satisfied(conn, task_id):
    filas = conn.execute(
        "SELECT t.status FROM task_links l JOIN tasks t ON t.id = l.parent_id"
        " WHERE l.child_id = ?",
        (task_id,),
    ).fetchall()
    return all(f["status"] in ("done", "archived") for f in filas)


def link_tasks(conn, parent_id, child_id, *, expected_child_run_id=None):
    """Referencia :1632; sin runs en el doble, el gate de running no aplica."""
    del expected_child_run_id
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        faltantes = [
            tid for tid in (parent_id, child_id) if _task_row(conn, tid) is None
        ]
        if faltantes:
            raise ValueError(f"unknown task(s): {', '.join(faltantes)}")
        if _habria_ciclo(conn, parent_id, child_id):
            raise ValueError(f"linking {parent_id} -> {child_id} would create a cycle")
        _link(conn, parent_id, child_id)
        _append_event(
            conn, child_id, "linked", {"parent": parent_id, "child": child_id}
        )
    return False


def _habria_ciclo(conn, parent_id, child_id):
    vistos = set()
    pila = [child_id]
    while pila:
        nodo = pila.pop()
        if nodo == parent_id:
            return True
        if nodo in vistos:
            continue
        vistos.add(nodo)
        filas = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (nodo,)
        ).fetchall()
        pila.extend(f["child_id"] for f in filas)
    return False


def complete_task(
    conn,
    task_id,
    *,
    result=None,
    summary=None,
    metadata=None,
    created_cards=None,
    expected_run_id=None,
    fire_lifecycle_hook=True,
    force=False,
):
    """Referencia :2731: running/ready/blocked/review -> done, con el resultado."""
    del summary, metadata, created_cards, expected_run_id, fire_lifecycle_hook, force
    if not _parents_satisfied(conn, task_id):
        return False
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            return False
        cursor = conn.execute(
            "UPDATE tasks SET status = 'done', result = ?, completed_at = ?"
            " WHERE id = ? AND status IN ('running', 'ready', 'blocked', 'review')",
            (result, int(time.time()), task_id),
        )
        if cursor.rowcount != 1:
            return False
        _append_event(conn, task_id, "completed", {"result": result})
    return True


def block_task(conn, task_id, *, reason=None, kind=None, expected_run_id=None):
    """Referencia :3216: running/ready -> blocked; tipos validos de la referencia."""
    del expected_run_id
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    with write_txn(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'blocked'"
            " WHERE id = ? AND status IN ('running', 'ready')",
            (task_id,),
        )
        if cursor.rowcount != 1:
            return False
        _append_event(conn, task_id, "blocked", {"reason": reason, "kind": kind})
    return True


def unblock_task(conn, task_id):
    """Referencia :3652: blocked -> ready; False en otro caso."""
    with write_txn(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'blocked'",
            (task_id,),
        )
        hecho = cursor.rowcount == 1
        if hecho:
            _append_event(conn, task_id, "unblocked", {})
    return hecho


def archive_task(conn, task_id, *, signal_fn=None):
    """Referencia :3884, reducida a lo observable para el doble: deja archived."""
    del signal_fn
    with write_txn(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        return cursor.rowcount == 1
