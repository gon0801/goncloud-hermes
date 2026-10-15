"""Respaldo completo de Hermes para reinstalar en otra PC.

Empaqueta el home de Hermes (menos lo que reinstalar recrea) y los logins de
las CLIs del enrutador en hermes-backup-YYYYMMDD.tar.gz, lo sube a gonserver
y conserva los 2 archivos locales mas nuevos. Imprime una linea: "OK ..." o
"ERROR ..." con salida 1; el cron de Hermes entrega esa linea al propietario.
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

SQLITE_HEADER = b"SQLite format 3\x00"
ARCHIVE_PATTERN = re.compile(r"^hermes-backup-\d{8}\.tar\.gz$")
MANIFEST = "manifest.json"
LOCAL_KEEP = 2
UPLOAD_ATTEMPTS = 3
DEFAULT_SERVER = "clawbackup@10.13.13.1"
# Hermes kills a no_agent cron script at 3600 s; stop first so the error line
# still reaches the owner.
DEFAULT_BUDGET = 3300


class BackupError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    """Decides a path under one root ("home" or "user").

    The pattern is a posix path relative to the root matched segment by
    segment ("*" stays inside one segment; a leading "**/" matches at any
    depth) and covers everything below what it names. The last matching rule
    wins. Under "user" only the paths a keep rule names are visited at all.
    """

    root: str
    pattern: str
    keep: bool
    reason: str


_REINSTALL = "el instalador oficial lo recrea en el commit fijado"
_CACHE = "cache que Hermes vuelve a llenar"
RULES = (
    Rule("home", "", True, "estado de Hermes"),
    Rule("home", "hermes-agent", False, _REINSTALL),
    Rule("home", "tools", False, _REINSTALL),
    Rule("home", "installs", False, _REINSTALL),
    Rule("home", "node", False, _REINSTALL),
    Rule("home", "bin", False, _REINSTALL),
    Rule("home", "cache", False, _CACHE),
    Rule("home", "image_cache", False, _CACHE),
    Rule("home", "audio_cache", False, _CACHE),
    Rule("home", "logs", False, "bitacoras; no hacen falta para reinstalar"),
    Rule("home", "backups", False, "copias viejas; este archivo las reemplaza"),
    Rule(
        "home", "recovery-backups", False, "copias viejas; este archivo las reemplaza"
    ),
    Rule("home", "state-snapshots", False, "copias viejas; este archivo las reemplaza"),
    Rule("home", "locks", False, "candados de procesos vivos"),
    Rule("home", "**/*.lock", False, "candado de un proceso vivo"),
    Rule("home", "**/*.pid", False, "pid de un proceso vivo"),
    Rule("home", "profiles/*/cache", False, _CACHE),
    Rule("home", "profiles/*/image_cache", False, _CACHE),
    Rule("home", "profiles/*/audio_cache", False, _CACHE),
    Rule("home", "profiles/*/logs", False, "bitacoras; no hacen falta para reinstalar"),
    Rule("user", ".claude", True, "login y ajustes de Claude Code (enrutador)"),
    Rule("user", ".claude.json", True, "login y ajustes de Claude Code (enrutador)"),
    Rule("user", ".codex", True, "login de Codex (enrutador)"),
    Rule("user", ".grok", True, "login de Grok (enrutador)"),
    Rule("user", ".kimi", True, "login de Kimi (enrutador)"),
    Rule("user", ".config/opencode", True, "ajustes de OpenCode (enrutador)"),
    Rule("user", ".local/share/opencode", True, "login de OpenCode (enrutador)"),
    Rule("user", "AppData/Roaming/himalaya", True, "cuentas de correo de himalaya"),
    Rule("user", ".gitconfig", True, "identidad de git del autoactualizador"),
    Rule("user", ".ssh", True, "llave con que el autoactualizador hace push"),
    Rule("user", ".claude/projects", False, "historial de sesiones; puede pesar GB"),
    Rule("user", ".claude/file-history", False, "historial de sesiones"),
    Rule("user", ".claude/shell-snapshots", False, _CACHE),
    Rule("user", ".claude/debug", False, "bitacoras"),
    Rule("user", ".claude/todos", False, "historial de sesiones"),
    Rule("user", ".claude/statsig", False, _CACHE),
    Rule("user", ".codex/sessions", False, "historial de sesiones"),
    Rule("user", ".codex/log", False, "bitacoras"),
    Rule("user", ".local/share/opencode/storage", False, "historial de sesiones"),
    Rule("user", ".local/share/opencode/log", False, "bitacoras"),
    Rule("user", "**/node_modules", False, "dependencias que la CLI reinstala"),
)


def _segments_match(pattern, parts):
    return len(parts) >= len(pattern) and all(
        fnmatch.fnmatchcase(part.lower(), want.lower())
        for part, want in zip(parts, pattern)
    )


def _matches(pattern, parts):
    if pattern.startswith("**/"):
        tail = pattern[3:].split("/")
        return any(_segments_match(tail, parts[i:]) for i in range(len(parts)))
    return _segments_match(pattern.split("/") if pattern else [], parts)


def decide(root, parts):
    """The rule that owns this path, or None when no rule names it."""
    owner = None
    for rule in RULES:
        if rule.root == root and _matches(rule.pattern, parts):
            owner = rule
    return owner


@dataclass(frozen=True)
class Item:
    arcname: str
    path: Path
    sqlite: bool


@dataclass
class Selection:
    items: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


def _is_sqlite(path):
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _is_link(entry):
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def collect(roots, avoid=()):
    """Every file the rules keep under each root, and what they skip."""
    selection = Selection()
    avoid = {Path(path).resolve() for path in avoid}
    seen = set()

    def skip(arcname, reason):
        selection.skipped.append({"path": arcname, "reason": reason})

    def visit(root, path, parts, is_dir):
        arcname = "/".join((root, *parts))
        if arcname in seen:
            return
        seen.add(arcname)
        rule = decide(root, parts)
        if rule is None:
            return
        if not rule.keep:
            skip(arcname, rule.reason)
            return
        if path.resolve() in avoid:
            skip(arcname, "carpeta de los respaldos")
            return
        if not is_dir:
            selection.items.append(Item(arcname, path, _is_sqlite(path)))
            return
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError as error:
            skip(arcname, f"no se pudo leer: {error.strerror or error}")
            return
        for entry in entries:
            child = (*parts, entry.name)
            if _is_link(entry):
                skip("/".join((root, *child)), "enlace; no se sigue")
                continue
            visit(root, Path(entry.path), child, entry.is_dir())

    for root, base in roots.items():
        starts = [rule.pattern for rule in RULES if rule.root == root and rule.keep]
        for start in starts:
            parts = tuple(start.split("/")) if start else ()
            path = base.joinpath(*parts)
            if path.is_symlink() or not path.exists():
                continue
            visit(root, path, parts, path.is_dir())

    databases = {item.arcname for item in selection.items if item.sqlite}
    kept = []
    for item in selection.items:
        owner = re.sub(r"-(wal|shm|journal)$", "", item.arcname)
        if owner != item.arcname and owner in databases:
            skip(item.arcname, "va dentro de la copia de su base")
        else:
            kept.append(item)
    selection.items = kept
    return selection


def copy_sqlite(source, target, arcname):
    """Online backup (includes WAL content), then integrity_check the copy."""
    try:
        live = sqlite3.connect(str(source), timeout=60)
        try:
            copy = sqlite3.connect(str(target))
            try:
                live.backup(copy)
                copy.execute("pragma journal_mode=delete")
                result = copy.execute("pragma integrity_check").fetchall()
            finally:
                copy.close()
        finally:
            live.close()
    except sqlite3.DatabaseError as error:
        raise BackupError(f"la base {arcname} no se pudo copiar: {error}") from error
    if result != [("ok",)]:
        raise BackupError(f"la base {arcname} no pasa integrity_check: {result[0][0]}")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _add(tar, arcname, spool, size, mtime):
    info = tarfile.TarInfo(arcname)
    info.size = size
    info.mtime = mtime
    info.mode = 0o600
    spool.seek(0)
    tar.addfile(info, spool)


def write_archive(selection, destination, meta, work):
    """Write the archive atomically and return its manifest."""
    files = []
    unreadable = []
    partial = destination.with_name(destination.name + ".tmp")
    try:
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            for number, item in enumerate(selection.items):
                if item.sqlite:
                    copy = work / f"db-{number}.sqlite"
                    copy_sqlite(item.path, copy, item.arcname)
                    source = copy
                else:
                    source = item.path
                # A live file can change while it is read; spooling it first
                # gives the tar entry a size that matches its bytes.
                digest = hashlib.sha256()
                try:
                    mtime = int(item.path.stat().st_mtime)
                    with (
                        source.open("rb") as handle,
                        tempfile.SpooledTemporaryFile(32 << 20, dir=work) as spool,
                    ):
                        for block in iter(lambda: handle.read(1 << 20), b""):
                            digest.update(block)
                            spool.write(block)
                        size = spool.tell()
                        _add(tar, item.arcname, spool, size, mtime)
                except OSError as error:
                    unreadable.append(
                        {"path": item.arcname, "error": error.strerror or str(error)}
                    )
                    continue
                finally:
                    if item.sqlite:
                        source.unlink(missing_ok=True)
                files.append(
                    {
                        "path": item.arcname,
                        "size": size,
                        "sha256": digest.hexdigest(),
                        "sqlite": item.sqlite,
                    }
                )
            manifest = {
                **meta,
                "files": files,
                "skipped": selection.skipped,
                "unreadable": unreadable,
            }
            body = json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8")
            with tempfile.SpooledTemporaryFile(32 << 20, dir=work) as spool:
                spool.write(body)
                _add(tar, MANIFEST, spool, len(body), int(time.time()))
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return manifest


def prune(out_dir, keep=LOCAL_KEEP):
    archives = sorted(
        (path for path in out_dir.iterdir() if ARCHIVE_PATTERN.match(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    for old in archives[keep:]:
        old.unlink()


def upload(archive, digest, settings, deadline):
    """Send through the receiver's forced command and compare its sha256."""
    key = settings.key_dir
    command = [
        *settings.ssh,
        "-i",
        str(key / "id_ed25519"),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        f"UserKnownHostsFile={key / 'known_hosts'}",
        "-o",
        "StrictHostKeyChecking=yes",
        settings.server,
        archive.name,
    ]
    failures = []
    for attempt in range(1, UPLOAD_ATTEMPTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failures.append(f"intento {attempt}: sin tiempo antes del corte del cron")
            break
        try:
            with archive.open("rb") as data:
                done = subprocess.run(
                    command,
                    stdin=data,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=remaining,
                )
        except subprocess.TimeoutExpired:
            failures.append(f"intento {attempt}: se acabo el tiempo")
            break
        except OSError as error:
            failures.append(f"intento {attempt}: {error.strerror or error}")
            break
        lines = done.stdout.strip().splitlines()
        remote = lines[-1].strip().lower() if lines else ""
        if done.returncode == 0 and remote == digest:
            return attempt
        if done.returncode != 0:
            detail = (done.stderr.strip().splitlines() or [""])[-1]
            failures.append(
                f"intento {attempt}: salida {done.returncode} {detail}".strip()
            )
        else:
            failures.append(
                f"intento {attempt}: sha256 remoto distinto ({remote[:12]})"
            )
        if attempt < UPLOAD_ATTEMPTS:
            time.sleep(max(0, min(settings.retry_wait, deadline - time.monotonic())))
    raise BackupError(f"no se pudo subir a {settings.server}: {'; '.join(failures)}")


def _git_head(git, path):
    if path is None or not path.exists():
        return None
    try:
        done = subprocess.run(
            [git, "-C", str(path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def _hermes_version(agent):
    try:
        text = (agent / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    found = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return found.group(1) if found else None


@dataclass(frozen=True)
class Settings:
    home: Path
    user_home: Path
    out_dir: Path
    key_dir: Path
    server: str = DEFAULT_SERVER
    ssh: tuple = ("ssh",)
    repo: Path | None = None
    git: str = "git"
    today: datetime.date | None = None
    budget: float = DEFAULT_BUDGET
    retry_wait: float = 30


def _megabytes(size):
    return f"{size / 1_000_000:.0f} MB"


def run(settings):
    """Build, keep and upload one archive; return the OK line."""
    deadline = time.monotonic() + settings.budget
    today = settings.today or datetime.date.today()
    out_dir = settings.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"hermes-backup-{today:%Y%m%d}.tar.gz"
    roots = {"home": settings.home, "user": settings.user_home}
    selection = collect(roots, avoid=[out_dir])
    meta = {
        "format": 1,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": platform.node(),
        "roots": {name: str(path) for name, path in roots.items()},
        "hermes": {
            "version": _hermes_version(settings.home / "hermes-agent"),
            "commit": _git_head(settings.git, settings.home / "hermes-agent"),
        },
        "repo": {
            "path": str(settings.repo) if settings.repo else None,
            "head": _git_head(settings.git, settings.repo),
        },
    }
    with tempfile.TemporaryDirectory(prefix=".trabajo-", dir=out_dir) as work:
        manifest = write_archive(selection, archive, meta, Path(work))
    prune(out_dir)
    digest = _sha256(archive)
    try:
        upload(archive, digest, settings, deadline)
    except BackupError as error:
        raise BackupError(f"{error}; copia local en {archive}") from error
    databases = sum(1 for entry in manifest["files"] if entry["sqlite"])
    line = (
        f"OK respaldo Hermes {archive.name}: {_megabytes(archive.stat().st_size)}, "
        f"{len(manifest['files'])} archivos, {databases} bases, "
        f"subido a {settings.server} (sha256 {digest[:12]})"
    )
    unreadable = manifest["unreadable"]
    if unreadable:
        line += f"; AVISO {len(unreadable)} sin leer, p. ej. {unreadable[0]['path']}"
    return line


def default_home(script):
    """The Hermes home that holds this script, when installed in a profile."""
    script = Path(script).resolve()
    if script.parent.name == "scripts" and script.parents[2].name == "profiles":
        return script.parents[3]
    return (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "hermes"
    )


def _installed_setup(script):
    try:
        return json.loads(
            (Path(script).resolve().parent / "autoupdate.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return {}


def parse_settings(argv):
    setup = _installed_setup(__file__)
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--home", type=Path, default=default_home(__file__))
    parser.add_argument("--user-home", type=Path, default=Path.home())
    parser.add_argument("--out", type=Path, help="default: <home>/../hermes-respaldo")
    parser.add_argument("--key-dir", type=Path, help="default: <home>/backup-key")
    parser.add_argument("--server", default=DEFAULT_SERVER)
    parser.add_argument("--ssh", default=shutil.which("ssh") or "ssh")
    parser.add_argument("--repo", type=Path, default=setup.get("checkout"))
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET,
        help="segundos; menor que cron.script_timeout_seconds de Hermes",
    )
    args = parser.parse_args(argv)
    home = args.home.resolve()
    return Settings(
        home=home,
        user_home=args.user_home.resolve(),
        out_dir=args.out or home.parent / "hermes-respaldo",
        key_dir=args.key_dir or home / "backup-key",
        server=args.server,
        ssh=(args.ssh,),
        repo=Path(args.repo) if args.repo else None,
        git=setup.get("git") or shutil.which("git") or "git",
        budget=args.budget,
    )


def main(argv=None, settings=None):
    try:
        print(run(settings or parse_settings(argv)))
    except BackupError as error:
        print(f"ERROR respaldo Hermes: {error}")
        return 1
    except Exception as error:  # noqa: BLE001 - the cron must deliver every failure
        print(f"ERROR respaldo Hermes: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
