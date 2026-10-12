"""One real call through one route, outside the subscription router.

Use it before putting a model into the table. The label and position compile
exactly as the table would, and the adapter is invoked directly, so nothing is
written to ``subscription-router.db``: a failed probe cannot quarantine an
adapter the way ``configure_subscriptions.py --live-probe-all`` does.

    python scripts/probe_route.py --position 3 "meta/muse-wonder-1.0 (max)"

It spends a few tokens. Run it on the PC with the Hermes venv python.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

# Piped through stdin (ssh) there is no file; the checkout is then the cwd.
ROOT = Path(__file__).resolve().parents[1] if "__file__" in globals() else Path.cwd()


def scrub(text, limit=300):
    """One line of evidence with anything key-shaped removed."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = re.sub(r"[A-Za-z0-9_\-+/=]{40,}", "<redacted>", text)
    text = re.sub(r"(?i)(sk-|xai-|key[-_ ]?)[A-Za-z0-9_\-]{10,}", r"\1<redacted>", text)
    return text[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("label", help='Table label, e.g. "meta/muse-wonder-1.0 (max)"')
    parser.add_argument(
        "--position", type=int, required=True, help="Table position 0-7"
    )
    parser.add_argument(
        "--plugin-dir", type=Path, default=ROOT / "plugins" / "goncloud-team"
    )
    parser.add_argument("--timeout", type=int, help="Seconds (default 120; muse 300)")
    args = parser.parse_args()

    sys.dont_write_bytecode = True
    for name in (
        "DISABLE_AUTOUPDATER",
        "OPENCODE_DISABLE_AUTOUPDATE",
        "MUSE_NO_AUTO_UPDATE",
    ):
        os.environ.setdefault(name, "1")
    sys.path.insert(0, str(args.plugin_dir))
    import subscriptions  # noqa: F401 - loads the plugin's private env store
    from subscriptions.adapters import build_adapter_registry, classify_failure
    from subscriptions.adapters._live_probe import PROBE_MARKER, PROBE_PROMPT
    from subscriptions.catalog import _compile_route
    from subscriptions.types import InvocationRequest

    # The router and its store must stay out: they are what records health.
    assert "subscriptions.router" not in sys.modules
    assert "subscriptions.state" not in sys.modules

    route = _compile_route(args.position, args.label)
    adapter = build_adapter_registry()[route.adapter]
    adapter.timeout_seconds = args.timeout or (300 if route.adapter == "muse" else 120)
    workspace = Path(tempfile.mkdtemp(prefix="goncloud-probe-"))
    record = {
        "label": args.label,
        "position": args.position,
        "adapter": route.adapter,
        "model": route.model,
        "effort": route.reasoning_effort,
    }
    started = time.monotonic()
    try:
        result = adapter.invoke(
            InvocationRequest(
                workflow_id="live-probe",
                stage_id=f"probe-{route.adapter}",
                role="claw",
                prompt=PROBE_PROMPT,
                workspace=workspace,
                start_revision="",
                current_revision="",
                max_steps=1,
            ),
            route,
        )
        record["success"] = bool(result.success)
        if result.success:
            record["marker_exact"] = result.text.strip() == PROBE_MARKER
        else:
            record["failure"] = str(result.failure)
            record["detail"] = scrub(getattr(result, "detail", ""))
            record["classified"] = str(
                classify_failure(
                    getattr(result, "returncode", 1) or 1,
                    getattr(result, "stream", "") or "",
                    getattr(result, "stderr", "") or "",
                )
            )
    except Exception as error:  # noqa: BLE001 - the probe reports, it does not raise
        record["success"] = False
        record["failure"] = f"{type(error).__name__}: {scrub(error, 200)}"
    finally:
        record["seconds"] = round(time.monotonic() - started, 1)
        shutil.rmtree(workspace, ignore_errors=True)
    print(json.dumps(record, ensure_ascii=False))
    return 0 if record.get("success") and record.get("marker_exact") else 1


if __name__ == "__main__":
    sys.exit(main())
