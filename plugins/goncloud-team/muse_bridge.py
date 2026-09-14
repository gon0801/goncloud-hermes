"""Safe Windows-to-WSL bridge for delegated Muse Code work."""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

try:
    from .subscriptions.process import SubscriptionProcessError, run_cli
    from .subscriptions.adapters import classify_failure
    from .subscriptions.types import FailureKind
    from .subscriptions.redaction import redact_data
except ImportError:
    from subscriptions.process import SubscriptionProcessError, run_cli
    from subscriptions.adapters import classify_failure
    from subscriptions.types import FailureKind
    from subscriptions.redaction import redact_data


class MuseBridgeError(RuntimeError):
    """Muse could not accept or complete a delegated task."""

    def __init__(self, message: str, failure=FailureKind.TASK_FAILURE):
        super().__init__(message)
        self.failure = failure


def windows_path_to_wsl(path: Path) -> str:
    raw = str(path)
    if len(raw) < 3 or raw[1:3] not in {":\\", ":/"}:
        raise MuseBridgeError("Muse workspace must be an absolute Windows path")
    drive = raw[0].lower()
    remainder = raw[3:].replace("\\", "/")
    return f"/mnt/{drive}/{remainder}"


def build_muse_command(
    workspace: Path, prompt_file: Path, max_steps: int = 80, model: str | None = None
) -> list[str]:
    if not 1 <= max_steps <= 500:
        raise MuseBridgeError("max_steps must be between 1 and 500")
    return [
        "wsl.exe",
        "-d",
        "Ubuntu",
        "--exec",
        "/home/goncloud/.local/bin/muse",
        "exec",
        "--json",
        "--prompt-file",
        windows_path_to_wsl(prompt_file),
        "--workspace",
        windows_path_to_wsl(workspace),
        "--trust-workspace",
        "--approval-mode",
        "never",
        "--user-input-auto-resolve",
        *(["--model", model] if model is not None else []),
        "--max-model-steps",
        str(max_steps),
    ]


def parse_muse_jsonl(output: str) -> dict:
    model = None
    session_id = None
    terminal_payload = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        stream = event.get("stream") or {}
        if isinstance(stream, dict) and stream.get("kind") == "session":
            session_id = stream.get("id") or session_id
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if event.get("payload_type") == "run.model.configured":
            if model and payload.get("model_id") != model:
                raise MuseBridgeError(
                    "Muse observed model changed", FailureKind.MODEL_UNAVAILABLE
                )
            model = payload.get("model_id") or model
        if event.get("payload_type") == "run.terminal.completed":
            terminal_payload = payload
    if terminal_payload is None:
        raise MuseBridgeError("Muse output did not contain a terminal record")
    return {
        "session_id": session_id,
        "model": model,
        "terminal": terminal_payload.get("terminal"),
        "text": terminal_payload.get("text", ""),
    }


def delegate_to_muse(
    prompt: str,
    workspace: Path,
    actor_role: str,
    max_steps: int = 80,
    timeout: int = 900,
    runner: Callable[..., Any] = subprocess.run,
    model: str | None = None,
) -> dict:
    """Delegate one task to Muse without interpolating user input into a shell."""
    if not prompt.strip():
        raise MuseBridgeError("Muse delegation prompt cannot be empty")

    prompt_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="goncloud-muse-",
            delete=False,
        ) as prompt_handle:
            prompt_handle.write(prompt)
            prompt_path = Path(prompt_handle.name)

        command = build_muse_command(
            workspace, prompt_path, max_steps=max_steps, model=model
        )
        try:
            completed = run_cli(
                command,
                "",
                workspace,
                timeout=min(timeout, 300),
                runner=runner,
            )
        except SubscriptionProcessError as exc:
            failure = exc.failure
            raise MuseBridgeError(
                f"Muse delegation failed ({failure.value})", failure
            ) from exc
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        failure = classify_failure(
            completed.returncode, completed.stdout, completed.stderr
        )
        raise MuseBridgeError(f"Muse delegation failed ({failure.value})", failure)

    result = parse_muse_jsonl(completed.stdout)
    if model is not None and result["model"] != model:
        raise MuseBridgeError(
            "Muse did not prove the requested model", FailureKind.MODEL_UNAVAILABLE
        )
    result["actor_role"] = actor_role
    return redact_data(result, (prompt,))
