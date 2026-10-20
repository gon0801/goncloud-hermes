"""Doble de kanban_db: las funciones que usan las raices y el plugin goncloud.

Reglas copiadas de la referencia 0.21.5:
- create_task (:1255) deduplica por idempotency_key SOLO contra tareas NO
  archivadas (:1325-1332); el estado inicial sale de initial_task_state
  (triage si triage=True, todo si un padre no esta done, ready en otro caso).
- link_tasks (:1632) rechaza ids desconocidos, auto-enlace y ciclos.
- complete_task (:2731) solo pasa de running/ready/blocked/review a done con
  padres satisfechos; deja el resultado en la columna result y cierra una
  corrida con su metadata.
- block_task (:3216) solo pasa de running/ready a blocked.
- unblock_task (:3652) devuelve blocked a ready.
- archive_task (:3884) deja la tarjeta archived y es idempotente.
- Promocion por dependencias: al completar una tarjeta, sus hijas todo cuyos
  padres estan done pasan a ready, y sus padres todo/triage cuyas hijas estan
  done o archived pasan a ready (es lo que deja ejecutable la raiz directora
  al cierre de la cadena).

Las filas viajan como Fila (dict con acceso por atributo): el plugin lee
task.body y companyia con atributos y las raices con corchetes; un dict ya no
alcanza para las dos (55 errores uniformes de la suite del orquestador en la
Mac antes de este cambio).
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
DONE_LIKE = ("done", "archived")

_CONTADORES = {"id": 0}


class Fila(dict):
    """Fila de la referencia: acceso por corchete y por atributo."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _append_event(conn, task_id, kind, payload):
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at)"
        " VALUES (?, ?, ?, ?)",
        (task_id, kind, json.dumps(payload, sort_keys=True), int(time.time())),
    )


def _task_row(conn, task_id):
    fila = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return Fila(fila) if fila is not None else None


def _children_ids(conn, task_id):
    return [
        f["child_id"]
        for f in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", (task_id,)
        ).fetchall()
    ]


def _promote_dependents(conn, task_id):
    """Promocion por dependencias tras completar task_id (regla de la referencia)."""
    for child_id in _children_ids(conn, task_id):
        child = _task_row(conn, child_id)
        if child["status"] != "todo":
            continue
        if all(
            _task_row(conn, parent)["status"] in DONE_LIKE
            for parent in parent_ids(conn, child_id)
        ):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'todo'",
                (child_id,),
            )
            _append_event(conn, child_id, "promoted", {"by": task_id})
    _promover_raices_descompuestas(conn)


def _promover_raices_descompuestas(conn):
    """Una raiz descompuesta con todas sus hijas done/archived pasa a ready.

    El parentesco de la descomposicion vive en el evento "decomposed"
    (child_ids), no en task_links: la raiz no es padre de las etapas y por
    eso no bloquea su avance; se vuelve ejecutable cuando la cadena cierra.
    """
    filas = conn.execute(
        "SELECT DISTINCT task_id FROM task_events WHERE kind = 'decomposed'"
    ).fetchall()
    for fila in filas:
        raiz = _task_row(conn, fila["task_id"])
        if raiz is None or raiz["status"] not in ("todo", "triage"):
            continue
        child_ids = None
        for evento in list_events(conn, fila["task_id"]):
            if evento["kind"] == "decomposed" and isinstance(evento["payload"], dict):
                child_ids = evento["payload"].get("child_ids")
        if not child_ids:
            continue
        if all(
            _task_row(conn, hijo)["status"] in DONE_LIKE
            for hijo in child_ids
            if _task_row(conn, hijo) is not None
        ) and all(_task_row(conn, hijo) is not None for hijo in child_ids):
            conn.execute(
                "UPDATE tasks SET status = 'ready' WHERE id = ? AND status IN ('todo', 'triage')",
                (fila["task_id"],),
            )
            _append_event(
                conn, fila["task_id"], "promoted", {"children_done": len(child_ids)}
            )


