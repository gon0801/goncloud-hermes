"""Doble de kanban_db_graph: la descomposicion atomica de la referencia.

initial_task_state vive en kanban_db.py del doble (misma firma que la
referencia). decompose_triage_task crea las hijas encadenadas dentro de UNA
transaccion, mueve la raiz triage a todo y deja el evento "decomposed" con
child_ids, que es lo que workflow.py lee para recuperar el grafo.
"""

from __future__ import annotations

from hermes_cli.kanban_db import _append_event, _task_row, create_task
from hermes_cli.kanban_db_connect import write_txn


def decompose_triage_task(conn, root_id, *, root_assignee, children, author):
    """Descompone la raiz triage en `children` encadenadas; ids nuevos o None."""
    raiz = _task_row(conn, root_id)
    if raiz is None or raiz["status"] != "triage":
        return None
    del root_assignee
    ids = []
    # Real v0.22.0 kanban_db_graph.py:116: `with write_txn(conn):` SIN
    # allow_nested (comentario :113-114: los helpers que abren su propio
    # write_txn no deben llamarse dentro). Anidada lanza RuntimeError.
    with write_txn(conn):
        for spec in children:
            padres = tuple(ids[i] for i in spec.get("parents", ()))
            ids.append(
                create_task(
                    conn,
                    title=spec["title"],
                    body=spec.get("body"),
                    assignee=spec.get("assignee"),
                    created_by=author,
                    workspace_kind=raiz["workspace_kind"],
                    workspace_path=raiz["workspace_path"],
                    tenant=raiz["tenant"],
                    parents=padres,
                    creator_task_id=root_id,
                )
            )
        conn.execute(
            "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'triage'",
            (root_id,),
        )
        _append_event(conn, root_id, "decomposed", {"child_ids": ids})
    return ids
