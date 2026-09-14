"""Install versioned team configuration without cloning credentials or user state."""

import argparse
import copy
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
MISSING = object()

MODEL_PROVIDER_ALIASES = {
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "kimi": "kimi-coding",
    "opencode": "opencode-zen",
    "opencode-go": "opencode-go",
    "opencode-go-resp": "opencode-go",
    "xai": "xai",
    "zai": "zai",
}


def translate_model_chain(chain):
    translated = []
    for route in chain:
        try:
            alias, model = route.split("/", 1)
        except ValueError as error:
            raise ValueError(f"Invalid model route: {route}") from error
        provider = MODEL_PROVIDER_ALIASES.get(alias)
        if provider is None:
            raise ValueError(f"Unknown model provider alias: {alias}")
        translated.append({"provider": provider, "model": model.removesuffix(" dup")})
    return translated


def model_config_for_profile(profile, routing):
    source_name = next(
        (
            source
            for source, mapped_profile in routing.get("profile_names", {}).items()
            if mapped_profile == profile
        ),
        profile,
    )
    source_name = routing.get("profile_inheritance", {}).get(profile, source_name)
    try:
        chain = translate_model_chain(routing["chains"][source_name])
    except KeyError as error:
        raise ValueError(f"No model chain configured for profile: {profile}") from error
    if not chain:
        raise ValueError(f"Empty model chain configured for profile: {profile}")
    primary, *fallbacks = chain
    return {
        "model": {"provider": primary["provider"], "default": primary["model"]},
        "fallback_providers": fallbacks,
    }


def merge_profile_config(
    existing, desired, previous, *, fresh=False, take_over_model=False
):
    merge_source = copy.deepcopy(existing)
    if take_over_model:
        merge_source.pop("model", None)
        merge_source.pop("fallback_providers", None)
    return merge_owned(merge_source, desired, previous, fresh=fresh)


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
    return result


def check_file(target, desired, previous=None):
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if current != desired and current != previous:
            raise ValueError(f"Local managed-file conflict: {target}")


def contained(path, parent):
    resolved = path.resolve()
    if not resolved.is_relative_to(parent.resolve()):
        raise ValueError(f"Path escapes runtime directory: {path}")
    return resolved


def write_changed(path, text):
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # A recoverable copy; no credentials are included in the ownership manifest.
    if path.exists():
        backup = path.with_name(path.name + ".goncloud-backup")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".goncloud-tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    temporary.replace(path)


def configure(runtime_home, install_dir, apply_model_routing=False):
    import yaml

    runtime_home = runtime_home.resolve()
    os.environ["HERMES_HOME"] = str(runtime_home)
    os.environ["HERMES_PROFILES_DIR"] = str(runtime_home / "profiles")
    sys.path.insert(0, str(install_dir.resolve()))
    from hermes_cli.profiles import create_profile, get_profile_dir, seed_profile_skills

    team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
    routing = json.loads(
        (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
    )
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
        config_path = contained(profile / "config.yaml", runtime_home)
        existing = (
            yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if config_path.exists()
            else {}
        )
        desired = {
            "plugins": {"enabled": ["goncloud-team"]},
            "platform_toolsets": {
                "cli": ["coding", "kanban", "goncloud_team"],
                "telegram": ["coding", "kanban", "goncloud_team"],
            },
            "terminal": {
                "backend": "local",
                "cwd": str(runtime_home / "workspaces" / role),
                "timeout": 180,
            },
            "kanban": team["kanban"],
        }
        if apply_model_routing:
            desired.update(model_config_for_profile(role, routing))
        previous_config = state.get("config", {})
        merged = merge_profile_config(
            existing or {},
            desired,
            previous_config,
            fresh=fresh,
            take_over_model=apply_model_routing and "model" not in previous_config,
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
        }
        for source in plugin_files:
            files[
                f"plugins/goncloud-team/{source.relative_to(plugin_root).as_posix()}"
            ] = source.read_text(encoding="utf-8")
        for relative, content in files.items():
            target = contained(profile / relative, runtime_home)
            old = state.get("files", {}).get(relative)
            if fresh and relative == "SOUL.md" and target.exists():
                old = target.read_text(encoding="utf-8")
            check_file(target, content, old)
        prepared.append((profile, config_path, merged, files, state_path, desired))
    # Validate every existing profile before changing any managed content.
    for profile, config_path, merged, files, state_path, desired in prepared:
        write_changed(
            config_path, yaml.safe_dump(merged, sort_keys=False, allow_unicode=True)
        )
        for relative, content in files.items():
            write_changed(profile / relative, content)
        write_changed(
            state_path,
            json.dumps(
                {"config": desired, "files": files}, indent=2, ensure_ascii=False
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
        help="Apply the user-approved model and fallback chains",
    )
    args = parser.parse_args()
    configure(args.home, args.install_dir, args.apply_model_routing)


if __name__ == "__main__":
    main()