def initial_task_state(conn, parents, initial_status, triage, tenant):
    del initial_status, tenant
    if triage:
        return "triage", None
    for pid in parents:
        fila = _task_row(conn, pid)
        if fila is None:
            raise ValueError(f"unknown parent task: {pid}")
        if fila["status"] not in DONE_LIKE:
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
            " created_by, created_at, workspace_kind, workspace_path, tenant,"
            " idempotency_key)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                title.strip(),
                body,
                assignee,
                estado,
                0,
                created_by,
                ahora,
                workspace_kind,
                workspace_path,
                tenant,
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
    return _task_row(conn, task_id)


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
    return [Fila(f) for f in conn.execute(consulta, parametros).fetchall()]


def _parents_satisfied(conn, task_id):
    return all(
        _task_row(conn, parent)["status"] in DONE_LIKE
        for parent in parent_ids(conn, task_id)
    )


def link_tasks(conn, parent_id, child_id, *, expected_child_run_id=None):
    """Referencia :1632: rechaza ids desconocidos, auto-enlace y ciclos.

    Una dependencia nueva deja de nuevo insatisfechos los padres de la hija:
    una tarjeta ready que gana una dependencia sin done vuelve a todo (el
    gate de consultas).
    """
    del expected_child_run_id
    if parent_id == child_id:
        raise ValueError("a task cannot be its own parent")
    if _task_row(conn, parent_id) is None or _task_row(conn, child_id) is None:
        raise ValueError("unknown task")
    if _habria_ciclo(conn, parent_id, child_id):
        raise ValueError("link would create a cycle")
    # Real v0.22.0 kanban_db.py:1580: write_txn(conn) sin allow_nested.
    with write_txn(conn):
        _link(conn, parent_id, child_id)
        hijo = _task_row(conn, child_id)
        if hijo["status"] == "ready" and not _parents_satisfied(conn, child_id):
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                (child_id,),
            )
            _append_event(conn, child_id, "linked", {"parent": parent_id})
            return
        _append_event(conn, child_id, "linked", {"parent": parent_id})


def _habria_ciclo(conn, parent_id, child_id):
    """child_id ya alcanza a parent_id -> enlazar cerraria un ciclo."""
    pendientes, vistos = [child_id], set()
    while pendientes:
        actual = pendientes.pop()
        if actual == parent_id:
            return True
        if actual in vistos:
            continue
        vistos.add(actual)
        pendientes.extend(_children_ids(conn, actual))
    return False


def _run_json(metadata):
    if metadata is None:
        return None
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)


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
    del summary, created_cards, expected_run_id, fire_lifecycle_hook, force
    if not _parents_satisfied(conn, task_id):
        return False
    # Real v0.22.0 kanban_db.py:2697: write_txn(conn) sin allow_nested.
    with write_txn(conn):
        if not _parents_satisfied(conn, task_id):
            return False
        ahora = int(time.time())
        cursor = conn.execute(
            "UPDATE tasks SET status = 'done', result = ?, completed_at = ?"
            " WHERE id = ? AND status IN ('running', 'ready', 'blocked', 'review')",
            (result, ahora, task_id),
        )
        if cursor.rowcount != 1:
            return False
        conn.execute(
            "INSERT INTO task_runs (task_id, started_at, metadata, ended_at)"
            " VALUES (?, ?, ?, ?)",
            (task_id, ahora, _run_json(metadata), ahora),
        )
        _append_event(conn, task_id, "completed", {"result": result})
        _promote_dependents(conn, task_id)
    return True


