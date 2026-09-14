"""Migrate launcher credentials into the private Hermes store, safely.

Two-phase: the replacement store is written and probed first; plaintext
launcher values are removed only after the probe passes. Backups stay
outside Git and values are never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
from subscriptions.adapters import classify_failure  # noqa: E402
from subscriptions.adapters._live_probe import PROBE_MARKER, PROBE_PROMPT  # noqa: E402
from subscriptions.process import ProcessEvidence  # noqa: E402
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


def _atomic_write(path: Path, data: bytes) -> None:
    """Flush a unique private file before atomically replacing its destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".subscription-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_private_env(path: Path, values: dict[str, str]) -> None:
    """Validate environment lines before an atomic, mode-restricted write."""
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or any(
            char in value for char in "\r\n\0"
        ):
            raise MigrationError("invalid private environment entry")
    _atomic_write(
        path,
        "".join(f"{key}={values[key]}\n" for key in sorted(values)).encode("utf-8"),
    )


def read_private_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
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
    private_env.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        private_env.with_name(private_env.name + ".lock"), os.O_RDWR | os.O_CREAT, 0o600
    )
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
    resolved_store = private_env.resolve()
    inside_checkout = resolved_store.is_relative_to(ROOT)
    for parent in resolved_store.parents:
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
    for launcher, _, sidecar in entries:
        if any(
            path is not None and path.resolve() == private_env.resolve()
            for path in (launcher, sidecar)
        ):
            raise MigrationError("credential paths must be distinct")
    with _migration_lock(private_env):
        return _migrate_locked(entries, private_env, probe)


def _migrate_locked(entries, private_env: Path, probe) -> MigrationResult:
    originals = {}
    cleaned = []
    backups = []
    store_written = False
    written_store = None
    old_store = private_env.read_bytes() if private_env.exists() else None
    try:
        stored = read_private_env(private_env)
        replacements = []
        for launcher, secret_name, sidecar in entries:
            if secret_name not in _SECRET_ADAPTERS:
                raise MigrationError("unknown credential binding")
            original = launcher.read_bytes()
            assignments = parse_assignments(original.decode("utf-8"))
            value = assignments.get(_SECRET_VAR)
            target = launcher
            replacement = _rewritten_launcher(original)
            originals[launcher] = original
            if not value and sidecar is not None and sidecar.is_file():
                sidecar_bytes = sidecar.read_bytes()
                if sidecar_bytes != f"# {_MIGRATED_NOTE}\n".encode():
                    value = sidecar_bytes.decode("utf-8").strip()
                    target, original = sidecar, sidecar_bytes
                    originals[target] = original
                    replacement = f"# {_MIGRATED_NOTE}\n".encode()
            if not value:
                if stored.get(secret_name):
                    continue  # No credential cleanup remains after a previous run.
                raise MigrationError("no credential found")
            if target.resolve() == private_env.resolve():
                raise MigrationError("credential paths must be distinct")
            stored[secret_name] = value
            if assignments.get(_BASE_VAR) and secret_name == "GLM_API_KEY":
                stored["GLM_BASE_URL"] = assignments[_BASE_VAR]
            replacements.append(
                _Replacement(secret_name, target, original, replacement)
            )
        if not replacements:
            return MigrationResult(tuple(name for _, name, _ in entries), private_env)

        write_private_env(private_env, stored)
        store_written = True
        written_store = private_env.read_bytes()
        for item in replacements:
            replacement_values = read_private_env(private_env)
            if replacement_values != stored:
                raise MigrationError("replacement store changed before verification")
            try:
                evidence = probe(item.secret_name, replacement_values[item.secret_name])
            except Exception:
                raise MigrationError("verified live probe required") from None
            adapter = _SECRET_ADAPTERS[item.secret_name]
            if (
                not isinstance(evidence, InvocationResult)
                or sanitize_live_result(adapter, evidence)["status"] != "ready"
            ):
                raise MigrationError("verified live probe required")

        # Flush exact snapshots before touching a launcher. A killed process
        # leaves either original or fully rewritten bytes plus the proven store.
        backup_dir = Path(
            tempfile.mkdtemp(prefix="subscription-backup-", dir=private_env.parent)
        )
        for index, (target, original) in enumerate(originals.items()):
            if target.read_bytes() != original:
                raise MigrationError("launcher changed during verification")
            backup = backup_dir / f"{index}-{target.name}.goncloud-backup"
            _atomic_write(backup, original)
            backups.append(backup)
        if read_private_env(private_env) != stored:
            raise MigrationError("replacement store changed after verification")
        for item in replacements:
            if item.target.read_bytes() != item.original:
                raise MigrationError("launcher changed during cleanup")
            _atomic_write(item.target, item.replacement)
            cleaned.append(item.target)
        return MigrationResult(
            tuple(name for _, name, _ in entries), private_env, tuple(backups)
        )
    except BaseException as error:
        restored = True
        for target in reversed(cleaned):
            try:
                _atomic_write(target, originals[target])
            except OSError:
                restored = False  # Keep proven store and durable recovery copies.
        if store_written and restored:
            try:
                if private_env.read_bytes() != written_store:
                    restored = False  # Never overwrite an intervening user edit.
                elif old_store is None:
                    private_env.unlink(missing_ok=True)
                else:
                    _atomic_write(private_env, old_store)
            except OSError:
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
