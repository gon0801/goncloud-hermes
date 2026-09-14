from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.adapters.opencode import (  # noqa: E402
    OpenCodeAdapter,
    parse_opencode_jsonl,
)
from subscriptions.types import (  # noqa: E402
    FailureKind,
    InvocationRequest,
    RouteSpec,
)

SUCCESS_JSONL = "\n".join(
    [
        '{"type":"session","sessionID":"sess_123"}',
        '{"type":"part","part":{"type":"text","text":"step one "}}',
        '{"type":"part","part":{"type":"text","text":"step two"}}',
        '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
    ]
)


class FakeCliRunner:
    def __init__(self, stdout="", stderr="", returncode=0):
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


def route(slot, adapter, model, billing="subscription"):
    return RouteSpec(
        slot=slot,
        label=f"historical/{model}",
        adapter=adapter,
        model=model,
        billing_mode=billing,
    )


def request(workspace, prompt="Do the bounded task.", mode="start", changed=()):
    return InvocationRequest(
        workflow_id="wf-1",
        stage_id="stage-7",
        role="implementer",
        prompt=prompt,
        workspace=workspace,
        start_revision="r0",
        current_revision="r1",
        mode=mode,
        changed_files=changed,
    )


class OpenCodeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_go_and_zen_use_exact_subscription_models(self):
        runner = FakeCliRunner(stdout=SUCCESS_JSONL)
        adapter = OpenCodeAdapter(runner=runner)

        adapter.invoke(
            request(self.workspace),
            route(0, "opencode_go", "muse-spark-1.3-contributor"),
        )

        self.assertEqual(runner.command[:4], ["opencode", "run", "--format", "json"])
        self.assertEqual(
            runner.command[4:6],
            ["--model", "opencode-go/muse-spark-1.3-contributor"],
        )
        self.assertEqual(runner.command[6:8], ["--dir", str(self.workspace)])
        self.assertFalse(
            any("TOKEN" in value or "KEY" in value for value in runner.command)
        )
        # The assignment travels by stdin, never as an argument.
        self.assertIn("Do the bounded task.", runner.kwargs["input"])
        self.assertFalse(runner.kwargs["shell"])

        zen = OpenCodeAdapter(runner=runner)
        zen.invoke(
            request(self.workspace),
            route(1, "opencode_zen", "muse-spark-1.3-contributor-free"),
        )
        self.assertEqual(
            runner.command[4:6],
            ["--model", "opencode/muse-spark-1.3-contributor-free"],
        )

    def test_invoke_parses_text_session_and_reports_subscription(self):
        adapter = OpenCodeAdapter(runner=FakeCliRunner(stdout=SUCCESS_JSONL))

        result = adapter.invoke(
            request(self.workspace, prompt="Inspect the repository."),
            route(0, "opencode_go", "deepseek-v4.1-flash"),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "step one step two")
        self.assertEqual(result.session_id, "sess_123")
        self.assertEqual(result.adapter, "opencode_go")
        self.assertEqual(result.model, "deepseek-v4.1-flash")
        self.assertEqual(result.billing_mode, "subscription")
        self.assertIsNone(result.failure)

    def test_exit_zero_without_terminal_event_is_failure(self):
        adapter = OpenCodeAdapter(
            runner=FakeCliRunner(
                stdout='{"type":"text","part":{"type":"text","text":"partial"}}'
            )
        )

        result = adapter.invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)

    def test_parser_exposes_valid_terminal_only_after_step_finish(self):
        parsed = parse_opencode_jsonl(SUCCESS_JSONL)
        partial = parse_opencode_jsonl(
            '{"type":"text","part":{"type":"text","text":"partial"}}'
        )

        self.assertTrue(parsed["terminal_valid"])
        self.assertFalse(partial["terminal_valid"])

    def test_malformed_jsonl_after_terminal_invalidates_parser_and_invoke(self):
        output = f"{SUCCESS_JSONL}\nnot-json-secret"
        parsed = parse_opencode_jsonl(output)
        result = OpenCodeAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(parsed["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("not-json-secret", result.text)

    def test_step_finish_requires_stop_reason_for_terminal_evidence(self):
        text = '{"type":"text","part":{"type":"text","text":"answer"}}'
        stopped = parse_opencode_jsonl(
            f'{text}\n{{"type":"step_finish","part":{{"type":"step-finish","reason":"stop"}}}}'
        )
        truncated = f'{text}\n{{"type":"step_finish","part":{{"type":"step-finish","reason":"length"}}}}'
        result = OpenCodeAdapter(runner=FakeCliRunner(stdout=truncated)).invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertTrue(stopped["terminal_valid"])
        self.assertFalse(parse_opencode_jsonl(truncated)["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)

    def test_malformed_exit_zero_cannot_be_reclassified_as_transport(self):
        output = f"{SUCCESS_JSONL}\nprovider.connection_error raw-secret"

        result = OpenCodeAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("raw-secret", repr(result))

    def test_embedded_jsonl_error_cannot_approve_terminal_event(self):
        adapter = OpenCodeAdapter(
            runner=FakeCliRunner(
                stdout="\n".join(
                    [
                        '{"type":"text","part":{"type":"text","text":"partial"}}',
                        '{"type":"error","error":{"message":"worker failed"}}',
                        '{"type":"step_finish","part":{"type":"step-finish"}}',
                    ]
                )
            )
        )

        result = adapter.invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)

    def test_failures_are_classified_without_leaking_output(self):
        cases = [
            (1, "error: unauthorized subscription", FailureKind.AUTH),
            (1, "insufficient balance on plan", FailureKind.QUOTA),
            (1, "rate limit exceeded, retry later", FailureKind.QUOTA),
            (1, "model mystery-x is not available", FailureKind.MODEL_UNAVAILABLE),
            (2, "invalid request payload", FailureKind.INVALID_REQUEST),
            (1, "agent could not finish", FailureKind.TASK_FAILURE),
            (1, "provider.connection_error", FailureKind.TRANSPORT),
            (1, "connection reset by peer", FailureKind.TRANSPORT),
            (1, "disconnected before response", FailureKind.TRANSPORT),
            (1, "DNS lookup failed", FailureKind.TRANSPORT),
            (1, "connection refused", FailureKind.TRANSPORT),
            (1, "user disconnected from the session", FailureKind.TASK_FAILURE),
            # Tool listings legitimately contain "auth"; that is not an
            # authentication failure (grok streams its available_commands).
            (
                1,
                "available_commands tools: saikit-auth-security, code-review",
                FailureKind.TASK_FAILURE,
            ),
        ]
        for returncode, stderr, expected in cases:
            adapter = OpenCodeAdapter(
                runner=FakeCliRunner(stderr=stderr, returncode=returncode)
            )
            result = adapter.invoke(
                request(self.workspace), route(1, "opencode_zen", "k3")
            )
            self.assertEqual(result.failure, expected, stderr)
            self.assertFalse(result.success)
            self.assertNotIn(stderr, result.text)

    def test_timeout_is_returned_as_timeout_failure(self):
        def timeout_runner(command, **kwargs):
            raise subprocess.TimeoutExpired(cmd=command, timeout=5)

        adapter = OpenCodeAdapter(runner=timeout_runner)

        result = adapter.invoke(request(self.workspace), route(0, "opencode_go", "k3"))

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TIMEOUT)

    def test_launch_errors_surface_as_task_failure(self):
        def missing_runner(command, **kwargs):
            raise FileNotFoundError(command[0])

        adapter = OpenCodeAdapter(runner=missing_runner)

        result = adapter.invoke(request(self.workspace), route(0, "opencode_go", "k3"))

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(result.text, "opencode_go invocation failed (task_failure)")

    def test_probe_reports_only_credential_classes_it_can_see(self):
        auth_listing = "Credentials\n- OpenCode Zen api\n- Z.AI Coding Plan api\n"

        def auth_runner(command, **kwargs):
            if command[:2] == ["opencode", "auth"]:
                return subprocess.CompletedProcess(
                    command, 0, stdout=auth_listing, stderr=""
                )
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        adapter = OpenCodeAdapter(runner=auth_runner)

        zen_health = adapter.probe(
            route(1, "opencode_zen", "muse-spark-1.3-contributor-free")
        )
        go_health = adapter.probe(route(0, "opencode_go", "muse-spark-1.3-contributor"))

        self.assertTrue(zen_health.ready)
        self.assertEqual(zen_health.account_class, "opencode_zen")
        self.assertFalse(go_health.ready)
        self.assertEqual(go_health.failure, FailureKind.AUTH)

    def test_probe_never_uses_generative_commands(self):
        runner = FakeCliRunner(stdout="Credentials\n- OpenCode Go api\n")

        OpenCodeAdapter(runner=runner).probe(
            route(0, "opencode_go", "muse-spark-1.3-contributor")
        )

        self.assertEqual(runner.command[:3], ["opencode", "auth", "list"])


if __name__ == "__main__":
    unittest.main()
