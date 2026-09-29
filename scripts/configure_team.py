"""Install versioned team configuration without cloning credentials or user state."""

import argparse
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MISSING = object()

#: Workers route their models through the subscription router and never touch a
#: browser or the machine; three toolsets cover their work.
_WORKER_TOOLSETS = ["coding", "kanban", "goncloud_team"]

#: The director drives Odoo and Edge over Telegram (owner decision 2026-09-15),
#: so it carries the full toolset — browser and computer_use included. Baking it
#: in stops the director from re-editing its own config every session. Its role
#: text, not a narrow toolset, keeps it out of logs and plugin code. No kanban:
#: the director may not create or dispatch cards (goncloud_local_run does and
#: goncloud_local_status reads them), and its 14 schemas cost 23.5 KB of every
#: call (hermes prompt-size, 2026-09-27).
_DIRECTOR_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "coding",
    "computer_use",
    "connections",
    "delegation",
    "file",
    "goncloud_team",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "vision",
    "web",
]

ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"

#: How Hermes reaches each table adapter natively. Native tool calls are the
#: point: through a CLI with its tools off, the director had to write calls as
#: text and 8 of 13 were dropped on mis-escaped JSON (2026-09-28).
_NATIVE_PROVIDERS = {
    "opencode_go": {"provider": "opencode-go"},
    # The owner's second Go account is a second credential of the same
    # provider; Hermes' credential pool rotates to it on 429/exhaustion.
    "opencode_go_2": {"provider": "opencode-go"},
    "claude": {"provider": "anthropic"},
    "muse": {"provider": "meta-ai"},
    "grok": {"provider": "xai-oauth"},
    "opencode_zai": {"provider": "zai", "base_url": ZAI_CODING_URL},
    "codex": {"provider": "openai-codex"},
    "kimi": {"provider": "kimi-coding"},
}


def director_native_config(source):
    """The director's native chain, compiled from the table's ``main`` row."""
    plugin_dir = str(ROOT / "plugins" / "goncloud-team")
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from subscriptions.catalog import compile_profile_routes

    entries, overrides = [], {}
    for route in compile_profile_routes("claw", source):
        binding = _NATIVE_PROVIDERS.get(route.adapter)
        if binding is None:
            raise ValueError(
                f"no native provider for table position {route.slot}: {route.label}"
            )
        entry = {**binding, "model": route.model}
        if not entries or entries[-1] != entry:
            entries.append(entry)
        if route.reasoning_effort:
            chosen = overrides.setdefault(route.model, route.reasoning_effort)
            if chosen != route.reasoning_effort:
                raise ValueError(f"two reasoning efforts for {route.model}")
    primary, *fallbacks = entries
    model = {"provider": primary["provider"], "default": primary["model"]}
    if "base_url" in primary:
        model["base_url"] = primary["base_url"]
    return {
        "model": model,
        "fallback_providers": fallbacks,
        "agent": {"reasoning_overrides": overrides},
    }


def remove_owned_native_routing(existing, previous):
    cleaned = copy.deepcopy(existing)
    for key in ("model", "fallback_providers"):
        if key not in previous:
            continue
        if cleaned.get(key, MISSING) != previous[key]:
            raise ValueError(f"user-managed model conflict: {key}")
        cleaned.pop(key, None)
    return cleaned


def merge_profile_config(existing, desired, previous, *, fresh=False):
    existing = dict(existing)
    desired = dict(desired)
    if "model" not in desired:
        return merge_owned(existing, desired, previous, fresh=fresh)
    # The director model is owned whole, never deep-merged: a stock or
    # user-tuned block would otherwise leak its provider/base_url into it and
    # the next apply would read that mixture as a user conflict.
    wanted = desired.pop("model")
    current = existing.pop("model", MISSING)
    before = previous.get("model", MISSING)
    if not (
        fresh
        or current is MISSING
        or current == wanted
        or (before is not MISSING and current == before)
    ):
        raise ValueError(
            "Local configuration conflict: model; preserve and reconcile manually"
        )
    merged = merge_owned(existing, desired, previous, fresh=fresh)
    merged["model"] = copy.deepcopy(wanted)
    return merged


def merge_owned(existing, desired, previous, fresh=False, prefix=""):
    if not isinstance(existing, dict):
        raise ValueError(f"Non-mapping configuration section: {prefix}")
    result = copy.deepcopy(existing)
    for key, value in desired.items():
        location = f"{prefix}.{key}".strip(".")
        current = existing.get(key, MISSING)
        before = previous.get(key, MISSING)
        if isinstance(value, dict):
            result[key] = merge_owned(
                {} if current is MISSING else current,
                value,
                before if isinstance(before, dict) else {},
                fresh,
                location,
            )
        elif (
            fresh
            or current is MISSING
            or current == value
            or (before is not MISSING and current == before)
        ):
            result[key] = copy.deepcopy(value)
        else:
            raise ValueError(
                f"Local configuration conflict: {location}; preserve and reconcile manually"
            )
    # A key this script owned last time and no longer ships is retired, unless
    # the user changed it, which is a conflict like any other.
    for key, before in previous.items():
        if key in desired or key not in existing:
            continue
        if existing[key] != before:
            location = f"{prefix}.{key}".strip(".")
            raise ValueError(
                f"Local configuration conflict: {location}; preserve and reconcile manually"
            )
        result.pop(key)
    return result


