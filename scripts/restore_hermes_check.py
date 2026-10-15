"""Simulacro de restauracion de un respaldo de Hermes.

Extrae hermes-backup-YYYYMMDD.tar.gz en una carpeta temporal nueva, comprueba
el manifiesto (tamano y sha256 de cada archivo), corre integrity_check en cada
SQLite, lee cada config.yaml y cada .env, y cuenta perfiles, crons y bases.
Nunca toca el home vivo. Imprime una linea "OK ..." o "ERROR ..." con salida 1.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from backup_hermes import native

SQLITE_HEADER = b"SQLite format 3\x00"
MANIFEST = "manifest.json"
# Without these a rebuilt Hermes has no keys, settings, login, board or way
# to upload; every profile also needs its own config.yaml.
REQUIRED = (
    "home/.env",
    "home/config.yaml",
    "home/auth.json",
    "home/kanban.db",
    "home/backup-key/id_ed25519",
)
ENV_LINE = re.compile(r"^(export\s+)?[A-Za-z_][A-Za-z0-9_.-]*\s*=")


class DrillError(Exception):
    pass


def extract(archive, target):
    """Only plain files with relative names inside target; anything else fails."""
    inside = target.resolve()
    with tarfile.open(archive) as tar:
        for member in tar:
            name = PurePosixPath(member.name)
            destination = target.joinpath(*name.parts)
            # Windows also splits on a backslash, so "..\x" would climb out.
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or ":" in member.name
                or "\\" in member.name
                or not destination.resolve().is_relative_to(inside)
            ):
                raise DrillError(f"entrada insegura en el archivo: {member.name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as source, destination.open("wb") as copy:
                shutil.copyfileobj(source, copy)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_sqlite(path):
    with path.open("rb") as handle:
        return handle.read(len(SQLITE_HEADER)) == SQLITE_HEADER


def _integrity(path):
    try:
        with contextlib.closing(sqlite3.connect(str(path))) as db:
            result = db.execute("pragma integrity_check").fetchall()
    except sqlite3.DatabaseError as error:
        return str(error)
    return None if result == [("ok",)] else result[0][0]


def _env_problem(path):
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = line.strip()
        if text and not text.startswith("#") and not ENV_LINE.match(text):
            return f"linea {number} no es CLAVE=valor"
    return None


def _yaml_problem(path, yaml):
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as error:
        return f"no se puede leer: {str(error).splitlines()[0]}"
    if data is not None and not isinstance(data, dict):
        return "no es un mapa"
    return None


def _cron_jobs(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("sin lista 'jobs'")
    return len(jobs)


def inspect(tree):
    """Problems found in an extracted backup, and its counts."""
    try:
        import yaml
    except ImportError as error:
        raise DrillError(
            "falta PyYAML; corre con el python del runtime Hermes"
        ) from error
    problems = []
    try:
        manifest = json.loads((tree / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise DrillError(f"manifiesto ilegible: {error}") from error
    for entry in manifest["unreadable"]:
        problems.append(f"{entry['path']}: no se pudo leer al respaldar")
    listed = {entry["path"]: entry for entry in manifest["files"]}
    present = {
        path.relative_to(tree).as_posix(): path
        for path in tree.rglob("*")
        if path.is_file() and path.relative_to(tree).as_posix() != MANIFEST
    }
    for name in sorted(listed.keys() - present.keys()):
        problems.append(f"{name}: falta")
    for name in sorted(present.keys() - listed.keys()):
        problems.append(f"{name}: no esta en el manifiesto")
    databases = 0
    for name in sorted(listed.keys() & present.keys()):
        path, entry = present[name], listed[name]
        if path.stat().st_size != entry["size"] or _sha256(path) != entry["sha256"]:
            problems.append(f"{name}: no coincide con el manifiesto")
            continue
        if _is_sqlite(path):
            databases += 1
            failure = _integrity(path)
            if failure:
                problems.append(f"{name}: integrity_check {failure}")
        elif path.name == "config.yaml":
            failure = _yaml_problem(path, yaml)
            if failure:
                problems.append(f"{name}: {failure}")
        elif path.name.endswith(".env"):
            failure = _env_problem(path)
            if failure:
                problems.append(f"{name}: {failure}")
    profiles_dir = tree / "home" / "profiles"
    profile_names = (
        sorted(path.name for path in profiles_dir.iterdir() if path.is_dir())
        if profiles_dir.is_dir()
        else []
    )
    required = {*REQUIRED} | {
        f"home/profiles/{name}/config.yaml" for name in profile_names
    }
    for name in sorted(required - listed.keys() - present.keys()):
        problems.append(f"{name}: falta y es obligatorio")
    crons = 0
    for path in sorted(
        [tree / "home/cron/jobs.json", *profiles_dir.glob("*/cron/jobs.json")]
    ):
        if not path.is_file():
            continue
        try:
            crons += _cron_jobs(path)
        except ValueError as error:
            problems.append(f"{path.relative_to(tree).as_posix()}: {error}")
    counts = {
        "profiles": len(profile_names),
        "crons": crons,
        "databases": databases,
        "files": len(present),
    }
    return problems, counts


def drill(archive, workdir=None):
    """The one-line verdict on an archive, restored into a fresh temp dir."""
    with tempfile.TemporaryDirectory(prefix="hermes-restore-", dir=workdir) as temp:
        tree = native(temp)
        extract(archive, tree)
        problems, counts = inspect(tree)
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (y {len(problems) - 5} mas)" if len(problems) > 5 else ""
        raise DrillError(f"{len(problems)} problemas: {shown}{more}")
    return (
        f"OK restauracion de prueba {archive.name}: {counts['profiles']} perfiles, "
        f"{counts['crons']} crons, {counts['databases']} bases, "
        f"{counts['files']} archivos"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--workdir",
        type=Path,
        help="carpeta para el temporal (por omision la temporal del sistema)",
    )
    args = parser.parse_args(argv)
    try:
        print(drill(args.archive, args.workdir))
    except (DrillError, OSError, tarfile.TarError) as error:
        print(f"ERROR restauracion de prueba {args.archive.name}: {error}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
