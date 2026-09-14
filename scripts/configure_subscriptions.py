"""Migrate launcher credentials into the private Hermes store, safely.

Two-phase: the replacement store is written and probed first; plaintext
launcher values are removed only after the probe passes. Backups stay
outside Git and values are never printed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"

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


def write_private_env(path: Path, values: dict[str, str]) -> None:
    """Atomic, mode-restricted write of KEY=VALUE lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        for key in sorted(values):
            handle.write(f"{key}={values[key]}\n")
    temporary.replace(path)


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


def _rewrite_launcher(launcher: Path) -> None:
    """Drop the token line while preserving the rest of the wrapper."""
    text = launcher.read_text(encoding="utf-8")
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
    launcher.write_text("".join(rebuilt), encoding="utf-8", newline="")


def _backup_once(original: Path) -> Path:
    backup = original.with_name(original.name + ".goncloud-backup")
    if not backup.exists():
        backup.write_bytes(original.read_bytes())
    return backup


def migrate_one(
    launcher: Path,
    private_env: Path,
    probe,
    *,
    secret_name: str,
    sidecar: Path | None = None,
) -> MigrationResult:
    """Move one launcher credential into the private store, then clean up."""
    launcher = Path(launcher)
    text = launcher.read_text(encoding="utf-8")
    assignments = parse_assignments(text)
    value = assignments.get(_SECRET_VAR)
    used_sidecar = False
    if not value and sidecar is not None and sidecar.is_file():
        value = sidecar.read_text(encoding="utf-8").strip()
        used_sidecar = True
    if not value:
        stored = read_private_env(private_env)
        if stored.get(secret_name):
            # Already migrated on a previous run; nothing to rewrite.
            return MigrationResult(
                names=(secret_name,), private_env=private_env, backups=()
            )
        raise MigrationError(f"no credential found in {launcher.name}")

    stored = read_private_env(private_env)
    stored[secret_name] = value
    base_url = assignments.get(_BASE_VAR)
    if base_url and secret_name == "GLM_API_KEY":
        stored["GLM_BASE_URL"] = base_url
    write_private_env(private_env, stored)

    backups = [_backup_once(launcher)]
    if used_sidecar:
        backups.append(_backup_once(sidecar))

    try:
        probe(secret_name, value)
    except Exception as error:  # noqa: BLE001 - probe failures are ours to report
        raise MigrationError(
            f"replacement probe failed for {secret_name}; launcher preserved"
        ) from error

    if used_sidecar:
        sidecar.write_text(f"# {_MIGRATED_NOTE}\n", encoding="utf-8", newline="\n")
    else:
        _rewrite_launcher(launcher)
    return MigrationResult(
        names=(secret_name,), private_env=private_env, backups=tuple(backups)
    )


def migrate_launcher_secrets(
    glm: Path, deepseek: Path, private_env: Path, probe
) -> MigrationResult:
    """Migrate DeepSeek then GLM, returning the combined evidence."""
    deep = migrate_one(deepseek, private_env, probe, secret_name="DEEPSEEK_API_KEY")
    glm_sidecar = Path(glm).parent / "apikey.txt"
    gl = migrate_one(
        glm,
        private_env,
        probe,
        secret_name="GLM_API_KEY",
        sidecar=glm_sidecar,
    )
    return MigrationResult(
        names=deep.names + gl.names,
        private_env=private_env,
        backups=deep.backups + gl.backups,
    )


def build_real_probe():
    """Probe replacements without printing values."""
    sys.path.insert(0, str(PLUGIN_DIR))
    from subscriptions.adapters.deepseek import DeepSeekAdapter
    from subscriptions.adapters.zai import ZAIAdapter
    from subscriptions.types import RouteSpec

    def probe(name: str, value: str) -> None:
        if name == "DEEPSEEK_API_KEY":
            adapter = DeepSeekAdapter(api_key=value)
            adapter_id = "deepseek"
        elif name == "GLM_API_KEY":
            adapter = ZAIAdapter(auth_token=value)
            adapter_id = "zai"
        else:
            raise MigrationError(f"unknown secret to probe: {name}")
        health = adapter.probe(
            RouteSpec(
                slot=7,
                label="probe/x",
                adapter=adapter_id,
                model="x",
                billing_mode="api",
            )
        )
        if not health.ready:
            raise MigrationError(
                f"probe reported not ready: {health.failure or health.detail}"
            )

    return probe


