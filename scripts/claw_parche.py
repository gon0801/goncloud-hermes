"""Change claw's live config through one path: pause, dry-run, patch, resume.

Runs on the gateway host (Mac Mini). Each objective is a fragment of the config
that must hold; the script patches only what differs, inside the exclusive
window ``claw_autoupdate.Gateway`` grants, and first saves the part that changes
to ``~/.openclaw/respaldos-parche/<objetivo>-<fecha>.pre.json``.

    python3 claw_parche.py OBJETIVO [--dry-run] [--openclaw PATH] [--config PATH]
    python3 claw_parche.py --revierte RESPALDO [--dry-run]

Objectives: defaults-v6, sesiones, dreaming.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import time
from pathlib import Path

from claw_autoupdate import (
    CONFIG_PATH,
    EXCLUSIVE_WAIT,
    PATCH_TIMEOUT,
    Busy,
    Gateway,
    failure,
    merge_patch,
    real_run,
)
from claw_models import desired_claw

ROOT = Path(__file__).resolve().parents[1]
RESPALDOS = Path("~/.openclaw/respaldos-parche")
DREAMING_MODEL = "anthropic/claude-sonnet-5-5"


def _defaults_v6():
    source = json.loads(
        (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
    )
    mapping = json.loads((ROOT / "claw-routing.json").read_text(encoding="utf-8"))
    defaults = desired_claw(source, mapping)["agents"].get("defaults")
    if defaults is None:
        raise ValueError("la tabla no da fila default para claw")
    return {"agents": {"defaults": {"model": defaults["model"]}}}


def _sesiones():
    return {
        "session": {
            "reset": {"idleMinutes": 240},
            "maintenance": {"pruneAfter": "2d"},
        }
    }


def _dreaming():
    return {
        "plugins": {
            "entries": {
                "memory-core": {
                    "config": {"dreaming": {"model": DREAMING_MODEL}},
                    "subagent": {"allowModelOverride": True},
                }
            }
        }
    }


OBJETIVOS = {"defaults-v6": _defaults_v6, "sesiones": _sesiones, "dreaming": _dreaming}


def overlay(config, patch):
    """``config`` with the merge-patch ``patch`` applied (null deletes, arrays replace)."""
    result = copy.deepcopy(config)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = overlay(result[key], value)
        elif isinstance(value, dict):
            result[key] = overlay({}, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _dry_run(gateway, patch, replace):
    """None when ``config patch --dry-run`` accepts the patch, else why not."""
    with tempfile.TemporaryDirectory(prefix="claw-parche-") as directory:
        path = Path(directory) / "patch.json"
        path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
        argv = [gateway.openclaw, "config", "patch", "--file", str(path)]
        for entry in replace:
            argv += ["--replace-path", entry]
        done = gateway.run([*argv, "--dry-run", "--json"], PATCH_TIMEOUT)
    return None if done.ok else f"config patch --dry-run: {failure(done)}"


def _save(respaldos, nombre, live, patch, replace, now):
    """Write the inverse of ``patch`` (what ``live`` holds there now) and return its path."""
    inverse, inverse_replace = merge_patch(overlay(live, patch), live)
    respaldos.mkdir(parents=True, exist_ok=True)
    path = respaldos / f"{nombre}-{time.strftime('%Y%m%d-%H%M%S', now)}.pre.json"
    path.write_text(
        json.dumps(
            {"objetivo": nombre, "patch": inverse, "replace_paths": inverse_replace},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def aplica(
    gateway, nombre, fragmento, respaldos, *, dry_run=False, wait=EXCLUSIVE_WAIT
):
    """``(exit code, message)`` for patching ``fragmento`` into the live config.

    An already-applied objective answers without the window; the patch itself is
    computed against the config read inside the window. Nothing is written without the window, on
    ``dry_run``, or when the config already holds ``fragmento``.
    """
    ya = f"{nombre}: la config ya esta asi; no escribi nada"
    live = gateway.config()
    if not merge_patch(live, overlay(live, fragmento))[0]:
        return 0, ya
    try:
        with gateway.exclusive(wait):
            live = gateway.config()
            patch, replace = merge_patch(live, overlay(live, fragmento))
            if not patch:
                return 0, ya
            problem = _dry_run(gateway, patch, replace)
            if problem:
                return 1, f"{nombre}: {problem}"
            if dry_run:
                return 0, f"{nombre}: --dry-run ok; cambiaria {json.dumps(patch)}"
            path = _save(respaldos, nombre, live, patch, replace, time.localtime())
            problem = gateway.patch(patch, replace, dry_run_first=False)
            if problem:
                return 1, f"{nombre}: {problem} (respaldo en {path})"
            return 0, f"{nombre}: aplicado; respaldo en {path}"
    except Busy as error:
        return 1, f"{nombre}: {error}; no escribi nada"


def lee_respaldo(path):
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("patch"), dict):
        raise TypeError(f"{path} no es un respaldo de claw_parche")
    return doc["objetivo"], doc["patch"]


def main(argv=None, run=real_run):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("objetivo", nargs="?", choices=sorted(OBJETIVOS))
    parser.add_argument("--revierte", type=Path, metavar="RESPALDO")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--openclaw", default="openclaw")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH.expanduser())
    parser.add_argument("--respaldos", type=Path, default=RESPALDOS.expanduser())
    args = parser.parse_args(argv)
    if bool(args.objetivo) == bool(args.revierte):
        parser.error("da un OBJETIVO o --revierte RESPALDO, no ambos ni ninguno")
    if args.revierte:
        nombre, fragmento = lee_respaldo(args.revierte)
        nombre = f"{nombre}-revierte"
    else:
        nombre, fragmento = args.objetivo, OBJETIVOS[args.objetivo]()
    gateway = Gateway(args.openclaw, run, config_path=args.config)
    code, message = aplica(
        gateway, nombre, fragmento, args.respaldos, dry_run=args.dry_run
    )
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
