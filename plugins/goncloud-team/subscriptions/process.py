"""Safe process boundary for vendor CLI execution.

Prompts travel by stdin or a private temporary file, secrets travel only in
the child environment, and captured output is redacted and capped before it
becomes evidence.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

_MAX_OUTPUT_CHARS = 20_000
_PROMPT_PLACEHOLDER = "{prompt_file}"


class SubscriptionProcessError(RuntimeError):
    """A vendor process could not be launched or did not terminate in time."""


@dataclass(frozen=True)
class ProcessEvidence:
    """Redacted, bounded output of one vendor process."""

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


def _redact_and_cap(text: str, secrets: Sequence[str]) -> str:
    redacted = text or ""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        redacted = redacted.replace(secret, "[redacted]")
    if len(redacted) > _MAX_OUTPUT_CHARS:
        keep = _MAX_OUTPUT_CHARS // 2
        dropped = len(redacted) - 2 * keep
        redacted = (
            redacted[:keep] + f"\n...[truncated {dropped} chars]...\n" + redacted[-keep:]
        )
    return redacted


def _write_private_prompt(prompt: str) -> Path:
    handle, name = tempfile.mkstemp(prefix="goncloud-sub-", suffix=".txt")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(prompt)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise
    return Path(name)


def run_cli(
    command: Sequence[str],
    prompt: str,
    workspace: Path,
    timeout: int,
    env_overlay: Mapping[str, str] | None = None,
    prompt_transport: Literal["stdin", "tempfile"] = "stdin",
    runner=subprocess.run,
) -> ProcessEvidence:
    """Run one vendor command without shell interpolation or leaked secrets."""
    workspace = Path(workspace).resolve()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise SubscriptionProcessError(
            "workspace must be an existing absolute directory"
        )
    command = [str(value) for value in command]
    overlay = dict(env_overlay or {})
    child_env = {**os.environ, **overlay}
    secrets = tuple(overlay.values())
    prompt_path: Path | None = None
    started = time.monotonic()
    try:
        if prompt_transport == "tempfile":
            prompt_path = _write_private_prompt(prompt)
            command = [
                str(prompt_path) if value == _PROMPT_PLACEHOLDER else value
                for value in command
            ]
            stdin_text = None
        else:
            stdin_text = prompt
        completed = runner(
            command,
            cwd=workspace,
            env=child_env,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SubscriptionProcessError(
            f"vendor process timed out after {timeout}s"
        ) from error
    except FileNotFoundError as error:
        raise SubscriptionProcessError(
            "vendor command not found: " + command[0]
        ) from error
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
    return ProcessEvidence(
        returncode=completed.returncode,
        stdout=_redact_and_cap(completed.stdout or "", secrets),
        stderr=_redact_and_cap(completed.stderr or "", secrets),
        duration_seconds=time.monotonic() - started,
    )
