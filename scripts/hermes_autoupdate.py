"""Move the routing table to a newer release of a model it uses, on its own.

The daily model watcher calls ``step`` on the Windows PC after reading the
public catalogs. A model moves only after every real probe of its new release
passes. Then the table is committed in the PC checkout and deployed, and a real
turn is checked. When that check fails, the commit is reverted and redeployed.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
import time

from model_lines import (
    FAMILIES,
    GO,
    HELD,
    ON_HOLD,
    Promotion,
    Ran,
    change_of,
    family_of,
    fresh_notices,
    newer_in_line,
    run as real_run,
    split_held,
)
from promote_model import probe_plan, replace_label, split_label

TABLE = "model-routing.source.json"
#: Written by configure_team.py next to the installed watcher.
SETUP = "autoupdate.json"
#: Hermes kills a no_agent cron script after this much wall clock
#: (cron/scheduler_script.py: HERMES_CRON_SCRIPT_TIMEOUT, else
#: cron.script_timeout_seconds, else 3600). A promotion starts only when its
#: worst case still fits, so it is never cut off half deployed.
SCRIPT_LIMIT = 3600
SAFETY = 300
PROBE_TIMEOUT = 330  # probe_route.py gives Muse 300 s
DEPLOY_TIMEOUT = 300
TURN_TIMEOUT = 300
GIT_TIMEOUT = 120
_GIT_STEPS = 7


@dataclass(frozen=True)
class Setup:
    checkout: Path
    python: str
    hermes: str
    git: str = "git"

    @classmethod
    def read(cls, path):
        fields = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            Path(fields["checkout"]),
            fields["python"],
            fields["hermes"],
            fields.get("git") or "git",
        )


@dataclass(frozen=True)
class Outcome:
    promotion: Promotion
    status: str  # "cambiado" | "no cambiado" | "revertido" | "pendiente"
    detail: str = ""


def plan_table(source, view):
    """Promotions for the table, and the labels skipped for running on two routes.

    A label whose provider is OpenCode Go follows the Go list; any other
    provider follows its vendor's catalog (v6 table, 2026-10-17: binding is
    by family on every position, Go only for DeepSeek Flash and Muse Spark).
    One model on both routes would need two catalogs to agree, so it is left
    to the owner.
    """
    cells_by_ref, refs_by_model = {}, {}
    for row, labels in source["chains"].items():
        for position, label in enumerate(labels):
            ref, _ = split_label(label)
            cells_by_ref.setdefault(ref, []).append(f"{row}:{position}")
            refs_by_model.setdefault(ref.rpartition("/")[2], set()).add(ref)
    promotions, mixed = [], []
    for model, refs in sorted(refs_by_model.items()):
        if len(refs) > 1:
            mixed.append(model)
            continue
        ref = next(iter(refs))
        provider, _, model_id = ref.rpartition("/")
        catalog = view.get(
            GO if provider.startswith(GO) else FAMILIES.get(family_of(model_id))
        )
        newer = newer_in_line(model_id, catalog) if catalog is not None else None
        if newer:
            promotions.append(
                Promotion(ref, f"{provider}/{newer}", tuple(cells_by_ref[ref]))
            )
    return promotions, mixed


def _probe_failure(done: Ran):
    if done.code is None:
        return done.err
    try:
        record = json.loads(done.out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return done.why()
    if record.get("success") and not record.get("marker_exact"):
        return "contesto otra cosa"
    return record.get("failure") or done.why()


def _is_pong(text):
    # hermes chat -Q prints the reply, a blank line and then "session_id: ...".
    return any(line.strip(" .!\t").upper() == "PONG" for line in text.splitlines())


def _pong_problem(setup, run):
    """None when claw answers a real turn with PONG, else why not."""
    turn = run(
        [
            setup.hermes,
            "-p",
            "claw",
            "chat",
            "-q",
            "Responde solo: PONG",
            "--oneshot",
            "-Q",
        ],
        TURN_TIMEOUT,
    )
    if not turn.ok:
        # The session id changes every run; a notice quoting it would repeat daily.
        out = "\n".join(
            line for line in turn.out.splitlines() if not line.startswith("session_id:")
        )
        return Ran(turn.code, out, turn.err).why()
    return None if _is_pong(turn.out) else "no dijo PONG"


def promote(setup, promotion, run, budget):
    """Probe, switch, deploy and check one promotion; revert when the check fails."""
    git = [setup.git, "-C", str(setup.checkout)]
    configure = [setup.python, str(setup.checkout / "scripts/configure_team.py")]
    table = setup.checkout / TABLE
    old, new = promotion.old_ref, promotion.new_ref

    def failed(detail, status="no cambiado"):
        return Outcome(promotion, status, detail)

    dirty = run([*git, "status", "--porcelain", "--", TABLE], GIT_TIMEOUT)
    if not dirty.ok or dirty.out.strip():
        return failed("la tabla del checkout tiene cambios sin commit")
    source = json.loads(table.read_text(encoding="utf-8"))
    changed = copy.deepcopy(source)
    try:
        cells = replace_label(changed, old, new)
    except ValueError as error:
        return failed(str(error))
    routes, unprobed = probe_plan(changed, cells)
    worst = (
        len(routes) * PROBE_TIMEOUT
        + 3 * DEPLOY_TIMEOUT
        + 2 * TURN_TIMEOUT
        + _GIT_STEPS * GIT_TIMEOUT
    )
    if worst > budget():
        return failed("no cupo en el tiempo de esta corrida", "pendiente")
    for position, label in routes:
        probe = run(
            [
                setup.python,
                str(setup.checkout / "scripts/probe_route.py"),
                "--position",
                str(position),
                label,
            ],
            PROBE_TIMEOUT,
        )
        if not probe.ok:
            return failed(f"sonda posicion {position}: {_probe_failure(probe)}")
    # The check after deploying is the same turn on today's chain. When it
    # fails before anything changes, a failure after would say nothing about
    # the new model and would revert it.
    baseline = _pong_problem(setup, run)
    if baseline is not None:
        return failed(f"turno PONG antes de cambiar: {baseline}", "pendiente")

    table.write_text(
        json.dumps(changed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    commit = run(
        [*git, "commit", "-m", f"auto(modelos): {old} -> {new}", "--", TABLE],
        GIT_TIMEOUT,
    )
    if not commit.ok:
        run([*git, "checkout", "--", TABLE], GIT_TIMEOUT)
        return failed(f"commit: {commit.why()}")

    problem = None
    deploy = run(configure, DEPLOY_TIMEOUT)
    if not deploy.ok:
        problem = f"despliegue: {deploy.why()}"
    if problem is None:
        check = run([*configure, "--check"], DEPLOY_TIMEOUT)
        if not check.ok:
            problem = f"configure_team --check: {check.why()}"
    if problem is None:
        turn = _pong_problem(setup, run)
        if turn is not None:
            problem = f"turno PONG: {turn}"
    if problem is not None:
        revert = run([*git, "revert", "--no-edit", "HEAD"], GIT_TIMEOUT)
        redeploy = run(configure, DEPLOY_TIMEOUT) if revert.ok else None
        if redeploy is None:
            problem += f"; NO pude revertir el commit: {revert.why()}"
        elif not redeploy.ok:
            problem += f"; revertido en git pero NO redesplegado: {redeploy.why()}"
        return failed(problem, "revertido")

    # A merge, not a rebase: rebasing would rewrite the commit just deployed,
    # and configure_team.py then refuses every later deploy as a regression.
    notes = []
    if unprobed:
        efforts = ", ".join(
            f"({effort})" for efforts in unprobed.values() for effort in efforts
        )
        notes.append(f"sin sondar: {efforts}")
    pull = run([*git, "pull", "--no-rebase", "--no-edit"], GIT_TIMEOUT)
    if not pull.ok:
        run([*git, "merge", "--abort"], GIT_TIMEOUT)
        notes.append(f"sin push (pull: {pull.why()})")
        return Outcome(promotion, "cambiado", "; ".join(notes))
    push = run([*git, "push"], GIT_TIMEOUT)
    if not push.ok:
        notes.append(f"sin push ({push.why()})")
        return Outcome(promotion, "cambiado", "; ".join(notes))
    return Outcome(promotion, "cambiado", "; ".join(notes))


def autoupdate(setup, promotions, *, run=real_run, clock=time.monotonic, deadline):
    return [
        promote(setup, promotion, run, lambda: deadline - clock())
        for promotion in promotions
    ]


def report(outcomes, mixed, last):
    """Lines for the message, and the notices to remember for the next run.

    A switch is said once by nature. Everything else repeats only when its
    wording changes.
    """
    lines, remembered = [], {}
    for outcome in outcomes:
        promotion = outcome.promotion
        change = change_of(promotion)
        if outcome.status == "cambiado":
            lines.append(
                f"- Cambiado: {change} en {', '.join(promotion.places)}"
                + (f"; {outcome.detail}" if outcome.detail else "")
                + "."
            )
        elif outcome.status == "revertido":
            remembered[HELD + change] = (
                f"- Revertido: {change} ({outcome.detail}). {ON_HOLD}"
            )
        else:
            remembered[change] = (
                f"- {outcome.status.capitalize()}: {change} ({outcome.detail})."
            )
    for ref in mixed:
        remembered[f"mixto:{ref}"] = (
            f"- {ref} corre en OpenCode Go y directo a la vez; no lo actualizo solo."
        )
    return lines + fresh_notices(remembered, last), remembered


def step(here, view, last, *, started, run=real_run, clock=time.monotonic):
    """The watcher's hook: ``(lines, notices to remember)``; nothing without setup."""
    setup_path = Path(here) / SETUP
    if not setup_path.is_file():
        return [], {}
    setup = Setup.read(setup_path)
    source = json.loads((setup.checkout / TABLE).read_text(encoding="utf-8"))
    promotions, mixed = plan_table(source, view)
    trying, kept = split_held(promotions, last)
    outcomes = autoupdate(
        setup,
        trying,
        run=run,
        clock=clock,
        deadline=started + SCRIPT_LIMIT - SAFETY,
    )
    lines, remembered = report(outcomes, mixed, last)
    return lines, {**remembered, **kept}