def block_task(conn, task_id, *, reason=None, kind=None, expected_run_id=None):
    """Referencia :3216: running/ready -> blocked; tipos validos de la referencia."""
    del expected_run_id
    if kind is not None and kind not in VALID_BLOCK_KINDS:
        raise ValueError(
            f"block kind must be one of {sorted(VALID_BLOCK_KINDS)} or None"
        )
    # Real v0.22.0 kanban_db.py:3165: write_txn(conn) sin allow_nested.
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
    # Real v0.22.0 kanban_db.py:3585: write_txn(conn) sin allow_nested.
    with write_txn(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'blocked'",
            (task_id,),
        )
        hecho = cursor.rowcount == 1
        if hecho:
            _append_event(conn, task_id, "unblocked", {})
    return hecho


def claim_task(conn, task_id, *, claimer, expected_run_id=None):
    """Referencia: reclama una tarjeta ready -> running con corrida activa.

    Devuelve el id de la corrida activa, o None si la tarjeta no esta lista
    para reclamar (padres sin done, estado distinto de ready).
    """
    del expected_run_id
    # Real v0.22.0 kanban_db.py:2210: write_txn(conn) sin allow_nested.
    with write_txn(conn):
        fila = _task_row(conn, task_id)
        if fila is None or fila["status"] != "ready":
            return None
        ahora = int(time.time())
        cursor = conn.execute(
            "INSERT INTO task_runs (task_id, started_at, ended_at) VALUES (?, ?, NULL)",
            (task_id, ahora),
        )
        run_id = str(cursor.lastrowid)
        conn.execute(
            "UPDATE tasks SET status = 'running', current_run_id = ?, claim_lock = ?"
            " WHERE id = ? AND status = 'ready'",
            (run_id, claimer, task_id),
        )
        _append_event(conn, task_id, "claimed", {"claimer": claimer})
    return run_id


def archive_task(conn, task_id, *, signal_fn=None):
    """Referencia :3813, reducida a lo observable para el doble: deja archived.

    Fiel a la referencia en lo transaccional: abre su PROPIO write_txn sin
    allow_nested (la referencia 0.22 no lo hace opt-in; anidada dentro de un
    write_txn externo lanza "already inside a transaction"), una transaccion
    por tarea.
    """
    del signal_fn
    with write_txn(conn):
        cursor = conn.execute(
            "UPDATE tasks SET status = 'archived' WHERE id = ? AND status != 'archived'",
            (task_id,),
        )
        hecho = cursor.rowcount == 1
        if hecho:
            _append_event(conn, task_id, "archived", {})
    return hecho


def add_comment(conn, task_id, author, body):
    # Referencia :1698: primitiva de composicion con allow_nested=True.
    with write_txn(conn, allow_nested=True):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at)"
            " VALUES (?, ?, ?, ?)",
            (task_id, author, body, int(time.time())),
        )


def list_comments(conn, task_id):
    return [
        Fila(f)
        for f in conn.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
    ]


def list_events(conn, task_id):
    """Eventos con el payload ya parseado, como la referencia."""
    eventos = []
    for f in conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall():
        evento = Fila(f)
        crudo = evento["payload"]
        try:
            evento["payload"] = json.loads(crudo) if crudo is not None else None
        except ValueError:
            evento["payload"] = crudo
        eventos.append(evento)
    return eventos


def list_runs(conn, task_id, *, include_active=False):
    """Corridas ordenadas por started_at/id; la metadata ya parseada.

    Sin include_active solo viajan las corridas cerradas, como en la
    referencia (las activas tienen ended_at NULL).
    """
    consulta = "SELECT * FROM task_runs WHERE task_id = ?"
    if not include_active:
        consulta += " AND ended_at IS NOT NULL"
    consulta += " ORDER BY started_at, id"
    corridas = []
    for f in conn.execute(consulta, (task_id,)).fetchall():
        corrida = Fila(f)
        try:
            corrida["metadata"] = (
                json.loads(corrida["metadata"]) if corrida["metadata"] else None
            )
        except ValueError:
            pass
        corridas.append(corrida)
    return corridas


def parent_ids(conn, task_id):
    return [
        f["parent_id"]
        for f in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ?", (task_id,)
        ).fetchall()
    ]
