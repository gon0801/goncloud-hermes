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

ROOT = Path(__file__).resolve().parents[1]
TABLE = ROOT / "model-routing.source.json"
CLAW = ROOT / "claw-routing.json"
_EFFORT = re.compile(r"\s+\(([^)]+)\)\s*$")
#: Positions 2+ pick their adapter from the model name (subscriptions/catalog.py).
_FAMILIES = ("muse-", "claude-", "gpt-", "k3", "kimi-", "glm-", "grok-", "deepseek-")


def _split(label):
    match = _EFFORT.search(label)
    if match is None:
        return label.strip(), ""
    return label[: match.start()].strip(), label[match.start() :]


def replace_label(source, old, new):
    """Swap ``old`` for ``new`` in place, effort markers kept; return the cells."""
    cells = []
    for row, labels in source["chains"].items():
        for position, label in enumerate(labels):
            model, effort = _split(label)
            if model == old:
                labels[position] = new + effort
                cells.append(f"{row}:{position}")
    if not cells:
        raise ValueError(f"no cell uses {old}")
    name = new.split("/", 1)[-1]
    direct = [cell for cell in cells if int(cell.rsplit(":", 1)[1]) >= 2]
    if direct and not name.startswith(_FAMILIES):
        raise ValueError(
            f"{name} is not a family the router binds, and these cells pick "
            f"their adapter from the name: {', '.join(direct)}"
        )
    return cells


def hold_claw(mapping, old, new):
    """Make claw keep ``old`` wherever the table now says ``new``."""
    mapping.setdefault("id_overrides", {})[new] = old


def release_claw(mapping, new):
    if new not in mapping.get("id_overrides", {}):
        raise ValueError(f"claw has no hold on {new}")
    del mapping["id_overrides"][new]


def probes(source, cells):
    """One real probe per route the change touches, as shell commands."""
    wanted = {}
    for cell in cells:
        row, position = cell.rsplit(":", 1)
        label = source["chains"][row][int(position)]
        # Each OpenCode Go account is its own route; a direct label is one
        # route wherever it sits.
        key = (int(position), label) if int(position) < 2 else (None, label)
        wanted.setdefault(key, int(position))
    return [
        f'python scripts/probe_route.py --position {position} "{label}"'
        for (_, label), position in sorted(wanted.items(), key=lambda item: item[1])
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
    print("Table written. Update the tests that pin the old name, then deploy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
