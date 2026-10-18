"""Doble de kanban_db_graph: lo que workflow.py importa al cargarse.

initial_task_state vive en kanban_db.py del doble (misma firma que la
referencia); decompose_triage_task no lo usa ninguna prueba de raices, asi que
solo existe para que la importacion del plugin no falle en la Mac.
"""


def decompose_triage_task(*args, **kwargs):
    raise NotImplementedError("el doble no descompone tarjetas de triage")
