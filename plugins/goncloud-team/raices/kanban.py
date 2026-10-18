"""El adaptador real del KanbanPort sobre hermes_cli.kanban_db (SUA-07 H2).

La unica pieza que importa hermes_cli, y el importe es perezoso: el nucleo
sigue corriendo en la Mac sin ella. El invariante I03 vive con la misma forma
que en workflow.py: UN write_txn de kanban_db_connect contiene la busqueda por
clave (incluidas las archivadas, que create_task no mira) y, si no hay tarjeta,
create_task con esa clave. Una tarjeta archivada se devuelve tal cual: el
ticker la trata como intento muerto y no se crea otra.

link_tasks, complete_task y block_task abren su propia transaccion en upstream
y no se anidan: dentro de un write_txn del puerto quedan diferidas y corren
recien tras el commit; con una excepcion viva se descartan. Fuera de txn se
aplican en el momento. `creaciones` anota las claves recien creadas solo
despues del commit, igual que el doble de H1.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

kanban_port = importlib.import_module("raices.kanban_port")

Tarjeta = kanban_port.Tarjeta

ESTADOS = {
    "triage": "pendiente",
    "todo": "pendiente",
    "scheduled": "pendiente",
    "ready": "pendiente",
    "running": "corriendo",
    "review": "corriendo",
    "done": "completada",
    "blocked": "bloqueada",
    "archived": "archivada",
}

_FILA_POR_CLAVE = (
    "SELECT * FROM tasks WHERE idempotency_key = ?"
    " ORDER BY (status = 'archived'), created_at DESC LIMIT 1"
)


def _a_json(documento):
    return json.dumps(
        documento, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _resultado_de(texto):
    if not texto:
        return None
    try:
        carga = json.loads(texto)
    except ValueError:
        return None
    return carga if isinstance(carga, dict) else None


def _tarjeta_de(fila):
    return Tarjeta(
        fila["id"],
        fila["idempotency_key"] or "",
        ESTADOS.get(fila["status"], "pendiente"),
        _resultado_de(fila["result"]),
    )


class _EscrituraReal:
    def __init__(self, puerto, conexion):
        self._puerto = puerto
        self._conexion = conexion
        self._pila = None
        self._diferidas = []
        self._creadas = []

    def _abrir_txn(self):
        if self._pila is None:
            from hermes_cli.kanban_db_connect import write_txn

            self._pila = ExitStack()
            self._pila.enter_context(write_txn(self._conexion))

    def buscar_o_crear(self, clave, spec):
        self._abrir_txn()
        fila = self._conexion.execute(_FILA_POR_CLAVE, (clave,)).fetchone()
        if fila is not None:
            return _tarjeta_de(fila)
        from hermes_cli.kanban_db import create_task

        id_nueva = create_task(
            self._conexion,
            title=spec.titulo,
            body=spec.cuerpo,
            assignee=spec.asignado_a,
            idempotency_key=clave,
        )
        fila = self._conexion.execute(
            "SELECT * FROM tasks WHERE id = ?", (id_nueva,)
        ).fetchone()
        tarjeta = _tarjeta_de(fila)
        self._creadas.append(tarjeta)
        return tarjeta

    def vincular(self, id_tarjeta, id_padre):
        self._diferir(("vincular", id_padre, id_tarjeta))

    def completar(self, id_tarjeta, resultado):
        self._diferir(("completar", id_tarjeta, resultado))

    def bloquear(self, id_tarjeta, motivo):
        self._diferir(("bloquear", id_tarjeta, motivo))

    def _diferir(self, operacion):
        if self._pila is not None:
            self._diferidas.append(operacion)
        else:
            self._ejecutar(operacion)

    def _ejecutar(self, operacion):
        from hermes_cli.kanban_db import block_task, complete_task, link_tasks

        clase = operacion[0]
        if clase == "completar":
            _, id_tarjeta, resultado = operacion
            complete_task(
                self._conexion,
                id_tarjeta,
                result=None if resultado is None else _a_json(resultado),
            )
        elif clase == "bloquear":
            _, id_tarjeta, motivo = operacion
            block_task(self._conexion, id_tarjeta, reason=motivo)
        else:
            _, id_padre, id_tarjeta = operacion
            link_tasks(self._conexion, id_padre, id_tarjeta)

    def cerrar(self, bien):
        if self._pila is not None:
            if bien:
                self._pila.close()
            else:
                _, exc, tb = sys.exc_info()
                if exc is not None:
                    # Upstream recibe __exit__ con la excepcion viva: hace su
                    # rollback y limpieza; un ROLLBACK a mano tragaria un
                    # sqlite3.Error y dejaria la pila sin cerrar.
                    self._pila.__exit__(type(exc), exc, tb)
                else:
                    with suppress(sqlite3.Error):
                        self._conexion.execute("ROLLBACK")
                    self._pila.close()
            self._pila = None
        if bien:
            self._puerto.creaciones.extend(tarjeta.clave for tarjeta in self._creadas)
            for operacion in self._diferidas:
                self._ejecutar(operacion)
        self._creadas.clear()
        self._diferidas.clear()


class KanbanHermes:
    def __init__(self, ruta_db):
        self.ruta_db = Path(ruta_db)
        self.creaciones = []
        self._conexion = None

    def cerrar(self):
        conexion = self._conexion
        if conexion is None:
            return
        try:
            conexion.close()
        except sqlite3.ProgrammingError:
            # Nacio en otro hilo: solo ese hilo puede cerrarla. Queda
            # referenciada para que su dueño la cierre; no se disimula.
            return
        self._conexion = None

    def _conexion_viva(self):
        if self._conexion is None:
            from hermes_cli.kanban_db_connect import connect

            self._conexion = connect(self.ruta_db)
        return self._conexion

    @contextmanager
    def write_txn(self):
        escritura = _EscrituraReal(self, self._conexion_viva())
        bien = False
        try:
            yield escritura
            bien = True
        finally:
            escritura.cerrar(bien)

    def leer_estado(self, clave):
        fila = self._conexion_viva().execute(_FILA_POR_CLAVE, (clave,)).fetchone()
        return _tarjeta_de(fila) if fila is not None else None
