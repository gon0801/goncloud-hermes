from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.process import (  # noqa: E402
    ProcessEvidence,
    SubscriptionProcessError,
    run_cli,
)


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
            'literal $(unsafe) `text`',
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
        self.assertEqual(runner.kwargs["input"], 'literal $(unsafe) `text`')
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
            return subprocess.CompletedProcess(
                command, 0, stdout="done", stderr=""
            )

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


if __name__ == "__main__":
    unittest.main()
