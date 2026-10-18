"""Replace one model with another in every cell of the routing table.

Without ``--apply`` it only shows the cells and the probes to run first; a
model goes into the table after its real probes pass, never before.

    python scripts/promote_model.py meta/muse-spark-1.3 meta/muse-wonder-1.0
    python scripts/promote_model.py meta/muse-spark-1.3 meta/muse-wonder-1.0 --apply --hold-claw
    python scripts/promote_model.py --release-claw meta/muse-wonder-1.0

``--hold-claw`` keeps claw on the old ID (an ``id_overrides`` entry in
``claw-routing.json``) for when claw cannot serve the new one yet;
``--release-claw`` removes that entry so claw follows the table again.
"""

import argparse
import json
from pathlib import Path
import re
import sys

from model_lines import family_of

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "model-routing.source.json"
CLAW = ROOT / "claw-routing.json"
_EFFORT = re.compile(r"\s+\(([^)]+)\)\s*$")


def split_label(label):
    """``("provider/model", " (effort)")``; the effort part is empty when absent."""
    match = _EFFORT.search(label)
    if match is None:
        return label.strip(), ""
    return label[: match.start()].strip(), label[match.start() :]


def replace_label(source, old, new):
    """Swap ``old`` for ``new`` in place, effort markers kept; return the cells."""
    cells = []
    for row, labels in source["chains"].items():
        for position, label in enumerate(labels):
            model, effort = split_label(label)
            if model == old:
                labels[position] = new + effort
                cells.append(f"{row}:{position}")
    if not cells:
        raise ValueError(f"no cell uses {old}")
    name = new.split("/", 1)[-1]
    # Every position binds by family now (v6 table, 2026-10-17): an unknown
    # family would take the whole row down at compile time.
    if family_of(name) is None:
        raise ValueError(
            f"{name} is not a family the router binds, and these cells pick "
            f"their adapter from the name: {', '.join(cells)}"
        )
    return cells


def hold_claw(mapping, old, new):
    """Make claw keep ``old`` wherever the table now says ``new``."""
    mapping.setdefault("id_overrides", {})[new] = old


def release_claw(mapping, new):
    if new not in mapping.get("id_overrides", {}):
        raise ValueError(f"claw has no hold on {new}")
    del mapping["id_overrides"][new]


#: Higher wins: the probe runs the model at the highest effort the table
#: gives it (owner call, 2026-10-18 — one probe per model, or grok-4.7's
#: three efforts blew the watcher's cron budget and never moved).
_EFFORT_RANK = {"max": 5, "xhigh": 4, "high": 3, "medium": 2, "low": 1}


def probe_plan(source, cells):
    """``(probes, unprobed)``: one real probe per MODEL, the rest recorded.

    ``probes`` is ``(position, label)`` with the label at the model's highest
    effort; ``unprobed`` maps the model to the efforts of its other labels,
    which are answered by the same probe result.
    """
    best, others = {}, {}
    for cell in cells:
        row, position = cell.rsplit(":", 1)
        label = source["chains"][row][int(position)]
        model, effort = split_label(label)
        effort = effort.strip().strip("()")
        rank = _EFFORT_RANK.get(effort, 0)
        if model not in best:
            best[model] = (int(position), label, rank)
            others[model] = []
        elif rank > best[model][2]:
            position0, label0, _ = best[model]
            others[model].append(split_label(label0)[1].strip().strip("()"))
            best[model] = (int(position), label, rank)
        elif rank < best[model][2]:
            others[model].append(effort)
    probes = [
        (position, label)
        for position, label, _ in sorted(best.values(), key=lambda item: item[0])
    ]
    unprobed = {
        model: list(dict.fromkeys(efforts))
        for model, efforts in others.items()
        if efforts
    }
    return probes, unprobed


def probe_routes(source, cells):
    """``(position, label)`` for one real probe per route the change touches."""
    return probe_plan(source, cells)[0]


def unprobed_efforts(source, cells):
    """``{model: [efforts]}`` the model probe does not answer for."""
    return probe_plan(source, cells)[1]


def probes(source, cells):
    """The same probes as shell commands to run on the PC."""
    return [
        f'python scripts/probe_route.py --position {position} "{label}"'
        for position, label in probe_routes(source, cells)
    ]


def _write(path, document):
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "old", nargs="?", help="Model in the table, e.g. meta/muse-spark-1.3"
    )
    parser.add_argument("new", nargs="?", help="Model to put in its place")
    parser.add_argument("--apply", action="store_true", help="Write the table")
    parser.add_argument(
        "--hold-claw", action="store_true", help="Keep claw on the old ID"
    )
    parser.add_argument(
        "--release-claw", metavar="NEW", help="Let claw follow the table"
    )
    args = parser.parse_args()

    mapping = json.loads(CLAW.read_text(encoding="utf-8"))
    if args.release_claw:
        release_claw(mapping, args.release_claw)
        _write(CLAW, mapping)
        print(f"claw follows the table for {args.release_claw} again")
        return 0
    if not (args.old and args.new):
        parser.error("give the old and the new model")
    source = json.loads(TABLE.read_text(encoding="utf-8"))
    cells = replace_label(source, args.old, args.new)
    print(f"{args.old} -> {args.new} in {len(cells)} cell(s): {', '.join(cells)}")
    if not args.apply:
        print("Nothing written. Run these on the PC first; each must print success:")
        for command in probes(source, cells):
            print(f"  {command}")
        return 0
    _write(TABLE, source)
    if args.hold_claw:
        hold_claw(mapping, args.old, args.new)
        _write(CLAW, mapping)
        print(f"claw stays on {args.old}")
    print("Table written. Commit it, then deploy with configure_team.py.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
