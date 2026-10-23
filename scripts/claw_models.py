"""Derive claw's model chains from the routing table and compare them to live.

The owner edits one table, ``model-routing.source.json``. Claw (OpenClaw)
keeps the same chains inside its own config; this script says what those
chains should be and where the live ones differ. It never writes to claw.

    python scripts/claw_models.py desired
    python scripts/claw_models.py check --live modelos-vivos-AAAA-MM-DD.json5
"""

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
_EFFORT = re.compile(r"\s+\(([^)]+)\)\s*$")


def split_label(label):
    """``provider/model (effort)`` -> (``provider/model``, effort or None)."""
    match = _EFFORT.search(label)
    if match is None:
        return label.strip(), None
    return label[: match.start()].strip(), match.group(1)


def desired_agent(labels, mapping, *, thinking):
    """One claw agent's ``model`` (and ``models``) for one table row."""
    cells = [split_label(label) for label in labels]
    overrides = mapping.get("id_overrides", {})
    ids = [overrides.get(model, model) for model, _ in cells]
    if cells[1][0] == cells[0][0]:
        # The table repeats position 0 to mean "same model, second OpenCode Go
        # account"; claw addresses that account as a provider of its own.
        provider, model = ids[0].split("/", 1)
        ids[1] = f"{provider}{mapping['second_account_suffix']}/{model}"
    agent = {"model": {"primary": ids[0], "fallbacks": ids[1:]}}
    efforts = {
        model_id: {"params": {"thinking": effort}}
        for model_id, (_, effort) in zip(ids, cells)
        if effort
    }
    if thinking and efforts:
        agent["models"] = efforts
    return agent


def desired_claw(source, mapping):
    """The ``agents.entries`` every claw agent should have, from the table."""
    entries = {}
    for agent, spec in mapping["agents"].items():
        row = source["chains"].get(spec["row"])
        if row is None:
            raise ValueError(f"{agent} points at a row the table lacks: {spec['row']}")
        entries[agent] = desired_agent(row, mapping, thinking=spec["thinking"])
    desired = {"agents": {"entries": entries}}
    default_row = source.get("profile_inheritance", {}).get("default")
    if default_row is not None:
        # Claw's default model is the row the table gives Hermes' ``default`` profile.
        desired["agents"]["defaults"] = desired_agent(
            source["chains"][default_row], mapping, thinking=False
        )
    return desired


def _thinking(entry):
    return {
        model: value["params"]["thinking"]
        for model, value in (entry.get("models") or {}).items()
        if (value.get("params") or {}).get("thinking")
    }


def drift(desired, live, mapping):
    """Human-readable differences between the table's chains and claw's."""
    wanted = desired["agents"]["entries"]
    found = live["agents"]["entries"]
    lines = []
    if "defaults" in desired["agents"]:
        want = desired["agents"]["defaults"]["model"]
        have = live["agents"].get("defaults", {}).get("model") or {}
        want_chain = [want["primary"], *want["fallbacks"]]
        have_chain = [have.get("primary"), *(have.get("fallbacks") or [])]
        for position in range(max(len(want_chain), len(have_chain))):
            a = have_chain[position] if position < len(have_chain) else "nothing"
            b = want_chain[position] if position < len(want_chain) else "nothing"
            if a != b:
                lines.append(
                    f"defaults position {position}: claw has {a}, the table wants {b}"
                )
    for agent in sorted(set(wanted) | set(found)):
        if agent not in found:
            lines.append(f"{agent}: in claw-routing.json but not in claw")
            continue
        if agent not in wanted:
            lines.append(f"{agent}: in claw but not in claw-routing.json")
            continue
        want, have = wanted[agent]["model"], found[agent].get("model") or {}
        want_chain = [want["primary"], *want["fallbacks"]]
        have_chain = [have.get("primary"), *(have.get("fallbacks") or [])]
        for position in range(max(len(want_chain), len(have_chain))):
            a = have_chain[position] if position < len(have_chain) else "nothing"
            b = want_chain[position] if position < len(want_chain) else "nothing"
            if a != b:
                lines.append(
                    f"{agent} position {position}: claw has {a}, the table wants {b}"
                )
        if not mapping["agents"][agent]["thinking"]:
            continue
        want_thinking, have_thinking = _thinking(wanted[agent]), _thinking(found[agent])
        for model in sorted(set(want_thinking) | set(have_thinking)):
            a = have_thinking.get(model, "nothing")
            b = want_thinking.get(model, "nothing")
            if a != b:
                lines.append(
                    f"{agent} thinking {model}: claw has {a}, the table wants {b}"
                )
    return lines


def load_live(path):
    """A dated snapshot: JSON with ``//`` comment lines above it."""
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(
        "\n".join(line for line in text.split("\n") if not line.startswith("//"))
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("desired", help="Print the chains claw should have")
    check = commands.add_parser("check", help="Compare the table with a live snapshot")
    check.add_argument("--live", required=True, help="Snapshot of claw's live chains")
    args = parser.parse_args()

    source = json.loads(
        (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
    )
    mapping = json.loads((ROOT / "claw-routing.json").read_text(encoding="utf-8"))
    desired = desired_claw(source, mapping)
    if args.command == "desired":
        print(json.dumps(desired, indent=2, ensure_ascii=False))
        return 0
    lines = drift(desired, load_live(args.live), mapping)
    for line in lines:
        print(line)
    print(f"{len(lines)} difference(s) in {len(desired['agents']['entries'])} agents")
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main())
