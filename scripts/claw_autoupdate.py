"""Move claw's live model chains to a newer release of a model they use, on its own.

Runs on the gateway host (Mac Mini) from launchd, daily. Claw does not read
this repo, so this works on its live config. For each ref in a chain with a
newer release in its own line it stages the new ref, probes it with a real
call, renames every use, and checks a real agent turn. A failed probe or check
takes those changes back out, keeping what any other writer changed meanwhile.
Each config write runs inside an exclusive window the
gateway grants (``gateway suspend``) only when no run is in flight.

    python3 claw_autoupdate.py [--openclaw PATH] [--config PATH] [--state PATH] [--to CHAT] [--dry-run]

It reads the live config from its file (``--config``, default
``~/.openclaw/openclaw.json``), never ``gateway call config.get``: that call
fails while the runner holds the gateway's suspension.

It is two files on the host: this one and ``model_lines.py`` beside it.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from model_lines import (
    FAMILIES,
    GO,
    HELD,
    ON_HOLD,
    Promotion,
    catalog_view,
    change_of,
    family_of,
    fresh_notices,
    held_notices,
    newer_in_line,
    read_catalogs,
    split_held,
    version_text,
)
from model_lines import (
    run as real_run,
)

#: How long a write waits for the gateway to grant an exclusive window. Never
#: ``suspend --wait N>0``: the gateway refuses new work (the owner's messages)
#: for the whole drain.
EXCLUSIVE_WAIT = 2400
#: A restore waits less, then runs anyway: a half-applied switch is worse.
RESTORE_WAIT = 600
#: Seconds between asks, whatever ``retryAfterMs`` says: every ask closes the
#: gateway's admission for 13-20 s even when it answers busy (live, 2026.9.7).
WINDOW_POLL = 300
#: The CLI's own 10 s default ran out on the live gateway; the process bound
#: sits above it so the CLI's own answer comes first.
SUSPEND_TIMEOUT_MS = "30000"
SUSPEND_RUN_TIMEOUT = 45
NO_WINDOW = "no hubo un momento sin corridas"
#: The owner's Telegram chat; the runner sends its own notices.
TELEGRAM_TO = "6470689715"
SEND_TIMEOUT = 60
READ_TIMEOUT = 30
CONFIG_PATH = Path("~/.openclaw/openclaw.json")
#: The config file can be caught mid-write; a read that does not parse retries.
CONFIG_TRIES = 4
CONFIG_RETRY = 1
PATCH_TIMEOUT = 60
TURN_TIMEOUT = 180
PROBE_AGENT = "skillverif"
REASON = "[openclaw] Reason:"
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


class _Tangled(Exception):
    """This runner's change and another writer's sit in one array."""


def undone(live, snapshot, old_ref, new_ref):
    """``live`` with this runner's staging and rename of ``old_ref`` taken back.

    A node still as the snapshot, the staging or the rename left it goes back
    to the snapshot; one only another writer changed keeps their value, and
    one both changed keeps theirs too (theirs is the later write). Where both
    changed an array, ``new_ref`` turns back into ``old_ref`` in it and the
    staged copy goes. None when that cannot be told apart: the snapshot already
    had ``new_ref`` in that array, or the ref sat inside a nested value.
    """
    stage = staged(snapshot, old_ref, new_ref)
    written = (stage, renamed(stage, old_ref, new_ref))
    provider, _, old_id = old_ref.partition("/")
    new_id = new_ref.partition("/")[2]
    rows_path = ("models", "providers", provider, "models")

    def refs(now, before):
        if mentions(before, new_ref) or any(
            item != old_ref and mentions(item, old_ref) for item in before
        ):
            raise _Tangled
        result = []
        for item in now:
            item = old_ref if item == new_ref else item
            if not (item == old_ref and old_ref in result):
                result.append(item)
        return result

    def rows(now, before):
        by_id = {row.get("id"): row for row in before if isinstance(row, dict)}
        if new_id in by_id or old_id not in by_id:
            raise _Tangled
        old_row = by_id[old_id]
        has_old = any(isinstance(row, dict) and row.get("id") == old_id for row in now)
        result = []
        for row in now:
            if isinstance(row, dict) and row.get("id") == new_id:
                if not has_old:
                    result.append(copy.deepcopy(old_row))
                    has_old = True
            else:
                result.append(row)
        return result

    def undo(now, before, ours, path):
        if now == before or now in ours:
            return before
        if all(value == before for value in ours):
            return now
        if all(isinstance(value, dict) for value in (now, before, *ours)):
            result = {}
            for key in dict.fromkeys([*before, *now, *(k for v in ours for k in v)]):
                value = undo(
                    now.get(key, _MISSING),
                    before.get(key, _MISSING),
                    tuple(value.get(key, _MISSING) for value in ours),
                    (*path, key),
                )
                if value is not _MISSING:
                    result[key] = value
            return result
        if isinstance(now, list) and isinstance(before, list):
            return (rows if path == rows_path else refs)(now, before)
        return now

    def outside_rows(config):
        trimmed = copy.deepcopy(config)
        trimmed.get("models", {}).pop("providers", None)
        return trimmed

    try:
        result = undo(live, snapshot, written, ())
    except _Tangled:
        return None
    # Another writer pointed something at new_ref meanwhile: keeping it would
    # leave a ref whose row and allow entry this restore just took away.
    if mentions(outside_rows(result), new_ref) and not mentions(
        outside_rows(snapshot), new_ref
    ):
        return None
    return result


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


def _first_json(text):
    """The JSON value at the start of ``text``; openclaw may print lines after it."""
    try:
        return json.JSONDecoder().raw_decode((text or "").lstrip())[0]
    except ValueError:
        return None


def reported_error(done, limit=200):
    """The error openclaw reported, on one line: its JSON ``error.message`` on
    stdout, else its ``[openclaw] Reason:`` line; None when it gave neither."""
    doc = _first_json(done.out)
    error = doc.get("error") if isinstance(doc, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    if not isinstance(message, str) or not message.strip():
        reasons = [
            line.split(REASON, 1)[1]
            for line in (done.err + "\n" + done.out).splitlines()
            if REASON in line
        ]
        message = reasons[-1] if reasons else ""
    return " ".join(message.split())[:limit] or None


def failure(done):
    """Why an openclaw command failed, for a notice.

    Its JSON is pretty-printed, so the last line of its output alone says
    nothing (``}``); the exit and last line are only the fallback.
    """
    return reported_error(done) or done.why()


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


class Busy(Exception):
    """The gateway granted no exclusive window before the deadline; nothing written."""


@dataclass(frozen=True)
class Outcome:
    promotion: Promotion
    status: str  # "cambiado" | "no cambiado" | "revertido" | "pendiente"
    detail: str = ""


class Gateway:
    """The openclaw commands the runner needs, through an injectable ``run``."""

    def __init__(
        self, openclaw, run, *, config_path, sleep=time.sleep, clock=time.monotonic
    ):
        self.openclaw = openclaw
        self.run = run
        self.config_path = Path(config_path)
        self.sleep = sleep
        self.clock = clock

    def config(self):
        """The live config, from its file; one caught mid-write is read again.

        Its contents hold secrets, so no error carries them.
        """
        for attempt in range(CONFIG_TRIES):
            if attempt:
                self.sleep(CONFIG_RETRY)
            try:
                doc = json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if isinstance(doc, dict):
                return doc
        raise RuntimeError(
            f"{self.config_path} no es JSON legible tras {CONFIG_TRIES} lecturas"
        )

    def _prepare(self, request):
        """The gateway's answer to one ``suspend --wait 0``; None when it gave none.

        No answer (a timeout, an exit without JSON) may still have been granted.
        """
        done = self.run(
            [
                self.openclaw,
                "gateway",
                "suspend",
                "--wait",
                "0",
                "--timeout",
                SUSPEND_TIMEOUT_MS,
                "--request-id",
                request,
                "--json",
            ],
            SUSPEND_RUN_TIMEOUT,
        )
        doc = _first_json(done.out)
        return doc if isinstance(doc, dict) and doc.get("status") else None

    @contextmanager
    def exclusive(self, wait):
        """Hold the gateway's suspension while the block runs; Busy if none by ``wait``.

        ``suspend --wait 0`` either grants it at once (no run in flight) or
        answers ``busy``, ``draining`` or ``conflict``: not yet. One request id
        serves the whole window, so asking again after an ask that gave no
        answer returns the lease that ask may have been granted instead of a
        second one. Any lease id an answer carried is resumed on the way out;
        one whose id never came back expires on its own (120 s), as it does
        if this process dies.
        """
        request = f"claw-autoupdate-{uuid.uuid4()}"
        deadline = self.clock() + wait
        suspension = None
        try:
            while True:
                answer = self._prepare(request) or self._prepare(request) or {}
                suspension = answer.get("suspensionId") or suspension
                if answer.get("status") == "ready" and answer.get("suspensionId"):
                    break
                if self.clock() + WINDOW_POLL > deadline:
                    raise Busy(NO_WINDOW)
                self.sleep(WINDOW_POLL)
            yield
        finally:
            if suspension:
                done = self.run(
                    [
                        self.openclaw,
                        "gateway",
                        "resume",
                        suspension,
                        "--timeout",
                        SUSPEND_TIMEOUT_MS,
                        "--json",
                    ],
                    SUSPEND_RUN_TIMEOUT,
                )
                if not done.ok:
                    print(
                        f"gateway resume {suspension}: {failure(done)}", file=sys.stderr
                    )

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
                    return f"config patch --dry-run: {failure(checked)}"
            done = self.run(argv, PATCH_TIMEOUT)
            return None if done.ok else f"config patch: {failure(done)}"

    def send(self, to, text):
        """None once Telegram took ``text``, else why not."""
        done = self.run(
            [
                self.openclaw,
                "message",
                "send",
                "--channel",
                "telegram",
                "-t",
                to,
                "-m",
                text,
                "--json",
            ],
            SEND_TIMEOUT,
        )
        doc = _first_json(done.out)
        if done.ok and not (isinstance(doc, dict) and doc.get("ok") is False):
            return None
        return failure(done)

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
            return False, failure(done), None
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
            return False, failure(done)
        try:
            doc = json.loads(done.out)
            text = (doc.get("outputs") or [{}])[0].get("text")
        except (ValueError, AttributeError, IndexError):
            return False, "respuesta no es JSON"
        if doc.get("ok") is not True or _last_word(text) != "OK":
            return False, reported_error(done) or "no dijo OK"
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
    if mentions(gateway.config(), promotion.old_ref):
        return f"{promotion.old_ref} sigue en la config"
    agents = [place for place in promotion.places if place != "defaults"]
    agent = PROBE_AGENT if PROBE_AGENT in agents else (agents or ["main"])[0]
    ok, why, winner = gateway.turn(agent, promotion.new_ref, purpose="verify")
    if ok and runtime and winner != runtime:
        ok, why = False, f"contesto {winner}, no {runtime}"
    return None if ok else f"turno en {agent}: {why}"


def _write(gateway, target, wait, *, dry_run_first=True):
    """Patch the live config into ``target(live)`` inside an exclusive window.

    The live config is read inside the window, so the patch is computed against
    what is there when it lands. None once applied, else why not.
    """
    with gateway.exclusive(wait):
        live = gateway.config()
        return gateway.patch(
            *merge_patch(live, target(live)), dry_run_first=dry_run_first
        )


def promote(gateway, promotion):
    old, new = promotion.old_ref, promotion.new_ref

    def outcome(status, detail=""):
        return Outcome(promotion, status, detail)

    snapshot = gateway.config()
    runtime = runtime_of(snapshot, old)
    status = "no cambiado"
    try:
        problem = _write(gateway, lambda live: staged(live, old, new), EXCLUSIVE_WAIT)
        if problem:
            problem = f"preparar: {problem}"
        else:
            problem = _probe(gateway, snapshot, old, new)
        if problem is None:
            status = "revertido"
            problem = _write(
                gateway, lambda live: renamed(live, old, new), EXCLUSIVE_WAIT
            )
            if problem is None:
                problem = _verify(gateway, promotion, runtime)
    except Busy as busy:
        # Before the staging nothing was written; after it, the staged ref is
        # additive and harmless and the next run picks up from there.
        return outcome("pendiente", str(busy))
    except Exception as error:  # noqa: BLE001 - the snapshot goes back first
        problem = f"{type(error).__name__}: {error}"
    if problem is None:
        return outcome("cambiado")
    return outcome(status, problem + _restore(gateway, snapshot, old, new))


def _restore(gateway, snapshot, old_ref, new_ref):
    """Take this runner's changes back out of the config live now; what to add
    to the notice.

    A patch that timed out may still have landed, so the restore is computed
    from a fresh read, never from what the runner believes it wrote, and it
    keeps what another writer changed meanwhile. When the two cannot be told
    apart the whole snapshot goes back, and the notice says so. With no window
    within RESTORE_WAIT it runs anyway: a half-applied switch is worse than a
    cut run.
    """
    whole = []

    def target(live):
        restored = undone(live, snapshot, old_ref, new_ref)
        if restored is None:
            whole.append(True)
            return snapshot
        return restored

    note = ""
    try:
        try:
            failed = _write(gateway, target, RESTORE_WAIT, dry_run_first=False)
        except Busy:
            live = gateway.config()
            patch, replace = merge_patch(live, target(live))
            failed = gateway.patch(patch, replace, dry_run_first=False)
            if patch:
                note = "; restaure con corridas en curso, pude cortar alguna"
    except Exception as error:  # noqa: BLE001 - said in the notice
        failed = f"{type(error).__name__}: {error}"
    if failed:
        return f"; NO pude restaurar: {failed}"
    if whole:
        note += "; restaure la config entera de antes, pude borrar cambios ajenos"
    return note


def _promote_or_say_why(gateway, promotion):
    try:
        return promote(gateway, promotion)
    except Exception as error:  # noqa: BLE001 - nothing was written yet
        return Outcome(promotion, "no cambiado", f"{type(error).__name__}: {error}")


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


def autoupdate(gateway, view, last):
    trying, kept = split_held(plan_claw(gateway.config(), view), last)
    outcomes = [_promote_or_say_why(gateway, promotion) for promotion in trying]
    lines, remembered = report(outcomes, last)
    return lines, {**remembered, **kept}


def said(gateway, view, last):
    """``(lines, notices to remember)``; a crash is one notice, never a traceback."""
    try:
        return autoupdate(gateway, view, last)
    except Exception as error:  # noqa: BLE001 - said once
        remembered = {
            **held_notices(last),
            "error": f"- La autoactualizacion de claw fallo: {type(error).__name__}: {error}",
        }
        return fresh_notices(remembered, last), remembered


def daily(gateway, view, state_path, to, *, today):
    """One run: switch what can switch, tell the owner, remember what was told.

    Says nothing when there is nothing new. A failed send leaves the state as
    it was, so the next run says it again; the exit code is then 1.
    """
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.is_file()
        else {}
    )
    lines, remembered = said(gateway, view, state.get("notices", {}))
    if lines:
        text = "\n".join([f"Claw: modelos {today}", *lines])
        print(text)
        problem = gateway.send(to, text)
        if problem:
            print(f"Telegram no tomo el aviso: {problem}", file=sys.stderr)
            return 1
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(
        json.dumps({"notices": remembered}, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(state_path)
    return 0


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--openclaw", default="openclaw", help="openclaw CLI path")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH.expanduser(),
        help="the gateway's live config file",
    )
    parser.add_argument(
        "--state", type=Path, default=here / "claw_autoupdate.state.json"
    )
    parser.add_argument("--to", default=TELEGRAM_TO, help="Telegram chat to notify")
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and change nothing"
    )
    args = parser.parse_args()
    gateway = Gateway(args.openclaw, real_run, config_path=args.config)
    view = catalog_view(*read_catalogs())
    if args.dry_run:
        for promotion in plan_claw(gateway.config(), view):
            print(
                f"{promotion.old_ref} -> {promotion.new_ref} ({', '.join(promotion.places)})"
            )
        return 0
    return daily(gateway, view, args.state, args.to, today=time.strftime("%Y-%m-%d"))


if __name__ == "__main__":
    sys.exit(main())