def check_file(target, desired, previous=None):
    if target.exists():
        current = (
            target.read_bytes()
            if isinstance(desired, bytes)
            else target.read_text(encoding="utf-8")
        )
        if isinstance(desired, bytes) and isinstance(previous, str):
            previous = previous.encode("utf-8")
        if current != desired and current != previous:
            raise ValueError(f"Local managed-file conflict: {target}")


def _prune_empty_dirs(directory, stop):
    """Remove directories a retired file left empty (``__pycache__`` aside).

    Never removes ``stop`` itself or anything outside it: the loop only
    continues while ``stop`` is a strict ancestor of ``directory``.
    """
    directory = directory.resolve()
    stop = stop.resolve()
    while stop in directory.parents and directory.is_dir():
        if any(child.name != "__pycache__" for child in directory.iterdir()):
            return
        shutil.rmtree(directory)
        directory = directory.parent


def contained(path, parent):
    resolved = path.resolve()
    if not resolved.is_relative_to(parent.resolve()):
        raise ValueError(f"Path escapes runtime directory: {path}")
    return resolved


def _git(root, *args):
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True
        )
    except OSError:
        return None


def current_revision(root=ROOT):
    """The commit this tree deploys from, or None outside a Git checkout."""
    result = _git(root, "rev-parse", "HEAD")
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def tree_contains(revision, root=ROOT):
    """True when HEAD descends from ``revision`` (an unknown commit counts as no)."""
    result = _git(root, "merge-base", "--is-ancestor", revision, "HEAD")
    return result is not None and result.returncode == 0


def refuse_regression(state, root=ROOT, *, allow_regression=False):
    """Refuse to overwrite profiles deployed from a commit this tree lacks.

    2026-09-15: a reapply from a sibling branch replaced every managed file
    with older content without a conflict (the on-disk files matched the
    recorded state), so the running gateway lost plugin symbols mid-flight.
    """
    deployed = state.get("source_revision")
    if not deployed or allow_regression or tree_contains(deployed, root):
        return
    head = current_revision(root) or "no revision"
    raise ValueError(
        f"HEAD {head[:12]} does not contain the deployed revision "
        f"{deployed[:12]}; merge it first or pass --allow-regression"
    )


def write_changed(path, content):
    if path.exists() and (
        path.read_bytes() == content
        if isinstance(content, bytes)
        else path.read_text(encoding="utf-8") == content
    ):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # A recoverable copy; no credentials are included in the ownership manifest.
    if path.exists():
        backup = path.with_name(path.name + ".goncloud-backup")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".goncloud-tmp")
    if isinstance(content, bytes):
        temporary.write_bytes(content)
    else:
        temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


