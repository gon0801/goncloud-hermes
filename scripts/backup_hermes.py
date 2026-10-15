"""Respaldo completo de Hermes para reinstalar en otra PC.

Empaqueta el home de Hermes (menos lo que reinstalar recrea) y los logins de
las CLIs del enrutador en hermes-backup-YYYYMMDD.tar.gz, lo sube a gonserver
y conserva los 2 archivos locales mas nuevos. Si todo sale bien no imprime
nada; si algo falla imprime una sola linea "ERROR ..." y sale con 1, y el cron
de Hermes entrega esa linea al propietario por Telegram.
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
class Deadline:
    """When the run must stop so its error line beats the cron's kill."""

    at: float
    budget: float
    clock: object

    def left(self):
        return self.at - self.clock()

    def check(self, doing):
        if self.left() <= 0:
            raise BackupError(
                f"se acabo el tiempo (--budget {self.budget:.0f} s) {doing}; "
                "no se subio nada"
            )


@dataclass(frozen=True)
class Rule:
    """Decides a path under one root ("home" or "user").

    The pattern is a posix path relative to the root matched segment by
    segment ("*" stays inside one segment; a leading "**/" matches at any
    depth) and covers everything below what it names. A small_file rule names
    only files of at most SMALL_FILE bytes at exactly its depth. The last
    matching rule wins. A path no rule names is not visited, unless a keep
    rule names something below it.
    """

    root: str
    pattern: str
    keep: bool
    reason: str
    small_file: bool = False


SMALL_FILE = 1_000_000
USER_FILE_LIMIT = 20_000_000
USER_TOTAL_LIMIT = 200_000_000

_REINSTALL = "el instalador oficial lo recrea en el commit fijado"
_CACHE = "cache que Hermes vuelve a llenar"
_OLD = "copias viejas; este archivo las reemplaza"
_LIVE = "candado de un proceso vivo"
_LOGS = "bitacoras; no hacen falta para reinstalar"

# Each profiles/<name>/ is a full HERMES_HOME, so these apply at both levels.
HERMES_HOME_DROPS = (
    ("hermes-agent", _REINSTALL),
    ("tools", _REINSTALL),
    ("installs", _REINSTALL),
    ("node", _REINSTALL),
    ("bin", _REINSTALL),
    ("cache", _CACHE),
    ("image_cache", _CACHE),
    ("audio_cache", _CACHE),
    ("lsp", "cache de los servidores de lenguaje; se recrea"),
    ("logs", _LOGS),
    ("backups", _OLD),
    ("recovery-backups", _OLD),
    ("state-snapshots", _OLD),
    ("locks", "candados de procesos vivos"),
    ("auth.lock", _LIVE),
    ("*.env.lock", _LIVE),
    ("*.live-probe.lock", _LIVE),
    ("*.dispatch-*.lock", _LIVE),
)

# The router's CLIs: their installers recreate everything but the login, the
# settings and what the owner wrote, which is all that is kept.
CLI_HOMES = (
    (".claude", "Claude Code"),
    (".codex", "Codex"),
    (".grok", "Grok"),
    (".kimi", "Kimi"),
    (".kimi-code", "Kimi Code"),
    (".config/opencode", "OpenCode"),
    (".local/share/opencode", "OpenCode"),
)
CLI_OWNER_DIRS = (
    "agent",
    "agents",
    "command",
    "commands",
    "credentials",
    "hooks",
    "memories",
    "memory",
    "memory-archive",
    "prompts",
    "rules",
    "skills",
    "skills-inactive",
)
_SESSIONS = "historial de sesiones; puede pesar GB"
CLI_DROPS = (
    ("packages", "binarios de la CLI; su instalador los recrea"),
    ("plugins", "plugins; se reinstalan desde la configuracion"),
    ("sessions", _SESSIONS),
    ("projects", _SESSIONS),
    ("file-history", _SESSIONS),
    ("storage", _SESSIONS),
    ("cache", "cache que la CLI vuelve a llenar"),
    ("marketplace-cache", "cache que la CLI vuelve a llenar"),
    ("downloads", "descargas que la CLI vuelve a bajar"),
    ("bin", "binarios de la CLI; su instalador los recrea"),
    (".sandbox-bin", "binarios de la CLI; su instalador los recrea"),
    (".tmp", "temporales"),
    ("tmp", "temporales"),
    ("log", _LOGS),
    ("logs", _LOGS),
)


def _hermes_home_rules():
    yield Rule("home", "", True, "estado de Hermes")
    for name, reason in HERMES_HOME_DROPS:
        yield Rule("home", name, False, reason)
        yield Rule("home", f"profiles/*/{name}", False, reason)
    yield Rule("home", "**/*.pid", False, "pid de un proceso vivo")


