"""A fake Hermes home, user profile and ssh receiver for the backup tests."""

import contextlib
import datetime
import io
import json
import sqlite3
import sys
import tarfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import backup_hermes  # noqa: E402

TODAY = datetime.date(2026, 10, 15)
ARCHIVE = "hermes-backup-20261015.tar.gz"

FAKE_SSH = textwrap.dedent(
    """
    import hashlib, json, pathlib, sys
    here = pathlib.Path(__file__).parent
    config = json.loads((here / "receiver.json").read_text())
    data = sys.stdin.buffer.read()
    calls = here / "calls.jsonl"
    count = len(calls.read_text().splitlines()) if calls.exists() else 0
    with calls.open("a") as log:
        log.write(json.dumps({"argv": sys.argv[1:], "bytes": len(data)}) + "\\n")
    if count < config.get("fail_first", 0) or config["mode"] == "fail":
        sys.stderr.write("ssh: connect to host 10.13.13.1 port 22: Connection timed out\\n")
        sys.exit(255)
    if config["mode"] == "badsum":
        print("0" * 64)
        sys.exit(0)
    (here / "received" / sys.argv[-1]).write_bytes(data)
    print(hashlib.sha256(data).hexdigest())
    """
)


def write(path, text=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, not text mode: on Windows text mode turns "\n" into "\r\n".
    path.write_bytes(text.encode("utf-8"))
    return path


def make_db(path, rows=("uno",)):
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path)) as db:
        db.execute("create table t (v text)")
        db.executemany("insert into t values (?)", [(row,) for row in rows])
        db.commit()
    return path


def make_install(base):
    """A Hermes home and user profile shaped like the PC's, in miniature."""
    home = base / "Local" / "hermes"
    user = base / "user"
    write(home / "hermes-agent/run_agent.py", "upstream")
    write(home / "tools/rg.exe", "binary")
    write(home / "logs/gateway.log", "log")
    write(home / "backups/old.zip", "old")
    write(home / ".env", "TELEGRAM_BOT_TOKEN=secreto\n")
    write(home / "auth.json", "{}")
    write(home / "auth.lock", "")
    write(home / "gateway.pid", "123")
    write(home / "config.yaml", "kanban:\n  dispatch: true\n")
    write(home / "subscription-router.env", "# router\nKIMI_KEY=x\n")
    write(home / "backup-key/id_ed25519", "PRIVATE")
    write(home / "cron/jobs.json", json.dumps({"jobs": [{"id": "a"}]}))
    make_db(home / "kanban.db")
    write(home / "profiles/claw/config.yaml", "model: x\n")
    write(home / "profiles/claw/.env", "export A=1\n")
    write(home / "profiles/claw/memories/MEMORY.md", "memoria")
    write(home / "profiles/claw/cache/blob", "cache")
    write(home / "profiles/claw/logs/agent.log", "log")
    write(home / "profiles/claw/scripts/backup-claw.sh", "#!/bin/sh")
    write(
        home / "profiles/claw/cron/jobs.json",
        json.dumps({"jobs": [{"id": "b"}, {"id": "c"}], "updated_at": "x"}),
    )
    make_db(home / "profiles/claw/state.db")
    write(home / "profiles/scout/config.yaml", "model: y\n")
    write(user / ".claude/.credentials.json", "{}")
    write(user / ".claude/projects/c--dev/session.jsonl", "transcript")
    write(user / ".claude.json", "{}")
    write(user / ".codex/auth.json", "{}")
    write(user / ".config/opencode/opencode.json", "{}")
    write(user / ".config/opencode/node_modules/pkg/index.js", "x")
    write(user / "Documents/private.txt", "not hermes")
    return home, user