def desired_profile_config(role, team, runtime_home):
    """The managed keys one profile owns; everything else stays the user's."""
    desired = {
        "plugins": {"enabled": ["goncloud-team"]},
        # Copilot and Copilot ACP are out of scope for this team. deepseek is
        # the one adapter that bills as API: the subscription router reaches
        # it deliberately at two slots through its own adapter, so leaving it
        # in a native catalog would let an agent spend outside that decision.
        # opencode-free is not one of the owner's accounts.
        "model_catalog": {
            "excluded_providers": [
                "copilot",
                "copilot-acp",
                "deepseek",
                "opencode-free",
            ]
        },
        "platform_toolsets": {
            "cli": list(_WORKER_TOOLSETS),
            "telegram": list(_WORKER_TOOLSETS),
        },
        "terminal": {
            "backend": "local",
            "cwd": str(runtime_home / "workspaces" / role),
            "timeout": 180,
        },
        "kanban": team["kanban"],
    }
    if role == team["director"]:
        # The director converses on native providers (native tool calls),
        # compiled from the authoritative main row so no hand-written chain
        # can contradict the table.
        desired.update(
            director_native_config(
                json.loads(
                    (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
                )
            )
        )
        # The director carries the full toolset over Telegram by owner decision
        # (browser, computer_use and the rest): it drives Odoo and Edge and runs
        # Gmail/Calendar/Drive through a terminal-driven skill. Its role text,
        # not a narrow toolset, keeps it out of logs and plugin code.
        desired["platform_toolsets"] = {
            "cli": list(_DIRECTOR_TOOLSETS),
            "telegram": list(_DIRECTOR_TOOLSETS),
        }
    return desired


def configure(
    runtime_home,
    install_dir,
    apply_model_routing=False,
    *,
    allow_regression=False,
    preserve_local_soul=False,
):
    import yaml

    runtime_home = runtime_home.resolve()
    os.environ["HERMES_HOME"] = str(runtime_home)
    os.environ["HERMES_PROFILES_DIR"] = str(runtime_home / "profiles")
    sys.path.insert(0, str(install_dir.resolve()))
    from hermes_cli.profiles import create_profile, get_profile_dir, seed_profile_skills

    team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
    plugin_root = ROOT / "plugins" / "goncloud-team"
    if not (plugin_root / "plugin.yaml").is_file():
        raise ValueError(
            "Workflow plugin is missing; finish the build before applying profiles"
        )
    plugin_files = [
        p
        for p in plugin_root.rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".md"}
    ]
    revision = current_revision()
    prepared = []
    for role, description in team["profiles"].items():
        profile = contained(get_profile_dir(role), runtime_home)
        fresh = not profile.exists()
        if fresh:
            create_profile(role, no_alias=True, description=description)
        state_path = contained(profile / "goncloud-managed.json", runtime_home)
        state = (
            json.loads(state_path.read_text(encoding="utf-8"))
            if state_path.exists()
            else {}
        )
        refuse_regression(state, allow_regression=allow_regression)
        config_path = contained(profile / "config.yaml", runtime_home)
        existing = (
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
        desired = desired_profile_config(role, team, runtime_home)
        previous_config = state.get("config", {})
        existing = remove_owned_native_routing(existing or {}, previous_config)
        merged = merge_profile_config(
            existing,
            desired,
            previous_config,
            fresh=fresh,
        )
        files = {
            "SOUL.md": "\n\n".join(
                (ROOT / name).read_text(encoding="utf-8")
                for name in (
                    "shared/CONTRACT.md",
                    "shared/BUSINESS.md",
                    f"roles/{role}.md",
                )
            ),
            # The router needs its slot table next to the plugin copy.
            "plugins/goncloud-team/model-routing.source.json": (
                ROOT / "model-routing.source.json"
            ).read_bytes(),
        }
        for source in plugin_files:
            files[
                f"plugins/goncloud-team/{source.relative_to(plugin_root).as_posix()}"
            ] = source.read_text(encoding="utf-8")
        shipped = set(files)
        if preserve_local_soul and (profile / "SOUL.md").exists():
            files.pop("SOUL.md")
        for relative, content in files.items():
            target = contained(profile / relative, runtime_home)
            old = state.get("files", {}).get(relative)
            if fresh and relative == "SOUL.md" and target.exists():
                old = target.read_text(encoding="utf-8")
            check_file(target, content, old)
        retired = [rel for rel in state.get("files", {}) if rel not in shipped]
        retired_targets = {}
        for relative in retired:
            # A retired path is confined to its own profile, never a
            # sibling's: containment is scoped to ``profile``, not the whole
            # runtime, so a managed-file record like "../scout/x" aborts the
            # deploy instead of touching another profile.
            target = contained(profile / relative, profile)
            # Deleting is safe only for the exact content this script wrote.
            check_file(target, state["files"][relative])
            retired_targets[relative] = target
        prepared.append(
            (
                profile,
                config_path,
                merged,
                files,
                state_path,
                desired,
                state,
                retired_targets,
                shipped,
            )
        )
    # Validate every existing profile before changing any managed content.
    for (
        profile,
        config_path,
        merged,
        files,
        state_path,
        desired,
        state,
        retired_targets,
        shipped,
    ) in prepared:
        recorded_files = {
            **{
                rel: content
                for rel, content in state.get("files", {}).items()
                if rel in shipped
            },
            **{
                relative: (
                    content.decode("utf-8") if isinstance(content, bytes) else content
                )
                for relative, content in files.items()
            },
        }
        write_changed(
            config_path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        )
        for relative, content in files.items():
            write_changed(profile / relative, content)
        for target in retired_targets.values():
            if target.exists():
                target.unlink()
            _prune_empty_dirs(target.parent, profile)
        write_changed(
            state_path,
            json.dumps(
                {
                    "config": desired,
                    "source_revision": revision,
                    "files": recorded_files,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        contained(Path(desired["terminal"]["cwd"]), runtime_home).mkdir(
            parents=True, exist_ok=True
        )
        if not any((profile / "skills").rglob("SKILL.md")):
            if seed_profile_skills(profile, quiet=True) is None:
                raise RuntimeError(
                    f"Could not seed bundled skills for {profile.name}; reapply to retry"
                )
        print(f"Configured {profile.name}: {profile}")


def main():
    default_home = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=default_home)
    parser.add_argument(
        "--install-dir", type=Path, default=default_home / "hermes-agent"
    )
    parser.add_argument(
        "--apply-model-routing",
        action="store_true",
        help="Deprecated compatibility flag; installs the authoritative routing source",
    )
    parser.add_argument(
        "--allow-regression",
        action="store_true",
        help="Overwrite profiles deployed from a commit this checkout does not contain",
    )
    parser.add_argument(
        "--preserve-local-soul",
        action="store_true",
        help=(
            "Keep existing profile SOUL.md files while updating managed routing, "
            "plugins, and configuration"
        ),
    )
    args = parser.parse_args()
    configure(
        args.home,
        args.install_dir,
        args.apply_model_routing,
        allow_regression=args.allow_regression,
        preserve_local_soul=args.preserve_local_soul,
    )


if __name__ == "__main__":
    main()
