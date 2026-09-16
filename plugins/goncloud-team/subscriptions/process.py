"""Safe process boundary for vendor CLI execution.

Prompts travel by stdin or a private temporary file. Vendors receive secrets
through their environment; Windows bootstrap configuration travels over a
private pipe. Captured output is redacted and capped before it becomes evidence.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Mapping, Sequence

from .private_files import open_private_file
from .redaction import redact_text, sensitive_values
from .types import FailureKind

_MAX_OUTPUT_CHARS = 20_000
_PROMPT_PLACEHOLDER = "{prompt_file}"
#: Adapters put this token in argv where the private prompt file path belongs.
PROMPT_PLACEHOLDER = _PROMPT_PLACEHOLDER
_CLEANUP_GRACE_SECONDS = 4
_WINDOWS_GATE = "goncloud-subscription-start!"
_WINDOWS_GATE_LAUNCHER = (
    "import json, subprocess, sys\n"
    f"gate = sys.stdin.buffer.read({len(_WINDOWS_GATE.encode('ascii'))})\n"
    f"if gate != {(_WINDOWS_GATE.encode('ascii'))!r}:\n"
    "    raise SystemExit(125)\n"
    "config = json.loads(sys.stdin.buffer.readline())\n"
    "payload = sys.stdin.buffer.read()\n"
    "child = subprocess.Popen(config['command'], executable=config['executable'], "
    "cwd=config['cwd'], "
    "env=config['env'], stdin=subprocess.PIPE, stdout=sys.stdout.buffer, "
    "stderr=sys.stderr.buffer)\n"
    "child.communicate(input=payload)\n"
    "raise SystemExit(child.wait())\n"
)

# A Windows Job cannot contain Linux descendants. This isolated Linux guardian
# owns/reaps its descendants, including children that create another session.
# Its absolute deadline survives loss of wsl.exe and includes WSL startup time.
_WSL_PROCESS_GUARD = """
import ctypes, os, pathlib, signal, subprocess, sys, time
deadline = float(sys.argv[1])
if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
    raise SystemExit(125)
stopped = False
def stop(signum, frame):
    global stopped
    stopped = True
for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
    signal.signal(sig, stop)
children_file = pathlib.Path('/proc/self/task') / str(os.getpid()) / 'children'
def children():
    return [int(pid) for pid in children_file.read_text().split()]
code = 124
child = None
try:
    if time.time() < deadline and not stopped:
        child = subprocess.Popen(sys.argv[2:], start_new_session=True)
        while time.time() < deadline and not stopped:
            code = child.poll()
            if code is not None:
                break
            time.sleep(min(0.02, max(0, deadline - time.time())))
        else:
            code = 124
finally:
    cleanup_deadline = time.monotonic() + 0.9
    while True:
        owned = children()
        if not owned:
            break
        for pid in owned:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        while True:
            try:
                if os.waitpid(-1, os.WNOHANG)[0] == 0:
                    break
            except ChildProcessError:
                break
        if time.monotonic() >= cleanup_deadline:
            code = 125
            break
        time.sleep(0.01)
    if child is not None:
        child.returncode = code