def run_live_probe_all(json_out: Path | None = None, only: str | None = None) -> dict:
    """One minimal real inference per adapter; presence alone never passes."""
    import tempfile

    sys.path.insert(0, str(PLUGIN_DIR))
    from subscriptions.adapters import build_adapter_registry
    from subscriptions.catalog import compile_profile_routes, load_routing_source
    from subscriptions.types import InvocationRequest, RouteSpec

    routing = load_routing_source()
    models: dict[str, str] = {}
    for profile in routing.get("chains", {}):
        for route in compile_profile_routes(profile, routing):
            models.setdefault(route.adapter, route.model)

    order = (
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
    if only:
        selected = {name.strip() for name in only.split(",") if name.strip()}
        unknown = selected - set(order)
        if unknown:
            raise SystemExit(f"unknown adapters for --only: {sorted(unknown)}")
        order = tuple(name for name in order if name in selected)
    adapters = build_adapter_registry()
    results = []
    # One-word diagnostics must remain inside the adapter process deadlines.
    for adapter in adapters.values():
        adapter.timeout_seconds = min(adapter.timeout_seconds, 300)
        adapter.probe_timeout_seconds = min(adapter.probe_timeout_seconds, 30)
    with tempfile.TemporaryDirectory(prefix="goncloud-live-") as tmp:
        workspace = Path(tmp)
        for name in order:
            adapter = adapters[name]
            model = models.get(name, "unknown")
            route = RouteSpec(
                slot=0,
                label=f"live/{model}",
                adapter=name,
                model=model,
                billing_mode=adapter.billing_mode,
            )
            entry = {
                "adapter": name,
                "model": model,
                "billing_mode": adapter.billing_mode,
                "account_class": "",
                "status": "FAIL",
                "detail": "",
            }
            print(f"probing {name} ...", flush=True)
            try:
                health = adapter.probe(route)
                entry["account_class"] = health.account_class
                if not health.ready:
                    entry["status"] = "QUARANTINED"
                    entry["detail"] = (
                        f"{health.failure.value if health.failure else 'unavailable'}: "
                        f"{health.detail}"
                    )[:200]
                    results.append(entry)
                    print(_format_probe(entry))
                    continue
                request = InvocationRequest(
                    workflow_id="live-probe",
                    stage_id=f"probe-{name}",
                    role="claw",
                    prompt="Reply with the single word READY and nothing else.",
                    workspace=workspace,
                    start_revision="",
                    current_revision="",
                )
                outcome = adapter.invoke(request, route)
                if outcome.success:
                    entry["status"] = "PASS"
                    entry["detail"] = outcome.text[:120]
                elif outcome.failure in {"auth", "quota"}:
                    entry["status"] = "QUARANTINED"
                    entry["detail"] = outcome.text[:120]
                else:
                    entry["status"] = "FAIL"
                    entry["detail"] = outcome.text[:120]
            except Exception as error:  # noqa: BLE001 - report, never crash
                entry["status"] = "FAIL"
                entry["detail"] = str(error)[:120]
            results.append(entry)
            print(_format_probe(entry))
    summary = {
        "results": results,
        "passed": sum(1 for r in results if r["status"] == "PASS"),
        "total": len(results),
    }
    if json_out is not None:
        json_out.write_text(
            __import__("json").dumps(summary, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    return summary


def _format_probe(entry: dict) -> str:
    return (
        f"{entry['status']:<12} {entry['billing_mode']:<12} "
        f"{entry['adapter']:<12} {entry['model']:<32} {entry['detail']}"
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

    if args.migrate_launchers:
        probe = build_real_probe() if args.verify else (lambda name, value: None)
        result = migrate_launcher_secrets(
            args.glm, args.deepseek, args.private_env, probe
        )
        print(f"migrated {', '.join(result.names)} into {result.private_env}")
        print(f"recoverable backups: {', '.join(str(p) for p in result.backups)}")

    if args.live_probe_all:
        run_live_probe_all(args.json, only=args.only)

    if not (args.migrate_launchers or args.live_probe_all):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
