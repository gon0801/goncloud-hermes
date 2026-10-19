"""Durable Goncloud engineering workflow on Hermes' native Kanban kernel."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Iterable
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from hermes_cli.kanban_db import (
    add_comment,
    block_task,
    complete_task,
    create_task,
    get_task,
    link_tasks,
    list_comments,
    list_events,
    list_runs,
    list_tasks,
    parent_ids,
)
from hermes_cli.kanban_db_connect import connect_closing, write_txn
from hermes_cli.kanban_db_graph import decompose_triage_task

try:  # Loaded as the Hermes plugin package.
    from .subscriptions.catalog import compile_profile_routes
    from .subscriptions.redaction import redact_data, sanitize_result
    from .subscriptions.router import SubscriptionRouter, default_db_path
    from .subscriptions.state import SubscriptionStateStore
    from .subscriptions.types import InvocationRequest
except ImportError:  # Loaded with the plugin directory on sys.path.
    from subscriptions.catalog import compile_profile_routes  # type: ignore
    from subscriptions.redaction import redact_data, sanitize_result  # type: ignore
    from subscriptions.router import (  # type: ignore
        SubscriptionRouter,
        default_db_path,
    )
    from subscriptions.state import SubscriptionStateStore  # type: ignore
    from subscriptions.types import InvocationRequest  # type: ignore


TEAM_ROLES = (
    "claw",
    "operaciones",
    "ingenieria",
    "scout",
    "implementer",
    "verifier",
    "adversary",
    "reviewer",
    "amazon",
    "meli",
    "mail",
    "limpia",
    "odoo",
    "pers",
    "arras",
    "gonserver",
    "skillverif",
    "tinkabot",
    "eggbot",
    "entrega",
)
INITIAL_STAGES = (
    "scout",
    "ingenieria",
    "implementer",
    "verifier",
    "adversary",
    "reviewer",
    "entrega",
)
CORRECTIVE_STAGES = ("implementer", "verifier", "adversary", "reviewer")
HIGH_SEVERITIES = {"high", "critical"}

_WORKFLOW_KEY_PREFIX = "goncloud-workflow:"
_CONTRACT_MARKER = "GONCLOUD_ENGINEERING_V1\n"
#: Public alias: local_orchestrator reads workflow contracts through it.
CONTRACT_MARKER = _CONTRACT_MARKER
#: Marker of the durable close receipt on the director root; a second close
#: recognizes it instead of writing another comment. The close tool and the
#: automatic close of ``start`` write the same receipt (encargo H punto 2).
CLOSE_COMMENT_MARKER = "GONCLOUD_LOCAL_CLOSE_V1\n"

#: The reason the automatic close leaves on a chain ``start`` freed for a
#: new one (encargo H punto 2, the same path goncloud_local_close uses).
AUTO_CLOSE_REASON = (
    "auto-closed by WorkflowService.start: the chain's driver had stopped "
    "and the repository was freed for a new chain"
)
_REVIEW_MARKER = "GONCLOUD_REVIEW_V1\n"
_SUBSCRIPTION_MARKER = "GONCLOUD_SUBSCRIPTION_V1\n"


def _driver_signals(workflow_id: str) -> tuple[bool, bool]:
    """(alive, stopped) according to the local driver's own result-file mark.

    The mark is the local driver's lifecycle record — local_orchestrator
    owns it, so the import stays deferred and one-directional. Three
    answers, never two collapsed into one: a running mark whose pid is
    alive is live work; a stopped outcome, or a running mark whose pid is
    gone, is positive evidence the driver stopped; anything else — no mark
    at all, an unreadable or non-object file that still reads as running —
    proves nothing (review F2: "no evidence" read as "stopped" had start
    archiving chains with a lying close reason). A helper that cannot even
    load answers alive: fail closed, never archive on missing evidence.
    """
    try:
        try:
            from .local_orchestrator import (
                driver_mark_live,
                driver_stopped,
                read_driver_mark,
            )
        except ImportError:
            from local_orchestrator import (
                driver_mark_live,
                driver_stopped,
                read_driver_mark,
            )
    except ImportError:
        return True, False
    mark = read_driver_mark(workflow_id)
    if (
        isinstance(mark, dict)
        and mark.get("outcome") == "running"
        and driver_mark_live(mark)
    ):
        return True, False
    return False, driver_stopped(mark)


_SUBSCRIPTION_EVIDENCE_KEYS = (
    "workflow_id",
    "stage_id",
    "profile",
    "slot",
    "success",
    "adapter",
    "model",
    "billing_mode",
    "failure",
    "session_id",
    "mode",
)


class WorkflowError(ValueError):
    """A rejected workflow operation safe to return through a model tool."""


class _StoreUnavailable:
    """Sentinel: the subscription store could not be opened, so lease
    evidence is unchecked and nothing may be auto-closed on its absence."""


#: Root statuses a chain close (the tool and the automatic one) accepts;
#: anything else is either live work, already delivered, or not a decidable
#: chain. "triage" covers a root whose decomposition failed after the insert.
CLOSEABLE_ROOT_STATUSES = frozenset({"todo", "ready", "scheduled", "blocked", "triage"})


def _build_subscription_router(**_kwargs):
    """Factory seam so tests can substitute a scripted router."""
    return SubscriptionRouter()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowError(f"{field} is required")
    return text


def _contract_body(contract: dict[str, Any]) -> str:
    return _CONTRACT_MARKER + json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _task_view(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "result": task.result,
    }


class WorkflowService:
    """Role-bound adapter over native Kanban APIs.

    ``actor_role`` is supplied by the Hermes ``PluginContext`` at registration
    time. It is an application authorization boundary for a trusted local
    Hermes profile, not OS-level isolation.
    """

    def __init__(self, *, actor_role: str, db_path: Path | None = None) -> None:
        self.actor_role = str(actor_role or "").strip()
        self.db_path = Path(db_path) if db_path is not None else None

    def _connect(self):
        return connect_closing(self.db_path)

    def _require_known_actor(self) -> None:
        if self.actor_role not in TEAM_ROLES:
            raise WorkflowError(
                f"unknown Goncloud role: {self.actor_role or '<empty>'}"
            )

    def _require_actor(self, expected: str) -> None:
        self._require_known_actor()
        if self.actor_role != expected:
            raise WorkflowError(f"this operation requires role {expected}")

    @contextmanager
    def local_transition(
        self,
        *,
        workflow_id,
        task_id,
        require_approval=False,
        allow_blocked=False,
        connection=None,
    ):
        """Fence authorization inside the native transition's own transaction.

        Native complete/block must own their commit because lifecycle hooks run
        after it. A connection-local trigger compares this snapshot atomically
        at their UPDATE; no nested transaction or persistent trigger is needed.
        """
        self._require_known_actor()
        with (
            nullcontext(connection) if connection is not None else self._connect()
        ) as conn:
            with write_txn(conn):
                root = self._require_workflow_root(conn, workflow_id)
                task = self._require_task(conn, task_id)
                if task.assignee != self.actor_role or root.assignee != "claw":
                    raise WorkflowError("stage assignment changed; transition refused")
                if root.status not in {"todo", "ready", "running"}:
                    raise WorkflowError("director is not executable")
                if task.status not in {"ready", "running", "done"} and not (
                    allow_blocked and task.status == "blocked"
                ):
                    raise WorkflowError("stage dependencies are incomplete")
                initial = self._decomposed_child_ids(conn, workflow_id) or []
                correction = self._correction_ids(conn, workflow_id)
                membership = set(initial) | set(correction.values())
                if task_id != workflow_id and task_id not in membership:
                    raise WorkflowError("task is not a stage of workflow")
                watched = {workflow_id, task_id, *parent_ids(conn, task_id)}
                review_ids = []
                if task_id == workflow_id:
                    self._require_actor("claw")
                    if any(
                        self._require_task(conn, member).status != "done"
                        for member in membership
                    ):
                        raise WorkflowError("workflow stages are incomplete")
                    watched |= membership
                if require_approval:
                    if len(initial) != len(INITIAL_STAGES):
                        raise WorkflowError("workflow graph is incomplete")
                    reviewer_id = (
                        correction.get("reviewer")
                        or dict(zip(INITIAL_STAGES, initial))["reviewer"]
                    )
                    reviewer = self._require_task(conn, reviewer_id)
                    review = self._recorded_review_run(conn, reviewer_id)
                    if (
                        reviewer.status != "done"
                        or reviewer.assignee != "reviewer"
                        or not review
                        or review.get("decision") != "approve"
                        or review.get("result", {}).get("outcome") != "approved"
                    ):
                        raise WorkflowError("durable reviewer approval is required")
                    watched.add(reviewer_id)
                    review_ids = [reviewer_id]
                columns = (
                    "id",
                    "assignee",
                    "status",
                    "workspace_kind",
                    "workspace_path",
                    "body",
                    "current_run_id",
                    "idempotency_key",
                )
                conn.execute(
                    "CREATE TEMP TABLE goncloud_local_target (id TEXT PRIMARY KEY)"
                ).close()
                conn.execute(
                    "INSERT INTO goncloud_local_target VALUES (?)", (task_id,)
                ).close()
                conn.execute(
                    "CREATE TEMP TABLE goncloud_local_expected (id TEXT PRIMARY KEY, assignee, status, workspace_kind, workspace_path, body, current_run_id, idempotency_key)"
                ).close()
                for member in watched:
                    conn.execute(
                        "INSERT INTO goncloud_local_expected SELECT "
                        + ",".join(columns)
                        + " FROM tasks WHERE id=?",
                        (member,),
                    ).close()
                conn.execute(
                    "CREATE TEMP TABLE goncloud_local_review_tasks (task_id TEXT PRIMARY KEY)"
                ).close()
                conn.execute(
                    "CREATE TEMP TABLE goncloud_local_reviews (id INTEGER PRIMARY KEY, task_id, started_at, metadata, ended_at)"
                ).close()
                for reviewer_id in review_ids:
                    conn.execute(
                        "INSERT INTO goncloud_local_review_tasks VALUES (?)",
                        (reviewer_id,),
                    ).close()
                    conn.execute(
                        "INSERT INTO goncloud_local_reviews SELECT id,task_id,started_at,metadata,ended_at FROM task_runs WHERE task_id=? AND ended_at IS NOT NULL",
                        (reviewer_id,),
                    ).close()
                changed = " OR ".join(
                    f"t.{column} IS NOT e.{column}" for column in columns[1:]
                )
                # Match list_runs(include_active=False) and its started_at/id
                # ordering. Both anti-joins fence the full ID set (and count),
                # including inserts and active runs that close after validation.
                # Metadata stays SQL data, never interpolated into trigger text.
                conn.execute(
                    "CREATE TEMP TRIGGER goncloud_local_fence BEFORE UPDATE OF status ON main.tasks "
                    "WHEN OLD.id IN (SELECT id FROM goncloud_local_target) BEGIN "
                    "SELECT CASE WHEN EXISTS(SELECT 1 FROM goncloud_local_expected e "
                    "LEFT JOIN main.tasks t ON t.id=e.id WHERE t.id IS NULL OR "
                    + changed
                    + ") "
                    "OR EXISTS(SELECT 1 FROM goncloud_local_reviews e LEFT JOIN main.task_runs r ON r.id=e.id "
                    "WHERE r.id IS NULL OR r.task_id IS NOT e.task_id OR r.started_at IS NOT e.started_at "
                    "OR r.metadata IS NOT e.metadata OR r.ended_at IS NOT e.ended_at) "
                    "OR EXISTS(SELECT 1 FROM main.task_runs r "
                    "JOIN goncloud_local_review_tasks s ON s.task_id=r.task_id "
                    "LEFT JOIN goncloud_local_reviews e ON e.id=r.id "
                    "WHERE r.ended_at IS NOT NULL AND e.id IS NULL) "
                    "THEN RAISE(ABORT, 'local transition authorization changed') END; END"
                ).close()
            try:
                yield conn, task
            except sqlite3.DatabaseError:
                raise WorkflowError(
                    "local transition authorization changed; transition refused"
                ) from None
            finally:
                for name in ("goncloud_local_fence",):
                    conn.execute(f"DROP TRIGGER IF EXISTS temp.{name}").close()
                for name in (
                    "goncloud_local_target",
                    "goncloud_local_expected",
                    "goncloud_local_review_tasks",
                    "goncloud_local_reviews",
                ):
                    conn.execute(f"DROP TABLE IF EXISTS temp.{name}").close()

    def start(
        self,
        *,
        request_key: str,
        repository: str,
        revision: str,
        instruction: str,
        sources: Iterable[dict[str, Any]],
        affects_business_rules: bool = False,
    ) -> dict[str, Any]:
        """Create the initial chain atomically, or return its existing IDs."""
        self._require_actor("claw")
        request_key = _required_text(request_key, "request_key")
        revision = _required_text(revision, "revision")
        instruction = _required_text(instruction, "instruction")
        repo_input = _required_text(repository, "repository")
        repo = Path(repo_input).expanduser()
        if not repo.is_absolute():
            raise WorkflowError("repository must be an absolute path")
        try:
            repo = repo.resolve(strict=True)
        except OSError as exc:
            raise WorkflowError(f"repository is unavailable: {exc}") from exc
        if not repo.is_dir():
            raise WorkflowError("repository must be an existing directory")

        source_rows = self._validate_sources(sources)
        contract = {
            "request_key": request_key,
            "repository": str(repo),
            "revision": revision,
            "instruction": instruction,
            "sources": source_rows,
            "affects_business_rules": bool(affects_business_rules),
        }
        body = _contract_body(contract)
        idempotency_key = _WORKFLOW_KEY_PREFIX + request_key

        # Encargo H punto 2, contrato r6: una sola cadena viva por repo.
        # (a) La otra cadena tiene evidencia de parada (regla A de r3 más
        # F12: outcome final, pid muerto o reciclado, o marca running sin
        # pid) → se cierra sola, atómica como hoy, y la nueva arranca.
        # (b) La otra cadena está viva → la nueva NO se crea: la respuesta
        # es busy, clara y sin error genérico, y no se escribe NADA (ni
        # Kanban ni archivo de resultado). Cuando la cadena viva termine,
        # su aviso a claw (encargo H) pide relanzarla.
        store = self._subscription_store_checked()
        with self._connect() as conn:
            # Fase 1 — veredictos sin candado de escritura (review F15):
            # estado de Kanban y leases aquí; las marcas de driver (y el ps
            # que cuesta cada viva) justo después, fuera de transacción.
            # La relectura que autoriza es la de _auto_close_stopped_chain.
            live_chain = None
            stopped_chains = []
            for active in list_tasks(conn, include_archived=True):
                if not (active.body or "").startswith(_CONTRACT_MARKER):
                    continue
                try:
                    active_repo = (
                        Path(active.workspace_path or "").expanduser().resolve()
                    )
                except OSError:
                    continue
                if active.workspace_kind != "dir" or active_repo != repo:
                    continue
                if active.status in {"done", "archived"}:
                    continue
                if active.idempotency_key == idempotency_key:
                    continue
                if self._foreign_db_alive(conn, active, store=store):
                    live_chain = live_chain or active.id
                    continue
                alive, stopped = _driver_signals(active.id)
                if alive or not stopped:
                    live_chain = live_chain or active.id
                else:
                    stopped_chains.append(active.id)
            if live_chain is not None:
                return self._busy_result(live_chain, str(repo))
            # Fase 2 — el cierre automático de cada cadena parada: nativo,
            # uno por cadena, con su propia valla (relectura dentro). Un
            # False aquí significa que la cadena despertó — el repo está
            # ocupado y NADA se creó aún.
            closed_chains = []
            for old_id in stopped_chains:
                if self._auto_close_stopped_chain(old_id, store=store):
                    closed_chains.append(old_id)
                else:
                    return self._busy_result(old_id, str(repo))
            # Fase 3 — BEGIN IMMEDIATE serializa dos starts concurrentes del
            # mismo repo. Cualquier cadena activa que este start no haya
            # cerrado él mismo bloquea: o despertó entre fases, o es la
            # raíz recién insertada por el start ganador — en ambos casos
            # busy, sin crear nada (la marca la juzgó la fase 1 y la valla
            # del cierre la releyó la fase 2).
            with write_txn(conn):
                for active in list_tasks(conn, include_archived=True):
                    if not (active.body or "").startswith(_CONTRACT_MARKER):
                        continue
                    if active.status in {"done", "archived"}:
                        continue
                    if active.idempotency_key == idempotency_key:
                        continue
                    try:
                        active_repo = (
                            Path(active.workspace_path or "").expanduser().resolve()
                        )
                    except OSError:
                        continue
                    if active.workspace_kind == "dir" and active_repo == repo:
                        return self._busy_result(active.id, str(repo))
                root_id = create_task(
                    conn,
                    title=f"Engineering: {instruction[:120]}",
                    body=body,
                    assignee="claw",
                    created_by=self.actor_role,
                    workspace_kind="dir",
                    workspace_path=str(repo),
                    triage=True,
                    idempotency_key=idempotency_key,
                )

            root = get_task(conn, root_id)
            if root is None:
                raise WorkflowError("native Kanban did not persist the workflow root")
            # La comparación es a contrato canónico: el worktree de la
            # cadena (r-H-cadena-worktree) y la rama base del PR
            # (H-entrega-pr) viajan en el cuerpo guardado y no cuentan como
            # contrato distinto al relanzar la misma clave.
            stored = self._contract_of(root)
            if {
                k: v for k, v in stored.items() if k not in ("worktree", "base_branch")
            } != contract:
                raise WorkflowError(
                    "request_key already belongs to a different contract; use a new request_key"
                )

            # Punto 1: si el repository es un repo git, la cadena trabaja en
            # SU worktree (rama cadena/<id>), jamás en el checkout original.
            worktree = self._ensure_worktree(root_id, str(repo), revision)
            if worktree is not None and "worktree" not in stored:
                stored["worktree"] = worktree
                # H-entrega-pr: la base del PR es la rama de la que nacio la
                # cadena, tal como estaba el checkout original EN ESTE
                # MOMENTO (no la rama por defecto del remoto).
                stored["base_branch"] = self._rama_base_del_repo(str(repo))
                with write_txn(conn):
                    conn.execute(
                        "UPDATE tasks SET body = ? WHERE id = ?",
                        (_contract_body(stored), root_id),
                    ).close()

            child_ids = self._decomposed_child_ids(conn, root_id)
            if child_ids is None:
                child_ids = decompose_triage_task(
                    conn,
                    root_id,
                    root_assignee="claw",
                    children=self._initial_children(root_id, stored),
                    author=self.actor_role,
                )
                if child_ids is None:
                    child_ids = self._decomposed_child_ids(conn, root_id)
            if child_ids is None or len(child_ids) != len(INITIAL_STAGES):
                raise WorkflowError(
                    "workflow root exists without a complete native stage graph"
                )
            result = self._start_result(root_id, child_ids)
            if worktree is not None:
                result["worktree"] = worktree
            result["closed_chains"] = sorted(closed_chains)
            return result

    def _ensure_worktree(
        self, root_id: str, repository: str, revision: str
    ) -> str | None:
        """Create the chain's own worktree (r-H-cadena-worktree punto 1).

        Un repo git recibe una rama propia ``cadena/<workflow_id>`` en un
        worktree aparte, bajo la raíz configurable GONCLOUD_WORKTREE_ROOT
        (por defecto la carpeta hermana ``wt`` del repo). El checkout
        original queda solo como origen: ni checkout, ni commit, ni reset.
        Si ``repository`` no es un repo git, no hay worktree — conducta de
        hoy (punto 4). Idempotente: la misma clave reusa el worktree.
        """
        try:
            probe = subprocess.run(
                ["git", "-C", repository, "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except OSError:
            return None
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return None
        repo_path = Path(repository)
        root = os.environ.get("GONCLOUD_WORKTREE_ROOT") or str(repo_path.parent / "wt")
        wt_path = Path(root) / f"{repo_path.name}-{root_id}"
        branch = f"cadena/{root_id}"
        if wt_path.is_dir() and (wt_path / ".git").exists():
            # Segunda llamada con la misma clave: el worktree ya es suyo.
            return str(wt_path)
        attempt = subprocess.run(
            [
                "git",
                "-C",
                repository,
                "worktree",
                "add",
                "-b",
                branch,
                str(wt_path),
                revision,
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if attempt.returncode != 0:
            # La revisión explícita puede no existir como commit (una
            # etiqueta de contrato, p. ej.): el worktree nace del HEAD del
            # repo, que es el estado que la cadena replicaría.
            attempt = subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(wt_path),
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
        if attempt.returncode != 0:
            raise WorkflowError(
                "git worktree add failed for chain "
                f"{root_id}: {(attempt.stderr or attempt.stdout).strip()[:300]}"
            )
        return str(wt_path)

    def _rama_base_del_repo(self, repository: str) -> str | None:
        """La rama de la que nace la cadena: HEAD del checkout original.

        El trabajo no siempre va contra la rama por defecto (en
        goncloud-hermes va contra feat/hermes-team): lo que vale es la rama
        que estaba cargada EN ESTE MOMENTO. En detached (HEAD pelado) el
        respaldo es la rama por defecto del remoto; sin ninguna, la cadena
        nace sin rama base y el PR la deduce despues.
        """
        try:
            hecho = subprocess.run(
                ["git", "-C", repository, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except OSError:
            return None
        rama = hecho.stdout.strip() if hecho.returncode == 0 else ""
        if rama and rama != "HEAD":
            return rama
        try:
            simbolico = subprocess.run(
                ["git", "-C", repository, "symbolic-ref", "refs/remotes/origin/HEAD"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except OSError:
            return None
        if simbolico.returncode == 0:
            salida = simbolico.stdout.strip()
            if salida.startswith("refs/remotes/origin/"):
                return salida[len("refs/remotes/origin/") :] or None
        return None

    def cleanup_chain_worktree(
        self, contract: dict[str, Any], workflow_id: str | None = None
    ) -> dict[str, Any] | None:
        """Close-side worktree handling (r-H-cadena-worktree punto 3).

        Con commits propios (revision..HEAD) el worktree se CONSERVA —
        ahí vive el trabajo de la cadena. Limpio y sin commits propios se
        quita con git worktree remove. Sucio sin commits propios también
        se conserva: borrarlo tiraría cambios del dueño.
        """
        worktree = contract.get("worktree")
        if not worktree:
            return None
        info: dict[str, Any] = {
            "path": worktree,
            "branch": f"cadena/{workflow_id or contract.get('workflow_id', '')}",
            "removed": False,
        }

        def git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["git", "-C", worktree, *args],
                capture_output=True,
                text=True,
                timeout=120,
            )

        if not Path(worktree).is_dir():
            info["reason"] = "el worktree ya no existe"
            return info
        revision = contract.get("revision") or "HEAD"
        own = git("rev-list", f"{revision}..HEAD", "--count")
        own_commits = (
            own.returncode == 0
            and own.stdout.strip().isdigit()
            and int(own.stdout.strip()) > 0
        )
        if own_commits:
            info["reason"] = "tiene commits sin publicar"
            return info
        dirty = git("status", "--porcelain")
        if dirty.stdout.strip():
            info["reason"] = "tiene cambios sin commit"
            return info
        removed = subprocess.run(
            ["git", "-C", str(contract["repository"]), "worktree", "remove", worktree],
            capture_output=True,
            text=True,
            timeout=120,
        )
        info["removed"] = removed.returncode == 0
        if not info["removed"]:
            info["reason"] = (removed.stderr or removed.stdout).strip()[:200]
        return info

    def _busy_result(self, live_id: str, repo: str) -> dict[str, Any]:
        """The busy answer (contrato r6): the repository already has a live
        chain — nothing was created, and the response tells claw which
        chain to relaunch when its aviso arrives."""
        return {
            "busy": True,
            "repo": repo,
            "cadena_viva": live_id,
            "mensaje": (
                f"El repo ya tiene la cadena {live_id} trabajando; "
                "cuando termine te llega su aviso y la relanzas"
            ),
        }

    @staticmethod
    def _subscription_store_checked() -> Any:
        """The subscription store for lease evidence, or a sentinel.

        A missing store file means no router ever leased anything here: that
        is checked ground, not a failure (None). A store that cannot be
        opened is unchecked ground — ``_StoreUnavailable`` — and the caller
        treats every chain as possibly alive instead of archiving without
        lease evidence.
        """
        path = default_db_path()
        if not path.exists():
            return None
        try:
            return SubscriptionStateStore(path)
        except Exception:
            return _StoreUnavailable()

    def _foreign_db_alive(self, conn: Any, active: Any, *, store: Any) -> bool:
        """The database half of another chain's liveness verdict.

        Decided from Kanban and lease state only — never from a driver
        mark — so start holds no ps subprocess under the Kanban write lock
        (review F15). True when the chain is live work on DB evidence
        alone; False leaves the verdict to the driver mark, which start
        reads outside the transaction.
        """
        if isinstance(store, _StoreUnavailable):
            return True
        if active.status == "running":
            return True
        initial_ids = self._decomposed_child_ids(conn, active.id) or []
        correction = self._correction_ids(conn, active.id)
        if any(
            self._require_task(conn, task_id).status == "running"
            for task_id in [*initial_ids, *correction.values()]
        ):
            return True
        if store is not None and store.active_leases(active.id):
            return True
        return False

    def _auto_close_stopped_chain(self, workflow_id: str, *, store: Any) -> bool:
        """Free a chain whose driver already stopped; True when this call did.

        Reglas simples del encargo H (B y D). The stop evidence is only the
        driver's own: a stopped outcome as the result file's last write, or
        a running mark whose pid is gone — a triage root without children is
        a chain being born, never evidence (F8). A missing subscription
        store is checked ground — no router ever
        leased here, so the decision rests on the mark alone; only a store
        that cannot be opened leaves the chain alone. The pid resolution
        (ps) and the lease check run BEFORE the transaction (review F15);
        inside it the chain state is re-read and the mark fence compares
        run_id and outcome against what was judged outside — anything that
        changed aborts the close before the first archive lands, and the
        caller then queues the new chain behind it (review F3).
        ``archive_task`` opens its own transaction and refuses nesting
        (reference 0.22), so the archive rides this transaction with the
        same guarded, idempotent UPDATE the native primitive applies.
        """
        if isinstance(store, _StoreUnavailable):
            return False
        # Outside the transaction (review F15): the mark, the pid it names
        # (ps) and the lease check — no subprocess under the Kanban write
        # lock. The fence inside compares this snapshot against the file.
        try:
            from .local_orchestrator import read_driver_mark
        except ImportError:
            from local_orchestrator import read_driver_mark
        seen = read_driver_mark(workflow_id)
        alive, stopped = _driver_signals(workflow_id)
        if alive or not stopped:
            return False
        if store is not None and store.active_leases(workflow_id):
            return False
        with self._connect() as conn:
            with write_txn(conn):
                root = self._require_workflow_root(conn, workflow_id)
                if root.status not in CLOSEABLE_ROOT_STATUSES:
                    return False
                initial_ids = self._decomposed_child_ids(conn, workflow_id) or []
                correction = self._correction_ids(conn, workflow_id)
                members = [(workflow_id, root.status)] + [
                    (task_id, self._require_task(conn, task_id).status)
                    for task_id in [*initial_ids, *correction.values()]
                ]
                if any(status == "running" for _, status in members):
                    return False
                # The fence (review F15): the file's last write is still
                # the one judged outside. A new write means a driver woke
                # or the state moved — the verdict is stale, abort.
                fresh = read_driver_mark(workflow_id)
                if (fresh or {}).get("outcome") != (seen or {}).get("outcome") or (
                    fresh or {}
                ).get("run_id") != (seen or {}).get("run_id"):
                    return False
                for member_id, member_status in members:
                    # Done stages are the chain's evidence: they stay as
                    # they are; archived ones are already closed — the
                    # guarded UPDATE makes a re-run a no-op.
                    if member_status in {"archived", "done"}:
                        continue
                    cursor = conn.execute(
                        "UPDATE tasks SET status = 'archived'"
                        " WHERE id = ? AND status != 'archived'",
                        (member_id,),
                    )
                    if cursor.rowcount != 1:
                        raise WorkflowError(
                            f"native Kanban refused to archive {member_id}"
                        )
                    # The same native audit trail archive_task leaves
                    # (review F14): one "archived" event per member, inside
                    # this same transaction, so both close paths leave
                    # identical rastros.
                    conn.execute(
                        "INSERT INTO task_events (task_id, kind, payload_json,"
                        " created_at) VALUES (?, 'archived', ?, ?)",
                        (
                            member_id,
                            json.dumps({}, sort_keys=True),
                            int(time.time()),
                        ),
                    ).close()
                comment = CLOSE_COMMENT_MARKER + json.dumps(
                    {"reason": AUTO_CLOSE_REASON, "workflow_id": workflow_id},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if not any(
                    item.body.startswith(CLOSE_COMMENT_MARKER)
                    for item in list_comments(conn, workflow_id)
                ):
                    add_comment(conn, workflow_id, "claw", comment)
            contract = self._contract_of(self._require_workflow_root(conn, workflow_id))
        # Punto 3: al cerrar la cadena, su worktree se conserva si tiene
        # commits propios y se quita si está limpio — mejor esfuerzo: un
        # fallo de limpieza no deshace el cierre.
        try:
            self.cleanup_chain_worktree(contract, workflow_id)
        except WorkflowError:
            pass
        return True

    def start_contract(self, *, request_key: str) -> dict | None:
        """Read the original provenance for an idempotent local start."""
        self._require_actor("claw")
        key = _WORKFLOW_KEY_PREFIX + _required_text(request_key, "request_key")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM tasks WHERE idempotency_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            return self._contract_of(self._require_workflow_root(conn, row[0]))

    @staticmethod
    def _validate_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        if isinstance(sources, (str, bytes, dict)):
            raise WorkflowError("sources must be a non-empty list of source records")
        try:
            raw_rows = list(sources)
        except TypeError as exc:
            raise WorkflowError(
                "sources must be a non-empty list of source records"
            ) from exc
        if not raw_rows:
            raise WorkflowError("sources must contain at least one source record")
        rows: list[dict[str, str]] = []
        for index, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                raise WorkflowError(f"sources[{index}] must be an object")
            rows.append(
                {
                    "reference": _required_text(
                        row.get("reference"), f"sources[{index}].reference"
                    ),
                    "observed_at": _required_text(
                        row.get("observed_at"), f"sources[{index}].observed_at"
                    ),
                }
            )
        return rows

    @staticmethod
    def _initial_children(
        root_id: str, contract: dict[str, Any]
    ) -> list[dict[str, Any]]:
        workspace = contract.get("worktree") or contract["repository"]
        common = (
            f"Goncloud engineering workflow {root_id}. Work only in the exact repository "
            f"{workspace}. The owner-approved baseline revision is "
            f"{contract['revision']}. Preserve the shared workspace and record the exact current "
            "revision (or dirty-tree state) in every native Kanban handoff so downstream roles "
            "verify the artifact actually produced. "
        )
        stage_directions = {
            "scout": "Investigate the code and constraints; report evidence, do not implement.",
            "ingenieria": (
                "Produce the technical plan and reconcile peer consultation findings. "
                "Plan only: do not create, edit or delete files and do not commit; "
                "the implementer stage makes the change, and an edited repository "
                "blocks this stage."
            ),
            "implementer": "Implement the approved change and its regression tests.",
            "verifier": (
                "Verify the requested behavior with fresh evidence. Also perform business-rule "
                "acceptance."
                if contract["affects_business_rules"]
                else "Verify the requested behavior with fresh evidence."
            ),
            "adversary": "Find risks and counterexamples; report findings and do not fix them.",
            "reviewer": (
                "Adjudicate findings with goncloud_engineering_review. A first high/critical "
                "rejection may open one corrective review; other unresolved material findings block."
            ),
            "entrega": (
                "Package the verified handoff. Do not deliver unless every dependency and review "
                "gate is complete; do not expand deployment or external-send authority."
            ),
        }
        children = []
        for index, stage in enumerate(INITIAL_STAGES):
            children.append(
                {
                    "title": f"[{stage}] {contract['instruction'][:120]}",
                    "body": common + stage_directions[stage],
                    "assignee": stage,
                    "parents": [] if index == 0 else [index - 1],
                }
            )
        return children

    @staticmethod
    def _decomposed_child_ids(conn: Any, root_id: str) -> list[str] | None:
        for event in list_events(conn, root_id):
            if event.kind != "decomposed" or not isinstance(event.payload, dict):
                continue
            ids = event.payload.get("child_ids")
            if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                return ids
        return None

    @staticmethod
    def _start_result(root_id: str, child_ids: list[str]) -> dict[str, Any]:
        return {
            "workflow_id": root_id,
            "stages": dict(zip(INITIAL_STAGES, child_ids)),
        }

    def status(self, *, workflow_id: str) -> dict[str, Any]:
        """Return compact durable state without changing the board."""
        self._require_known_actor()
        workflow_id = _required_text(workflow_id, "workflow_id")
        with self._connect() as conn:
            root = self._require_workflow_root(conn, workflow_id)
            initial_ids = self._decomposed_child_ids(conn, workflow_id)
            if initial_ids is None or len(initial_ids) != len(INITIAL_STAGES):
                raise WorkflowError("workflow stage graph is incomplete")
            initial = {
                stage: _task_view(self._require_task(conn, task_id))
                for stage, task_id in zip(INITIAL_STAGES, initial_ids)
            }
            corrective_ids = self._correction_ids(conn, workflow_id)
            corrective = {
                stage: _task_view(self._require_task(conn, task_id))
                for stage, task_id in corrective_ids.items()
            }
            return {
                "workflow_id": workflow_id,
                "root": _task_view(root),
                "stages": initial,
                "corrective_stages": corrective,
                "delivery_blocked": initial["entrega"]["status"]
                not in {"ready", "running", "done"},
            }

    def consult(
        self, *, task_id: str, target_role: str, question: str, request_key: str
    ) -> dict[str, Any]:
        """Create one durable peer card and gate the source until it completes."""
        self._require_known_actor()
        task_id = _required_text(task_id, "task_id")
        target_role = _required_text(target_role, "target_role")
        question = _required_text(question, "question")
        request_key = _required_text(request_key, "request_key")
        if target_role not in TEAM_ROLES:
            raise WorkflowError(f"unknown consultation target role: {target_role}")
        if target_role == self.actor_role:
            raise WorkflowError("consultation target must be a different role")

        idempotency_key = f"goncloud-consult:{task_id}:{request_key}"
        body = (
            f"Peer consultation requested by {self.actor_role} for native task {task_id}.\n"
            f"Question: {question}\n"
            "Complete this consultation with a concise evidence-backed summary; the source task "
            "is dependency-gated on this card."
        )
        with self._connect() as conn:
            source = self._require_task(conn, task_id)
            if source.assignee != self.actor_role:
                raise WorkflowError(
                    "the bound role may consult only for a task assigned to it"
                )
            if source.status not in {"ready", "running", "todo"}:
                raise WorkflowError(
                    f"task {task_id} cannot consult from status {source.status}"
                )
            consultation_id = create_task(
                conn,
                title=f"Consult {target_role}: {question[:100]}",
                body=body,
                assignee=target_role,
                created_by=self.actor_role,
                workspace_kind=source.workspace_kind,
                workspace_path=source.workspace_path,
                tenant=source.tenant,
                idempotency_key=idempotency_key,
                creator_task_id=task_id,
            )
            consultation = self._require_task(conn, consultation_id)
            if consultation.body != body or consultation.assignee != target_role:
                raise WorkflowError(
                    "consultation request_key already belongs to a different consultation"
                )
            if consultation_id not in parent_ids(conn, task_id):
                link_tasks(conn, consultation_id, task_id)
            source = self._require_task(conn, task_id)
            if source.status == "running":
                block_task(
                    conn,
                    task_id,
                    reason=f"waiting for consultation {consultation_id}",
                    kind="dependency",
                )
            return {
                "source_task_id": task_id,
                "consultation_task_id": consultation_id,
                "target_role": target_role,
                "source_status": self._require_task(conn, task_id).status,
            }

    def subscription_run(
        self,
        *,
        workflow_id: str,
        task_id: str,
        instruction: str,
        repository: str,
        revision: str,
        sources: Iterable[dict[str, Any]],
        max_steps: int,
        result_transform=None,
    ) -> dict[str, Any]:
        """Run the current stage through the profile's subscription routes."""
        self._require_known_actor()
        workflow_id = _required_text(workflow_id, "workflow_id")
        task_id = _required_text(task_id, "task_id")
        instruction = _required_text(instruction, "instruction")
        revision = _required_text(revision, "revision")
        if type(max_steps) is not int or not 1 <= max_steps <= 200:
            raise WorkflowError("max_steps must be an integer between 1 and 200")
        source_rows = self._validate_sources(sources)
        del source_rows  # provenance is validated, not forwarded to vendors
        assignment = dict(
            workflow_id=workflow_id,
            task_id=task_id,
            repository=repository,
            revision=revision,
        )
        repo, fingerprint = self._subscription_assignment_snapshot(**assignment)

        def authorize() -> None:
            try:
                _, current = self._subscription_assignment_snapshot(**assignment)
            except Exception:
                raise WorkflowError(
                    "subscription assignment authorization changed; request refused"
                ) from None
            if current != fingerprint:
                raise WorkflowError(
                    "subscription assignment authorization changed; request refused"
                )

        router = _build_subscription_router()
        routes = compile_profile_routes(self.actor_role, router.routing_source)
        request = InvocationRequest(
            workflow_id=workflow_id,
            stage_id=task_id,
            role=self.actor_role,
            prompt=instruction,
            workspace=repo,
            start_revision=revision,
            current_revision=revision,
            max_steps=max_steps,
            pre_invoke=authorize,
            result_transform=result_transform,
        )
        authorize()
        result = sanitize_result(router.run(request, routes), (instruction,))
        evidence = {
            "workflow_id": workflow_id,
            "stage_id": task_id,
            "profile": self.actor_role,
            "slot": None,
            "success": result.success,
            "adapter": result.adapter,
            "model": result.model,
            "billing_mode": result.billing_mode,
            "failure": result.failure.value if result.failure else None,
            "session_id": result.session_id,
            "text": (result.text or "")[:2000],
        }
        attempts = []
        store = getattr(router, "store", None)
        if store is not None:
            for attempt in store.stage_attempts(workflow_id, task_id):
                attempts.append(
                    {
                        "slot": attempt["slot"],
                        "adapter": attempt["adapter"],
                        "model": attempt["model"],
                        "billing_mode": attempt["billing_mode"],
                        "mode": attempt["mode"],
                        "success": attempt["success"],
                        "failure": attempt["failure"],
                    }
                )
                if attempt["success"]:
                    evidence["slot"] = attempt["slot"]
        evidence["attempts"] = attempts
        self.attach_subscription_result(evidence)
        return evidence

    def validate_subscription_assignment(
        self, *, workflow_id: str, task_id: str, repository: str, revision: str
    ) -> Path:
        """Authorize an executable stage before constructing any vendor routes."""
        repo, _ = self._subscription_assignment_snapshot(
            workflow_id=workflow_id,
            task_id=task_id,
            repository=repository,
            revision=revision,
        )
        return repo

    def _subscription_assignment_snapshot(
        self, *, workflow_id: str, task_id: str, repository: str, revision: str
    ) -> tuple[Path, bytes]:
        """Read authorization and its fingerprint in one short transaction.

        The pre-invoke snapshot is the authorization linearization point.
        Release the Kanban lock before inference so workers can use Kanban.
        """
        self._require_known_actor()
        workflow_id = _required_text(workflow_id, "workflow_id")
        task_id = _required_text(task_id, "task_id")
        revision = _required_text(revision, "revision")
        repo_input = _required_text(repository, "repository")
        repo = Path(repo_input).expanduser()
        if not repo.is_absolute():
            raise WorkflowError("repository must be an absolute path")
        try:
            repo = repo.resolve(strict=True)
        except OSError as exc:
            raise WorkflowError(f"repository is unavailable: {exc}") from exc
        if not repo.is_dir():
            raise WorkflowError("repository must be an existing directory")

        with self._connect() as conn, write_txn(conn):
            root = self._require_workflow_root(conn, workflow_id)
            if root.assignee != "claw" or root.status not in {
                "todo",
                "ready",
                "running",
            }:
                raise WorkflowError("director is not executable or assigned to claw")
            task = self._require_task(conn, task_id)
            if task.assignee != self.actor_role:
                raise WorkflowError(
                    f"task {task_id} is not assigned to role {self.actor_role}"
                )
            membership = self._require_subscription_membership(
                conn, workflow_id, task_id
            )
            if task.status not in {"ready", "running"}:
                raise WorkflowError(
                    f"task {task_id} cannot execute from status {task.status}"
                )
            contract = self._contract_of(root)
            origin = Path(contract["repository"]).expanduser().resolve()
            # r-H-cadena-worktree: la cadena trabaja en SU worktree; el
            # repositorio original es el origen y no se toca.
            contract_worktree = (
                Path(contract["worktree"]).expanduser().resolve()
                if contract.get("worktree")
                else None
            )
            allowed = {origin} | (
                {contract_worktree} if contract_worktree is not None else set()
            )
            for member in (root, task):
                if member.workspace_kind != "dir" or not member.workspace_path:
                    raise WorkflowError(
                        f"task {member.id} workspace does not match repository"
                    )
                try:
                    task_repo = Path(member.workspace_path).expanduser().resolve()
                except OSError as exc:
                    raise WorkflowError(
                        f"task {member.id} workspace does not match repository: {exc}"
                    ) from exc
                # La tarjeta raíz apunta al repo original y la cadena
                # trabaja en su worktree — ambas rutas son legítimas aquí.
                if task_repo not in allowed:
                    raise WorkflowError(
                        f"task {member.id} workspace does not match repository"
                    )
            # r-H-cadena-worktree: los workspaces legítimos de la asignación
            # son el original y el worktree de la cadena; cualquier otro es
            # un error de workspace, como siempre fue (el motivo de hoy).
            if repo not in allowed:
                raise WorkflowError(
                    f"task {task_id} workspace does not match repository"
                )
            if contract.get("revision") != revision:
                raise WorkflowError("revision does not match the workflow contract")
            authorization = [
                self.actor_role,
                root.idempotency_key,
                membership,
                *[
                    [
                        member.id,
                        member.assignee,
                        member.status,
                        member.workspace_kind,
                        member.workspace_path,
                        member.body,
                    ]
                    for member in (root, task)
                ],
            ]
            fingerprint = hashlib.sha256(
                json.dumps(authorization, ensure_ascii=False).encode("utf-8")
            ).digest()
        return repo, fingerprint

    def _require_subscription_membership(
        self, conn, workflow_id, stage_id
    ) -> tuple[str, ...]:
        initial_ids = self._decomposed_child_ids(conn, workflow_id) or []
        corrective = self._correction_ids(conn, workflow_id)
        membership = tuple(sorted(set(initial_ids) | set(corrective.values())))
        if stage_id not in membership:
            raise WorkflowError(
                f"stage identity mismatch: task {stage_id} is not a stage "
                f"of workflow {workflow_id}"
            )
        return membership

    def subscription_status(
        self,
        *,
        workflow_id: str | None = None,
        stage_id: str | None = None,
    ) -> dict[str, Any]:
        """Non-generative route matrix for the calling profile."""
        self._require_known_actor()
        router = _build_subscription_router()
        return router.status(
            self.actor_role, workflow_id=workflow_id, stage_id=stage_id
        )

    def attach_subscription_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Record durable subscription evidence on its own stage, or refuse."""
        self._require_known_actor()
        if not isinstance(result, dict):
            raise WorkflowError("subscription result must be an object")
        workflow_id = _required_text(result.get("workflow_id"), "workflow_id")
        stage_id = _required_text(result.get("stage_id"), "stage_id")
        with self._connect() as conn:
            self._require_workflow_root(conn, workflow_id)
            task = self._require_task(conn, stage_id)
            if (
                task.assignee != self.actor_role
                or result.get("profile") != self.actor_role
            ):
                raise WorkflowError(
                    f"stage identity mismatch: task {stage_id} belongs to "
                    f"{task.assignee}, not {self.actor_role}"
                )
            self._require_subscription_membership(conn, workflow_id, stage_id)
            record = redact_data(
                {
                    key: result.get(key)
                    for key in _SUBSCRIPTION_EVIDENCE_KEYS
                    if result.get(key) is not None
                }
            )
            body = _SUBSCRIPTION_MARKER + json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            if any(comment.body == body for comment in list_comments(conn, stage_id)):
                return {"attached": False, "duplicate": True, "stage_id": stage_id}
            add_comment(conn, stage_id, self.actor_role, body)
        return {"attached": True, "stage_id": stage_id}

    @staticmethod
    def _contract_of(root: Any) -> dict[str, Any]:
        try:
            return json.loads((root.body or "")[len(_CONTRACT_MARKER) :])
        except json.JSONDecodeError as exc:
            raise WorkflowError(
                f"workflow {root.id} has a malformed source contract"
            ) from exc

    def guard_native_completion(self, task_id: str) -> dict[str, str] | None:
        """Veto a reviewer card bypassing the durable adjudication tool.

        This is consumed by the plugin's ``pre_tool_call`` hook. Other roles
        and ordinary Kanban cards are deliberately outside this policy.
        """
        if self.actor_role != "reviewer":
            return None
        task_id = _required_text(task_id, "task_id")
        with self._connect() as conn:
            task = get_task(conn, task_id)
            if task is None or task.assignee != "reviewer":
                return None
            workflow_id = self._workflow_for_reviewer_task(conn, task_id)
            if workflow_id is None:
                return None
            return {
                "action": "block",
                "message": (
                    "Goncloud reviewer cards cannot use kanban_complete directly. "
                    "Adjudicate with goncloud_engineering_review."
                ),
            }

    def review(
        self,
        *,
        workflow_id: str,
        task_id: str,
        decision: str,
        summary: str,
        severity: str | None = None,
        local_handoff: dict | None = None,
    ) -> dict[str, Any]:
        """Adjudicate review and enforce the hard two-round ceiling."""
        self._require_actor("reviewer")
        workflow_id = _required_text(workflow_id, "workflow_id")
        task_id = _required_text(task_id, "task_id")
        decision = _required_text(decision, "decision").lower()
        summary = _required_text(summary, "summary")
        if decision not in {"approve", "reject"}:
            raise WorkflowError("decision must be approve or reject")
        severity_value = str(severity or "").strip().lower() or None
        if decision == "reject" and severity_value not in {
            "low",
            "medium",
            "high",
            "critical",
        }:
            raise WorkflowError(
                "a rejection requires severity low, medium, high, or critical"
            )

        with self._connect() as conn:
            root = self._require_workflow_root(conn, workflow_id)
            initial_ids = self._decomposed_child_ids(conn, workflow_id)
            if initial_ids is None or len(initial_ids) != len(INITIAL_STAGES):
                raise WorkflowError("workflow stage graph is incomplete")
            initial = dict(zip(INITIAL_STAGES, initial_ids))
            correction = self._correction_ids(conn, workflow_id)
            if task_id == initial["reviewer"]:
                round_number = 1
            elif correction.get("reviewer") == task_id:
                round_number = 2
            else:
                raise WorkflowError("task_id is not a reviewer stage in this workflow")
            task = self._require_task(conn, task_id)
            if task.assignee != "reviewer":
                raise WorkflowError("review task is not assigned to role reviewer")

            requested = {
                "decision": decision,
                "severity": severity_value,
                "summary": summary,
                "round": round_number,
            }
            existing = self._recorded_review_run(conn, task_id)
            if existing is not None:
                recorded_request = {key: existing.get(key) for key in requested}
                if recorded_request != requested:
                    raise WorkflowError(
                        "this review task already has a different durable decision"
                    )
                result = existing.get("result")
                if not isinstance(result, dict):
                    raise WorkflowError("native review run metadata is malformed")
                return result

            if task.status == "done":
                raise WorkflowError(
                    "review task completed without Goncloud review metadata; delivery must be audited"
                )
            if task.status == "blocked":
                if decision != "reject":
                    raise WorkflowError(
                        "a blocked rejected review cannot be changed to approval"
                    )
                return {
                    "outcome": "blocked",
                    "round": round_number,
                    "task_id": task_id,
                    "reason": "unresolved material review finding",
                }
            if task.status not in {"ready", "running"}:
                raise WorkflowError(
                    f"review task dependencies are incomplete or task is closed ({task.status})"
                )

            if (
                round_number == 1
                and correction
                and not (decision == "reject" and severity_value in HIGH_SEVERITIES)
            ):
                raise WorkflowError(
                    "corrective round already exists; the first high-severity rejection is durable"
                )

            if decision == "approve":
                result = {
                    "outcome": "approved",
                    "round": round_number,
                    "task_id": task_id,
                }
            elif round_number == 1 and severity_value in HIGH_SEVERITIES:
                correction = self._ensure_corrective_round(
                    conn, root, initial, finding=summary, severity=severity_value
                )
                result = {
                    "outcome": "corrective_round_created",
                    "round": 1,
                    "task_id": task_id,
                    "corrective_stages": correction,
                }
            else:
                result = {
                    "outcome": "blocked",
                    "round": round_number,
                    "task_id": task_id,
                    "reason": "unresolved material review finding",
                }

            record = {**requested, "task_id": task_id, "result": result}
            self._add_review_audit(conn, workflow_id, record)
            if local_handoff is None:
                self._finish_recorded_review(conn, task_id, record)
            else:
                with self.local_transition(
                    workflow_id=workflow_id, task_id=task_id, connection=conn
                ):
                    self._finish_recorded_review(conn, task_id, record, local_handoff)
            return result

    @staticmethod
    def _finish_recorded_review(
        conn: Any,
        task_id: str,
        record: dict[str, Any],
        local_handoff: dict | None = None,
    ) -> None:
        task = get_task(conn, task_id)
        if task is None:
            raise WorkflowError(f"unknown task {task_id}")
        result = record["result"]
        summary = record["summary"]
        outcome = result.get("outcome")
        if outcome == "blocked":
            if task.status == "blocked":
                return
            if not block_task(conn, task_id, reason=summary, kind="needs_input"):
                raise WorkflowError(
                    "native Kanban refused to block the rejected review"
                )
            return
        if task.status == "done":
            return
        metadata = {"goncloud_review": record}
        if local_handoff is not None:
            metadata["goncloud_local"] = local_handoff
        if not complete_task(
            conn,
            task_id,
            result=(
                "review approved"
                if outcome == "approved"
                else "review rejected; correction opened"
            ),
            summary=summary,
            metadata=metadata,
            expected_run_id=task.current_run_id,
        ):
            raise WorkflowError("native Kanban refused the review transition")

    def _ensure_corrective_round(
        self,
        conn: Any,
        root: Any,
        initial: dict[str, str],
        *,
        finding: str,
        severity: str,
    ) -> dict[str, str]:
        ids: dict[str, str] = {}
        parent = initial["reviewer"]
        for stage in CORRECTIVE_STAGES:
            key = f"{_WORKFLOW_KEY_PREFIX}{root.id}:round2:{stage}"
            body = (
                f"Corrective review round 2 for workflow {root.id}. Severity: {severity}.\n"
                f"Finding: {finding}\n"
                f"Use the exact shared repository {root.workspace_path}, and record the exact current "
                "revision or dirty-tree state in the handoff. This is the final permitted review "
                "round; unresolved material findings must block entrega."
            )
            task_id = create_task(
                conn,
                title=f"[round 2 {stage}] {root.title[:100]}",
                body=body,
                assignee=stage,
                created_by="reviewer",
                workspace_kind=root.workspace_kind,
                workspace_path=root.workspace_path,
                tenant=root.tenant,
                parents=(parent,),
                idempotency_key=key,
                creator_task_id=root.id,
            )
            task = self._require_task(conn, task_id)
            if task.body != body or task.assignee != stage:
                raise WorkflowError(
                    "corrective stage idempotency key has conflicting content"
                )
            ids[stage] = task_id
            parent = task_id
        delivery_id = initial["entrega"]
        if ids["reviewer"] not in parent_ids(conn, delivery_id):
            link_tasks(conn, ids["reviewer"], delivery_id)
        return ids

    @staticmethod
    def _recorded_review_run(conn: Any, task_id: str) -> dict[str, Any] | None:
        """Read plugin-produced native run metadata; comments are audit-only."""
        for run in reversed(list_runs(conn, task_id, include_active=False)):
            metadata = run.metadata
            if not isinstance(metadata, dict):
                continue
            record = metadata.get("goncloud_review")
            if isinstance(record, dict):
                return record
        return None

    @staticmethod
    def _add_review_audit(conn: Any, workflow_id: str, record: dict[str, Any]) -> None:
        body = _REVIEW_MARKER + json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if any(comment.body == body for comment in list_comments(conn, workflow_id)):
            return
        add_comment(conn, workflow_id, "reviewer", body)

    @staticmethod
    def _correction_ids(conn: Any, workflow_id: str) -> dict[str, str]:
        expected = {
            f"{_WORKFLOW_KEY_PREFIX}{workflow_id}:round2:{stage}": stage
            for stage in CORRECTIVE_STAGES
        }
        found: dict[str, str] = {}
        for task in list_tasks(conn, include_archived=True):
            stage = expected.get(task.idempotency_key or "")
            if stage:
                found[stage] = task.id
        return {stage: found[stage] for stage in CORRECTIVE_STAGES if stage in found}

    def _workflow_for_reviewer_task(
        self, conn: Any, reviewer_task_id: str
    ) -> str | None:
        for candidate in list_tasks(conn, include_archived=True):
            if not (candidate.idempotency_key or "").startswith(_WORKFLOW_KEY_PREFIX):
                continue
            if not (candidate.body or "").startswith(_CONTRACT_MARKER):
                continue
            initial_ids = self._decomposed_child_ids(conn, candidate.id)
            if initial_ids and len(initial_ids) == len(INITIAL_STAGES):
                if (
                    dict(zip(INITIAL_STAGES, initial_ids))["reviewer"]
                    == reviewer_task_id
                ):
                    return candidate.id
            if (
                self._correction_ids(conn, candidate.id).get("reviewer")
                == reviewer_task_id
            ):
                return candidate.id
        return None

    @staticmethod
    def _require_task(conn: Any, task_id: str):
        task = get_task(conn, task_id)
        if task is None:
            raise WorkflowError(f"unknown task {task_id}")
        return task

    def _require_workflow_root(self, conn: Any, workflow_id: str):
        root = self._require_task(conn, workflow_id)
        if not (root.idempotency_key or "").startswith(_WORKFLOW_KEY_PREFIX):
            raise WorkflowError(
                f"task {workflow_id} is not a Goncloud engineering workflow"
            )
        if not (root.body or "").startswith(_CONTRACT_MARKER):
            raise WorkflowError(
                f"workflow {workflow_id} has no supported source contract"
            )
        return root