raise SystemExit(code)
"""


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
        # The internal shell must never use provider PATH/cwd lookup. /d skips
        # registry AutoRun; SYSTEMROOT comes from the host, not the overlay.
        shell = Path(os.environ.get("SYSTEMROOT", "")) / "System32" / "cmd.exe"
        try:
            available = shell.is_absolute() and shell.is_file()
        except OSError:
            available = False
        if not available:
            raise SubscriptionProcessError("Windows system shell unavailable")
        return [str(shell), "/d", "/c", resolved, *command[1:]]
    return command


class SubscriptionProcessError(RuntimeError):
    """A vendor process could not be launched or did not terminate in time.

    The kind travels with the error so callers never have to re-derive it from
    the message: a deadline is recoverable and advances the slot, while a launch
    failure is not, and guessing that difference from text blocks live stages.
    """

    def __init__(
        self, message: str, failure: FailureKind = FailureKind.TASK_FAILURE
    ) -> None:
        super().__init__(message)
        self.failure = failure


@dataclass(frozen=True)
class ProcessEvidence:
    """Redacted, bounded output of one vendor process.

    ``stdout``/``stderr`` are what may become evidence, so they stay capped.
    ``stream`` is the same redacted stdout without that cap and exists only so
    adapters parse the whole vendor stream: the cap cuts mid-line and its
    marker is not JSON, which turned a completed stage into a task failure.
    It is kept out of ``repr`` and equality so it never leaks into evidence.
    """

    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    stream: str = field(default="", repr=False, compare=False)


def _redact_and_cap(text: str, secrets: Sequence[str]) -> str:
    return _cap(redact_text(text, secrets))


def _cap(redacted: str) -> str:
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
    # The assignment is a secret everywhere else; the file that carries it is
    # owner-only from creation, so a crash before cleanup leaks nothing.
    path = Path(tempfile.gettempdir()) / f"goncloud-sub-{os.urandom(8).hex()}.txt"
    handle = open_private_file(path)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as prompt_file:
            prompt_file.write(prompt)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def _windows_bootstrap_env() -> dict[str, str]:
    return {
        name: value
        for name in ("COMSPEC", "SYSTEMROOT", "WINDIR")
        if (value := os.environ.get(name))
    }


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


def _fail_closed_launcher(
    process: subprocess.Popen[str], job: _WindowsProcessJob
) -> None:
    """Stop a suspended-by-gate launcher before it can release a vendor command."""
    try:
        job.close()
    except OSError:
        pass
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    finally:
        _close_process_streams(process)
    try:
        process.wait(timeout=0.25)
    except (OSError, subprocess.TimeoutExpired):
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
    windows = os.name == "nt"
    job = _create_windows_process_job()
    if windows and job is None:
        raise SubscriptionProcessError("vendor process containment unavailable")
    launched_command = command
    launched_stdin = stdin_text
    launch_cwd = cwd
    launch_env = env
    if windows:
        # Resolve without executing vendor code: the bootstrap deliberately has
        # no PATH, and Windows CreateProcess ignores PATH in a child's env.
        try:
            if os.path.isabs(command[0]) and os.path.isfile(command[0]):
                executable = command[0]
            else:
                # Only provider executables use search-path resolution. The
                # internal shim shell already has a validated absolute path.
                search_path = env.get("PATH", os.defpath)
                if systemroot := os.environ.get("SYSTEMROOT"):
                    search_path += os.pathsep + os.path.join(systemroot, "System32")
                executable = shutil.which(command[0], path=search_path)
            if executable is None:
                raise FileNotFoundError(command[0])
            launch_config = json.dumps(
                {
                    "command": command,
                    "executable": os.path.abspath(executable),
                    "cwd": str(cwd),
                    "env": dict(env),
                }
            )
        except BaseException:
            try:
                job.close()
            except OSError:
                pass
            raise
        launched_command = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _WINDOWS_GATE_LAUNCHER,
        ]
        # communicate sends this only after Job assignment. JSON escapes
        # embedded newlines, so the anonymous pipe frames config separately
        # from the payload forwarded to the vendor's own stdin pipe. Neither
        # credentials nor launch config enter a file or the bootstrap's env.
        launched_stdin = _WINDOWS_GATE + launch_config + "\n" + (stdin_text or "")
        launch_cwd = Path(sys.executable).resolve().parent
        launch_env = _windows_bootstrap_env()
    popen_kwargs = {
        "cwd": launch_cwd,
        "env": launch_env,
        "stdin": subprocess.PIPE if launched_stdin is not None else None,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
    }
    if windows:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    try:
        try:
            process = subprocess.Popen(launched_command, **popen_kwargs)
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
                _fail_closed_launcher(process, job)
                raise SubscriptionProcessError("vendor process containment unavailable")
        try:
            stdout, stderr = process.communicate(input=launched_stdin, timeout=timeout)
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
                _close_process_streams(process)
            raise
        return process.returncode, stdout or "", stderr or ""
    finally:
        if job is not None:
            try:
                job.close()
            except OSError:
                pass


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
    secrets = sensitive_values(child_env) + tuple(overlay.values())
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
            wsl = (
                len(command) > 4
                and Path(command[0]).name.lower() == "wsl.exe"
                and command[1] == "-d"
                and command[3] == "--exec"
            )
            if wsl:
                command = command[:4] + [
                    "/usr/bin/python3",
                    "-I",
                    "-S",
                    "-c",
                    _WSL_PROCESS_GUARD,
                    str(time.time() + timeout),
                    *command[4:],
                ]
            returncode, stdout, stderr = _run_with_process_tree(
                _resolve_windows_shim(command),
                cwd=workspace,
                env=child_env,
                stdin_text=stdin_text,
                timeout=timeout + 1 if wsl else timeout,
            )
            if wsl and returncode == 124:
                raise subprocess.TimeoutExpired(command, timeout)
            if wsl and returncode == 125:
                raise SubscriptionProcessError("vendor process containment unavailable")
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
    except subprocess.TimeoutExpired:
        raise SubscriptionProcessError(
            f"vendor process timed out after {timeout}s", FailureKind.TIMEOUT
        ) from None
    except FileNotFoundError:
        raise SubscriptionProcessError("vendor command not found") from None
    except OSError:
        raise SubscriptionProcessError("vendor process unavailable") from None
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
    redacted_stdout = redact_text(stdout, secrets)
    return ProcessEvidence(
        returncode=returncode,
        stdout=_cap(redacted_stdout),
        stderr=_redact_and_cap(stderr, secrets),
        duration_seconds=time.monotonic() - started,
        stream=redacted_stdout,
    )
