"""Migrate launcher credentials into the private Hermes store, safely.

Two-phase: the replacement store is written and probed first; plaintext
launcher values are removed only after the probe passes. Backups stay
outside Git and values are never printed.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
from subscriptions.adapters import classify_failure  # noqa: E402
from subscriptions.adapters._live_probe import PROBE_MARKER, PROBE_PROMPT  # noqa: E402
from subscriptions.process import ProcessEvidence  # noqa: E402
from subscriptions.private_files import (  # noqa: E402
    open_private_file,
    stable_directories,
    create_private_directory,
    certify_files,
    read_owned,
    directory_is_pinned,
    move_owned as _move_owned,
    publish_exclusive as _publish_exclusive,
)
from subscriptions.types import (  # noqa: E402
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
)

ADAPTER_ORDER = (
    "opencode_go",
    "opencode_zen",
    "muse",
    "claude",
    "codex",
    "kimi",
    "zai",
    "grok",
    "deepseek",
)
_SECRET_ADAPTERS = {"DEEPSEEK_API_KEY": "deepseek", "GLM_API_KEY": "zai"}

DEFAULT_PRIVATE_ENV_NAME = "subscription-router.env"
_MIGRATED_NOTE = "migrated to the private Hermes subscription store"

_CMD_ASSIGNMENT = re.compile(
    r'^\s*set\s+"([A-Za-z_][A-Za-z0-9_]*)=(.*)"\s*$', re.IGNORECASE
)
_PS_LITERAL = re.compile(
    r"^\s*\$env:([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"]([^'\"]+)['\"]\s*$"
)
_SECRET_VAR = "ANTHROPIC_AUTH_TOKEN"
_BASE_VAR = "ANTHROPIC_BASE_URL"


class MigrationError(RuntimeError):
    """A migration step refused to proceed; nothing was destroyed."""


@dataclass(frozen=True)
class MigrationResult:
    names: tuple[str, ...]
    private_env: Path
    backups: tuple[Path, ...] = ()


def sanitize_live_result(adapter: str, evidence) -> dict:
    """The only public live evidence: fixed identifiers and normalized enums."""
    adapter = adapter if adapter in ADAPTER_ORDER else "unknown"
    billing = "api" if adapter == "deepseek" else "subscription"
    failure = FailureKind.TASK_FAILURE
    ready = False
    if isinstance(evidence, InvocationResult):
        ready = (
            evidence.success is True
            and evidence.failure is None
            and evidence.text == PROBE_MARKER
            and evidence.adapter == adapter
            and evidence.billing_mode == billing
        )
        failure = evidence.failure or failure
    elif isinstance(evidence, ProcessEvidence):
        ready = evidence.returncode == 0 and evidence.stdout == PROBE_MARKER
        if evidence.returncode != 0:
            failure = classify_failure(
                evidence.returncode, evidence.stdout, evidence.stderr
            )
    elif isinstance(evidence, AdapterHealth):
        failure = evidence.failure or failure
    elif isinstance(evidence, str):
        ready = evidence == PROBE_MARKER
    elif isinstance(evidence, TimeoutError):
        failure = FailureKind.TIMEOUT
    elif isinstance(evidence, ConnectionError):
        failure = FailureKind.TRANSPORT
    try:
        failure = FailureKind(failure)
    except (ValueError, TypeError):
        failure = FailureKind.TASK_FAILURE
    ready = ready and adapter != "unknown"
    return {
        "adapter": adapter,
        "status": "ready"
        if ready
        else (
            "quarantined"
            if failure in {FailureKind.AUTH, FailureKind.QUOTA}
            else "failed"
        ),
        "failure": None if ready else failure.value,
        "billing_mode": billing,
    }


def default_private_env_path() -> Path:
    configured = os.environ.get("GONCLOUD_SUBSCRIPTION_ENV")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) / "hermes" if local else Path.home() / ".goncloud"
    return base / DEFAULT_PRIVATE_ENV_NAME


def default_deepseek_launcher() -> Path:
    roaming = os.environ.get("APPDATA")
    if roaming:
        return Path(roaming) / "npm" / "deepseek.cmd"
    return Path.home() / "deepseek.cmd"


def default_glm_launcher() -> Path:
    """The npm wrapper that carries the inline GLM token."""
    roaming = os.environ.get("APPDATA")
    if roaming and (Path(roaming) / "npm" / "glm.cmd").is_file():
        return Path(roaming) / "npm" / "glm.cmd"
    return Path.home() / "glm-claude" / "glm-code.ps1"


def parse_assignments(text: str) -> dict[str, str]:
    """Collect only known literal assignments, never execute anything."""
    found: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        cmd = _CMD_ASSIGNMENT.match(line)
        ps = _PS_LITERAL.match(line)
        if cmd:
            found[cmd.group(1).upper()] = cmd.group(2).strip()
        elif ps:
            found[ps.group(1).upper()] = ps.group(2).strip()
    return found


def _checked_path(path: Path) -> Path:
    """Reject aliases before resolving; every later operation uses this result."""
    spelling = str(path)
    if os.name == "nt":
        if spelling.startswith("\\\\?\\"):
            spelling = spelling[4:]
            if not re.match(r"^[A-Za-z]:[\\/]", spelling):
                raise MigrationError("unsupported credential device path")
        parsed = Path(spelling)
        components = parsed.parts[1:] if parsed.anchor else parsed.parts
        if any(
            part.rstrip(" .") != part for part in components if part not in {".", ".."}
        ):
            raise MigrationError("ambiguous credential path spelling")
        if ":" in spelling[2:]:
            raise MigrationError("unsupported credential stream path")
        devices = {"CON", "PRN", "AUX", "NUL"} | {
            f"{prefix}{number}" for prefix in ("COM", "LPT") for number in range(1, 10)
        }
        if any(part.split(".")[0].upper() in devices for part in components):
            raise MigrationError("unsupported credential device path")
        if not re.match(r"^[A-Za-z]:[\\/]", os.path.abspath(spelling)):
            raise MigrationError("unsupported credential device path")
    absolute = Path(os.path.abspath(spelling))
    for candidate in (absolute, *absolute.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MigrationError(
                "credential paths cannot contain links or reparse points"
            )
        if candidate == absolute and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise MigrationError("credential files cannot have hard-link aliases")
    return absolute if directory_is_pinned(absolute.parent) else absolute.resolve()


def _private_store_path(path: Path) -> Path:
    resolved = _checked_path(path)
    inside_checkout = resolved.is_relative_to(ROOT)
    for parent in resolved.parents:
        if not (parent / ".git").exists():
            continue
        result = subprocess.run(
            ["git", "-C", str(parent), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
            shell=False,
        )
        inside_checkout = inside_checkout or (
            result.returncode == 0 and result.stdout.strip() == "true"
        )
    if inside_checkout:
        raise MigrationError("private store must be outside the repository")
    return resolved


_UNCONDITIONAL = object()


def _snapshot(path: Path):
    """Capture one stable file identity and content, or an absent-file identity."""
    try:
        with read_owned(path) as descriptor:
            before = os.fstat(descriptor)
            chunks = []
            while chunk := os.read(descriptor, 65536):
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
    except FileNotFoundError:
        return None, None

    def identity(info):
        return (
            info.st_dev,
            info.st_ino,
            info.st_size,
            info.st_mtime_ns,
        )

    if identity(before) != identity(after):
        raise MigrationError("credential file changed while reading")
    return data, (identity(after), hashlib.sha256(data).hexdigest())


def _atomic_write(
    path: Path, data: bytes, *, expected=_UNCONDITIONAL, before_publish=None
) -> None:
    """Move an owned old object aside, then publish exclusively; retain recovery.

    No check/replace or check/unlink pair touches a public destination. Even an
    external writer winning the vacant publication slot retains its own object.
    """
    path = _checked_path(path)
    with stable_directories([path.parent]):
        if expected is _UNCONDITIONAL:
            expected = _snapshot(path)[1]
        transaction = path.parent / f"subscription-backup-{uuid.uuid4().hex}"
        create_private_directory(transaction)
        temporary = transaction / f".subscription-{uuid.uuid4().hex}"
        _write_candidate(temporary, data)
        if before_publish is not None:
            before_publish(_snapshot(temporary)[1][0])
        if expected is not None:
            _move_owned(path, transaction / "displaced-original", expected)
        try:
            _publish_exclusive(temporary, path)
        except BaseException:
            if expected is not None:
                try:
                    _publish_exclusive(transaction / "displaced-original", path)
                except BaseException:
                    pass  # Neither the external winner nor its old object is lost.
            raise


def _write_candidate(path: Path, data: bytes) -> None:
    with stable_directories([path.parent]):
        descriptor = open_private_file(path)
        try:
            handle = os.fdopen(descriptor, "wb")
        except BaseException:
            os.close(descriptor)
            raise
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())


def _encode_private_env(values: dict[str, str]) -> bytes:
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or any(
            char in value for char in "\r\n\0"
        ):
            raise MigrationError("invalid private environment entry")
    return "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode("utf-8")


def write_private_env(
    path: Path, values: dict[str, str], *, expected=_UNCONDITIONAL, before_publish=None
) -> None:
    """Validate environment lines before an atomic, mode-restricted write."""
    data = _encode_private_env(values)
    path = _private_store_path(path)
    with stable_directories([path.parent]):
        if _private_store_path(path) != path:
            raise MigrationError("private store parent changed")
        _atomic_write(
            path,
            data,
            expected=expected,
            before_publish=before_publish,
        )


def read_private_env(path: Path) -> dict[str, str]:
    return _parse_private_env(_snapshot(_checked_path(path))[0] or b"")


def _parse_private_env(data: bytes) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in data.decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def _rewritten_launcher(original: bytes) -> bytes:
    """Drop the token line while preserving the rest of the wrapper."""
    text = original.decode("utf-8")
    lines = text.splitlines(keepends=True)
    rebuilt = []
    for raw in lines:
        ending = "\r\n" if raw.endswith("\r\n") else "\n" if raw.endswith("\n") else ""
        line = raw[: len(raw) - len(ending)] if ending else raw
        cmd = _CMD_ASSIGNMENT.match(line)
        ps = _PS_LITERAL.match(line)
        name = None
        if cmd:
            name = cmd.group(1).upper()
        elif ps:
            name = ps.group(1).upper()
        if name == _SECRET_VAR:
            rebuilt.append(("rem " if cmd else "# ") + _MIGRATED_NOTE + ending)
        else:
            rebuilt.append(raw)
    return "".join(rebuilt).encode("utf-8")


@dataclass(frozen=True)
class _Replacement:
    secret_name: str
    target: Path
    original: bytes = field(repr=False)
    replacement: bytes = field(repr=False)


def migrate_one(
    launcher: Path,
    private_env: Path,
    probe,
    *,
    secret_name: str,
    sidecar: Path | None = None,
) -> MigrationResult:
    """Move one launcher credential into the private store, then clean up."""
    return _migrate_batch(
        [(Path(launcher), secret_name, sidecar)], Path(private_env), probe
    )


def migrate_launcher_secrets(
    glm: Path, deepseek: Path, private_env: Path, probe
) -> MigrationResult:
    """Migrate DeepSeek then GLM, returning the combined evidence."""
    return _migrate_batch(
        [
            (Path(deepseek), "DEEPSEEK_API_KEY", None),
            (Path(glm), "GLM_API_KEY", Path(glm).parent / "apikey.txt"),
        ],
        Path(private_env),
        probe,
    )


@contextmanager
def _migration_lock(private_env: Path):
    """An OS lock survives file replacement and is released after a crash."""
    private_env = _checked_path(private_env)
    private_env.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _checked_path(private_env.with_name(private_env.name + ".lock"))
    descriptor = open_private_file(lock_path, exclusive=False)
    with os.fdopen(descriptor, "r+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)

            def lock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

            def unlock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def lock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unlock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

        try:
            lock()
        except OSError:
            raise MigrationError("migration already running") from None
        try:
            yield
        finally:
            unlock()


def _migrate_batch(entries, private_env: Path, probe) -> MigrationResult:
    """Verify all replacements before cleanup; retain backups across crashes."""
    if not callable(probe):
        raise MigrationError("verified live probe required")
    if os.name != "nt":
        # No portable primitive can deny an uncooperative POSIX open writer.
        # Refuse BEFORE creating a store or journal, not during rollback.
        raise MigrationError("safe credential migration ownership unavailable")
    private_env = _private_store_path(private_env)
    entries = [
        (
            _checked_path(launcher),
            name,
            _checked_path(sidecar) if sidecar is not None else None,
        )
        for launcher, name, sidecar in entries
    ]
    reserved = {
        private_env,
        private_env.with_name(private_env.name + ".lock"),
        _journal_path(private_env),
    }
    targets = [
        path
        for launcher, _, sidecar in entries
        for path in (launcher, sidecar)
        if path is not None
    ]
    paths = list(reserved) + targets
    for index, path in enumerate(paths):
        if any(
            path == other or (path.exists() and other.exists() and path.samefile(other))
            for other in paths[:index]
        ):
            raise MigrationError("credential paths must be distinct")
    try:
        with stable_directories([path.parent for path in paths]):
            private_env = _private_store_path(private_env)
            # Object moves cannot cross volumes; refuse before writing snapshots.
            if any(
                path.parent.stat().st_dev != private_env.parent.stat().st_dev
                for path in targets
            ):
                raise MigrationError("credential transaction requires one volume")
            with _migration_lock(private_env):
                return _migrate_locked(entries, private_env, probe)
    except OSError:
        raise MigrationError("safe credential transaction unavailable") from None


def _journal_path(private_env: Path) -> Path:
    return private_env.with_name(private_env.name + ".migration.json")


def _digest(data: bytes | None):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _as_fingerprint(value):
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 2
        or len(value[0]) != 4
        or any(type(item) is not int for item in value[0])
        or not re.fullmatch(r"[0-9a-f]{64}", value[1])
    ):
        raise ValueError("invalid private identity")
    return tuple(value[0]), value[1]


def _retire_journal(private_env, directory, fingerprint):
    # Moving by owned handle never unlinks an external journal replacement.
    _move_owned(
        _journal_path(private_env), directory / "completed-journal", fingerprint
    )


def _restore_operation(operation, directory):
    target = Path(operation["target"])
    before = _as_fingerprint(operation["before"])
    candidate = _as_fingerprint(operation["candidate_identity"])
    original = directory / operation["displaced"]
    current = _snapshot(target)[1]
    if current == before:
        return
    if current is not None:
        if current != candidate:
            raise MigrationError("migration recovery preserved an uncertain object")
        _move_owned(target, directory / f"retired-{uuid.uuid4().hex}", candidate)
    if before is not None:
        # Restore the original OBJECT, not a copy inferred from matching bytes.
        if _snapshot(original)[1] != before:
            raise MigrationError("migration recovery requires the original object")
        _publish_exclusive(original, target)


def _recover_migration(private_env: Path, allowed_targets: set[Path]) -> None:
    """Idempotent rollback using recorded identities, including pre-publish gaps."""
    journal_path = _journal_path(private_env)
    data, journal_identity = _snapshot(journal_path)
    if data is None:
        return
    try:
        if len(data) > 65536:
            raise ValueError()
        document = json.loads(data)
        directory = _private_store_path(Path(document["backup_dir"]))
        if (
            document["version"] != 2
            or directory.parent != private_env.parent
            or not directory.name.startswith("subscription-backup-")
        ):
            raise ValueError()
        operations = document["operations"]
        if not operations or Path(operations[0]["target"]) != private_env:
            raise ValueError()
        seen = set()
        for index, operation in enumerate(operations):
            target = _checked_path(Path(operation["target"]))
            if target in seen or (index and target not in allowed_targets):
                raise ValueError()
            seen.add(target)
            for field in ("candidate", "displaced"):
                if Path(operation[field]).name != operation[field]:
                    raise ValueError()
            _as_fingerprint(operation["before"])
            _as_fingerprint(operation["candidate_identity"])
    except (OSError, ValueError, KeyError, TypeError):
        raise MigrationError(
            "migration recovery requires intact private snapshots"
        ) from None

    with stable_directories([directory]):
        uncertain = False
        for operation in operations[1:]:
            try:
                _restore_operation(operation, directory)
            except (MigrationError, OSError):
                uncertain = True
        if uncertain:
            raise MigrationError("migration recovery preserved an uncertain launcher")
        # Pin EVERY restored original through store retirement and journal commit.
        # If the platform cannot certify immutability, retain both for recovery.
        with certify_files(
            [
                (Path(item["target"]), _as_fingerprint(item["before"]))
                for item in operations[1:]
            ]
        ):
            _restore_operation(operations[0], directory)
            _retire_journal(private_env, directory, journal_identity)


def _publish_operation(operation, directory):
    target = Path(operation["target"])
    before = _as_fingerprint(operation["before"])
    if before is not None:
        _move_owned(target, directory / operation["displaced"], before)
    _publish_exclusive(directory / operation["candidate"], target)


def _migrate_locked(entries, private_env: Path, probe) -> MigrationResult:
    allowed_targets = {
        path
        for launcher, _, sidecar in entries
        for path in (launcher, sidecar)
        if path is not None
    }
    _recover_migration(private_env, allowed_targets)
    old_store, original_fingerprint = _snapshot(private_env)
    try:
        stored = _parse_private_env(old_store or b"")
        replacements = []
        originals = {}
        for launcher, secret_name, sidecar in entries:
            if secret_name not in _SECRET_ADAPTERS:
                raise MigrationError("unknown credential binding")
            original, fingerprint = _snapshot(launcher)
            if original is None:
                raise MigrationError("no credential found")
            assignments = parse_assignments(original.decode("utf-8"))
            value = assignments.get(_SECRET_VAR)
            target = launcher
            replacement = _rewritten_launcher(original)
            if not value and sidecar is not None:
                sidecar_bytes, sidecar_identity = _snapshot(sidecar)
                if (
                    sidecar_bytes is not None
                    and sidecar_bytes != f"# {_MIGRATED_NOTE}\n".encode()
                ):
                    value = sidecar_bytes.decode("utf-8").strip()
                    target, original, fingerprint = (
                        sidecar,
                        sidecar_bytes,
                        sidecar_identity,
                    )
                    replacement = f"# {_MIGRATED_NOTE}\n".encode()
            if not value:
                if stored.get(secret_name):
                    continue
                raise MigrationError("no credential found")
            stored[secret_name] = value
            if assignments.get(_BASE_VAR) and secret_name == "GLM_API_KEY":
                stored["GLM_BASE_URL"] = assignments[_BASE_VAR]
            originals[target] = (original, fingerprint)
            replacements.append(
                _Replacement(secret_name, target, original, replacement)
            )
        if not replacements:
            return MigrationResult(tuple(name for _, name, _ in entries), private_env)

        candidate_store = _encode_private_env(stored)
        directory = private_env.parent / f"subscription-backup-{uuid.uuid4().hex}"
        create_private_directory(directory)
        backups = []
        operations = []
        with stable_directories([directory]):
            pending = [(private_env, candidate_store, original_fingerprint)] + [
                (item.target, item.replacement, originals[item.target][1])
                for item in replacements
            ]
            for index, (target, candidate_data, before) in enumerate(pending):
                candidate = directory / f"candidate-{index}"
                _write_candidate(candidate, candidate_data)
                operations.append(
                    {
                        "target": str(target),
                        "before": before,
                        "candidate": candidate.name,
                        "candidate_identity": _snapshot(candidate)[1],
                        "displaced": f"displaced-{index}",
                    }
                )
                if index:
                    backup = directory / f"{index}-{target.name}.goncloud-backup"
                    _write_candidate(backup, originals[target][0])
                    backups.append(backup)
            # One immutable write-ahead journal records every candidate identity.
            # Every later move/publication is recoverable without post-write flags.
            journal = {
                "version": 2,
                "backup_dir": str(directory),
                "operations": operations,
            }
            journal_candidate = directory / "journal-candidate"
            _write_candidate(journal_candidate, json.dumps(journal).encode())
            journal_identity = _snapshot(journal_candidate)[1]
            _publish_exclusive(journal_candidate, _journal_path(private_env))
            _publish_operation(operations[0], directory)
            store_identity = _as_fingerprint(operations[0]["candidate_identity"])
            for item in replacements:
                current_store, fingerprint = _snapshot(private_env)
                if fingerprint != store_identity:
                    raise MigrationError(
                        "replacement store changed before verification"
                    )
                values = _parse_private_env(current_store)
                try:
                    evidence = probe(item.secret_name, values[item.secret_name])
                except Exception:
                    raise MigrationError("verified live probe required") from None
                if (
                    not isinstance(evidence, InvocationResult)
                    or sanitize_live_result(
                        _SECRET_ADAPTERS[item.secret_name], evidence
                    )["status"]
                    != "ready"
                ):
                    raise MigrationError("verified live probe required")
            # Keep the proven replacement immutable throughout all cleanup.
            with certify_files([(private_env, store_identity)]):
                for operation in operations[1:]:
                    _publish_operation(operation, directory)
                with certify_files(
                    [
                        (
                            Path(item["target"]),
                            _as_fingerprint(item["candidate_identity"]),
                        )
                        for item in operations[1:]
                    ]
                ):
                    _retire_journal(private_env, directory, journal_identity)
            return MigrationResult(
                tuple(name for _, name, _ in entries), private_env, tuple(backups)
            )
    except BaseException as error:
        try:
            _recover_migration(private_env, allowed_targets)
            restored = True
        except BaseException:
            restored = False
        if not isinstance(error, Exception):
            raise
        if isinstance(error, MigrationError) and restored:
            raise MigrationError(str(error)) from None
        raise MigrationError(
            "verified live probe required; migration failed; recovery copies preserved"
        ) from None


def _probe_routes():
    from subscriptions.catalog import compile_profile_routes, load_routing_source

    routing = load_routing_source()
    routes = {}
    for profile in routing.get("chains", {}):
        for route in compile_profile_routes(profile, routing):
            routes.setdefault(route.adapter, route)
    return routes


def build_real_probe(private_env: Path | None = None):
    """Infer with the persisted replacement and the table's first family model."""
    from subscriptions.adapters.deepseek import DeepSeekAdapter
    from subscriptions.adapters.zai import ZAIAdapter

    routes = _probe_routes()

    def probe(name: str, value: str) -> InvocationResult:
        stored = read_private_env(private_env) if private_env is not None else {}
        if private_env is not None and stored.get(name) != value:
            raise MigrationError("replacement store verification mismatch")
        if name == "DEEPSEEK_API_KEY":
            adapter = DeepSeekAdapter(
                api_key=value, base_url=stored.get("DEEPSEEK_BASE_URL")
            )
            adapter_id = "deepseek"
        elif name == "GLM_API_KEY":
            adapter = ZAIAdapter(auth_token=value, base_url=stored.get("GLM_BASE_URL"))
            adapter_id = "zai"
        else:
            raise MigrationError("unknown credential binding")
        return adapter.live_probe(routes[adapter_id])

    return probe


