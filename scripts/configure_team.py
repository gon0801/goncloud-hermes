"""Install versioned team configuration without cloning credentials or user state."""

import argparse
import collections
import copy
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
MISSING = object()

#: Workers route their models through the subscription router and never touch a
#: browser or the machine; three toolsets cover their work. `connections` is
#: the one Hermes 0.21.5 added to every profile on update (2026-10-02); the
#: owner keeps it (2026-10-03). The order is the one Hermes wrote, so a
#: redeploy reads the live list as already reconciled.
_WORKER_TOOLSETS = ["coding", "connections", "goncloud_team", "kanban"]

#: The director drives Odoo and Edge over Telegram (owner decision 2026-09-15),
#: so it carries the full toolset — browser and computer_use included. Baking it
#: in stops the director from re-editing its own config every session. Its role
#: text, not a narrow toolset, keeps it out of logs and plugin code. kanban is
#: in since 2026-10-03 (owner; adopted 2026-10-12): kanban_create is how the
#: director hands work to a specialist profile, and unlike the CLI it
#: subscribes the chat, so the gateway reports back when the card finishes.
#: Its 14 schemas cost 23.9 KB of every call. The order is the live one.
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
    "kanban",
    "memory",
    "session_search",
    "skills",
    "terminal",
    "todo",
    "vision",
    "web",
]

ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"
#: Kimi's OpenAI wire, not its Anthropic one: Hermes picks the Anthropic
#: Messages wire for any `/coding` URL, and on that wire K3 dropped 5 of 12
#: tool calls across identical runs (2026-09-28 evidence); the OpenAI wire
#: dropped 1.
KIMI_CODING_URL = "https://api.kimi.com/coding/v1"

#: Owner decision (2026-09-28): director's vision chain order is GLM 5.3 Flash
#: first, then Kimi For Coding, then Claude Opus 5. Note: glm-5.3 (text only)
#: and glm-5.3-flashx (not in the plan) cannot serve vision.
_DIRECTOR_VISION_AUXILIARY = {
    "vision": {
        "provider": "zai",
        "model": "glm-5.3-flash",
        "base_url": ZAI_CODING_URL,
        "fallback_chain": [
            {
                "provider": "kimi-coding",
                "model": "kimi-for-coding",
                "base_url": KIMI_CODING_URL,
                "api_mode": "chat_completions",
            },
            {"provider": "anthropic", "model": "claude-opus-5-5"},
        ],
    }
}

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
    "kimi": {
        "provider": "kimi-coding",
        "base_url": KIMI_CODING_URL,
        "api_mode": "chat_completions",
    },
}

#: ``muse login`` in WSL mints an inference key bound to the owner's Muse
#: subscription; the Muse CLI itself sends it to api.meta.ai. Native
#: ``meta-ai`` positions use that key, never a paid one from Meta's console
#: (owner, 2026-10-15). It is copied into each profile that needs it on every
#: deploy, so a new login reaches them on the next one.
MUSE_LOGIN = "/home/goncloud/.config/muse/auth.json"
#: The first variable Hermes' meta-ai provider reads, so no other one wins.
MUSE_KEY_VARIABLE = "MODEL_API_KEY"
#: A login file read instead of WSL's: the tests point it at a fake one so a
#: deploy under test never copies the owner's key.
MUSE_LOGIN_OVERRIDE = "GONCLOUD_MUSE_LOGIN"


def director_native_config(source):
    """The director's native chain, compiled from the table's ``main`` row."""
    return native_config(source, "claw")


def native_config(source, role):
    """``model`` + ``fallback_providers`` + efforts for ``role``'s row, on native providers."""
    plugin_dir = str(ROOT / "plugins" / "goncloud-team")
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from subscriptions.catalog import compile_profile_routes

    entries, overrides = [], {}
    for route in compile_profile_routes(role, source):
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
    extras = {k: v for k, v in primary.items() if k not in ("provider", "model")}
    model = {"provider": primary["provider"], "default": primary["model"], **extras}
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


