"""Daily watcher: say when the public catalogs change for the models in use.

Runs as a Hermes ``no_agent`` cron script: whatever it prints is delivered to
the owner, and printing nothing is a silent run. It calls no model and changes
nothing; switching a model stays the owner's decision.

    python model_watch.py [--table PATH] [--state PATH] [--dry-run]
"""

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.request

#: Model-name prefix -> the models.dev provider that carries that vendor's own
#: catalog. The prefixes are the families the subscription router binds
#: (subscriptions/catalog.py); a test keeps the two lists equal.
FAMILIES = {
    "muse-": "meta",
    "claude-": "anthropic",
    "gpt-": "openai",
    "grok-": "xai",
    "glm-": "zai-coding-plan",
    "k3": "kimi-code-plan-global",
    "kimi-": "kimi-code-plan-global",
    "deepseek-": "deepseek",
}
GO = "opencode-go"
_NAMES = {
    GO: "OpenCode Go",
    "meta": "Meta",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "xai": "xAI",
    "zai-coding-plan": "Z.AI",
    "kimi-code-plan-global": "Kimi",
    "deepseek": "DeepSeek",
    "models.dev": "models.dev",
}
MODELS_DEV_URL = "https://models.dev/api.json"
GO_MODELS_URL = "https://opencode.ai/zen/go/v1/models"
MUSE_CHANNEL_URL = "https://api.meta.ai/muse-code/channels/muse-stable"
_EFFORT = re.compile(r"\s+\(([^)]+)\)\s*$")
BLIND_AFTER = 3


def family_of(model):
    return next((prefix for prefix in FAMILIES if model.startswith(prefix)), None)


def models_in_use(source):
    """Model ID -> the ``row:position`` cells of the table that use it."""
    in_use = {}
    for row, labels in source["chains"].items():
        for position, label in enumerate(labels):
            model = _EFFORT.sub("", label).split("/", 1)[-1].strip()
            in_use.setdefault(model, []).append(f"{row}:{position}")
    return in_use


def _on_go(uses):
    # Positions 0 and 1 always run on OpenCode Go; the rest go to the vendor.
    return [use for use in uses if int(use.rsplit(":", 1)[1]) < 2]


def _direct(uses):
    return [use for use in uses if int(use.rsplit(":", 1)[1]) >= 2]


def compare(previous, current, in_use, pins=None):
    """Notices for what changed between two catalog views; none on the first run.

    ``pins`` are models written by hand outside the table (ID -> places); they
    are reported when they leave any catalog that listed them.
    """
    if previous is None:
        return []
    families = {family_of(model) for model in in_use} - {None}
    new, gone = [], []
    for source in sorted(current):
        if source not in previous:
            continue
        for model in sorted(set(current[source]) - set(previous[source])):
            family = family_of(model)
            if family in families and source in (GO, FAMILIES[family]):
                new.append(
                    {
                        "kind": "new",
                        "source": source,
                        "id": model,
                        **current[source][model],
                    }
                )
    for model, uses in sorted(in_use.items()):
        family = family_of(model)
        routes = [(GO, _on_go(uses))]
        if family is not None:
            routes.append((FAMILIES[family], _direct(uses)))
        for source, cells in routes:
            if not cells or source not in previous or source not in current:
                continue
            if model in previous[source] and model not in current[source]:
                gone.append(
                    {"kind": "gone", "source": source, "id": model, "uses": cells}
                )
    for model, places in sorted((pins or {}).items()):
        if model in in_use:
            for notice in gone:
                if notice["id"] == model:
                    notice["uses"] = [*notice["uses"], *places]
            continue
        for source in sorted(previous):
            if (
                source in current
                and model in previous[source]
                and model not in current[source]
            ):
                gone.append(
                    {
                        "kind": "gone",
                        "source": source,
                        "id": model,
                        "uses": list(places),
                    }
                )
    return new + gone


def merge_state(previous, current):
    """The next baseline: what was read now, and the old view of what was not."""
    return {**(previous or {}), **current}


def cli_notices(installed, latest, notified):
    return [
        {"kind": "cli", "name": name, "installed": installed[name], "latest": version}
        for name, version in sorted(latest.items())
        if name in installed
        and installed[name] != version
        and notified.get(name) != version
    ]


def blind_notices(failures, read_ok):
    """Count consecutive unreadable runs per source; speak on the third."""
    after = {
        source: failures.get(source, 0) + 1 for source, ok in read_ok.items() if not ok
    }
    notices = [
        {"kind": "blind", "source": source}
        for source, count in sorted(after.items())
        if count == BLIND_AFTER
    ]
    return notices, after