def run_live_probe_all(
    json_out: Path | None = None,
    only: str | None = None,
    *,
    private_env: Path | None = None,
) -> dict:
    """One minimal real inference per adapter; presence alone never passes."""
    from subscriptions.adapters import build_adapter_registry

    routes = _probe_routes()
    order = ADAPTER_ORDER
    if only:
        selected = {name.strip() for name in only.split(",") if name.strip()}
        unknown = selected - set(order)
        if unknown:
            raise SystemExit("unknown adapters for --only")
        order = tuple(name for name in order if name in selected)
    stored = read_private_env(private_env or default_private_env_path())
    adapters = build_adapter_registry(
        zai_auth_token=stored.get("GLM_API_KEY"),
        zai_base_url=stored.get("GLM_BASE_URL"),
        deepseek_api_key=stored.get("DEEPSEEK_API_KEY"),
        deepseek_base_url=stored.get("DEEPSEEK_BASE_URL"),
    )
    results = []
    # One-word diagnostics must remain inside the adapter process deadlines.
    for adapter in adapters.values():
        adapter.timeout_seconds = min(adapter.timeout_seconds, 300)
        adapter.probe_timeout_seconds = min(adapter.probe_timeout_seconds, 30)
    with tempfile.TemporaryDirectory(prefix="goncloud-live-") as tmp:
        workspace = Path(tmp)
        for name in order:
            adapter = adapters[name]
            route = routes[name]
            print(f"probing {name} ...", flush=True)
            try:
                if name in {"zai", "deepseek"}:
                    evidence = adapter.live_probe(route)
                else:
                    evidence = adapter.probe(route)
                    if evidence.ready:
                        evidence = adapter.invoke(
                            InvocationRequest(
                                workflow_id="live-probe",
                                stage_id=f"probe-{name}",
                                role="claw",
                                prompt=PROBE_PROMPT,
                                workspace=workspace,
                                start_revision="",
                                current_revision="",
                                max_steps=1,
                            ),
                            route,
                        )
                entry = sanitize_live_result(name, evidence)
            except Exception as error:
                entry = sanitize_live_result(name, error)
            results.append(entry)
            print(_format_probe(entry))
    summary = {
        "results": results,
        "passed": sum(1 for r in results if r["status"] == "ready"),
        "total": len(results),
    }
    if json_out is not None:
        _atomic_write(json_out, (json.dumps(summary, indent=2) + "\n").encode())
    return summary


