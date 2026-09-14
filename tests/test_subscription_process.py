from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from subscriptions.process import (  # noqa: E402
    ProcessEvidence,
    SubscriptionProcessError,
    _WindowsProcessJob,
    run_cli,
)
from subscriptions.adapters import SubscriptionAdapter  # noqa: E402
from subscriptions.adapters.muse import MuseAdapter  # noqa: E402
from subscriptions.types import FailureKind, InvocationRequest, RouteSpec  # noqa: E402


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
        self.addCleanup(cleanup_temp_directory, self.tmp)
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

    def test_inherited_credentials_and_token_email_patterns_are_redacted(self):
        sensitive = (
            "opaque-inherited-fixture",
            "sk-synthetic-pattern",
            "fixture@example.test",
        )
        with patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive[0]}):
            evidence = run_cli(
                ["vendor"],
                "",
                self.workspace,
                5,
                runner=FakeRunner(
                    stdout=" ".join(sensitive), stderr=" ".join(sensitive)
                ),
            )
        self.assertTrue(
            all(value not in repr(evidence) for value in sensitive),
            "process evidence exposed a synthetic secret",
        )

    def test_os_launch_failure_has_only_normalized_diagnostics(self):
        def denied(command, **kwargs):
            raise PermissionError("private synthetic launch diagnostic")

        failure = None
        try:
            run_cli(["vendor"], "", self.workspace, 5, runner=denied)
        except Exception as error:
            failure = error
        self.assertTrue(
            isinstance(failure, SubscriptionProcessError),
            "raw operating-system launch exception escaped the process boundary",
        )
        self.assertEqual(str(failure), "vendor process unavailable")
        self.assertTrue(failure.__suppress_context__)

    def test_secret_redaction_control_flags_do_not_corrupt_model_identifiers(self):
        with patch.dict(
            os.environ,
            {
                "GONCLOUD_SECRET_REDACTION_ENABLED": "1",
                "SERVICE_PASSWORD": "opaque-fixture",
            },
            clear=True,
        ):
            evidence = run_cli(
                ["vendor"],
                "",
                self.workspace,
                5,
                runner=FakeRunner(stdout="muse-spark-1.3-contributor opaque-fixture"),
            )
        self.assertEqual(
            evidence.stdout.rstrip(), "muse-spark-1.3-contributor [redacted]"
        )

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

    def test_muse_timeout_uses_containment_and_preserves_failure_kind(self):
        command, pid_files, marker = _tree_command(self.workspace, parent_wait=True)
        request = InvocationRequest(
            "wf", "stage", "scout", "inspect", self.workspace, "r0", "r0"
        )
        route = RouteSpec(
            2, "muse", "muse", "muse-spark-1.3-contributor", "subscription"
        )
        adapter = MuseAdapter()
        adapter.timeout_seconds = 1
        try:
            with patch("muse_bridge.build_muse_command", return_value=command):
                started = time.monotonic()
                result = adapter.invoke(request, route)
            self.assertLess(time.monotonic() - started, 6)
            self.assertFalse(result.success)
            self.assert_tree_stopped(pid_files)
            self.assertEqual(result.failure, FailureKind.TIMEOUT)
        finally:
            _cleanup_tree_pids(pid_files, marker)

    @unittest.skipUnless(
        os.name == "nt" and shutil.which("wsl.exe"), "requires local WSL"
    )
    def test_wsl_deadline_reaps_only_its_descendants_in_separate_sessions(self):
        from muse_bridge import windows_path_to_wsl

        pid_file = self.workspace / "wsl-grandchild.pid"
        marker = f"goncloud-test-{secrets.token_hex(12)}"
        fixture = (
            "import pathlib, subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', sys.argv[2]], start_new_session=True); "
            "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
        )
        command = [
            "wsl.exe",
            "-d",
            "Ubuntu",
            "--exec",
            "python3",
            "-I",
            "-S",
            "-c",
            fixture,
            windows_path_to_wsl(pid_file),
            marker,
        ]
        check = (
            "import pathlib, os, sys; "
            "pid = int(pathlib.Path(sys.argv[1]).read_text()); "
            "p = pathlib.Path('/proc') / str(pid); "
            "alive = p.exists() and ') Z ' not in (p / 'stat').read_text(); "
            "owned = alive and sys.argv[2].encode() in (p / 'cmdline').read_bytes(); "
            "os.kill(pid, 9) if owned and sys.argv[3] == 'cleanup' else None; "
            "raise SystemExit(1 if alive else 0)"
        )
        try:
            # Cold WSL startup is not the behavior under test. Warm only a
            # harmless interpreter before timing the descendant fixture.
            try:
                warmup = subprocess.run(
                    command[:4] + ["/usr/bin/python3", "-I", "-S", "-c", "pass"],
                    capture_output=True,
                    timeout=45,
                )
            except subprocess.TimeoutExpired:
                self.skipTest("isolated WSL interpreter unavailable within 45s")
            if warmup.returncode != 0:
                self.skipTest("Ubuntu isolated Python interpreter unavailable")
            started = time.monotonic()
            try:
                evidence = run_cli(command, "", self.workspace, timeout=5)
            except SubscriptionProcessError as error:
                self.assertIn("timed out after 5s", str(error))
            else:
                self.fail(f"synthetic fixture returned before deadline: {evidence}")
            self.assertLess(time.monotonic() - started, 10)
            self.assertTrue(pid_file.exists(), "fixture did not start")
            status = subprocess.run(
                command[:4]
                + [
                    "python3",
                    "-I",
                    "-S",
                    "-c",
                    check,
                    windows_path_to_wsl(pid_file),
                    marker,
                    "check",
                ],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(status.returncode, 0, "WSL descendant survived timeout")
        finally:
            if pid_file.exists():
                subprocess.run(
                    command[:4]
                    + [
                        "python3",
                        "-I",
                        "-S",
                        "-c",
                        check,
                        windows_path_to_wsl(pid_file),
                        marker,
                        "cleanup",
                    ],
                    capture_output=True,
                    timeout=10,
                )

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

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_job_creation_failure_does_not_run_the_vendor(self):
        side_effect = self.workspace / "vendor-ran.txt"
        started = time.monotonic()
        with patch(
            "subscriptions.process._create_windows_process_job", return_value=None
        ):
            with self.assertRaisesRegex(
                SubscriptionProcessError, "containment unavailable"
            ):
                run_cli(
                    _side_effect_command(side_effect), "", self.workspace, timeout=1
                )
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(side_effect.exists())

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_job_assignment_failure_does_not_run_the_vendor(self):
        side_effect = self.workspace / "vendor-ran.txt"
        failed_job = _FailingJob()
        started = time.monotonic()
        with patch(
            "subscriptions.process._create_windows_process_job", return_value=failed_job
        ):
            with self.assertRaisesRegex(
                SubscriptionProcessError, "containment unavailable"
            ):
                run_cli(
                    _side_effect_command(side_effect), "", self.workspace, timeout=1
                )
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(failed_job.closed)
        self.assertFalse(side_effect.exists())

    def test_real_vendor_receives_semantically_exact_stdin_prompt(self):
        command = [
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())",
        ]
        for prompt in ("short", "x" * 10_000, "Héllo 👋\n日本語"):
            with self.subTest(length=len(prompt)):
                evidence = run_cli(command, prompt, self.workspace, timeout=5)
                self.assertEqual(evidence.stdout, prompt)

    @unittest.skipUnless(os.name == "nt", "Windows executable lookup is Windows-only")
    def test_path_only_executable_preserves_vendor_arguments_cwd_and_environment(self):
        vendor_bin = self.workspace / "bin"
        vendor_bin.mkdir()
        executable = vendor_bin / "goncloud-path-vendor.exe"
        systemroot = os.environ["SYSTEMROOT"]
        shutil.copyfile(Path(systemroot) / "System32" / "cmd.exe", executable)
        marker = self.workspace / "vendor-ran.txt"
        script = self.workspace / "check_vendor.py"
        script.write_text(
            "import os, pathlib, sys\n"
            "assert sys.argv[1:] == ['first', 'last']\n"
            f"assert pathlib.Path.cwd() == pathlib.Path({str(self.workspace)!r})\n"
            f"assert os.environ['PATH'] == {str(vendor_bin)!r}\n"
            "assert os.environ['VENDOR_TOKEN'] == 'sentinel-only-for-test'\n"
            f"pathlib.Path({str(marker)!r}).write_text('ran')\n"
            "print('vendor-ok')\n",
            encoding="utf-8",
        )
        original_assign = _WindowsProcessJob.assign

        def assign_after_gate_check(job, process):
            time.sleep(0.2)
            self.assertFalse(marker.exists())
            original_assign(job, process)

        with (
            patch.dict(
                os.environ,
                {"SYSTEMROOT": systemroot, "PATH": str(vendor_bin)},
                clear=True,
            ),
            patch.object(_WindowsProcessJob, "assign", assign_after_gate_check),
        ):
            evidence = run_cli(
                [
                    executable.name,
                    "/d",
                    "/c",
                    sys.executable,
                    str(script),
                    "first",
                    "last",
                ],
                "",
                self.workspace,
                timeout=5,
                env_overlay={"VENDOR_TOKEN": "sentinel-only-for-test"},
            )
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertEqual(evidence.stdout.strip(), "vendor-ok")
        self.assertTrue(marker.exists())

    @unittest.skipUnless(
        os.name == "nt", "Windows launch configuration is Windows-only"
    )
    def test_launch_configuration_never_creates_a_file_or_enters_bootstrap_environment(
        self,
    ):
        original_mkstemp = tempfile.mkstemp
        original_popen = subprocess.Popen
        created_files = []
        bootstrap_invocations = []
        sentinel = "sentinel-only-for-test"

        def track_temporary_file(*args, **kwargs):
            descriptor, path = original_mkstemp(*args, **kwargs)
            created_files.append(path)
            return descriptor, path

        def track_bootstrap(command, **kwargs):
            bootstrap_invocations.append((command, kwargs["env"]))
            return original_popen(command, **kwargs)

        command = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            "import os, sys; "
            "assert os.environ['VENDOR_TOKEN'] == 'sentinel-only-for-test'; "
            "assert sys.stdin.read() == 'vendor prompt'; print('private-ok')",
        ]
        with (
            patch.dict(
                os.environ, {"SYSTEMROOT": os.environ["SYSTEMROOT"]}, clear=True
            ),
            patch("subscriptions.process.tempfile.mkstemp", track_temporary_file),
            patch("subscriptions.process.subprocess.Popen", track_bootstrap),
        ):
            evidence = run_cli(
                command,
                "vendor prompt",
                self.workspace,
                timeout=5,
                env_overlay={"VENDOR_TOKEN": sentinel},
            )
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertEqual(evidence.stdout.strip(), "private-ok")
        self.assertEqual(created_files, [], "launch configuration was exposed on disk")
        self.assertEqual(len(bootstrap_invocations), 1)
        bootstrap_command, bootstrap_env = bootstrap_invocations[0]
        self.assertNotIn(sentinel, repr(bootstrap_command))
        self.assertNotIn(sentinel, repr(bootstrap_env))

    @unittest.skipUnless(os.name == "nt", "Windows command shims are Windows-only")
    def test_cmd_shim_resolves_system_shell_when_path_contains_only_vendor_directory(
        self,
    ):
        (self.workspace / "vendor.cmd").write_text("@echo shim-ok\n", encoding="utf-8")
        with patch.dict(
            os.environ,
            {"SYSTEMROOT": os.environ["SYSTEMROOT"], "PATH": str(self.workspace)},
            clear=True,
        ):
            evidence = run_cli(["vendor.cmd"], "", self.workspace, timeout=5)
        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertEqual(evidence.stdout.strip(), "shim-ok")

    @unittest.skipUnless(os.name == "nt", "Windows command shims are Windows-only")
    def test_shim_ignores_cmd_exe_shadowed_in_cwd_or_path(self):
        systemroot = os.environ["SYSTEMROOT"]
        trusted_shell = Path(systemroot) / "System32" / "cmd.exe"
        original_dumps = json.dumps
        for location in ("cwd", "path"):
            with self.subTest(location=location):
                root = self.workspace / location
                root.mkdir()
                vendor_bin = root / "bin"
                vendor_bin.mkdir()
                shadow_dir = root if location == "cwd" else vendor_bin
                malicious_marker = root / "malicious-shell-ran.txt"
                # Inert fixture: the config guard below must reject this path
                # before Popen, even while reproducing the vulnerable lookup.
                (shadow_dir / "cmd.exe").write_text(
                    f"@echo untrusted>{malicious_marker}\n", encoding="utf-8"
                )
                vendor_marker = root / "vendor-ran.txt"
                (vendor_bin / "vendor.cmd").write_text(
                    "@echo off\n"
                    'if not "%VENDOR_TOKEN%"=="sentinel-only-for-test" exit /b 17\n'
                    f'echo ran>"{vendor_marker}"\n'
                    "echo shim-ok\n",
                    encoding="utf-8",
                )
                configurations = []

                def guard_launch_config(config, *args, **kwargs):
                    if not isinstance(config, dict):
                        return original_dumps(config, *args, **kwargs)
                    configurations.append(config)
                    self.assertEqual(Path(config["executable"]), trusted_shell)
                    self.assertEqual(Path(config["command"][0]), trusted_shell)
                    return original_dumps(config, *args, **kwargs)

                with (
                    chdir(root),
                    patch.dict(
                        os.environ,
                        {"SYSTEMROOT": systemroot, "PATH": str(vendor_bin)},
                        clear=True,
                    ),
                    patch("subscriptions.process.json.dumps", guard_launch_config),
                ):
                    evidence = run_cli(
                        ["vendor.cmd"],
                        "",
                        root,
                        timeout=5,
                        env_overlay={"VENDOR_TOKEN": "sentinel-only-for-test"},
                    )
                self.assertEqual(len(configurations), 1)
                self.assertEqual(evidence.returncode, 0, evidence.stderr)
                self.assertEqual(evidence.stdout.strip(), "shim-ok")
                self.assertTrue(vendor_marker.is_file())
                self.assertFalse(malicious_marker.exists())

    @unittest.skipUnless(os.name == "nt", "Windows command shims are Windows-only")
    def test_shim_fails_closed_without_an_absolute_system_shell_file(self):
        trusted_shell_dir = Path(os.environ["SYSTEMROOT"]) / "System32"
        missing_shell_root = self.workspace / "sentinel-systemroot"
        missing_shell_root.mkdir()
        directory_shell_root = self.workspace / "directory-systemroot"
        (directory_shell_root / "System32" / "cmd.exe").mkdir(parents=True)
        (self.workspace / "vendor.cmd").write_text("@echo shim-ok\n", encoding="utf-8")
        for systemroot in (
            None,
            "",
            "relative-systemroot",
            str(missing_shell_root),
            str(directory_shell_root),
        ):
            with self.subTest(systemroot=systemroot):
                environment = {
                    "PATH": os.pathsep.join(
                        (str(self.workspace), str(trusted_shell_dir))
                    )
                }
                if systemroot is not None:
                    environment["SYSTEMROOT"] = systemroot
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch(
                        "subscriptions.process.subprocess.Popen",
                        side_effect=AssertionError("unsafe shell launch attempted"),
                    ),
                ):
                    with self.assertRaisesRegex(
                        SubscriptionProcessError, "system shell unavailable"
                    ) as caught:
                        run_cli(
                            ["vendor.cmd"],
                            "private prompt",
                            self.workspace,
                            timeout=5,
                            env_overlay={"VENDOR_TOKEN": "sentinel-only-for-test"},
                        )
                self.assertNotIn("sentinel", str(caught.exception))
                self.assertNotIn("private prompt", str(caught.exception))
                self.assertNotIn(str(self.workspace), str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "Windows Job Objects are Windows-only")
    def test_bootstrap_cannot_import_workspace_or_pythonpath_before_assignment(self):
        bootstrap_marker = self.workspace / "bootstrap-ran.txt"
        pythonpath = self.workspace / "pythonpath"
        pythonpath.mkdir()
        (self.workspace / "subprocess.py").write_text(
            f"from pathlib import Path; Path(r'{bootstrap_marker}').write_text('cwd')",
            encoding="utf-8",
        )
        (pythonpath / "sitecustomize.py").write_text(
            f"from pathlib import Path; Path(r'{bootstrap_marker}').write_text('path')",
            encoding="utf-8",
        )
        failed_job = _FailingJob(delay_seconds=0.2)
        with patch(
            "subscriptions.process._create_windows_process_job", return_value=failed_job
        ):
            with self.assertRaisesRegex(
                SubscriptionProcessError, "containment unavailable"
            ):
                run_cli(
                    _side_effect_command(self.workspace / "vendor-ran.txt"),
                    "",
                    self.workspace,
                    timeout=1,
                    env_overlay={"PYTHONPATH": str(pythonpath)},
                )
        self.assertFalse(bootstrap_marker.exists())

    def assert_tree_stopped(self, pid_files: tuple[Path, Path]) -> None:
        pids = _required_pids(pid_files)
        self.assertEqual(len(pids), 2)
        self.assertEqual(len(set(pids)), 2)
        for pid in pids:
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


def _required_pids(pid_files: tuple[Path, Path]) -> tuple[int, int]:
    parent_pid, grandchild_pid = (
        int(pid_file.read_text(encoding="utf-8")) for pid_file in pid_files
    )
    return parent_pid, grandchild_pid


class _FailingJob:
    def __init__(self, delay_seconds: float = 0) -> None:
        self.closed = False
        self.delay_seconds = delay_seconds

    def assign(self, process) -> None:
        time.sleep(self.delay_seconds)
        raise OSError("job assignment failed")

    def close(self) -> None:
        self.closed = True


def _side_effect_command(path: Path) -> list[str]:
    return [
        sys.executable,
        "-c",
        "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')",
        str(path),
    ]


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