def render(notices, in_use, today):
    if not notices:
        return ""
    lines = [f"Modelos: novedades del {today}"]
    for notice in notices:
        source = _NAMES.get(notice.get("source"), notice.get("source"))
        if notice["kind"] == "new":
            facts = []
            if notice.get("release_date"):
                facts.append(f"salio {notice['release_date']}")
            if notice.get("context"):
                facts.append(f"contexto {notice['context']}")
            family = family_of(notice["id"])
            mine = sorted(model for model in in_use if family_of(model) == family)
            lines.append(
                f"- Nuevo en {source}: {notice['id']}"
                + (f" ({', '.join(facts)})" if facts else "")
                + f". Hoy usas de esa familia: {', '.join(mine)}."
            )
        elif notice["kind"] == "gone":
            lines.append(
                f"- Ya no aparece en {source}: {notice['id']} "
                f"(lo usan {', '.join(notice['uses'])})."
            )
        elif notice["kind"] == "cli":
            lines.append(
                f"- {notice['name']} atrasado: instalado {notice['installed']}, "
                f"disponible {notice['latest']}."
            )
        elif notice["kind"] == "blind":
            lines.append(
                f"- No pude leer {source} en {BLIND_AFTER} corridas seguidas; "
                "mientras siga asi no veo cambios ahi."
            )
    lines.append("No se cambio nada: cambiar un modelo lo decides tu.")
    return "\n".join(lines)


def catalog_view(models_dev, go_ids):
    """Reduce the public catalogs to the sources and facts the watcher compares."""
    view = {}
    if models_dev is not None:
        for provider in sorted(set(FAMILIES.values())):
            models = (models_dev.get(provider) or {}).get("models") or {}
            view[provider] = {
                model: {
                    key: value
                    for key, value in (
                        ("release_date", facts.get("release_date")),
                        ("context", (facts.get("limit") or {}).get("context")),
                        ("status", facts.get("status")),
                    )
                    if value
                }
                for model, facts in models.items()
            }
    if go_ids is not None:
        view[GO] = {model: {} for model in go_ids}
    return view


def _get_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "goncloud-model-watch"}
    )
    with urllib.request.urlopen(request, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def _try(fetch):
    try:
        return fetch()
    except Exception:  # noqa: BLE001 - any failure means "not read this run"
        return None


def _installed_muse():
    """The Muse the adapter runs lives in WSL; read its version file, not the CLI."""
    if os.name != "nt":
        return None
    result = subprocess.run(
        [
            "wsl.exe",
            "-d",
            "Ubuntu",
            "--exec",
            "cat",
            "/home/goncloud/.local/bin/.muse-version",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    version = result.stdout.strip().splitlines()[-1:] if result.returncode == 0 else []
    return version[0] if version else None


def main():
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--table", type=Path, help="model-routing.source.json")
    parser.add_argument("--state", type=Path, default=here / "model_watch.state.json")
    parser.add_argument(
        "--dry-run", action="store_true", help="Do not save the new baseline"
    )
    args = parser.parse_args()
    table = args.table or next(
        path
        for path in (
            here.parent / "plugins/goncloud-team/model-routing.source.json",
            here.parent / "model-routing.source.json",
        )
        if path.is_file()
    )
    in_use = models_in_use(json.loads(table.read_text(encoding="utf-8")))
    state = (
        json.loads(args.state.read_text(encoding="utf-8"))
        if args.state.is_file()
        else {}
    )

    models_dev = _try(lambda: _get_json(MODELS_DEV_URL))
    go = _try(lambda: [row["id"] for row in _get_json(GO_MODELS_URL)["data"]])
    channel = _try(lambda: _get_json(MUSE_CHANNEL_URL)["version"])
    muse = _try(_installed_muse)

    current = catalog_view(models_dev, go)
    pins_file = here / "pinned-models.json"
    pins = (
        json.loads(pins_file.read_text(encoding="utf-8")) if pins_file.is_file() else {}
    )
    notices = compare(state.get("sources"), current, in_use, pins)
    latest = {"muse": channel} if channel else {}
    cli = cli_notices(
        {"muse": muse} if muse else {}, latest, state.get("cli_notified", {})
    )
    blind, failures = blind_notices(
        state.get("failures", {}),
        {"models.dev": models_dev is not None, GO: go is not None},
    )

    text = render(notices + cli + blind, in_use, datetime.date.today().isoformat())
    if text:
        print(text)
    if not args.dry_run:
        new_state = {
            "sources": merge_state(state.get("sources"), current),
            "cli_notified": {
                **state.get("cli_notified", {}),
                **{notice["name"]: notice["latest"] for notice in cli},
            },
            "failures": failures,
        }
        temporary = args.state.with_name(args.state.name + ".tmp")
        temporary.write_text(
            json.dumps(new_state, ensure_ascii=False), encoding="utf-8"
        )
        temporary.replace(args.state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