def make_cli_homes(user):
    """The router CLIs' dirs, shaped like the PC's listing of 2026-10-15."""
    keep = (
        ".codex/auth.json",
        ".codex/config.toml",
        ".codex/hooks.json",
        ".codex/AGENTS.md",
        ".codex/installation_id",
        ".codex/version.json",
        ".codex/agents/worker.toml",
        ".codex/hooks/stop.ps1",
        ".codex/memories/notes.md",
        ".codex/prompts/review.md",
        ".codex/rules/default.rules",
        ".codex/skills/poteto/SKILL.md",
        ".codex/skills/poteto/uv.lock",
        ".claude/.credentials.json",
        ".claude/.claude.json",
        ".claude/settings.json",
        ".claude/settings.local.json",
        ".claude/CLAUDE.md",
        ".claude/agents/reviewer.md",
        ".claude/commands/ship.md",
        ".claude/hooks/guard.py",
        ".claude/skills/verify/SKILL.md",
        ".claude/skills-inactive/old/SKILL.md",
        ".claude/memory/MEMORY.md",
        ".claude/memory-archive/2026.md",
        ".claude/plugins/known_marketplaces.json",
        ".grok/config.toml",
        ".grok/agents/reviewer.md",
        ".kimi/config.toml",
        ".kimi/credentials/kimi-code.json",
        ".kimi-code/config.toml",
        ".local/share/opencode/auth.json",
        ".config/opencode/opencode.jsonc",
        ".config/opencode/agent/poteto-agent.md",
    )
    drop = (
        ".codex/packages/standalone/codex.exe",
        ".codex/sessions/2026/10/15/rollout.jsonl",
        ".codex/plugins/cache/nvidia/SKILL.md",
        ".codex/.sandbox-bin/rg.exe",
        ".codex/.tmp/plugins/x.md",
        ".codex/cache/models.json",
        ".codex/sqlite/state.db",
        ".codex/vendor_imports/skills.json",
        ".codex/tmp/arg0/x",
        ".codex/history.jsonl",
        ".codex/session_index.jsonl",
        ".codex/sandbox.20261015.log",
        ".codex/config.toml.bak1",
        ".codex/state_5.sqlite",
        ".codex/skills/poteto/run.log",
        ".claude/plugins/cache/pstack/SKILL.md",
        ".claude/projects/c--dev/session.jsonl",
        ".claude/state/x.json",
        ".claude/jobs/x.json",
        ".claude/file-history/x",
        ".claude/history.jsonl",
        ".grok/bin/grok.exe",
        ".grok/marketplace-cache/x.json",
        ".grok/downloads/x.zip",
        ".kimi/sessions/x.jsonl",
        ".local/share/opencode/bin/rg.exe",
        ".local/share/opencode/storage/session/x.json",
    )
    for name in keep + drop:
        write(user / name, "x")
    (user / ".grok/models.json").write_bytes(b"x" * 1_000_001)
    return {f"user/{name}" for name in keep}


def make_receiver(base, mode="ok", fail_first=0):
    receiver = base / "receiver"
    (receiver / "received").mkdir(parents=True)
    write(receiver / "fake_ssh.py", FAKE_SSH)
    write(
        receiver / "receiver.json", json.dumps({"mode": mode, "fail_first": fail_first})
    )
    return receiver


def calls(receiver):
    log = receiver / "calls.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def settings(home, user, base, receiver, **overrides):
    values = {
        "home": home,
        "user_home": user,
        "out_dir": base / "respaldo",
        "key_dir": home / "backup-key",
        "ssh": (sys.executable, str(receiver / "fake_ssh.py")),
        "today": TODAY,
        "retry_wait": 0,
    }
    values.update(overrides)
    return backup_hermes.Settings(**values)


def run_main(config):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = backup_hermes.main(settings=config)
    return code, out.getvalue()


def members(archive):
    with tarfile.open(archive) as tar:
        return {member.name: member for member in tar.getmembers()}


def manifest(archive):
    with tarfile.open(archive) as tar:
        return json.load(tar.extractfile(backup_hermes.MANIFEST))