def _user_rules():
    for path, name in CLI_HOMES:
        yield Rule(
            "user",
            path,
            False,
            f"{name} lo recrea al reinstalarse; solo entran su login, sus ajustes "
            "(archivos sueltos de hasta 1 MB) y lo que escribio el dueno",
        )
        for child, reason in CLI_DROPS:
            yield Rule("user", f"{path}/{child}", False, reason)
        yield Rule(
            "user", f"{path}/*", True, f"login y ajustes de {name}", small_file=True
        )
        for child in CLI_OWNER_DIRS:
            yield Rule(
                "user", f"{path}/{child}", True, f"lo escribio el dueno de {name}"
            )
    yield Rule("user", ".claude.json", True, "login y ajustes de Claude Code")
    yield Rule(
        "user", "AppData/Roaming/himalaya", True, "cuentas de correo de himalaya"
    )
    yield Rule("user", ".gitconfig", True, "identidad de git del autoactualizador")
    yield Rule("user", ".ssh", True, "llave con que el autoactualizador hace push")
    yield Rule("user", "**/node_modules", False, "dependencias que la CLI reinstala")
    yield Rule("user", "**/*.log", False, _LOGS)
    yield Rule("user", "**/*.bak*", False, _OLD)
    yield Rule("user", "**/history.jsonl", False, _SESSIONS)
    yield Rule("user", "**/session_index.jsonl", False, _SESSIONS)
    yield Rule("user", ".codex/*.sqlite*", False, _SESSIONS)
    yield Rule(
        "user",
        ".claude/plugins/known_marketplaces.json",
        True,
        "de donde Claude Code reinstala los plugins de settings.json",
    )


RULES = (*_hermes_home_rules(), *_user_rules())

# A live process holds or rewrites these, so failing to read one is expected
# and only recorded as skipped. Any other unreadable path fails the run.
_LIVE_FILE = "archivo de un proceso vivo"
TRANSIENT = (
    ("**/*.sock", _LIVE_FILE),
    ("**/*.guard", _LIVE_FILE),
    ("**/*.tmp", "temporal"),
    ("**/*.tmp-*", "temporal"),
    ("**/.sqlite-maintenance.lock", _LIVE_FILE),
)
RECENT_OUTPUT = "**/cron/output"
RECENT_SECONDS = 60


def _segments_match(pattern, parts):
    return len(parts) >= len(pattern) and all(
        fnmatch.fnmatchcase(part.lower(), want.lower())
        for part, want in zip(parts, pattern)
    )


def _pattern_matches(pattern, parts):
    if pattern.startswith("**/"):
        tail = pattern[3:].split("/")
        return any(_segments_match(tail, parts[i:]) for i in range(len(parts)))
    return _segments_match(pattern.split("/") if pattern else [], parts)


def _matches(rule, parts, size):
    if rule.small_file and (
        size is None or size > SMALL_FILE or len(parts) != len(rule.pattern.split("/"))
    ):
        return False
    return _pattern_matches(rule.pattern, parts)


def _transient_reason(path, parts):
    for pattern, reason in TRANSIENT:
        if _pattern_matches(pattern, parts):
            return reason
    if _pattern_matches(RECENT_OUTPUT, parts):
        try:
            age = time.time() - os.stat(path).st_mtime
        except OSError:
            return None
        if age < RECENT_SECONDS:
            return "salida de un cron que se estaba escribiendo"
    return None


def decide(root, parts, size=None):
    """The rule that owns this path, or None when no rule names it.

    size is the file's size in bytes, or None for a directory.
    """
    owner = None
    for rule in RULES:
        if rule.root == root and _matches(rule, parts, size):
            owner = rule
    return owner


def _leads_to_keep(root, parts):
    """Whether a keep rule names something strictly below this directory."""
    for rule in RULES:
        if rule.root != root or not rule.keep or rule.pattern.startswith("**/"):
            continue
        pattern = rule.pattern.split("/") if rule.pattern else []
        if len(pattern) > len(parts) and _segments_match(pattern[: len(parts)], parts):
            return True
    return False


@dataclass(frozen=True)
class Item:
    arcname: str
    path: Path
    size: int
    sqlite: bool


@dataclass
class Selection:
    items: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    unreadable: list = field(default_factory=list)
    problems: list = field(default_factory=list)

    def cannot_read(self, arcname, path, error):
        reason = _transient_reason(path, tuple(arcname.split("/")[1:]))
        if reason:
            self.skipped.append({"path": arcname, "reason": reason})
        else:
            self.unreadable.append(
                {"path": arcname, "error": error.strerror or str(error)}
            )


def _is_sqlite(path):
    try:
        with path.open("rb") as handle:
            return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    except OSError:
        return False


def _is_link(entry):
    is_junction = getattr(entry, "is_junction", None)
    return entry.is_symlink() or bool(is_junction and is_junction())