def _format_probe(entry: dict) -> str:
    return (
        f"{entry['status']:<12} {entry['billing_mode']:<12} "
        f"{entry['adapter']:<12} {entry['failure'] or '-'}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate-launchers", action="store_true")
    parser.add_argument("--deepseek", type=Path, default=default_deepseek_launcher())
    parser.add_argument("--glm", type=Path, default=default_glm_launcher())
    parser.add_argument("--private-env", type=Path, default=default_private_env_path())
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Probe replacements against the vendor before rewriting launchers",
    )
    parser.add_argument("--live-probe-all", action="store_true")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated adapter subset for --live-probe-all batches",
    )
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    if args.migrate_launchers and not args.verify:
        parser.error("--migrate-launchers requires --verify")

    if args.migrate_launchers:
        try:
            migrate_launcher_secrets(
                args.glm,
                args.deepseek,
                args.private_env,
                build_real_probe(args.private_env),
            )
        except Exception:
            print("migration failed; launcher credentials remain recoverable")
            return 1
        print("migration verified; private recovery backups retained")

    if args.live_probe_all:
        try:
            summary = run_live_probe_all(
                args.json, only=args.only, private_env=args.private_env
            )
        except Exception:
            print("live verification failed")
            return 1
        return 0 if summary["passed"] == summary["total"] else 1

    if not (args.migrate_launchers or args.live_probe_all):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
