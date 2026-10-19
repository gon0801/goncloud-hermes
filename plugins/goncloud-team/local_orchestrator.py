"""Bounded local driver of the native durable engineering workflow."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import hermes_cli
from hermes_cli.kanban_db import (
    add_comment,
    archive_task,
    block_task,
    complete_task,
    list_comments,
    list_runs,
    list_tasks,
    parent_ids,
    unblock_task,
)
from hermes_cli.kanban_db_connect import write_txn

try:
    from .subscriptions.redaction import SENSITIVE_TEXT, usable_secret
    from .subscriptions.router import GitInspector, default_db_path
    from .subscriptions.state import SubscriptionStateStore
    from .workflow import (
        CLOSEABLE_ROOT_STATUSES as _CLOSEABLE_ROOT_STATUSES,
        CLOSE_COMMENT_MARKER as _CLOSE_COMMENT_MARKER,
        CONTRACT_MARKER as _CONTRACT_MARKER_REF,
        CORRECTIVE_STAGES,
        INITIAL_STAGES,
        QUEUED_COMMENT_MARKER as _QUEUED_COMMENT_MARKER,
        WorkflowError,
        WorkflowService,
    )
except ImportError:
    from subscriptions.redaction import SENSITIVE_TEXT, usable_secret
    from subscriptions.router import GitInspector, default_db_path
    from subscriptions.state import SubscriptionStateStore
    from workflow import (
        CLOSEABLE_ROOT_STATUSES as _CLOSEABLE_ROOT_STATUSES,
        CLOSE_COMMENT_MARKER as _CLOSE_COMMENT_MARKER,
        CONTRACT_MARKER as _CONTRACT_MARKER_REF,
        CORRECTIVE_STAGES,
        INITIAL_STAGES,
        QUEUED_COMMENT_MARKER as _QUEUED_COMMENT_MARKER,
        WorkflowError,
        WorkflowService,
    )

MAX_TERMINAL_BYTES = 1800
#: Stages that must leave the repository exactly as they found it. Live
#: 2026-09-28, ingenieria wrote and committed the change itself and scout did
#: the same on another run; instructions alone did not hold.
_READ_ONLY_STAGES = frozenset({"scout", "ingenieria"})

#: Durable driver outcomes that prove the driver process stopped. "paused"
#: and "blocked" are its own checkpoints; "waiting" is a stage whose parents
#: never went ready (resume wrote it and quit, local_orchestrator ~880) and
#: "error" the bounded result of a failed driver (the bootstrap catch writes
#: it, ~141). Live review F5: waiting and error jam the repository exactly
#: like paused (root not done, start mutex holds, close refused), so they
#: count as stop evidence for the hint and the close alike. Live review F8:
#: the result file doubles as the driver's running mark — every start
#: overwrites it with "running" under a fresh run id before any stage, so a
#: stopped outcome is only evidence while it is the file's last write.
_STOPPED_DRIVER_OUTCOMES = frozenset({"paused", "blocked", "waiting", "error"})

#: How far a running mark's started_at may sit from the marked process's own
#: start (encargo H punto 4). The mark keeps seconds precision and the POSIX
#: read of the process start is second-granular elapsed time, so a small
#: tolerance absorbs the rounding; a recycled pid starts much earlier or
#: later than the mark and reads as dead.
_DRIVER_START_TOLERANCE_SECONDS = 5

#: Stages the retry generation never reopens: the reviewer's two-round
#: ceiling and its durable decision are a product contract, not a jam.
_RETRY_EXEMPT_ROLES = frozenset({"reviewer", "round2:reviewer"})

#: The reason is bounded like the other model-supplied texts.
MAX_CLOSE_REASON_CHARS = 1000


def _workspace_fingerprint(repository):
    """HEAD plus every tracked and untracked change, or None without git."""
    try:
        head = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        status = subprocess.run(
            ["git", "-C", str(repository), "status", "--porcelain", "-uall"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if head.returncode != 0 or status.returncode != 0:
        return None
    return head.stdout.strip(), status.stdout


def _edited_by_read_only_stage(role, before, after):
    """Blocked envelope when a planning stage changed the repository."""
    changed = [line[3:] for line in after[1].splitlines()][:5]
    evidence = [f"HEAD {before[0][:12]} -> {after[0][:12]}"] + [
        f"changed: {path}"[:120] for path in changed
    ]
    return {
        "status": "blocked",
        "summary": (
            f"{role} changed the repository, which its stage must not do; the "
            "implementer stage makes changes. Review and revert before resuming."
        ),
        "evidence": evidence,
        "review": None,
    }


MAX_INPUT_BYTES = 24000
#: Same bound the local CLI applies to an assignment read from a file or stdin.
MAX_INSTRUCTION_CHARS = 32768

#: The detached driver: only paths and identifiers reach it through argv, it
#: prints nothing (vendor diagnostics are not a public channel) and writes one
#: bounded JSON result atomically for goncloud_local_status to read.
# The driver runs isolated (-I), so it sees none of the launcher's packages.
# Hermes now selects its third-party dependencies at boot (hermes_bootstrap ->
# pm.environments.activate_dependencies); without that step the plugin import
# died on `ruamel` before writing any result, and every chain stayed at zero
# attempts (live 2026-10-17, t_220bd43c). The imports sit inside the try so an
# unloadable driver still leaves a bounded error result instead of vanishing.
_DRIVER_BOOTSTRAP = (
    "import json, os, sys\n"
    "plugin, hermes, workflow_id, out = sys.argv[1:5]\n"
    "stop_after = sys.argv[5] if len(sys.argv) > 5 and sys.argv[5] else None\n"
    "launch_run_id = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] else None\n"
    "sys.path.insert(0, hermes)\n"
    "try:\n"
    "    try:\n"
    "        import hermes_bootstrap\n"
    "    except ModuleNotFoundError as exc:\n"
    "        if exc.name != 'hermes_bootstrap':\n"
    "            raise\n"
    "    sys.path.insert(0, plugin)\n"
    "    from local_orchestrator import LocalOrchestrator\n"
    "    result = LocalOrchestrator().resume(workflow_id, stop_after=stop_after,\n"
    "                                        launch_run_id=launch_run_id)\n"
    "except Exception:\n"
    "    result = {'workflow_id': workflow_id, 'outcome': 'error',\n"
    "              'error': 'local driver failed; check durable status'}\n"
    "tmp = out + '.%d.tmp' % os.getpid()\n"
    "with open(tmp, 'w', encoding='utf-8') as handle:\n"
    "    json.dump(result, handle, ensure_ascii=True)\n"
    "os.replace(tmp, out)\n"
)


def _default_home():
    local = os.environ.get("LOCALAPPDATA")
    return Path(local) / "hermes" if local else Path.home() / ".hermes"


def _result_file(workflow_id, home):
    return Path(home) / "goncloud-local" / f"{workflow_id}.json"


def _queue_order(task):
    """FIFO by creation (encargo H punto 3): the native ids are t_<n>."""
    try:
        return (0, int(task.id.split("_", 1)[1]))
    except (IndexError, ValueError):
        return (1, task.id)


def _write_result(workflow_id, payload, *, home):
    """Publish one bounded driver result atomically; readers never see half."""
    results = Path(home) / "goncloud-local"
    results.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = results / f"{workflow_id}.{os.getpid()}.{uuid4().hex}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=True), encoding="utf-8")
    os.replace(tmp, results / f"{workflow_id}.json")


def _running_mark(workflow_id, run_id):
    """The mark every driver writes before its first stage (live review F8).

    The result file doubles as the running mark: a start overwrites whatever
    a previous driver left there — a stale stopped outcome was exactly the
    evidence that got live chains archived — with "running" under its own
    run id, and the same run overwrites the mark with its outcome when it
    quits. The pid stays as evidence for telling a dead driver from a live
    one (encargo H punto 4); nothing acted on it before that check existed.
    """
    return {
        "workflow_id": workflow_id,
        "outcome": "running",
        "run_id": run_id,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


class _UncheckedPid:
    """``_process_start_epoch``'s answer for a pid it could not check.

    Review F4: "the check failed" is not "the process is gone". The cause
    rides on the instance so the reader can record it (driver_mark_live
    leaves it on the mark, where goncloud_local_status shows it).
    """

    def __init__(self, cause: str):
        self.cause = cause

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"_UncheckedPid({self.cause!r})"


def _process_start_epoch(pid):
    """Epoch seconds when the process started, or how the check failed.

    POSIX asks ps for the elapsed time (both macOS and Linux serve it; macOS
    has no /proc and no etimes keyword) and derives the start from it.
    Windows asks for the creation time through OpenProcess/GetExitCodeProcess-grade
    handles. Three answers (review F4): the epoch float for a live process,
    None only for a pid proven gone (ps reports no such process, Windows
    ERROR_INVALID_PARAMETER), and an ``_UncheckedPid`` with the cause for
    everything unanswerable — a missing ps, a timed-out query, an access
    denied, an unparsable elapsed time. Only a proven-dead answer may read
    as a stopped driver.
    """
    if type(pid) is not int or pid <= 0:
        return _UncheckedPid("malformed pid")
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                # 87 = ERROR_INVALID_PARAMETER: no such process. Anything
                # else (5 = ERROR_ACCESS_DENIED, ...) is unchecked ground.
                if ctypes.get_last_error() == 87:
                    return None
                return _UncheckedPid(
                    f"OpenProcess refused (error {ctypes.get_last_error()})"
                )
            try:
                creation, exit_time, kernel_time, user_time = (
                    wintypes.FILETIME(),
                    wintypes.FILETIME(),
                    wintypes.FILETIME(),
                    wintypes.FILETIME(),
                )
                if not kernel32.GetProcessTimes(
                    handle,
                    ctypes.byref(creation),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                ):
                    return _UncheckedPid(
                        f"GetProcessTimes refused (error {ctypes.get_last_error()})"
                    )
            finally:
                kernel32.CloseHandle(handle)
            stamp = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            return stamp / 10_000_000 - 11644473600
        except (OSError, AttributeError, ValueError) as exc:
            return _UncheckedPid(f"windows pid check failed ({exc})")
    try:
        finished = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return _UncheckedPid("ps timed out")
    except OSError as exc:
        return _UncheckedPid(f"ps unavailable ({exc})")
    if finished.returncode != 0:
        # ps answers for existing pids only: this one is gone.
        return None
    elapsed = finished.stdout.strip()
    try:
        # [[dd-]hh:]mm:ss — a young process carries no day part.
        day_text, has_days, clock = elapsed.partition("-")
        values = (
            [int(part) for part in clock.split(":")]
            if has_days
            else [int(part) for part in elapsed.split(":")]
        )
        if len(values) == 3:
            hours, minutes, seconds = values
        elif len(values) == 2:
            hours, minutes, seconds = 0, values[0], values[1]
        else:
            raise ValueError(f"unparsable etime {elapsed!r}")
        lived = (
            (int(day_text) if has_days else 0) * 86400
            + hours * 3600
            + minutes * 60
            + seconds
        )
    except ValueError:
        # ps served the pid but not a usable elapsed time: it exists, its
        # start is simply unknown.
        return _UncheckedPid(f"unparsable ps etime {elapsed!r}")
    return time.time() - max(lived, 0)


def driver_mark_live(mark, *, process_start=None):
    """Whether a running mark's pid belongs to the process that wrote it.

    Encargo H punto 4: a dead driver is not a live one. A missing or
    unreadable pid fails closed (a driver may be alive). The marked process
    must predate its own mark — it wrote it while running. A pid that only
    came to exist after the mark was written was recycled by another
    process: the marked driver is gone, so the mark reads as dead. A
    long-lived launcher (the gateway) predates its marks by hours and reads
    as live, exactly like the short-lived driver that starts with its mark.

    Review F4: a pid the platform refuses to answer about is not a dead
    one. The mark then reads as live — no close, no retry — and the cause
    is recorded on the mark under ``pid_check``, where goncloud_local_status
    shows it.
    """
    if not isinstance(mark, dict):
        return True
    pid = mark.get("pid")
    if type(pid) is not int or pid <= 0:
        return True
    started = (process_start or _process_start_epoch)(pid)
    if isinstance(started, _UncheckedPid):
        mark["pid_check"] = f"unchecked ({started.cause})"
        return True
    if started is None:
        return False
    try:
        marked = datetime.fromisoformat(
            str(mark.get("started_at", "")).replace("Z", "+00:00")
        )
    except ValueError:
        return True
    if marked.tzinfo is None:
        marked = marked.replace(tzinfo=timezone.utc)
    return started <= marked.timestamp() + _DRIVER_START_TOLERANCE_SECONDS


def driver_stopped(mark):
    """Durable evidence the driver stopped: one of its stopped outcomes as
    the file's last write, or a running mark whose process is gone (a dead
    or recycled pid — encargo H punto 4). Anything else reads as a driver
    that may still be running."""
    outcome = mark.get("outcome") if isinstance(mark, dict) else None
    if outcome in _STOPPED_DRIVER_OUTCOMES:
        return True
    return outcome == "running" and not driver_mark_live(mark)


def read_driver_mark(workflow_id, *, home=None):
    """The result file's last write, or None when there is no file.

    An unreadable file proves nothing about the driver (live review F8):
    fail closed as a running mark instead of deriving a stopped outcome.
    """
    check_workflow_id(workflow_id)
    home = Path(home or os.environ.get("HERMES_HOME") or _default_home())
    result_file = _result_file(workflow_id, home)
    if not result_file.is_file():
        return None
    try:
        return json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"workflow_id": workflow_id, "outcome": "running"}


# Vendors differ on whether they break the line before the opening fence, so the
# fence may carry a prose prefix. The closing fence is still checked separately
# for standing alone, and nothing may follow it.
_FENCE = re.compile(r"```([^\r\n]*)\r?$", re.MULTILINE)
# One set of credential shapes for both boundaries: this copy once lagged the
# router's and let ghp_/xai- tokens through to the director and Telegram.
_SENSITIVE = SENSITIVE_TEXT
# Identifiers travel in argv and name the result file; never a path fragment.
_WORKFLOW_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def check_stop_after(stop_after):
    """Reject an unknown stop before anything durable is created."""
    if stop_after is not None and stop_after not in INITIAL_STAGES:
        raise WorkflowError("stop_after must name a workflow stage")


def check_workflow_id(workflow_id):
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID.match(workflow_id):
        raise WorkflowError("workflow_id is malformed")
    return workflow_id


def close_hint(
    status_output, driver_outcome, *, driver_live=False, pid_live=None, queued=False
):
    """What goncloud_local_status tells claw about closing this chain.

    Every driver start overwrites the result file with a running mark under
    its own run id, and the driver's stopped outcome is only written when it
    quits — so a stopped outcome there means the last driver is gone. A
    close is announced only with one of those stopped outcomes (paused,
    blocked, waiting or error), a running mark whose process is gone
    (pid_live=False, encargo H punto 4), a chain still sitting in the queue
    (it never had a driver), no live lease, and a root status
    that is closeable at all (a done root was delivered; an archived one is
    already closed; a running or unknown one is never announced).
    """
    root_status = (status_output.get("root") or {}).get("status")
    if root_status not in _CLOSEABLE_ROOT_STATUSES:
        return None
    stopped = (
        driver_outcome in _STOPPED_DRIVER_OUTCOMES
        or (queued and driver_outcome is None)
        or (driver_outcome == "running" and pid_live is False)
    )
    if not stopped or driver_live:
        return None
    return (
        "driver stopped (paused, blocked, waiting or error as the last "
        "write; a running mark whose pid is gone counts too; a queued "
        "chain never had one): close it with "
        "goncloud_local_close {workflow_id, reason} to free the repository "
        "for a new chain"
    )


_ATTEMPT_KEYS = (
    "slot",
    "adapter",
    "model",
    "billing_mode",
    "mode",
    "success",
    "failure",
    "disposition",
)


def _invalid_terminal():
    return WorkflowError("invalid or oversized terminal envelope")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _invalid_terminal()
        result[key] = value
    return result


def _invalid_constant(_value):
    raise _invalid_terminal()


def parse_terminal_envelope(text: str, *, role: str) -> dict:
    """Accept only the final fenced JSON object, with no trailing content."""
    if type(text) is not str or role not in INITIAL_STAGES:
        raise _invalid_terminal()
    if len(text) > MAX_INPUT_BYTES:
        raise _invalid_terminal()
    try:
        if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            raise _invalid_terminal()
        fences = list(_FENCE.finditer(text))
        if len(fences) < 2:
            raise _invalid_terminal()
        opening, closing = fences[-2:]
        if opening.group(1) != "json" or closing.group(1) != "":
            raise _invalid_terminal()
        if text[: closing.start()].rsplit("\n", 1)[-1].strip():
            raise _invalid_terminal()
        if text[closing.end() :].strip():
            raise _invalid_terminal()
        body = text[opening.end() : closing.start()].strip()
        if len(body.encode("utf-8")) > MAX_TERMINAL_BYTES:
            raise _invalid_terminal()
        result = json.loads(
            body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant
        )
    except (ValueError, UnicodeError, RecursionError):
        raise _invalid_terminal() from None
    if type(result) is not dict or set(result) != {
        "status",
        "summary",
        "evidence",
        "review",
    }:
        raise _invalid_terminal()
    if type(result["status"]) is not str or result["status"] not in {
        "completed",
        "blocked",
    }:
        raise _invalid_terminal()
    summary, evidence, review = result["summary"], result["evidence"], result["review"]
    if type(summary) is not str or not summary.strip():
        raise _invalid_terminal()
    if type(evidence) is not list or any(
        type(item) is not str or not item.strip() for item in evidence
    ):
        raise _invalid_terminal()
    if role == "reviewer" and result["status"] == "completed":
        if type(review) is not dict or set(review) != {"decision", "severity"}:
            raise _invalid_terminal()
        decision, severity = review["decision"], review["severity"]
        if decision == "approve" and severity is None:
            pass
        elif (
            decision == "reject"
            and type(severity) is str
            and severity in {"low", "medium", "high", "critical"}
        ):
            pass
        else:
            raise _invalid_terminal()
    elif review is not None:
        raise _invalid_terminal()
    return result


def _sanitize(text, instruction=""):
    # Same floor and marker guard as the router: a short value would rewrite
    # JSON keys, and a piece of the marker would grow on every pass.
    secrets = [instruction] if usable_secret(instruction) else []
    secrets.extend(
        value
        for key, value in os.environ.items()
        if usable_secret(value)
        and any(
            word in key.upper() for word in ("TOKEN", "SECRET", "PASSWORD", "API_KEY")
        )
    )
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "[redacted]")
    return _SENSITIVE.sub("[redacted]", text)


def _trimmed(text, limit):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _normalized_terminal(text, role, instruction):
    parsed = parse_terminal_envelope(text, role=role)
    # Over-long fields are trimmed, not rejected: a correct 454-char scout
    # summary was thrown away live (2026-09-28). Trim only after redaction, so
    # a cut never leaves a credential fragment the exact-value pass cannot see.
    parsed["summary"] = _trimmed(_sanitize(parsed["summary"], instruction), 400)
    parsed["evidence"] = [
        _trimmed(_sanitize(item, instruction), 120) for item in parsed["evidence"][:8]
    ]
    text = (
        "```json\n"
        + json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
        + "\n```"
    )
    if len(text.encode("utf-8")) > MAX_TERMINAL_BYTES:
        raise _invalid_terminal()
    return parsed, text


def _safe_handoff(value, role, instruction):
    try:
        text = (
            "```json\n" + json.dumps(value, ensure_ascii=False) + "\n```"
            if isinstance(value, dict)
            else value
        )
        return _normalized_terminal(text, role, instruction)[0]
    except (ValueError, TypeError, RecursionError):
        return None


def _terminal_transform(role, instruction):
    def transform(result):
        if not result.success:
            # The adapter's failure text is synthetic, built from the route and
            # the failure kind, so it names which route failed and why without
            # carrying vendor output. Replacing it left the durable row unable
            # to tell a rejected envelope apart from a dead account.
            return replace(result, session_id="")
        _, text = _normalized_terminal(result.text, role, instruction)
        return replace(result, text=text, session_id="")

    return transform


class LocalOrchestrator:
    """Resume from Kanban state; route all executable stages through its service."""

    def __init__(self, *, service_factory=None, stage_runner=None):
        self.services = service_factory or (
            lambda role: WorkflowService(actor_role=role)
        )
        self.stage_runner = stage_runner or (
            lambda service, **assignment: service.subscription_run(**assignment)
        )

    @staticmethod
    def _check_stop(stop_after):
        check_stop_after(stop_after)

    def start(
        self,
        *,
        request_key,
        repository,
        revision,
        instruction,
        sources,
        max_steps,
        stop_after=None,
        affects_business_rules=False,
    ):
        self._check_stop(stop_after)
        created = self.create(
            request_key=request_key,
            repository=repository,
            revision=revision,
            instruction=instruction,
            sources=sources,
            max_steps=max_steps,
            affects_business_rules=affects_business_rules,
        )
        return self.resume(created["workflow_id"], stop_after=stop_after)

    def create(
        self,
        *,
        request_key,
        repository,
        revision,
        instruction,
        sources,
        max_steps,
        affects_business_rules=False,
    ):
        """Register the durable chain and its step limit without running a stage.

        A messaging transport calls this and then detaches the driver, so the
        gateway answers with the workflow id at once instead of blocking for
        the whole chain. The bounds are the same ones the local CLI enforces.
        """
        if type(max_steps) is not int or not 1 <= max_steps <= 200:
            raise WorkflowError("max_steps must be an integer between 1 and 200")
        if (
            type(instruction) is not str
            or not instruction.strip()
            or len(instruction) > MAX_INSTRUCTION_CHARS
        ):
            raise WorkflowError("instruction is empty or oversized")
        # Check the repository before deriving anything from it: a mangled
        # path otherwise surfaces as "revision is required" and misleads.
        repo = Path(str(repository)).expanduser()
        if not (repo.is_absolute() and repo.is_dir()):
            raise WorkflowError(
                "repository must be an existing absolute directory; pass the path "
                "exactly as the owner gave it"
            )
        service = self.services("claw")
        original = service.start_contract(request_key=request_key)
        resolved_revision = (
            revision
            if revision is not None
            else (
                original["revision"]
                if original
                else GitInspector().current_revision(repo)
            )
        )
        resolved_sources = (
            sources
            if sources is not None
            else (
                original["sources"]
                if original
                else [
                    {
                        "reference": "owner local assignment",
                        "observed_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
        )
        assignment = dict(
            request_key=request_key,
            repository=repository,
            revision=resolved_revision,
            instruction=instruction,
            sources=resolved_sources,
            affects_business_rules=affects_business_rules,
        )
        try:
            started = service.start(**assignment)
        except WorkflowError:
            # Concurrent first starts may compute different implicit timestamps.
            # Retry once against the winner's immutable provenance; explicit
            # changes still go through the service's exact contract comparison.
            winner = (
                service.start_contract(request_key=request_key)
                if original is None and (revision is None or sources is None)
                else None
            )
            if winner is None:
                raise
            if revision is None:
                assignment["revision"] = winner["revision"]
            if sources is None:
                assignment["sources"] = winner["sources"]
            started = service.start(**assignment)
        workflow_id = started["workflow_id"]
        with service._connect() as conn, write_txn(conn):
            conn.execute(
                "CREATE TABLE IF NOT EXISTS goncloud_local_workflows "
                "(workflow_id TEXT PRIMARY KEY, max_steps INTEGER NOT NULL)"
            ).close()
            row = conn.execute(
                "SELECT max_steps FROM goncloud_local_workflows WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
            if row is not None and row[0] != max_steps:
                raise WorkflowError("request_key already has a different step limit")
            conn.execute(
                "INSERT OR IGNORE INTO goncloud_local_workflows VALUES (?, ?)",
                (workflow_id, max_steps),
            ).close()
        result = {"workflow_id": workflow_id, "stages": dict(started["stages"])}
        # Encargo H punto 2: start ya no rechaza — la cadena vieja parada se
        # cerro sola y una viva deja la nueva en cola. El transporte no
        # lanza driver sobre una cola: la cadena arranca sola cuando el
        # driver de la vieja escribe su outcome final (punto 3), o con el
        # siguiente goncloud_local_run (o resume).
        if started.get("queued_behind") is not None:
            result["queued_behind"] = started["queued_behind"]
        if started.get("closed_chains"):
            result["closed_chains"] = list(started["closed_chains"])
        return result

    def launch_detached(self, workflow_id, *, home=None, stop_after=None):
        """Drive a created workflow in a detached process and return at once.

        Only identifiers travel in argv (the workflow id, its launch's run id
        and, for a read-only inspection, the stage to stop after); the
        assignment stays in the durable contract. Vendor diagnostics go
        nowhere (the driver prints nothing); the bounded result lands in a
        private file under the home.
        """
        check_workflow_id(workflow_id)
        self._check_stop(stop_after)
        home = Path(home or os.environ.get("HERMES_HOME") or _default_home())
        # A relaunch never spawns a second driver over a live one (encargo H
        # punto 2): the running mark written by that driver is the life
        # signal, and the queue — not a refusal — is the answer. Our own
        # process's pid marks a previous run of this very process, not
        # another driver.
        mark = read_driver_mark(workflow_id, home=home)
        if (
            isinstance(mark, dict)
            and mark.get("outcome") == "running"
            and driver_mark_live(mark)
            and mark.get("pid") != os.getpid()
        ):
            return {"workflow_id": workflow_id, "queued": True}
        # The start itself invalidates whatever outcome a previous driver
        # left (live review F8): from here on the file says running under
        # this launch's id, until this run's driver overwrites it with its
        # own outcome — a close can never mistake the old file for proof
        # that the new driver stopped.
        mark = _running_mark(workflow_id, uuid4().hex)
        _write_result(workflow_id, mark, home=home)
        result_file = _result_file(workflow_id, home)
        argv = [
            sys.executable,
            "-I",
            "-c",
            _DRIVER_BOOTSTRAP,
            str(Path(__file__).resolve().parent),
            str(Path(hermes_cli.__file__).resolve().parent.parent),
            str(workflow_id),
            str(result_file),
            # Fixed positions (review F1): stop_after is optional but its
            # slot is not — splicing the run_id into the empty slot made
            # every launch without a checkpoint die on check_stop_after
            # before running a single stage. The bootstrap maps "" to None.
            str(stop_after or ""),
            str(mark["run_id"]),
        ]
        detach = (
            {
                "creationflags": subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP
            }
            if os.name == "nt"
            else {"start_new_session": True}
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=str(home),
                env=dict(os.environ),
                stdin=None,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                **detach,
            )
        except Exception as exc:
            # Review F7: no driver was spawned, so no running mark may
            # survive pointing at this launcher's pid — a close would be
            # refused and the chain read as live until the gateway
            # restarts. The launch failure becomes the file's last word: a
            # stopped outcome a later start can close and retry over.
            _write_result(
                workflow_id,
                {
                    "workflow_id": workflow_id,
                    "outcome": "error",
                    "error": f"launch failed: {exc}",
                    "run_id": mark["run_id"],
                },
                home=home,
            )
            raise
        # The pre-spawn mark carries the launcher's pid, which outlives the
        # driver (the gateway stays up for days): a driver dead before its
        # own entry-mark would read as live forever. With the driver's real
        # pid, that death is exactly what the pid-liveness check (encargo H
        # punto 4) reads as a stopped driver. The run id stays the launch's.
        _write_result(workflow_id, {**mark, "pid": process.pid}, home=home)
        return {
            "workflow_id": workflow_id,
            "driver_pid": process.pid,
            "run_id": mark["run_id"],
            "result_file": str(result_file),
        }

    def driver_result(self, workflow_id, *, home=None):
        """The detached driver's bounded outcome, or None while it still runs.

        A running mark comes back annotated with ``pid_check`` when the pid
        could not be checked (review F4/F11): the cause is visible in what
        goncloud_local_status reports, not discarded with a local copy.
        """
        mark = read_driver_mark(workflow_id, home=home)
        if isinstance(mark, dict) and mark.get("outcome") == "running":
            driver_mark_live(mark)
        return mark

    def close(self, workflow_id, *, reason, store=None, home=None):
        """Close a chain the driver already stopped and free its repository.

        Archives the director root and every stage that is not done with the
        native Kanban archive (done stages stay as evidence), and leaves the
        reason as a comment on the root. A chain whose driver may still be
        working is refused: closing requires the driver's own durable
        stopped outcome (paused, blocked, waiting or error — the result file
        it writes when it quits) as the file's last write — every driver
        start overwrites that file with a running mark first, so a relaunched
        chain waits for its own driver — plus no live subscription lease,
        and no stage the kernel shows running. A root stuck in "triage"
        without its stage graph never had a driver, so it closes without
        one. The native writes run one transaction per task, outside any
        transaction of this close (F6: the reference write_txn refuses
        nesting); the receipt is derived from the durable post-close state,
        so closing an already-closed chain returns the same receipt without
        writing anything twice.
        """
        check_workflow_id(workflow_id)
        if type(reason) is not str or not reason.strip():
            raise WorkflowError("reason is required")
        reason = reason.strip()
        if len(reason) > MAX_CLOSE_REASON_CHARS:
            raise WorkflowError("reason is oversized")
        service = self.services("claw")
        with service._connect() as conn:
            # Identity first, so an unknown or foreign id is a typed
            # rejection before anything else is consulted. The graph may be
            # incomplete (a decomposition that failed after the root
            # insert); those roots close too, with whatever stages exist.
            service._require_workflow_root(conn, workflow_id)
            initial_ids = service._decomposed_child_ids(conn, workflow_id) or []
            correction = service._correction_ids(conn, workflow_id)
            # Encargo H punto 3: a chain sitting in the queue never had a
            # driver, so the durable queue marker — not a driver mark — is
            # its close evidence.
            queued = any(
                item.body.startswith(_QUEUED_COMMENT_MARKER)
                for item in list_comments(conn, workflow_id)
            )
        stages = dict(zip(INITIAL_STAGES, initial_ids))
        stages.update(
            {f"round2:{role}": task_id for role, task_id in correction.items()}
        )
        if store is None:
            path = default_db_path()
            # No subscription store means no router ever drove this chain.
            if path.exists():
                store = SubscriptionStateStore(path)
        comment = _CLOSE_COMMENT_MARKER + json.dumps(
            {"reason": reason, "workflow_id": workflow_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        # No write_txn propio: la referencia 0.22 falla siempre anidada
        # (archive_task abre write_txn sin allow_nested y el write_txn real
        # lanza "already inside a transaction"). Los guards se leen primero y
        # cada escritura nativa (un archive_task por tarea, el comentario al
        # final) abre y confirma su propia transaccion; la idempotencia y el
        # recibo salen del estado durable leido despues.
        with service._connect() as conn:
            root = service._require_workflow_root(conn, workflow_id)
            if root.status == "running":
                raise WorkflowError(
                    "workflow is running; goncloud_local_close only closes "
                    "chains the driver already stopped"
                )
            if root.status == "done":
                raise WorkflowError(
                    "workflow already delivered; goncloud_local_close only "
                    "closes chains the driver already stopped"
                )
            if root.status not in _CLOSEABLE_ROOT_STATUSES | {"archived"}:
                raise WorkflowError(
                    f"workflow root status {root.status} is not closeable"
                )
            if any(
                service._require_task(conn, task_id).status == "running"
                for task_id in stages.values()
            ):
                raise WorkflowError(
                    "workflow is running; goncloud_local_close only closes "
                    "chains the driver already stopped"
                )
            if store is not None and store.active_leases(workflow_id):
                raise WorkflowError(
                    "workflow is running; goncloud_local_close only closes "
                    "chains the driver already stopped"
                )
            if root.status != "archived" and stages:
                # The driver's result file is the marker it writes when it
                # stops. Without it the chain may have a live driver between
                # stages (nothing marks a stage running), so the close waits.
                # A running mark whose pid is gone is a dead driver: stop
                # evidence too (encargo H punto 4). A queued chain never
                # had a driver — its durable queue marker is the evidence
                # (encargo H punto 3).
                if not (
                    queued or driver_stopped(self.driver_result(workflow_id, home=home))
                ):
                    raise WorkflowError(
                        "no durable driver stop (paused, blocked, waiting or "
                        "error) as the result file's last write; the driver "
                        "may still be running; goncloud_local_close refuses"
                    )
            members = [(workflow_id, root.status)] + [
                (task_id, service._require_task(conn, task_id).status)
                for task_id in stages.values()
            ]
            for member_id, member_status in members:
                # Done stages are the chain's evidence: they stay exactly as
                # they are; archived ones are already closed — the durable
                # status is what makes a re-run a no-op (F6: one native
                # transaction per task, never under an outer one).
                if member_status in {"archived", "done"}:
                    continue
                if not archive_task(conn, member_id):
                    raise WorkflowError(f"native Kanban refused to archive {member_id}")
            existing = next(
                (
                    item.body
                    for item in list_comments(conn, workflow_id)
                    if item.body.startswith(_CLOSE_COMMENT_MARKER)
                ),
                None,
            )
            if existing is None:
                add_comment(conn, workflow_id, "claw", comment)
                recorded_reason = reason
            else:
                try:
                    recorded_reason = json.loads(
                        existing[len(_CLOSE_COMMENT_MARKER) :]
                    )["reason"]
                except (ValueError, KeyError, TypeError):
                    recorded_reason = reason
            final = {
                role: service._require_task(conn, task_id).status
                for role, task_id in stages.items()
            }
        return {
            "workflow_id": workflow_id,
            "outcome": "closed",
            "root_status": "archived",
            "archived_stages": sorted(
                role for role, status in final.items() if status == "archived"
            ),
            "done_stages": sorted(
                role for role, status in final.items() if status == "done"
            ),
            "reason": recorded_reason,
        }

    def close_hint(self, workflow_id, *, store=None, home=None):
        """Whether goncloud_local_status announces the close, with evidence.

        Same contract the close enforces: one of the driver's durable
        stopped outcomes (paused, blocked, waiting or error) — or a running
        mark whose pid is gone —, a chain still sitting in the queue (it
        never had a driver), no live subscription lease, and a
        closeable root.
        A chain whose status cannot even be read (unknown id, or the triage
        root of a failed decomposition) has no driver outcome to speak of,
        so it never gets a hint.
        """
        check_workflow_id(workflow_id)
        try:
            state = self.status(workflow_id)
        except WorkflowError:
            return None
        driver = read_driver_mark(workflow_id, home=home)
        # A result file holding valid JSON that is not an object proves
        # nothing (review F6): no outcome to speak of, never a crash.
        outcome = driver.get("outcome") if isinstance(driver, dict) else None
        pid_live = driver_mark_live(driver) if outcome == "running" else None
        service = self.services("claw")
        with service._connect() as conn:
            queued = any(
                item.body.startswith(_QUEUED_COMMENT_MARKER)
                for item in list_comments(conn, workflow_id)
            )
        if store is None:
            path = default_db_path()
            if path.exists():
                store = SubscriptionStateStore(path)
        live = bool(store.active_leases(workflow_id)) if store is not None else False
        return close_hint(
            state, outcome, driver_live=live, pid_live=pid_live, queued=queued
        )

    def _context(self, workflow_id):
        service = self.services("claw")
        with service._connect() as conn:
            root = service._require_workflow_root(conn, workflow_id)
            contract = service._contract_of(root)
            table = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='goncloud_local_workflows'"
            ).fetchone()
            row = (
                conn.execute(
                    "SELECT max_steps FROM goncloud_local_workflows WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()
                if table
                else None
            )
        if row is None or type(row[0]) is not int or not 1 <= row[0] <= 200:
            raise WorkflowError("workflow has no valid local execution contract")
        return contract, row[0]

    def status(self, workflow_id):
        service = self.services("claw")
        state = service.status(workflow_id=workflow_id)
        # Native titles and results may contain the private assignment; expose
        # only explicit status and the bounded metadata this driver produced.
        with service._connect() as conn:
            instruction = service._contract_of(
                service._require_workflow_root(conn, workflow_id)
            )["instruction"]
            output = {
                "workflow_id": state["workflow_id"],
                "root": {"id": state["root"]["id"], "status": state["root"]["status"]},
                "delivery_blocked": state["delivery_blocked"],
                "stages": {},
                "corrective_stages": {},
            }
            for group in ("stages", "corrective_stages"):
                for role, stage in state[group].items():
                    entry = {
                        "id": stage["id"],
                        "role": role,
                        "status": stage["status"],
                        "attempts": [],
                    }
                    for run in list_runs(conn, stage["id"]):
                        metadata = run.metadata or {}
                        local = metadata.get("goncloud_local")
                        if isinstance(local, dict):
                            terminal = _safe_handoff(
                                local.get("terminal"), role, instruction
                            )
                            if terminal:
                                entry["terminal"] = terminal
                    if role == "reviewer":
                        review = service._recorded_review_run(conn, stage["id"])
                        if review:
                            entry["review"] = {
                                key: review.get(key)
                                for key in ("decision", "severity", "round")
                            }
                    # Route evidence is attached even when the stage blocks or
                    # reviewer completion is owned by WorkflowService.review.
                    route_status = self.services(role).subscription_status(
                        workflow_id=workflow_id, stage_id=stage["id"]
                    )
                    attempts = route_status.get("stage", {}).get("attempts", [])
                    if "terminal" not in entry:
                        for attempt in reversed(attempts):
                            if (
                                attempt.get("success")
                                and attempt.get("status") == "completed"
                            ):
                                terminal = _safe_handoff(
                                    attempt.get("detail"), role, instruction
                                )
                                if terminal:
                                    entry["terminal"] = terminal
                                break
                    entry["attempts"] = [
                        {key: item.get(key) for key in _ATTEMPT_KEYS}
                        for item in attempts
                    ]
                    output[group][role] = entry
        output["outcome"] = (
            "delivered"
            if output["root"]["status"] == "done"
            else "blocked"
            if output["root"]["status"] in {"blocked", "archived"}
            or any(
                stage["status"] == "blocked"
                for group in ("stages", "corrective_stages")
                for stage in output[group].values()
            )
            else "pending"
        )
        return output

    def _report(self, workflow_id, outcome, *, home=None, run_id=None):
        report = {**self.status(workflow_id), "outcome": outcome}
        if run_id is None:
            return report
        # The same run id the run started with: this outcome is the run's
        # last write to the result file, so it is the stop evidence a close
        # reads (live review F8).
        report["run_id"] = run_id
        _write_result(workflow_id, report, home=home)
        # Encargo H punto 3 (review F9): the final outcome frees the
        # repository, so the queue's head launches here — FIFO, one chain,
        # from the driver's own process. A failure launching never touches
        # the outcome just written: the queue stays durable and the next
        # run (or resume) starts the chain.
        try:
            launched = self._launch_next_queued(workflow_id, home=home)
        except Exception:
            launched = None
        if launched is not None:
            report["launched_queued"] = launched
        return report

    def _launch_next_queued(self, finished_id, *, home=None):
        """Launch the repository queue's head (encargo H punto 3, F9).

        The chain that just wrote its final outcome freed the repository;
        the first queued chain of the same repository — FIFO by native id,
        the creation order — launches with the same detached driver any
        start uses. One chain per call. Queued means the durable queue
        marker plus demonstrably no live work: no running member, no live
        subscription lease, no live driver mark (the launched chain stops
        reading as queued the moment its own driver writes its mark, so a
        second concurrent report cannot spawn a second driver — the
        launch's own guard refuses).
        """
        service = self.services("claw")
        with service._connect() as conn:
            root = service._require_workflow_root(conn, finished_id)
            repo = Path(str(root.workspace_path or "")).expanduser().resolve()
            queued = []
            for active in list_tasks(conn, include_archived=True):
                if active.id == finished_id:
                    continue
                if not (active.body or "").startswith(_CONTRACT_MARKER_REF):
                    continue
                if active.workspace_kind != "dir" or not active.workspace_path:
                    continue
                if active.status in {"done", "archived", "running"}:
                    continue
                try:
                    if Path(active.workspace_path).expanduser().resolve() != repo:
                        continue
                except OSError:
                    continue
                if not any(
                    comment.body.startswith(_QUEUED_COMMENT_MARKER)
                    for comment in list_comments(conn, active.id)
                ):
                    continue
                children = service._decomposed_child_ids(conn, active.id) or []
                correction = service._correction_ids(conn, active.id)
                if any(
                    service._require_task(conn, task_id).status == "running"
                    for task_id in [*children, *correction.values()]
                ):
                    continue
                queued.append(active)
        queued.sort(key=_queue_order)
        if not queued:
            return None
        path = default_db_path()
        store = SubscriptionStateStore(path) if path.exists() else None
        for candidate in queued:
            if store is not None and store.active_leases(candidate.id):
                continue
            mark = read_driver_mark(candidate.id, home=home)
            if (
                isinstance(mark, dict)
                and mark.get("outcome") == "running"
                and driver_mark_live(mark)
            ):
                continue
            self.launch_detached(candidate.id, home=home)
            return candidate.id
        return None

    def _approved(self, state):
        service = self.services("reviewer")
        reviewer = (state["corrective_stages"] or state["stages"])["reviewer"]
        with service._connect() as conn:
            record = service._recorded_review_run(conn, reviewer["id"])
        return bool(
            record
            and record.get("decision") == "approve"
            and record.get("result", {}).get("outcome") == "approved"
        )

    def _queued_behind(
        self, workflow_id, repository, *, home=None, store=None, own_mark=None
    ):
        """The first chain whose driver may still be working this repository.

        Encargo H punto 2: the answer is a chain id to queue behind, never a
        refusal. Live work is a running root or stage, an unexpired
        subscription lease, or a running mark whose pid is alive — except a
        mark our own process wrote (pid == os.getpid()), which is a previous
        sequential run of this process, not another driver. The caller's own
        chain is excluded by id; its mark arrives via ``own_mark`` so a
        relaunch over a live driver of the same chain also queues.
        """
        if (
            isinstance(own_mark, dict)
            and own_mark.get("outcome") == "running"
            and driver_mark_live(own_mark)
            and own_mark.get("pid") != os.getpid()
        ):
            return workflow_id
        if store is None:
            path = default_db_path()
            if path.exists():
                store = SubscriptionStateStore(path)
        try:
            repo = Path(str(repository)).expanduser().resolve()
        except OSError:
            return None
        service = self.services("claw")
        with service._connect() as conn:
            for active in list_tasks(conn, include_archived=True):
                if active.id == workflow_id:
                    continue
                if not (active.body or "").startswith(_CONTRACT_MARKER_REF):
                    continue
                if active.status in {"done", "archived"}:
                    continue
                if active.workspace_kind != "dir" or not active.workspace_path:
                    continue
                try:
                    active_repo = Path(active.workspace_path).expanduser().resolve()
                except OSError:
                    continue
                if active_repo != repo:
                    continue
                if active.status == "running":
                    return active.id
                initial_ids = service._decomposed_child_ids(conn, active.id) or []
                correction = service._correction_ids(conn, active.id)
                if any(
                    service._require_task(conn, task_id).status == "running"
                    for task_id in [*initial_ids, *correction.values()]
                ):
                    return active.id
                if store is not None and store.active_leases(active.id):
                    return active.id
                mark = read_driver_mark(active.id, home=home)
                if (
                    isinstance(mark, dict)
                    and mark.get("outcome") == "running"
                    and driver_mark_live(mark)
                ):
                    return active.id
        return None

    def _reopen_blocked_stages(self, workflow_id, service, *, mark, own_launch, home):
        """Relanzar = reintentar (encargo H punto 1).

        A relaunch over a chain whose driver already stopped reopens its
        blocked stages with the native unblock and opens a new attempt
        generation for each, so the router calls the model again instead of
        replaying the recorded result. Done stages are respected and there
        is no retry cap. Reviewer stages are exempt: the two-round review
        ceiling and its durable decision are a product contract, not a jam.
        The stop evidence is the previous driver's own — a stopped outcome
        as the file's last write, or a running mark whose pid is gone — or,
        on a relaunched driver (own launch mark already in place), the
        blocked stages themselves: only a stopped driver leaves them.
        """
        state = service.status(workflow_id=workflow_id)
        blocked = [
            (role, stage)
            for group in ("stages", "corrective_stages")
            for role, stage in state[group].items()
            if stage["status"] == "blocked" and role not in _RETRY_EXEMPT_ROLES
        ]
        if not blocked:
            # Blocked stages are the jam a relaunch retries. A blocked root
            # on its own is an operator's pause: it stays until the owner
            # resumes or closes it.
            return
        if not (own_launch or driver_stopped(mark)):
            return
        with service._connect() as conn:
            for _, stage in blocked:
                if not unblock_task(conn, stage["id"]):
                    raise WorkflowError(
                        f"native Kanban refused to unblock {stage['id']}"
                    )
            if state["root"]["status"] == "blocked" and not unblock_task(
                conn, workflow_id
            ):
                raise WorkflowError(f"native Kanban refused to unblock {workflow_id}")
        path = default_db_path()
        if not path.exists():
            return
        store = SubscriptionStateStore(path)
        for _, stage in blocked:
            store.open_generation(workflow_id, stage["id"])

    def resume(self, workflow_id, *, stop_after=None, home=None, launch_run_id=None):
        self._check_stop(stop_after)
        contract, max_steps = self._context(workflow_id)
        service = self.services("claw")
        home = Path(home or os.environ.get("HERMES_HOME") or _default_home())
        # Encargo H punto 2: a driver never runs stages over another live
        # driver in the same repository — it queues instead, without
        # touching the result file (the live mark stays the life signal).
        # The guard runs for every entry, the detached driver included
        # (review F3): own_launch only means the mark is this launch's,
        # and the own chain is excluded by id — skipping the guard skipped
        # it for every detached driver, and two of them could work one
        # checkout.
        mark = read_driver_mark(workflow_id, home=home)
        own_launch = (
            launch_run_id is not None
            and isinstance(mark, dict)
            and mark.get("run_id") == launch_run_id
        )
        behind = self._queued_behind(
            workflow_id,
            contract["repository"],
            home=home,
            own_mark=None if own_launch else mark,
        )
        if behind is not None:
            return {**self.status(workflow_id), "outcome": "queued"}
        # Every driver enters here — the detached one the bootstrap spawns
        # and the CLI resume alike — so this is the one place a run marks
        # the result file running before its first stage (live review F8).
        # From this write on, the previous driver's stopped outcome is no
        # longer the file's last word.
        run_id = uuid4().hex
        _write_result(workflow_id, _running_mark(workflow_id, run_id), home=home)
        # Encargo H punto 1: relanzar es reintentar.
        self._reopen_blocked_stages(
            workflow_id, service, mark=mark, own_launch=own_launch, home=home
        )

        def report(outcome):
            return self._report(workflow_id, outcome, home=home, run_id=run_id)

        while True:
            state = service.status(workflow_id=workflow_id)
            if state["root"]["status"] in {"done", "archived"}:
                return report(
                    "delivered" if state["root"]["status"] == "done" else "blocked"
                )
            stages = [(role, state["stages"][role]) for role in INITIAL_STAGES[:-1]]
            stages += [
                (role, state["corrective_stages"][role])
                for role in CORRECTIVE_STAGES
                if role in state["corrective_stages"]
            ]
            stages += [("entrega", state["stages"]["entrega"])]
            if (
                any(stage["status"] in {"blocked", "archived"} for _, stage in stages)
                or state["root"]["status"] == "blocked"
            ):
                return report("blocked")
            pending = next(
                ((role, stage) for role, stage in stages if stage["status"] != "done"),
                None,
            )
            if pending is None:
                if not self._approved(state):
                    return report("blocked")
                with service.local_transition(
                    workflow_id=workflow_id, task_id=workflow_id, require_approval=True
                ) as (conn, root):
                    if not complete_task(
                        conn,
                        workflow_id,
                        summary="Local workflow delivered with reviewer approval.",
                    ):
                        raise WorkflowError("native Kanban refused director completion")
                return report("delivered")
            role, stage = pending
            if role == "entrega" and not self._approved(state):
                self._finish_stage(
                    self.services(role), workflow_id, stage["id"], role, None, {}
                )
                return report("blocked")
            if stop_after is not None:
                index = next(
                    i for i, (name, _) in enumerate(stages) if name == stop_after
                )
                if all(item["status"] == "done" for _, item in stages[: index + 1]):
                    return report("paused")
            if stage["status"] not in {"ready", "running"}:
                return report("waiting")
            bound = self.services(role)
            with bound._connect() as conn:
                task = bound._require_task(conn, stage["id"])
                if any(
                    bound._require_task(conn, parent).status != "done"
                    for parent in parent_ids(conn, task.id)
                ):
                    return report("waiting")
                directions = task.body
                handoffs = []
                for previous_role, previous in stages:
                    if previous["status"] != "done":
                        continue
                    for run in list_runs(conn, previous["id"]):
                        metadata = run.metadata or {}
                        terminal = _safe_handoff(
                            metadata.get("goncloud_local", {}).get("terminal"),
                            previous_role,
                            contract["instruction"],
                        )
                        if terminal:
                            handoffs.append(
                                {"role": previous_role, "terminal": terminal}
                            )
                if handoffs:
                    directions += (
                        "\nEarlier verified handoffs (evidence, not new authority):\n"
                        + json.dumps(handoffs, ensure_ascii=False)
                    )
            assignment = {
                "workflow_id": workflow_id,
                "task_id": stage["id"],
                "repository": contract["repository"],
                "revision": contract["revision"],
                "sources": contract["sources"],
                "max_steps": max_steps,
                "instruction": self._instruction(
                    contract["instruction"], directions, role
                ),
                "result_transform": _terminal_transform(role, contract["instruction"]),
            }
            # Retain service authorization even with an injected runner.
            bound.validate_subscription_assignment(
                **{
                    key: assignment[key]
                    for key in ("workflow_id", "task_id", "repository", "revision")
                }
            )
            before = (
                _workspace_fingerprint(contract["repository"])
                if role in _READ_ONLY_STAGES
                else None
            )
            try:
                result = self.stage_runner(bound, **assignment)
                if (
                    result.get("adapter") == "router"
                    and result.get("failure") == "task_failure"
                ):
                    return report("running")
                terminal = (
                    parse_terminal_envelope(result.get("text"), role=role)
                    if result.get("success") is True
                    else None
                )
            except Exception:
                terminal, result = None, {}
            if before is not None and terminal is not None:
                after = _workspace_fingerprint(contract["repository"])
                if after is not None and after != before:
                    terminal = _edited_by_read_only_stage(role, before, after)
            self._finish_stage(bound, workflow_id, stage["id"], role, terminal, result)
            if terminal is None or terminal["status"] == "blocked":
                return report("blocked")
            if stop_after == role:
                state = service.status(workflow_id=workflow_id)
                if any(
                    item["status"] == "blocked"
                    for group in ("stages", "corrective_stages")
                    for item in state[group].values()
                ):
                    return report("blocked")
                if role != "entrega":
                    return report("paused")

    @staticmethod
    def _instruction(instruction, directions, role):
        return (
            f"{instruction}\n\nStage duties: {directions}\n"
            # Live 2026-09-28: scout read an implementation request, rightly did
            # not implement, then returned blocked and stopped the whole chain.
            "The instruction above is the goal of the whole chain; later stages "
            "carry out the parts outside your duties. Report status completed once "
            "your own stage duties are done, even when the goal itself is not yet "
            "met; use blocked only when you cannot do your own stage.\n"
            "Return one final fenced json object, its closing fence alone on its own "
            "line, and no content after that closing fence. "
            "Exact keys: status (completed or blocked), summary (nonempty, at most 400 characters), "
            "evidence (at most 8 nonempty strings of at most 120 characters), review. "
            "Total JSON at most 1800 UTF-8 bytes. Do not include credentials, account identities, "
            "the assignment prompt, or raw logs. Do not update Kanban or complete cards yourself. "
            + (
                'For completed reviewer use review={"decision":"approve","severity":null} or '
                'review={"decision":"reject","severity":"low|medium|high|critical"}. '
                "The local driver applies the decision through WorkflowService.review."
                if role == "reviewer"
                else "Use review=null."
            )
            + " For blocked status use review=null."
        )

    @staticmethod
    def _finish_stage(service, workflow_id, task_id, role, terminal, result):
        local_handoff = {
            "terminal": terminal,
            "attempts": [
                {key: row.get(key) for key in _ATTEMPT_KEYS}
                for row in result.get("attempts", [])
            ],
        }
        if (
            role == "reviewer"
            and terminal is not None
            and terminal["status"] == "completed"
        ):
            service.review(
                workflow_id=workflow_id,
                task_id=task_id,
                summary=terminal["summary"],
                local_handoff=local_handoff,
                **terminal["review"],
            )
            return
        if terminal is None or terminal["status"] == "blocked":
            with service.local_transition(
                workflow_id=workflow_id, task_id=task_id, allow_blocked=True
            ) as (
                conn,
                task,
            ):
                if task.status not in {"done", "blocked"}:
                    if not block_task(
                        conn,
                        task_id,
                        reason="Local stage blocked; owner review required.",
                        kind="needs_input",
                    ):
                        raise WorkflowError("native Kanban refused stage blocking")
            return
        metadata = {"goncloud_local": local_handoff}
        with service.local_transition(
            workflow_id=workflow_id,
            task_id=task_id,
            require_approval=(role == "entrega"),
        ) as (conn, task):
            if task.status != "done" and not complete_task(
                conn,
                task_id,
                summary=terminal["summary"],
                metadata=metadata,
                expected_run_id=task.current_run_id,
            ):
                raise WorkflowError("native Kanban refused stage completion")
