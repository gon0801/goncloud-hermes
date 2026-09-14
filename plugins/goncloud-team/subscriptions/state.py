"""Durable SQLite state for subscription attempts and adapter health."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscription_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    stage_id TEXT NOT NULL,
    slot INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    adapter TEXT NOT NULL,
    model TEXT NOT NULL,
    billing_mode TEXT NOT NULL,
    mode TEXT NOT NULL,
    success INTEGER NOT NULL,
    failure TEXT,
    session_id TEXT NOT NULL DEFAULT '',
    start_revision TEXT NOT NULL DEFAULT '',
    end_revision TEXT NOT NULL DEFAULT '',
    changed_files TEXT NOT NULL DEFAULT '[]',
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(workflow_id, stage_id, slot, attempt)
);
CREATE TABLE IF NOT EXISTS subscription_health (
    adapter TEXT PRIMARY KEY,
    ready INTEGER NOT NULL,
    account_class TEXT NOT NULL,
    failure TEXT,
    detail TEXT NOT NULL DEFAULT '',
    cooldown_until TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
"""

_ATTEMPT_COLUMNS = (
    "id",
    "workflow_id",
    "stage_id",
    "slot",
    "attempt",
    "adapter",
    "model",
    "billing_mode",
    "mode",
    "success",
    "failure",
    "session_id",
    "start_revision",
    "end_revision",
    "changed_files",
    "detail",
    "created_at",
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class SubscriptionStateStore:
    """Thread-safe private store; attempt identity is durable and unique."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        try:
            yield conn
        finally:
            # Closing releases Windows file locks and checkpoints WAL files.
            conn.close()

    # -- attempts ---------------------------------------------------------

    def record_attempt(
        self,
        *,
        workflow_id: str,
        stage_id: str,
        slot: int,
        attempt: int,
        adapter: str,
        model: str,
        billing_mode: str,
        mode: str,
        success: bool,
        failure: Optional[str],
        session_id: str,
        start_revision: str,
        end_revision: str,
        changed_files: list[str],
        detail: str,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO subscription_attempts "
                "(workflow_id, stage_id, slot, attempt, adapter, model, "
                "billing_mode, mode, success, failure, session_id, "
                "start_revision, end_revision, changed_files, detail) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    stage_id,
                    int(slot),
                    int(attempt),
                    adapter,
                    model,
                    billing_mode,
                    mode,
                    1 if success else 0,
                    failure,
                    session_id,
                    start_revision,
                    end_revision,
                    json.dumps(sorted(changed_files), ensure_ascii=False),
                    detail,
                ),
            )
            conn.commit()

    def stage_attempts(self, workflow_id: str, stage_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM subscription_attempts "
                "WHERE workflow_id = ? AND stage_id = ? ORDER BY id",
                (workflow_id, stage_id),
            ).fetchall()
        return [_attempt_view(row) for row in rows]

    def latest_success(
        self, workflow_id: str, stage_id: str
    ) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscription_attempts "
                "WHERE workflow_id = ? AND stage_id = ? AND success = 1 "
                "ORDER BY id DESC LIMIT 1",
                (workflow_id, stage_id),
            ).fetchone()
        return _attempt_view(row) if row is not None else None

    def next_attempt(self, workflow_id: str, stage_id: str, slot: int) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total FROM subscription_attempts "
                "WHERE workflow_id = ? AND stage_id = ? AND slot = ?",
                (workflow_id, stage_id, int(slot)),
            ).fetchone()
        return int(row["total"])

    def stage_mutation(
        self, workflow_id: str, stage_id: str
    ) -> Optional[dict[str, Any]]:
        """Last recorded mutation evidence (revision + changed files)."""
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscription_attempts "
                "WHERE workflow_id = ? AND stage_id = ? "
                "AND changed_files != '[]' "
                "ORDER BY id DESC LIMIT 1",
                (workflow_id, stage_id),
            ).fetchone()
        return _attempt_view(row) if row is not None else None

    # -- health -----------------------------------------------------------

    def save_health(
        self,
        adapter: str,
        *,
        ready: bool,
        account_class: str,
        failure: Optional[str] = None,
        detail: str = "",
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO subscription_health "
                "(adapter, ready, account_class, failure, detail, updated_at) "
                "VALUES (?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%fZ','now')) "
                "ON CONFLICT(adapter) DO UPDATE SET ready=excluded.ready, "
                "account_class=excluded.account_class, failure=excluded.failure, "
                "detail=excluded.detail, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
                (adapter, 1 if ready else 0, account_class, failure, detail),
            )
            conn.commit()

    def health(self, adapter: str) -> Optional[dict[str, Any]]:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM subscription_health WHERE adapter = ?",
                (adapter,),
            ).fetchone()
        if row is None:
            return None
        return {
            "ready": bool(row["ready"]),
            "account_class": row["account_class"],
            "failure": row["failure"],
            "detail": row["detail"],
            "cooldown_until": row["cooldown_until"],
            "updated_at": row["updated_at"],
        }

    def set_cooldown(
        self, adapter: str, *, hours: float = 0.0, until: Optional[datetime] = None
    ) -> None:
        moment = until or (_utcnow() + timedelta(hours=hours))
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO subscription_health "
                "(adapter, ready, account_class, cooldown_until) "
                "VALUES (?, 0, '', ?) "
                "ON CONFLICT(adapter) DO UPDATE SET "
                "cooldown_until=excluded.cooldown_until",
                (adapter, moment.isoformat()),
            )
            conn.commit()

    def cooldown_until(self, adapter: str) -> Optional[datetime]:
        cached = self.health(adapter)
        return _parse_iso(cached.get("cooldown_until")) if cached else None


def _attempt_view(row: sqlite3.Row) -> dict[str, Any]:
    view = {column: row[column] for column in _ATTEMPT_COLUMNS}
    view["success"] = int(row["success"])
    try:
        view["changed_files"] = json.loads(row["changed_files"] or "[]")
    except json.JSONDecodeError:
        view["changed_files"] = []
    return view
