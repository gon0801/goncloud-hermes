"""Start, resume, or inspect the durable local subscription workflow."""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]


class SafeParser(argparse.ArgumentParser):
    def error(self, _message):
        raise ValueError("invalid command arguments")


def _orchestrator():
    sys.path.insert(0, str(ROOT / "plugins" / "goncloud-team"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        sys.path.insert(0, str(Path(local) / "hermes" / "hermes-agent"))
    from local_orchestrator import LocalOrchestrator, jev_de_produccion

    return LocalOrchestrator(jev=jev_de_produccion())


def main(argv=None, *, orchestrator_factory=None):
    parser = SafeParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", allow_abbrev=False)
    start.add_argument("--repository", required=True)
    start.add_argument("--revision")
    start.add_argument("--request-key")
    start.add_argument(
        "--instruction-file", default="-", help="UTF-8 file, or - for stdin (default)"
    )
    start.add_argument("--sources-file", type=Path)
    start.add_argument("--max-steps", type=int, default=20)
    start.add_argument(
        "--stop-after",
        choices=[
            "scout",
            "ingenieria",
            "implementer",
            "verifier",
            "adversary",
            "reviewer",
            "entrega",
        ],
    )
    start.add_argument("--affects-business-rules", action="store_true")
    start.add_argument("--json", action="store_true")
    for name in ("resume", "status"):
        command = commands.add_parser(name, allow_abbrev=False)
        command.add_argument("--workflow-id", required=True)
        command.add_argument("--json", action="store_true")
    try:
        args = parser.parse_args(argv)
        # Vendor diagnostics never become CLI stdout/stderr, including failures
        # while constructing dependencies. Only the bounded result is printed.
        with (
            open(os.devnull, "w", encoding="utf-8") as sink,
            redirect_stdout(sink),
            redirect_stderr(sink),
        ):
            orchestrator = (orchestrator_factory or _orchestrator)()
            if args.command == "start":
                if args.instruction_file == "-":
                    instruction = sys.stdin.read(32769)
                else:
                    with open(args.instruction_file, encoding="utf-8-sig") as source:
                        instruction = source.read(32769)
                if not instruction.strip() or len(instruction) > 32768:
                    raise ValueError("invalid instruction")
                sources = None
                if args.sources_file:
                    with args.sources_file.open(encoding="utf-8-sig") as source:
                        raw = source.read(32769)
                    if len(raw) > 32768:
                        raise ValueError("oversized sources")
                    sources = json.loads(raw)
                    if not isinstance(sources, list):
                        raise ValueError("sources must be a list")
                result = orchestrator.start(
                    request_key=args.request_key or uuid4().hex,
                    repository=args.repository,
                    revision=args.revision,
                    instruction=instruction,
                    sources=sources,
                    max_steps=args.max_steps,
                    stop_after=args.stop_after,
                    affects_business_rules=args.affects_business_rules,
                )
            else:
                result = getattr(orchestrator, args.command)(args.workflow_id)
    except Exception:
        print(
            json.dumps(
                {
                    "outcome": "error",
                    "error": "local workflow request failed; check assignment and durable status",
                }
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 1 if result.get("outcome") in {"blocked", "error"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