def _size(path):
    """A file's size; 0 when stat fails, so reading it reports the failure."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _megabytes(size):
    if size < 1_000_000:
        return f"{size / 1000:.0f} KB"
    return f"{size / 1_000_000:.0f} MB"


def _bound_user_side(selection):
    """Leave out oversized user files and report what breaks the limits."""
    kept, user_total = [], 0
    for item in selection.items:
        if item.arcname.startswith("user/") and item.size > USER_FILE_LIMIT:
            selection.skipped.append(
                {"path": item.arcname, "reason": "pasa del tope por archivo"}
            )
            selection.problems.append(
                f"{item.arcname} pesa {_megabytes(item.size)} (tope "
                f"{_megabytes(USER_FILE_LIMIT)} por archivo del perfil de usuario) y "
                "quedo fuera; si es una cache nueva agrega una regla"
            )
            continue
        if item.arcname.startswith("user/"):
            user_total += item.size
        kept.append(item)
    selection.items = kept
    if user_total > USER_TOTAL_LIMIT:
        biggest = max(
            (item for item in kept if item.arcname.startswith("user/")),
            key=lambda item: item.size,
        )
        selection.problems.append(
            f"el perfil de usuario suma {_megabytes(user_total)} (tope "
            f"{_megabytes(USER_TOTAL_LIMIT)}); el archivo mas grande es "
            f"{biggest.arcname} ({_megabytes(biggest.size)})"
        )


def collect(roots, deadline, avoid=()):
    """Every file the rules keep under each root, and what they skip."""
    selection = Selection()
    avoid = {Path(path).resolve() for path in avoid}

    def skip(arcname, reason):
        selection.skipped.append({"path": arcname, "reason": reason})

    def visit(root, path, parts, is_dir, is_link=False):
        arcname = "/".join((root, *parts))
        deadline.check(f"juntando {arcname}")
        size = None if is_dir else _size(path)
        rule = decide(root, parts, size)
        leads = is_dir and _leads_to_keep(root, parts)
        if rule is None and not leads:
            return
        if is_link:
            skip(arcname, "enlace; no se sigue")
            return
        if rule is not None and not rule.keep and not leads:
            skip(arcname, rule.reason)
            return
        if path.resolve() in avoid:
            skip(arcname, "carpeta de los respaldos")
            return
        if not is_dir:
            selection.items.append(Item(arcname, path, size, _is_sqlite(path)))
            return
        try:
            entries = sorted(os.scandir(path), key=lambda entry: entry.name)
        except OSError as error:
            selection.cannot_read(arcname, path, error)
            return
        for entry in entries:
            visit(
                root,
                Path(entry.path),
                (*parts, entry.name),
                entry.is_dir(follow_symlinks=False),
                _is_link(entry),
            )

    for root, base in roots.items():
        if base.is_dir() and not base.is_symlink():
            visit(root, base, (), True)

    databases = {item.arcname for item in selection.items if item.sqlite}
    kept = []
    for item in selection.items:
        owner = re.sub(r"-(wal|shm|journal)$", "", item.arcname)
        if owner != item.arcname and owner in databases:
            skip(item.arcname, "va dentro de la copia de su base")
        else:
            kept.append(item)
    selection.items = kept
    _bound_user_side(selection)
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


def write_archive(selection, destination, meta, work, deadline):
    """Write the archive atomically and return its manifest."""
    files = []
    partial = destination.with_name(destination.name + ".tmp")
    try:
        with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
            for number, item in enumerate(selection.items):
                deadline.check(f"escribiendo {item.arcname}")
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
                    selection.cannot_read(item.arcname, item.path, error)
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
                "unreadable": selection.unreadable,
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
        remaining = deadline.left()
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
            time.sleep(max(0, min(settings.retry_wait, deadline.left())))
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
    clock: object = time.monotonic


def remove_leftovers(out_dir):
    """What a run killed mid-write left behind; a new run never resumes it."""
    for path in out_dir.glob("hermes-backup-*.tar.gz.tmp"):
        path.unlink(missing_ok=True)
    for path in out_dir.glob(".trabajo-*"):
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def run(settings):
    """Build, keep and upload one archive."""
    deadline = Deadline(
        settings.clock() + settings.budget, settings.budget, settings.clock
    )
    today = settings.today or datetime.date.today()
    out_dir = settings.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    remove_leftovers(out_dir)
    archive = out_dir / f"hermes-backup-{today:%Y%m%d}.tar.gz"
    roots = {"home": settings.home, "user": settings.user_home}
    selection = collect(roots, deadline, avoid=[out_dir])
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
        manifest = write_archive(selection, archive, meta, Path(work), deadline)
    prune(out_dir)
    digest = _sha256(archive)
    try:
        upload(archive, digest, settings, deadline)
    except BackupError as error:
        raise BackupError(f"{error}; copia local en {archive}") from error
    problems = list(selection.problems)
    unreadable = manifest["unreadable"]
    if unreadable:
        problems.append(f"{len(unreadable)} sin leer, p. ej. {unreadable[0]['path']}")
    if problems:
        raise BackupError(f"subido, pero {'; '.join(problems)}")


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
        run(settings or parse_settings(argv))
    except BackupError as error:
        print(f"ERROR respaldo Hermes: {error}")
        return 1
    except Exception as error:  # noqa: BLE001 - the cron must deliver every failure
        print(f"ERROR respaldo Hermes: {type(error).__name__}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