#: Why a run with --adoptar-ruteo-manual accepted the live routing as its
#: previous. Recorded verbatim in every goncloud-managed.json it touches.
ADOPTION_REASON = (
    "2026-10-17: owner moved Hermes profiles to Anthropic on his Claude "
    "subscription; ruteo vivo aceptado como previo con --adoptar-ruteo-manual"
)
ADOPTION_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def live_routing_changes(existing, previous):
    """The routing keys the owner changed by hand since the last deploy.

    Only the routing block counts: ``model``, ``fallback_providers`` and the
    chain's ``agent.reasoning_overrides``. Anything absent from the live
    config is not adopted; deleting a managed key stays a conflict.
    """
    adopted = {}
    for key in ("model", "fallback_providers"):
        if key in existing and existing[key] != previous.get(key):
            adopted[key] = existing[key]
    overrides = existing.get("agent", {}).get("reasoning_overrides")
    before = previous.get("agent", {}).get("reasoning_overrides")
    if overrides is not None and overrides != before:
        adopted["agent.reasoning_overrides"] = overrides
    return adopted


def with_adopted_routing(previous, adopted):
    """``previous`` with the live routing values recorded in it."""
    effective = copy.deepcopy(previous)
    for key, value in adopted.items():
        if key == "agent.reasoning_overrides":
            effective.setdefault("agent", {})["reasoning_overrides"] = copy.deepcopy(
                value
            )
        else:
            effective[key] = copy.deepcopy(value)
    return effective


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
    # The result keeps the desired key order, not the existing one: a second
    # deploy must read back what the first one wrote and produce byte-identical
    # output, or every redeploy would rewrite every profile's config.yaml.
    result = {}
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
    # Keys the user owns and this script never managed keep their values,
    # after the managed ones.
    for key, value in existing.items():
        if key not in result:
            result[key] = copy.deepcopy(value)
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
    if not target.exists():
        return
    raw = target.read_bytes()
    wanted = desired if isinstance(desired, bytes) else desired.encode("utf-8")
    before = previous.encode("utf-8") if isinstance(previous, str) else previous
    if raw == wanted or (before is not None and raw == before):
        return

    def normalized(data):
        if data is None:
            return None
        return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

    # A Windows checkout can leave a managed text file in CRLF with content
    # identical to the managed bytes: that is the same file, and the deploy
    # rewrites it with the canonical line endings instead of blocking behind
    # a manual reconciliation. Any other difference is still a conflict.
    if normalized(raw) in (normalized(wanted), normalized(before)):
        return
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


#: The gateway runs one Kanban dispatcher for every multiplexed profile and it
#: reads these keys from the ROOT config.yaml only; a profile's own kanban
#: block never reaches it. Without the allowlist the dispatcher claims any
#: `ready` card whose assignee is a profile, stage cards included, and runs
#: the stage with a native agent outside the subscription router.
_ROOT_KANBAN_KEYS = ("auto_decompose", "dispatch_profiles")
_KANBAN_BLOCK = re.compile(r"^kanban:[ \t]*(#.*)?$")


def root_kanban_policy(team):
    """The kanban keys the gateway dispatcher must find in the root config."""
    return {key: team["kanban"][key] for key in _ROOT_KANBAN_KEYS}


