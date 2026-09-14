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
_CLEANUP_GRACE_SECONDS = 4


class _WindowsProcessJob:
    """A kill-on-close Job Object that contains a Windows CLI's descendants."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_longlong),
                ("per_job_user_time_limit", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "read_operation_count",
                    "write_operation_count",
                    "other_operation_count",
                    "read_transfer_count",
                    "write_transfer_count",
                    "other_transfer_count",
                )
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", BasicLimitInformation),
                ("io_info", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        self._ctypes = ctypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        self._kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        self._kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = ExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = 0x00002000
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            9,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())

    def assign(self, process: subprocess.Popen[str]) -> None:
        if not self._kernel32.AssignProcessToJobObject(self._handle, process._handle):
            raise self._ctypes.WinError(self._ctypes.get_last_error())

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def _create_windows_process_job() -> _WindowsProcessJob | None:
    if os.name != "nt":
        return None
    try:
        return _WindowsProcessJob()
    except OSError:
        return None


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


def _terminate_process_tree(
    process: subprocess.Popen[str], job: _WindowsProcessJob | None
) -> None:
    """Kill only the timed-out process and descendants in its own group."""
    if os.name == "nt":
        if job is not None:
            try:
                job.close()
            except OSError:
                pass
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=_CLEANUP_GRACE_SECONDS,
                check=False,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            if process.poll() is None:
                process.kill()
        except OSError:
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass


def _close_process_streams(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
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
    job = _create_windows_process_job()
    try:
        process = subprocess.Popen(command, **popen_kwargs)
    except BaseException:
        if job is not None:
            try:
                job.close()
            except OSError:
                pass
        raise
    if job is not None:
        try:
            job.assign(process)
        except OSError:
            try:
                job.close()
            except OSError:
                pass
            job = None
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        cleanup_deadline = time.monotonic() + _CLEANUP_GRACE_SECONDS
        _terminate_process_tree(process, job)
        try:
            remaining = cleanup_deadline - time.monotonic()
            if remaining > 0:
                process.communicate(timeout=remaining)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass
        finally:
            if job is not None:
                try:
                    job.close()
                except OSError:
                    pass
            _close_process_streams(process)
        raise
    finally:
        if job is not None:
            try:
                job.close()
            except OSError:
                pass
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
