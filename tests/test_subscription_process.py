from __future__ import annotations

import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.process import (  # noqa: E402
    ProcessEvidence,
    SubscriptionProcessError,
    run_cli,
)
from subscriptions.adapters import SubscriptionAdapter  # noqa: E402


class FakeRunner:
    """Records the exact invocation and never touches a real process."""

    def __init__(self, stdout="ok", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.command = None
        self.kwargs = None

    def __call__(self, command, **kwargs):
        self.command = list(command)
        self.kwargs = kwargs
        return subprocess.CompletedProcess(
            self.command, self.returncode, stdout=self.stdout, stderr=self.stderr
        )

    @property
    def shell(self):
        return bool(self.kwargs.get("shell"))

    @property
    def prompt_path(self) -> Path:
        index = self.command.index("--prompt-file")
        return Path(self.command[index + 1])


class TimeoutRunner(FakeRunner):
    def __call__(self, command, **kwargs):
        super().__call__(command, **kwargs)
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs.get("timeout"))


class SafeProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_prompt_and_secret_never_enter_command_or_result(self):
        runner = FakeRunner(stdout="ran with super-secret inside")

        evidence = run_cli(
            ["vendor", "exec", "-"],
            "literal $(unsafe) `text`",
            self.workspace,
            timeout=30,
            env_overlay={"VENDOR_TOKEN": "super-secret"},
            runner=runner,
        )

        self.assertEqual(evidence.returncode, 0)
        joined = " ".join(runner.command)
        self.assertNotIn("super-secret", joined)
        self.assertNotIn("literal $(unsafe) `text`", joined)
        self.assertFalse(runner.shell)
        self.assertEqual(runner.kwargs["input"], "literal $(unsafe) `text`")
        self.assertEqual(runner.kwargs["cwd"], self.workspace)
        self.assertEqual(
            runner.kwargs["env"]["VENDOR_TOKEN"],
            "super-secret",
        )
        # The redacted evidence keeps the secret out of every field and repr.
        self.assertNotIn("super-secret", evidence.stdout)
        self.assertNotIn("super-secret", evidence.stderr)
        self.assertNotIn("super-secret", repr(evidence))
        self.assertIsInstance(evidence, ProcessEvidence)

    def test_temp_prompt_is_deleted_after_timeout(self):
        runner = TimeoutRunner()

        with self.assertRaises(SubscriptionProcessError):
            run_cli(
                ["vendor", "--prompt-file", "{prompt_file}"],
                "task",
                self.workspace,
                timeout=1,
                prompt_transport="tempfile",
                runner=runner,
            )

        self.assertFalse(runner.prompt_path.exists())
        self.assertNotIn("task", " ".join(runner.command))

    def test_temp_prompt_carries_content_and_is_deleted_on_success(self):
        captured = {}

        def capture_runner(command, **kwargs):
            captured["command"] = list(command)
            captured["text"] = runner_path(command).read_text(encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="done", stderr="")

        def runner_path(command):
            index = command.index("--prompt-file")
            return Path(command[index + 1])

        evidence = run_cli(
            ["vendor", "--prompt-file", "{prompt_file}"],
            'whole $(prompt) "with" quotes',
            self.workspace,
            timeout=5,
            prompt_transport="tempfile",
            runner=capture_runner,
        )

        self.assertEqual(evidence.stdout, "done")
        self.assertEqual(captured["text"], 'whole $(prompt) "with" quotes')
        self.assertFalse(runner_path(captured["command"]).exists())
        self.assertNotIn("whole $(prompt)", " ".join(captured["command"]))

    def test_workspace_must_be_an_existing_absolute_directory(self):
        with self.assertRaisesRegex(SubscriptionProcessError, "absolute"):
            run_cli(
                ["vendor"],
                "task",
                Path("relative/nowhere"),
                timeout=5,
                runner=FakeRunner(),
            )
        with self.assertRaisesRegex(SubscriptionProcessError, "existing"):
            run_cli(
                ["vendor"],
                "task",
                self.workspace / "missing",
                timeout=5,
                runner=FakeRunner(),
            )

    def test_output_is_capped_to_bound_evidence_size(self):
        runner = FakeRunner(stdout="x" * 200_000, stderr="y" * 200_000)

        evidence = run_cli(["vendor"], "task", self.workspace, timeout=5, runner=runner)

        self.assertLess(len(evidence.stdout), 50_000)
        self.assertLess(len(evidence.stderr), 50_000)

    def test_missing_command_is_reported_without_raising_oserror(self):
        def missing_runner(command, **kwargs):
            raise FileNotFoundError(command[0])

        with self.assertRaisesRegex(SubscriptionProcessError, "not found"):
            run_cli(
                ["absent-vendor"],
                "task",
                self.workspace,
                timeout=5,
                runner=missing_runner,
            )

    def test_deadline_must_be_a_positive_integer(self):
        with self.assertRaisesRegex(SubscriptionProcessError, "positive integer"):
            run_cli(
                ["vendor"],
                "task",
                self.workspace,
                timeout=1.5,
                runner=FakeRunner(),
            )

    def test_adapter_deadlines_cannot_exceed_process_limits(self):
        class OverdueAdapter(SubscriptionAdapter):
            timeout_seconds = 900
            probe_timeout_seconds = 120

        adapter = OverdueAdapter()

        self.assertEqual(adapter.timeout_seconds, 300)
        self.assertEqual(adapter.probe_timeout_seconds, 30)
        adapter.timeout_seconds = 7
        adapter.probe_timeout_seconds = 8
        self.assertEqual(adapter.timeout_seconds, 7)
        self.assertEqual(adapter.probe_timeout_seconds, 8)
        adapter.timeout_seconds = 900
        adapter.probe_timeout_seconds = 120
        self.assertEqual(adapter.timeout_seconds, 300)
        self.assertEqual(adapter.probe_timeout_seconds, 30)

    def test_timeout_terminates_the_entire_child_process_tree(self):
        """A timed-out CLI cannot leave its direct child running in the background."""
        command, pid_files, marker = _tree_command(self.workspace, parent_wait=True)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(SubscriptionProcessError, "timed out after 1s"):
                run_cli(command, "", self.workspace, timeout=1)
            self.assertLess(time.monotonic() - started, 6)
            self.assert_tree_stopped(pid_files)
        finally:
            _cleanup_tree_pids(pid_files, marker)

    def test_timeout_contains_descendant_after_parent_exits_early(self):
        """Inherited output handles cannot keep an escaped grandchild alive."""
        command, pid_files, marker = _tree_command(self.workspace, parent_wait=False)
        try:
            started = time.monotonic()
            with self.assertRaisesRegex(SubscriptionProcessError, "timed out after 1s"):
                run_cli(command, "", self.workspace, timeout=1)
            self.assertLess(time.monotonic() - started, 6)
            self.assert_tree_stopped(pid_files)
        finally:
            _cleanup_tree_pids(pid_files, marker)

    @unittest.skipUnless(os.name == "nt", "taskkill is Windows-only")
    def test_taskkill_failure_does_not_replace_the_timeout(self):
        """A taskkill launch error still returns the normalized timeout outcome."""
        for cleanup_error in (PermissionError, FileNotFoundError):
            with self.subTest(cleanup_error=cleanup_error.__name__):
                command, pid_files, marker = _tree_command(
                    self.workspace, parent_wait=False
                )
                try:
                    with patch(
                        "subscriptions.process.subprocess.run",
                        side_effect=cleanup_error("taskkill unavailable"),
                    ) as taskkill:
                        with self.assertRaisesRegex(
                            SubscriptionProcessError, "timed out after 1s"
                        ):
                            run_cli(
                                command, "", self.workspace, timeout=1, runner=taskkill
                            )
                    self.assert_tree_stopped(pid_files)
                finally:
                    _cleanup_tree_pids(pid_files, marker)

    def assert_tree_stopped(self, pid_files: tuple[Path, Path]) -> None:
        for pid in _pids_from_files(pid_files):
            self.assertFalse(
                _pid_is_alive(pid), f"child process {pid} survived timeout"
            )


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        probe = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
        return f'"{pid}"' in probe.stdout
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.is_file() and ") Z " in stat_path.read_text(encoding="utf-8"):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tree_command(
    workspace: Path, *, parent_wait: bool
) -> tuple[list[str], tuple[Path, Path], str]:
    parent_pid_file = workspace / "parent.pid"
    grandchild_pid_file = workspace / "grandchild.pid"
    marker = f"goncloud-test-{secrets.token_hex(12)}"
    parent_tail = "time.sleep(60)" if parent_wait else ""
    command = [
        sys.executable,
        "-c",
        (
            "import os, pathlib, subprocess, sys, time; "
            "parent = pathlib.Path(sys.argv[1]); "
            "grandchild = pathlib.Path(sys.argv[2]); "
            "parent.write_text(str(os.getpid()), encoding='utf-8'); "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(8)', sys.argv[3]]); "
            "grandchild.write_text(str(child.pid), encoding='utf-8'); "
            f"{parent_tail}"
        ),
        str(parent_pid_file),
        str(grandchild_pid_file),
        marker,
    ]
    return command, (parent_pid_file, grandchild_pid_file), marker


def _pids_from_files(pid_files: tuple[Path, Path]) -> tuple[int, ...]:
    found = []
    for pid_file in pid_files:
        try:
            found.append(int(pid_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return tuple(found)


def _cleanup_tree_pids(pid_files: tuple[Path, Path], marker: str) -> None:
    for pid in _pids_from_files(pid_files):
        _terminate_test_pid(pid, marker)


def _terminate_test_pid(pid: int, marker: str) -> None:
    """Best-effort cleanup scoped to a PID created by this test only."""
    if not _pid_is_alive(pid) or not _is_our_test_process(pid, marker):
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
    else:
        os.kill(pid, 9)


def _is_our_test_process(pid: int, marker: str) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"$process = Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}'; "
                    f"if ($process -and $process.CommandLine -like '*{marker}*') {{ exit 0 }}; "
                    "exit 1"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            shell=False,
        )
        return result.returncode == 0
    proc_cmdline = Path(f"/proc/{pid}/cmdline")
    try:
        return marker.encode("utf-8") in proc_cmdline.read_bytes()
    except OSError:
        return False


if __name__ == "__main__":
    unittest.main()
