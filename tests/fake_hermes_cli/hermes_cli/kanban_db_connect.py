"""Doble de kanban_db_connect: connect y write_txn con la semantica de la referencia.

Referencia: connect (kanban_db_connect.py:668) abre e inicializa el esquema;
write_txn (:1191) es BEGIN IMMEDIATE, y el anidamiento es opt-in por savepoint
(allow_nested=True); sin el, un write_txn dentro de otro es un RuntimeError.
"""

from __future__ import annotations

import contextlib
import secrets
import sqlite3
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  title            TEXT NOT NULL,
  body             TEXT,
  assignee         TEXT,
  status           TEXT NOT NULL,
  priority         INTEGER DEFAULT 0,
  created_by       TEXT,
  created_at       INTEGER NOT NULL,
  workspace_kind   TEXT,
  workspace_path   TEXT,
  claim_lock       TEXT,
  claim_expires    INTEGER,
  tenant           TEXT,
  result           TEXT,
  branch_name      TEXT,
  project_id       TEXT,
  idempotency_key  TEXT,
  completed_at     INTEGER
);
CREATE TABLE IF NOT EXISTS task_links (
  parent_id TEXT NOT NULL,
  child_id  TEXT NOT NULL,
  PRIMARY KEY (parent_id, child_id)
);
CREATE TABLE IF NOT EXISTS task_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,
  payload_json TEXT,
  created_at   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_comments (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT NOT NULL,
  author     TEXT NOT NULL,
  body       TEXT NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_idempotency
  ON tasks (idempotency_key);
"""


def connect(db_path=None, *, board=None):
    """Abre (e inicializa si hace falta) el kanban. Referencia :668.
    check_same_thread=False como el Store de H1: un puerto se abre y se cierra
    donde quiera que su dueño decida, y un cierre entre hilos no deja el
    archivo agarrado (WinError 32 en Windows)."""
    if db_path is None:
        raise ValueError("el doble exige una ruta explicita")
    ruta = Path(db_path)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(ruta), isolation_level=None, timeout=5, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(ESQUEMA)
    return conn


@contextlib.contextmanager
def connect_closing(db_path=None, *, board=None):
    """Referencia :739: abre y garantiza el cierre del descriptor."""
    conn = connect(db_path, board=board)
    try:
        yield conn
    finally:
        with contextlib.suppress(Exception):
            conn.close()


@contextlib.contextmanager
def write_txn(conn, *, allow_nested=False):
    """Referencia :1191: BEGIN IMMEDIATE; anidar exige allow_nested=True."""
    if getattr(conn, "in_transaction", False):
        if not allow_nested:
            raise RuntimeError(
                "write_txn: already inside a transaction. Nested composition "
                "must opt in explicitly with write_txn(conn, allow_nested=True) "
                "(savepoint semantics)."
            )
        savepoint = f"fake_nested_{secrets.token_hex(8)}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield conn
        except Exception:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(f"ROLLBACK TO {savepoint}")
                conn.execute(f"RELEASE {savepoint}")
            raise
        else:
            conn.execute(f"RELEASE {savepoint}")
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
