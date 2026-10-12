"""Find the model names written by hand outside the routing table.

Launchers, per-CLI agent files and scripts pin model IDs that no table
governs. This lists every one with where it is, checks it against the public
catalogs, and can save the list so the daily watcher reports when one of them
disappears.

    python3 scripts/pinned_models.py            # report
    python3 scripts/pinned_models.py --write    # report and save scripts/pinned-models.json
"""

import argparse
import glob
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "pinned-files.json"
PINS = ROOT / "scripts" / "pinned-models.json"
#: A family prefix alone is not enough: claude-code, kimi-claude, grok-build and
#: muse-bin are tools, not models. Each alternative demands the shape of an ID.
_MODEL = re.compile(
    r"""(?<![\w.-])(
        claude-(?:opus|sonnet|haiku|fable|mythos)-\d[\w.-]*
      | gpt-\d[\w.-]*
      | grok-\d[\w.-]*
      | glm-\d[\w.-]*
      | kimi-k\d[\w.-]*
      | kimi-for-coding
      | k3(?:-\d+k)?
      | deepseek-(?:v\d|flash|pro|chat|reasoner)[\w.-]*
      | muse-(?:spark|wonder|glimmer)-[\w.-]*
    )(?![\w-])""",
    re.VERBOSE,
)


def find_ids(text):
    """(line number, model ID) for every model ID in ``text``."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in _MODEL.finditer(line):
            found.append((number, match.group(1).rstrip(".-")))
    return found


def scan(patterns):
    """Model ID -> every ``path:line`` that pins it, over the given globs."""
    pins = {}
    for pattern in patterns:
        for path in sorted(glob.glob(os.path.expanduser(pattern))):
            file = Path(path)
            if not file.is_file() or file.stat().st_size > 2_000_000:
                continue
            for number, model in find_ids(
                file.read_text(encoding="utf-8", errors="ignore")
            ):
                pins.setdefault(model, []).append(f"{file.as_posix()}:{number}")
    return pins


def classify(pins, view):
    """Each pinned ID against every catalog source: ok, deprecated or absent."""
    status = {}
    for model in pins:
        facts = [models[model] for models in view.values() if model in models]
        if not facts:
            status[model] = "not in the catalogs"
        elif any(entry.get("status") == "deprecated" for entry in facts):
            status[model] = "deprecated"
        else:
            status[model] = "ok"
    return status


def _model_watch():
    spec = importlib.util.spec_from_file_location(
        "model_watch", ROOT / "scripts/model_watch.py"
    )
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--write", action="store_true", help="Save the list for the watcher"
    )
    args = parser.parse_args()

    watch = _model_watch()
    pins = scan(json.loads(FILES.read_text(encoding="utf-8"))["files"])
    in_table = watch.models_in_use(
        json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))
    )
    models_dev = watch._try(lambda: watch._get_json(watch.MODELS_DEV_URL))
    go = watch._try(
        lambda: [row["id"] for row in watch._get_json(watch.GO_MODELS_URL)["data"]]
    )
    if models_dev is None or go is None:
        print("Could not read the public catalogs; nothing classified.")
        return 2
    status = classify(pins, watch.catalog_view(models_dev, go))
    home = str(Path.home())
    for label in ("not in the catalogs", "deprecated", "ok"):
        rows = sorted(model for model in pins if status[model] == label)
        print(f"\n{label} ({len(rows)})")
        for model in rows:
            places = [place.replace(home, "~") for place in pins[model]]
            table = " [also in the table]" if model in in_table else ""
            shown = ", ".join(places[:4]) + (
                f" (+{len(places) - 4})" if len(places) > 4 else ""
            )
            print(f"  {model}{table}: {shown}")
    if args.write:
        saved = {
            model: [place.replace(home, "~") for place in places]
            for model, places in sorted(pins.items())
        }
        PINS.write_text(
            json.dumps(saved, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"\nSaved {len(saved)} pinned models to {PINS.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
