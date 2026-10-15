"""Move claw's live model chains to a newer release of a model they use, on its own.

Runs on the gateway host (Mac Mini) as an OpenClaw cron command job, daily.
Claw does not read this repo, so this works on its live config. For each ref
in a chain with a newer release in its own line it waits for a moment with no
model runs, stages the new ref, probes it with a real call, renames every use,
and checks a real agent turn. A failed probe or check restores the snapshot.

    python3 claw_autoupdate.py [--openclaw PATH] [--state PATH] [--dry-run]

It is two files on the host: this one and ``model_lines.py`` beside it.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
import tempfile
import time

from model_lines import (
    FAMILIES,
    GO,
    HELD,
    Promotion,
    catalog_view,
    change_of,
    family_of,
    fresh_notices,
    held_notices,
    newer_in_line,
    read_catalogs,
    run as real_run,
    split_held,
    version_text,
)

#: The cron delivery suppresses a command whose whole output is this
#: (OpenClaw docs/cli/cron.md, command jobs).
SILENT = "NO_REPLY"
#: The status counts this runner's own cron run in ``cronRuns``.
OWN_CRON_RUNS = 1
#: Install the cron job with --timeout-seconds and --no-output-timeout-seconds
#: of RUN_LIMIT: the runner prints only at the end. A promotion starts only when
#: its worst case still fits.
RUN_LIMIT = 1800
SAFETY = 200
IDLE_WAIT = 120
IDLE_POLL = 15
#: The gateway reloads its config after a patch; a read right after may still
#: show the old one, so a check polls this long before calling it a failure.
SETTLE_WAIT = 30
SETTLE_POLL = 3
READ_TIMEOUT = 30
PATCH_TIMEOUT = 60
TURN_TIMEOUT = 180
PROBE_AGENT = "skillverif"
_MISSING = object()


# Pure planning ----------------------------------------------------------------


def chain_refs(config):
    """Ref -> where it sits in a chain (agent names, ``defaults`` for the default)."""
    agents = config.get("agents") or {}
    chains = [("defaults", (agents.get("defaults") or {}).get("model"))]
    chains += [
        (name, entry.get("model"))
        for name, entry in sorted((agents.get("entries") or {}).items())
    ]
    places = {}
    for place, chain in chains:
        if isinstance(chain, str):
            chain = {"primary": chain}
        for ref in [
            (chain or {}).get("primary"),
            *((chain or {}).get("fallbacks") or []),
        ]:
            if ref and place not in places.setdefault(ref, []):
                places[ref].append(place)
    return places


def catalog_for(provider, model, view):
    source = GO if provider.startswith(GO) else FAMILIES.get(family_of(model))
    return view.get(source)


def plan_claw(config, view):
    promotions = []
    for ref, places in chain_refs(config).items():
        provider, _, model = ref.partition("/")
        catalog = catalog_for(provider, model, view)
        newer = newer_in_line(model, catalog) if catalog is not None else None
        if newer:
            promotions.append(Promotion(ref, f"{provider}/{newer}", tuple(places)))
    return promotions


def _provider_rows(config, provider):
    rows = (
        ((config.get("models") or {}).get("providers") or {}).get(provider) or {}
    ).get("models")
    return rows if isinstance(rows, list) else None


def _cloned_row(row, old_id, new_id):
    clone = {**copy.deepcopy(row), "id": new_id}
    old_text, new_text = version_text(old_id), version_text(new_id)
    name = clone.get("name")
    if isinstance(name, str):
        clone["name"] = (
            name.replace(old_text, new_text, 1)
            if old_text and new_text and old_text in name
            else new_id
        )
    return clone


def staged(config, old_ref, new_ref):
    """The config with ``new_ref`` callable beside ``old_ref``; nothing removed."""
    result = copy.deepcopy(config)
    provider, _, old_id = old_ref.partition("/")
    new_id = new_ref.partition("/")[2]
    rows = _provider_rows(result, provider)
    if rows is not None and not any(row.get("id") == new_id for row in rows):
        old_row = next((row for row in rows if row.get("id") == old_id), None)
        if old_row is not None:
            rows.append(_cloned_row(old_row, old_id, new_id))
    defaults = (result.get("agents") or {}).get("defaults") or {}
    allow = (defaults.get("modelPolicy") or {}).get("allow")
    if isinstance(allow, list) and old_ref in allow and new_ref not in allow:
        allow.append(new_ref)
    models = defaults.get("models")
    if isinstance(models, dict) and old_ref in models and new_ref not in models:
        models[new_ref] = copy.deepcopy(models[old_ref])
    return result


def _renamed_refs(node, old_ref, new_ref, path=()):
    # Provider rows are keyed by the bare id; renamed() handles them apart.
    if path == ("models", "providers"):
        return copy.deepcopy(node)
    if isinstance(node, str):
        return new_ref if node == old_ref else node
    if isinstance(node, list):
        return [_renamed_refs(item, old_ref, new_ref, path) for item in node]
    if isinstance(node, dict):
        renamed = {}
        for key, value in node.items():
            value = _renamed_refs(value, old_ref, new_ref, (*path, key))
            if key == old_ref:
                renamed.setdefault(new_ref, value)
            else:
                renamed[key] = value
        return renamed
    return node


def renamed(config, old_ref, new_ref):
    """The config with every use of ``old_ref`` turned into ``new_ref``.

    Every string and key equal to the ref changes, wherever it is; a custom
    provider's row (keyed by the bare id) is cloned for the new id and the old
    row dropped. The allowlist keeps one entry per ref.
    """
    result = _renamed_refs(config, old_ref, new_ref)
    provider, _, old_id = old_ref.partition("/")
    new_id = new_ref.partition("/")[2]
    rows = _provider_rows(result, provider)
    if rows is not None:
        has_new = any(row.get("id") == new_id for row in rows)
        rows[:] = [
            _cloned_row(row, old_id, new_id) if row.get("id") == old_id else row
            for row in rows
            if not (has_new and row.get("id") == old_id)
        ]
    defaults = (result.get("agents") or {}).get("defaults") or {}
    allow = (defaults.get("modelPolicy") or {}).get("allow")
    if isinstance(allow, list):
        allow[:] = list(dict.fromkeys(allow))
    return result


def _path_text(path):
    text = ""
    for key in path:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            text += ("." if text else "") + key
        else:
            text += f"[{json.dumps(key)}]"
    return text


def merge_patch(before, after, path=()):
    """``(patch, replace_paths)`` turning ``before`` into ``after``.

    Objects merge, arrays replace and null deletes. The CLI refuses to drop any
    element of an existing array unless its path is passed as --replace-path,
    so every array that loses one is listed, whatever its length.
    """
    patch, replace = {}, []
    for key in before.keys() - after.keys():
        patch[key] = None
    for key, value in after.items():
        old = before.get(key, _MISSING)
        if old == value:
            continue
        if isinstance(value, dict) and isinstance(old, dict):
            patch[key], inner = merge_patch(old, value, (*path, key))
            replace += inner
        else:
            patch[key] = copy.deepcopy(value)
            if (
                isinstance(value, list)
                and isinstance(old, list)
                and any(item not in value for item in old)
            ):
                replace.append(_path_text((*path, key)))
    return patch, sorted(replace)


def rename_patch(config, old_ref, new_ref):
    return merge_patch(config, renamed(config, old_ref, new_ref))


def efforts(config, ref):
    """The thinking levels the chains run ``ref`` with, for the probe."""
    agents = config.get("agents") or {}
    tables = [(agents.get("defaults") or {}).get("models") or {}]
    tables += [
        entry.get("models") or {} for entry in (agents.get("entries") or {}).values()
    ]
    return sorted(
        {
            level
            for table in tables
            if isinstance(
                level := ((table.get(ref) or {}).get("params") or {}).get("thinking"),
                str,
            )
        }
    )


def runtime_of(config, ref):
    """The agent runtime a ref runs through (``claude-cli`` for anthropic), if any."""
    models = ((config.get("agents") or {}).get("defaults") or {}).get("models") or {}
    return ((models.get(ref) or {}).get("agentRuntime") or {}).get("id")


def mentions(node, ref):
    if isinstance(node, str):
        return node == ref
    if isinstance(node, list):
        return any(mentions(item, ref) for item in node)
    if isinstance(node, dict):
        return any(key == ref or mentions(value, ref) for key, value in node.items())
    return False


def is_idle(status):
    work = (status.get("shutdownBudget") or {}).get("activeWork")
    if not isinstance(work, dict):
        return False
    return (
        all(count == 0 for key, count in work.items() if key != "cronRuns")
        and work.get("cronRuns", 0) <= OWN_CRON_RUNS
    )


def _last_word(text):
    lines = [line.strip(" .!\t").upper() for line in (text or "").splitlines()]
    return next((line for line in reversed(lines) if line), "")


def turn_reply(doc):
    """``(text, winner provider)`` of an ``openclaw agent --json`` result."""
    result = doc.get("result") if isinstance(doc.get("result"), dict) else doc
    text = "\n".join(
        payload.get("text") or ""
        for payload in result.get("payloads") or []
        if isinstance(payload, dict)
    )
    meta = result.get("meta") or {}
    trace = meta.get("executionTrace") or {}
    winner = trace.get("winnerProvider") or (meta.get("agentMeta") or {}).get(
        "provider"
    )
    return text, winner


# Side effects -----------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    promotion: Promotion
    status: str  # "cambiado" | "no cambiado" | "revertido" | "pendiente"
    detail: str = ""


class Gateway:
    """The openclaw commands the runner needs, through an injectable ``run``."""

    def __init__(self, openclaw, run, *, sleep=time.sleep, clock=time.monotonic):
        self.openclaw = openclaw
        self.run = run
        self.sleep = sleep
        self.clock = clock

    def _json(self, argv, timeout):
        done = self.run([self.openclaw, *argv], timeout)
        if not done.ok:
            raise RuntimeError(f"{' '.join(argv[:3])}: {done.why()}")
        return json.loads(done.out)

    def config(self):
        return self._json(
            ["gateway", "call", "config.get", "--params", "{}", "--json"], READ_TIMEOUT
        )["parsed"]

    def settled(self, ready):
        """The live config once ``ready(config)`` holds, or the last read at SETTLE_WAIT."""
        deadline = self.clock() + SETTLE_WAIT
        while True:
            live = self.config()
            if ready(live) or self.clock() + SETTLE_POLL > deadline:
                return live
            self.sleep(SETTLE_POLL)

    def wait_idle(self):
        deadline = self.clock() + IDLE_WAIT
        while True:
            try:
                if is_idle(
                    self._json(["gateway", "call", "status", "--json"], READ_TIMEOUT)
                ):
                    return True
            except (RuntimeError, ValueError):
                pass
            if self.clock() + IDLE_POLL > deadline:
                return False
            self.sleep(IDLE_POLL)

    def patch(self, patch, replace_paths, *, dry_run_first=True):
        """None once the patch is applied, else why not; an empty one is not sent.

        The CLI refuses an empty patch, and nothing to change is not a failure.
        """
        if not patch:
            return None
        with tempfile.TemporaryDirectory(prefix="claw-autoupdate-") as directory:
            path = Path(directory) / "patch.json"
            path.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
            argv = [self.openclaw, "config", "patch", "--file", str(path)]
            for replace in replace_paths:
                argv += ["--replace-path", replace]
            if dry_run_first:
                checked = self.run([*argv, "--dry-run", "--json"], PATCH_TIMEOUT)
                if not checked.ok:
                    return f"config patch --dry-run: {checked.why()}"
            done = self.run(argv, PATCH_TIMEOUT)
            return None if done.ok else f"config patch: {done.why()}"

    def turn(self, agent, ref, *, thinking=None, purpose="probe"):
        """``(ok, why, winner provider)`` of one real agent turn on ``ref``.

        Its session is deleted afterwards, whatever happened.
        """
        key = f"agent:{agent}:autoupdate-{purpose}"
        argv = [
            self.openclaw,
            "agent",
            "--agent",
            agent,
            "--session-key",
            key,
            "--model",
            ref,
            "--message",
            "Responde solo: PONG",
            "--json",
        ]
        if thinking:
            argv += ["--thinking", thinking]
        done = self.run(argv, TURN_TIMEOUT)
        self.run(
            [self.openclaw, "sessions", "delete", "--yes", "--json", key], READ_TIMEOUT
        )
        if not done.ok:
            return False, done.why(), None
        try:
            text, winner = turn_reply(json.loads(done.out))
        except (ValueError, AttributeError):
            return False, "respuesta no es JSON", None
        if _last_word(text) != "PONG":
            return False, "no dijo PONG", winner
        return True, "", winner

    def infer(self, ref, *, thinking=None):
        argv = [
            self.openclaw,
            "infer",
            "model",
            "run",
            "--model",
            ref,
            "--gateway",
            "--agent",
            "main",
            "--prompt",
            "Responde solo: OK",
            "--json",
        ]
        if thinking:
            argv += ["--thinking", thinking]
        done = self.run(argv, TURN_TIMEOUT)
        if not done.ok:
            return False, done.why()
        try:
            doc = json.loads(done.out)
            text = (doc.get("outputs") or [{}])[0].get("text")
        except (ValueError, AttributeError, IndexError):
            return False, "respuesta no es JSON"
        if doc.get("ok") is not True or _last_word(text) != "OK":
            return False, "no dijo OK"
        return True, ""


def _probe(gateway, config, old_ref, new_ref):
    """None when every probe of ``new_ref`` passes, else why not.

    It runs with each thinking level the chains give ``old_ref`` today.
    """
    runtime = runtime_of(config, old_ref)
    for thinking in efforts(config, old_ref) or [None]:
        label = f" ({thinking})" if thinking else ""
        if runtime:
            ok, why, winner = gateway.turn(PROBE_AGENT, new_ref, thinking=thinking)
            if ok and winner != runtime:
                ok, why = False, f"contesto {winner}, no {runtime}"
        else:
            ok, why = gateway.infer(new_ref, thinking=thinking)
        if not ok:
            return f"sonda{label}: {why}"
    return None


def _verify(gateway, promotion, runtime):
    live = gateway.settled(lambda config: not mentions(config, promotion.old_ref))
    if mentions(live, promotion.old_ref):
        return f"{promotion.old_ref} sigue en la config"
    agents = [place for place in promotion.places if place != "defaults"]
    agent = PROBE_AGENT if PROBE_AGENT in agents else (agents or ["main"])[0]
    ok, why, winner = gateway.turn(agent, promotion.new_ref, purpose="verify")
    if ok and runtime and winner != runtime:
        ok, why = False, f"contesto {winner}, no {runtime}"
    return None if ok else f"turno en {agent}: {why}"


def worst_case(config, ref):
    probes = max(1, len(efforts(config, ref)))
    return (
        3 * IDLE_WAIT
        + 5 * READ_TIMEOUT
        + SETTLE_WAIT
        + 5 * PATCH_TIMEOUT
        + (probes + 1) * (TURN_TIMEOUT + READ_TIMEOUT)
    )


def promote(gateway, promotion, budget):
    old, new = promotion.old_ref, promotion.new_ref

    def outcome(status, detail=""):
        return Outcome(promotion, status, detail)

    if not gateway.wait_idle():
        return outcome("pendiente", "no hubo un momento sin corridas")
    snapshot = gateway.config()
    if worst_case(snapshot, old) > budget():
        return outcome("pendiente", "no cupo en el tiempo de esta corrida")
    runtime = runtime_of(snapshot, old)
    status = "no cambiado"
    try:
        problem = gateway.patch(*merge_patch(snapshot, staged(snapshot, old, new)))
        if problem:
            problem = f"preparar: {problem}"
        else:
            problem = _probe(gateway, snapshot, old, new)
        if problem is None:
            if not gateway.wait_idle():
                # The staged ref is additive and harmless; the next run picks up here.
                return outcome("pendiente", "no hubo un momento sin corridas")
            status = "revertido"
            problem = gateway.patch(*rename_patch(gateway.config(), old, new))
            if problem is None:
                problem = _verify(gateway, promotion, runtime)
    except Exception as error:  # noqa: BLE001 - the snapshot goes back first
        problem = f"{type(error).__name__}: {error}"
    if problem is None:
        return outcome("cambiado")
    return outcome(status, problem + _restore(gateway, snapshot))


def _restore(gateway, snapshot):
    """Put ``snapshot`` back over the config live now; what to add to the notice.

    A patch that timed out may still have landed, so the restore is computed
    from a fresh read, never from what the runner believes it wrote. It runs
    even when the gateway stays busy: a half-applied switch is worse than a
    cut run.
    """
    idle = gateway.wait_idle()
    try:
        patch, replace = merge_patch(gateway.config(), snapshot)
        failed = gateway.patch(patch, replace, dry_run_first=False)
    except Exception as error:  # noqa: BLE001 - said in the notice
        patch, failed = True, f"{type(error).__name__}: {error}"
    if failed:
        return f"; NO pude restaurar: {failed}"
    if patch and not idle:
        return "; restaure con corridas en curso, pude cortar alguna"
    return ""


def _promote_or_say_why(gateway, promotion, budget):
    try:
        return promote(gateway, promotion, budget)
    except Exception as error:  # noqa: BLE001 - nothing was written yet
        return Outcome(promotion, "no cambiado", f"{type(error).__name__}: {error}")


#: Said with a reverted pair: it stays as it is until the owner looks.
ON_HOLD = "No lo vuelvo a intentar hasta que lo revises."


def report(outcomes, last):
    lines, remembered = [], {}
    for outcome in outcomes:
        promotion = outcome.promotion
        change = change_of(promotion)
        if outcome.status == "cambiado":
            lines.append(f"- Cambiado: {change} en {', '.join(promotion.places)}.")
        elif outcome.status == "revertido":
            remembered[HELD + change] = (
                f"- Revertido: {change} ({outcome.detail}). {ON_HOLD}"
            )
        else:
            remembered[change] = (
                f"- {outcome.status.capitalize()}: {change} ({outcome.detail})."
            )
    return lines + fresh_notices(remembered, last), remembered


def autoupdate(gateway, view, last, *, started):
    deadline = started + RUN_LIMIT - SAFETY
    trying, kept = split_held(plan_claw(gateway.config(), view), last)
    outcomes = [
        _promote_or_say_why(gateway, promotion, lambda: deadline - gateway.clock())
        for promotion in trying
    ]
    lines, remembered = report(outcomes, last)
    return lines, {**remembered, **kept}


def said(gateway, view, last, *, started):
    """``(lines, notices to remember)``; a crash is one notice, never a traceback."""
    try:
        return autoupdate(gateway, view, last, started=started)
    except Exception as error:  # noqa: BLE001 - said once
        remembered = {
            **held_notices(last),
            "error": f"- La autoactualizacion de claw fallo: {type(error).__name__}: {error}",
        }
        return fresh_notices(remembered, last), remembered


def main():
    started = time.monotonic()
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--openclaw", default="openclaw", help="openclaw CLI path")
    parser.add_argument(
        "--state", type=Path, default=here / "claw_autoupdate.state.json"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and change nothing"
    )
    args = parser.parse_args()
    state = (
        json.loads(args.state.read_text(encoding="utf-8"))
        if args.state.is_file()
        else {}
    )
    gateway = Gateway(args.openclaw, real_run)
    view = catalog_view(*read_catalogs())
    if args.dry_run:
        for promotion in plan_claw(gateway.config(), view):
            print(
                f"{promotion.old_ref} -> {promotion.new_ref} ({', '.join(promotion.places)})"
            )
        return 0
    lines, remembered = said(gateway, view, state.get("notices", {}), started=started)
    if lines:
        print("\n".join([f"Claw: modelos {time.strftime('%Y-%m-%d')}", *lines]))
    else:
        print(SILENT)
    temporary = args.state.with_name(args.state.name + ".tmp")
    temporary.write_text(
        json.dumps({"notices": remembered}, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