def apply_root_kanban(text, policy):
    """Set ``policy`` inside the top-level ``kanban:`` block of ``text``.

    The root config is the owner's commented file, so the keys are edited in
    place: every other line, and the file's newline style, stay as they were.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    rendered = {key: f"{key}: {json.dumps(value)}" for key, value in policy.items()}
    start = next((i for i, line in enumerate(lines) if _KANBAN_BLOCK.match(line)), None)
    if start is None:
        if any(line.startswith("kanban:") for line in lines):
            raise ValueError(
                "Root kanban block is written inline; make it a plain mapping first"
            )
        while lines and not lines[-1]:
            lines.pop()
        block = ["kanban:", *(f"  {line}" for line in rendered.values()), ""]
        return newline.join([*lines, *block])
    end = start + 1
    # The block runs to the next top-level key; blank lines and comments in
    # between still belong to it.
    while end < len(lines) and (not lines[end] or lines[end][0] in " \t#"):
        end += 1
    children = [line for line in lines[start + 1 : end] if line[:1] in " \t"]
    indent = (
        children[0][: len(children[0]) - len(children[0].lstrip())]
        if children
        else "  "
    )
    kept, missing, index = [], dict(rendered), start + 1
    while index < end:
        line = lines[index]
        key = line[len(indent) :].split(":", 1)[0]
        index += 1
        if line.startswith(indent) and key in missing:
            kept.append(indent + missing.pop(key))
            # A block-style value owns the deeper-indented lines under it.
            while index < end and lines[index].startswith(indent + " "):
                index += 1
            continue
        kept.append(line)
    added = [indent + line for line in missing.values()]
    return newline.join([*lines[: start + 1], *added, *kept, *lines[end:]])


def write_changed(path, content, *, backup=True):
    raw = content if isinstance(content, bytes) else content.encode("utf-8")
    if path.exists() and path.read_bytes() == raw:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    # A recoverable copy; no credentials are included in the ownership manifest.
    if backup and path.exists():
        backup = path.with_name(path.name + ".goncloud-backup")
        if not backup.exists():
            backup.write_bytes(path.read_bytes())
    temporary = path.with_name(path.name + ".goncloud-tmp")
    if isinstance(content, bytes):
        temporary.write_bytes(content)
    else:
        temporary.write_text(content, encoding="utf-8", newline="\n")
    temporary.replace(path)


#: One access control entry as ``icacls`` prints it: the principal and the
#: flags inside its parentheses (``I``, ``OI``, ``CI``, ``F``, ``RX``, ``DENY``,
#: or the specific rights of a ``(Rc,S,RA,...)`` list, one token each).
Ace = collections.namedtuple("Ace", "principal flags")
WindowsUser = collections.namedtuple("WindowsUser", "sid names")
_ACE_LINE = re.compile(r"^(?P<principal>.*?):(?P<flags>(?:\([^()]*\))+)\s*$")
#: Principals whose read+execute reaches the user's filtered token. Owner
#: rights and Administrators are not here on purpose: the gateway task runs
#: with the user's medium-integrity token, where Administrators is deny-only,
#: and OWNER RIGHTS names BUILTIN\Administrators on the installed tools.
_PUBLIC_PRINCIPALS = ("builtin\\users", "everyone", "nt authority\\authenticated users")
_EXECUTE_FLAGS = frozenset({"F", "M", "RX"})


def parse_icacls(text):
    """The ACEs in ``icacls <path>`` output; the path on the first line is dropped."""
    aces = []
    for line in text.splitlines():
        match = _ACE_LINE.match(line.strip())
        if match is None:
            continue
        flags = frozenset(
            token.strip()
            for group in re.findall(r"\(([^()]*)\)", match.group("flags"))
            for token in group.split(",")
        )
        aces.append(Ace(match.group("principal"), flags))
    return aces


def _names_user(principal, user):
    """``principal`` denotes ``user``, allowing the path ``icacls`` prefixes to line 1."""
    principal = principal.lower()
    accepted = [user.sid.lower(), "*" + user.sid.lower(), *_PUBLIC_PRINCIPALS]
    accepted += [name.lower() for name in user.names]
    return any(principal == name or principal.endswith(" " + name) for name in accepted)


def user_can_execute(aces, user):
    """``user``'s own token may read and execute the directory and its children.

    The grant must reach children (``OI`` and ``CI``, not ``NP``): the gateway
    task fails on ``python.exe`` inside the directory, not on the directory.
    """
    for principal, flags in aces:
        if "DENY" in flags or not _names_user(principal, user):
            continue
        if not {"OI", "CI"} <= flags or "NP" in flags:
            continue
        if _EXECUTE_FLAGS & flags or {"R", "X"} <= flags:
            return True
    return False


def _run_windows(*command):
    return subprocess.run(
        list(command), capture_output=True, text=True, errors="replace", check=False
    )


def current_windows_user(run=_run_windows):
    """The interactive user's SID and account names (``whoami`` plus env forms).

    The SID is what the grant uses: a name resolved over ssh came back as
    ``WORKGROUP\\user`` and ``icacls`` found no mapping for it (2026-10-15).
    """
    result = run("whoami", "/user", "/fo", "csv", "/nh")
    if result.returncode != 0:
        raise ValueError(f"whoami failed: {result.stderr.strip() or result.stdout}")
    account, sid = next(csv.reader([result.stdout.strip()]))
    user = account.rsplit("\\", 1)[-1]
    names = {account}
    for variable in ("COMPUTERNAME", "USERDOMAIN"):
        if os.environ.get(variable):
            names.add(f"{os.environ[variable]}\\{user}")
    return WindowsUser(sid, sorted(names))


def tools_without_user_access(tools_dir, user, run=_run_windows):
    """The directories directly under ``tools_dir`` the user's token cannot execute.

    Hermes installs each tool elevated with a protected ACL (SYSTEM,
    Administrators and OWNER RIGHTS only, 2026-10-02). The ``Hermes_Gateway``
    task then reports success while ``python.exe`` answers "Access is denied"
    and no gateway starts. An unreadable ACL counts as no access.
    """
    lacking = []
    for directory in sorted(p for p in tools_dir.iterdir() if p.is_dir()):
        result = run("icacls", str(directory))
        aces = parse_icacls(result.stdout) if result.returncode == 0 else []
        if not user_can_execute(aces, user):
            lacking.append(directory)
    return lacking


def grant_user_execute(directory, user, run=_run_windows):
    """Give ``user`` read+execute on ``directory`` and everything under it."""
    result = run(
        "icacls",
        str(directory),
        "/grant",
        f"*{user.sid}:(OI)(CI)RX",
        "/T",
        "/C",
        "/Q",
    )
    if result.returncode != 0:
        raise ValueError(
            f"icacls could not grant read/execute on {directory} "
            f"(run elevated): {result.stderr.strip() or result.stdout.strip()}"
        )


def ensure_tools_executable(runtime_home, *, check_only, run=_run_windows):
    """Keep every installed tool executable by the user; Windows only.

    In check mode a tool the user cannot execute fails the check and is named,
    so a reinstall or update that reset the ACLs cannot pass unnoticed.
    """
    tools_dir = runtime_home / "tools"
    if os.name != "nt" or not tools_dir.is_dir():
        return []
    user = current_windows_user(run)
    lacking = tools_without_user_access(tools_dir, user, run)
    if check_only and lacking:
        listed = "\n".join(f"  {directory}" for directory in lacking)
        raise ValueError(
            "Tools the user cannot execute (the Hermes_Gateway task will not "
            f"start the gateway); apply elevated to grant read/execute:\n{listed}"
        )
    for directory in lacking:
        grant_user_execute(directory, user, run)
        print(f"Granted read/execute on {directory}")
    return lacking


DIRECTOR_SCRIPTS = (
    "model_watch.py",
    "model_lines.py",
    "hermes_autoupdate.py",
    "promote_model.py",
    "backup_hermes.py",
)


def autoupdate_setup(runtime_home):
    """Where the installed watcher finds the checkout, its Python and Hermes."""
    return (
        json.dumps(
            {
                "checkout": str(ROOT),
                "python": sys.executable,
                "hermes": str(runtime_home / "bin" / "hermes.cmd"),
                "git": shutil.which("git") or "git",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def desired_profile_config(role, team, runtime_home):
    """The managed keys one profile owns; everything else stays the user's."""
    routing_source = json.loads(
        (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
    )
    desired = {
        "plugins": {"enabled": ["goncloud-team"]},
        # Copilot and Copilot ACP are out of scope for this team. deepseek is
        # the one adapter that bills as API: leaving it in a native catalog
        # would let an agent spend outside that decision. opencode-free is
        # not one of the owner's accounts.
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
    # Every team profile converses on native providers compiled from its own
    # v6 row (2026-10-17: the owner moved every profile to Anthropic on his
    # Claude subscription), so no hand-written chain can contradict the table.
    desired.update(native_config(routing_source, role))
    if role == team["director"]:
        # The director carries the full toolset over Telegram by owner decision
        # (browser, computer_use and the rest): it drives Odoo and Edge and runs
        # Gmail/Calendar/Drive through a terminal-driven skill. Its role text,
        # not a narrow toolset, keeps it out of logs and plugin code.
        desired["platform_toolsets"] = {
            "cli": list(_DIRECTOR_TOOLSETS),
            "telegram": list(_DIRECTOR_TOOLSETS),
        }
        # Not the dispatcher's switch (that is the root config, see
        # apply_root_kanban): in a profile this only tells `hermes kanban
        # create` whether to warn that nobody will run the card. The
        # director's cards do run, so its copy says so (owner, 2026-10-03).
        desired["kanban"] = {**team["kanban"], **team.get("director_kanban", {})}
        desired["auxiliary"] = copy.deepcopy(_DIRECTOR_VISION_AUXILIARY)
    return desired


def _run_wsl(*command):
    # A WSL cold start can take well over a minute (the Muse adapter allows 120 s).
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        timeout=120,
    )


def muse_login_key(run=_run_wsl):
    """The key ``muse login`` stored in WSL, or None when it cannot be read."""
    override = os.environ.get(MUSE_LOGIN_OVERRIDE)
    try:
        if override:
            text = Path(override).read_text(encoding="utf-8")
        else:
            result = run("wsl.exe", "-d", "Ubuntu", "--exec", "cat", MUSE_LOGIN)
            if result.returncode != 0:
                return None
            text = result.stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        key = json.loads(text)["providers"]["meta"]["api_key"]
    except (ValueError, KeyError, TypeError):
        return None
    # A key is one printable token: anything else would inject lines into a .env.
    if not isinstance(key, str) or not re.fullmatch(r"[!-~]+", key.strip()):
        return None
    return key.strip()


def uses_meta_ai(config):
    """Whether a profile's native chain has a ``meta-ai`` position."""
    chain = [config.get("model", {}), *config.get("fallback_providers", [])]
    return any(entry.get("provider") == "meta-ai" for entry in chain)


def with_env_value(text, name, value):
    """A ``.env`` text with ``name`` set to ``value``; every other line kept."""
    lines, placed = [], False
    for line in text.lstrip("﻿").splitlines():
        if line.split("=", 1)[0].removeprefix("export ").strip() == name:
            if not placed:
                lines.append(f"{name}={value}")
                placed = True
            continue
        lines.append(line)
    if not placed:
        lines.append(f"{name}={value}")
    return "\n".join(lines) + "\n"


def configure(
    runtime_home,
    install_dir,
    apply_model_routing=False,
    *,
    allow_regression=False,
    preserve_local_soul=False,
    check_only=False,
    adopt_manual_routing=False,
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
    routing_source = json.loads(
        (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
    )
    prepared = []
    adoptions = {}
    # Team profiles are created and fully shaped here. A specialist profile is
    # the owner's: it is never created, and only its native model chain, its
    # copy of the plugin and its Muse key line are managed, so a card
    # dispatched to it runs on a known chain instead of whatever its
    # environment happens to select.
    targets = [(role, text, True) for role, text in team["profiles"].items()] + [
        (role, None, False) for role in team["kanban"]["dispatch_profiles"]
    ]
    for role, description, is_team in targets:
        profile = contained(get_profile_dir(role), runtime_home)
        fresh = not profile.exists()
        if fresh and not is_team:
            print(f"Skipped specialist {role}: no such profile")
            continue
        if fresh and not check_only:
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
        desired = (
            desired_profile_config(role, team, runtime_home)
            if is_team
            else native_config(routing_source, role)
        )
        previous_config = state.get("config", {})
        adopted = (
            live_routing_changes(existing or {}, previous_config)
            if adopt_manual_routing
            else {}
        )
        if adopted:
            previous_config = with_adopted_routing(previous_config, adopted)
            adoptions[role] = adopted
        existing = remove_owned_native_routing(existing or {}, previous_config)
        merged = merge_profile_config(
            existing,
            desired,
            previous_config,
            fresh=fresh,
        )
        files = {
            # The router needs its slot table next to the plugin copy.
            "plugins/goncloud-team/model-routing.source.json": (
                ROOT / "model-routing.source.json"
            ).read_bytes(),
        }
        if is_team:
            files["SOUL.md"] = "\n\n".join(
                (ROOT / name).read_text(encoding="utf-8")
                for name in (
                    "shared/CONTRACT.md",
                    "shared/BUSINESS.md",
                    f"roles/{role}.md",
                )
            )
        for source in plugin_files:
            files[
                f"plugins/goncloud-team/{source.relative_to(plugin_root).as_posix()}"
            ] = source.read_text(encoding="utf-8")
        if role == team["director"]:
            # The daily model watcher and the daily full backup are no_agent
            # cron scripts of the director's gateway; cron resolves them under
            # <profile>/scripts. The watcher moves the table to newer releases
            # through the modules beside it, working in this checkout with
            # this Python.
            for name in DIRECTOR_SCRIPTS:
                files[f"scripts/{name}"] = (ROOT / "scripts" / name).read_text(
                    encoding="utf-8"
                )
            files["scripts/autoupdate.json"] = autoupdate_setup(runtime_home)
            # Models pinned by hand outside the table (launchers, agent files):
            # the watcher reports when one of them leaves the catalogs.
            files["scripts/pinned-models.json"] = (
                ROOT / "scripts/pinned-models.json"
            ).read_text(encoding="utf-8")
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
    root_config = contained(runtime_home / "config.yaml", runtime_home)
    root_text = root_config.read_bytes().decode("utf-8") if root_config.exists() else ""
    root_desired = apply_root_kanban(root_text, root_kanban_policy(team))
    # Every profile validated above; nothing has been written yet.
    keyed = [entry[0] for entry in prepared if uses_meta_ai(entry[5])]
    key = muse_login_key() if keyed else None
    env_files = {}
    for profile in keyed if key else ():
        env = contained(profile / ".env", runtime_home)
        current = env.read_text(encoding="utf-8") if env.exists() else ""
        env_files[env] = with_env_value(current, MUSE_KEY_VARIABLE, key)
    if keyed and not key:
        print(
            f"Muse login key: unreadable at {MUSE_LOGIN} in WSL; meta-ai fails "
            f"over in {len(keyed)} profiles until `muse login` is redone"
        )
    # The tools ACL: in check mode it fails the check, otherwise it grants.
    ensure_tools_executable(runtime_home, check_only=check_only)
    if check_only:
        print(f"Check passed: {len(prepared)} profiles would deploy {revision}")
        if adoptions:
            listed = ", ".join(sorted(adoptions))
            print(f"Ruteo manual a adoptar como previo en: {listed}")
        print(
            "Root dispatch allowlist: "
            + ("up to date" if root_desired == root_text else "would be written")
        )
        if env_files:
            stale = [
                env
                for env, text in env_files.items()
                if not env.exists() or env.read_text(encoding="utf-8") != text
            ]
            print(
                "Muse login key: "
                + (
                    f"would be written to {len(stale)} profiles"
                    if stale
                    else "up to date"
                )
            )
        return
    # The allowlist goes first: it is what keeps the gateway off stage cards.
    write_changed(root_config, root_desired.encode("utf-8"))
    adoption_stamp = datetime.now(timezone.utc).strftime(ADOPTION_STAMP)
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
        recorded = {
            "config": desired,
            "source_revision": revision,
            "files": recorded_files,
        }
        adopted = adoptions.get(profile.name)
        if adopted:
            recorded["adopted_manual_routing"] = {
                **(state.get("adopted_manual_routing") or {}),
                adoption_stamp: {
                    "reason": ADOPTION_REASON,
                    "keys": sorted(adopted),
                },
            }
        elif "adopted_manual_routing" in state:
            recorded["adopted_manual_routing"] = state["adopted_manual_routing"]
        write_changed(
            state_path,
            json.dumps(
                recorded,
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
        )
        if "terminal" in desired:
            # Team profiles only: a specialist keeps its own workspace and skills.
            contained(Path(desired["terminal"]["cwd"]), runtime_home).mkdir(
                parents=True, exist_ok=True
            )
            if not any((profile / "skills").rglob("SKILL.md")):
                if seed_profile_skills(profile, quiet=True) is None:
                    raise RuntimeError(
                        f"Could not seed bundled skills for {profile.name}; reapply to retry"
                    )
        print(f"Configured {profile.name}: {profile}")
    for env, text in env_files.items():
        # No backup: it would leave a plaintext copy of the profile's credentials.
        write_changed(env, text, backup=False)
    if env_files:
        print(f"Muse login key: {MUSE_KEY_VARIABLE} set in {len(env_files)} profiles")


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
        "--adoptar-ruteo-manual",
        action="store_true",
        help=(
            "Accept the live model/fallback_providers/reasoning_overrides the "
            "owner set by hand as the managed previous (recorded with date and "
            "reason), then apply the table; without it the conflict remains"
        ),
    )
    parser.add_argument(
        "--preserve-local-soul",
        action="store_true",
        help=(
            "Keep existing profile SOUL.md files while updating managed routing, "
            "plugins, and configuration"
        ),
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate every profile against this checkout and exit without writing",
    )
    args = parser.parse_args()
    configure(
        args.home,
        args.install_dir,
        args.apply_model_routing,
        allow_regression=args.allow_regression,
        preserve_local_soul=args.preserve_local_soul,
        check_only=args.check,
        adopt_manual_routing=args.adoptar_ruteo_manual,
    )


if __name__ == "__main__":
    main()
