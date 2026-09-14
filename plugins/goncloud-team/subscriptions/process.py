"""Safe process boundary for vendor CLI execution.

Prompts travel by stdin or a private temporary file, secrets travel only in
the child environment, and captured output is redacted and capped before it
becomes evidence.
"""

from __future__ import annotations

import os
import signal
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence

_MAX_OUTPUT_CHARS = 20_000
_PROMPT_PLACEHOLDER = "{prompt_file}"
_CLEANUP_GRACE_SECONDS = 5


def _resolve_windows_shim(command: list[str]) -> list[str]:
    """npm installs CLIs as .cmd shims that CreateProcess cannot execute."""
    if os.name != "nt":
        return command
    resolved = shutil.which(command[0])
    if resolved and resolved.lower().endswith((".cmd", ".bat")):
        # /d skips registry AutoRun; the resolved absolute path is explicit.
        return ["cmd.exe", "/d", "/c", resolved, *command[1:]]
    return command


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
            redacted[:keep]
            + f"\n...[truncated {dropped} chars]...\n"
            + redacted[-keep:]
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


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    """Kill only the timed-out process and descendants in its own group."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=_CLEANUP_GRACE_SECONDS,
                check=False,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            # The parent is still our own process and is safer to stop than leave.
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _run_with_process_tree(
    command: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_text: str | None,
    timeout: int,
) -> tuple[int, str, str]:
    """Run a real CLI in an isolated group so timeout cleanup includes children."""
    popen_kwargs = {
        "cwd": cwd,
        "env": env,
        "stdin": subprocess.PIPE if stdin_text is not None else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
        _terminate_process_tree(process)
        try:
            remaining = cleanup_deadline - time.monotonic()
            if remaining > 0:
                process.communicate(timeout=remaining)
            elif process.poll() is None:
                process.kill()
        except subprocess.TimeoutExpired:
            process.kill()
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        raise
    return process.returncode, stdout or "", stderr or ""


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
    if type(timeout) is not int or timeout <= 0:
        raise SubscriptionProcessError(
            "timeout must be a positive integer number of seconds"
        )
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
        if runner is subprocess.run:
            returncode, stdout, stderr = _run_with_process_tree(
                _resolve_windows_shim(command),
                cwd=workspace,
                env=child_env,
                stdin_text=stdin_text,
                timeout=timeout,
            )
        else:
            completed = runner(
                command,
                cwd=workspace,
                env=child_env,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                shell=False,
            )
            returncode = completed.returncode
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
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
        returncode=returncode,
        stdout=_redact_and_cap(stdout, secrets),
        stderr=_redact_and_cap(stderr, secrets),
        duration_seconds=time.monotonic() - started,
    )
