from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from subscriptions.process import (  # noqa: E402
    _MAX_OUTPUT_CHARS as MAX_OUTPUT_CHARS,
    run_cli,
)
from subscriptions import adapters  # noqa: E402
from subscriptions.adapters import classify_failure  # noqa: E402
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


class SequencedCliRunner:
    """Answers each successive invocation with the next stdout."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        return subprocess.CompletedProcess(
            list(command), 0, stdout=self.outputs.pop(0), stderr=""
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
        self.addCleanup(cleanup_temp_directory, self.tmp)
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

    def test_the_second_go_account_runs_the_same_model_on_its_own_key(self):
        # Owner, 2026-09-27: a second OpenCode Go account, an exact copy of
        # Primary, for the agents and for main. The saved CLI login stays the
        # first account; the second key reaches opencode by env reference.
        runner = FakeCliRunner(stdout=SUCCESS_JSONL)
        with patch.dict(os.environ, {"OPENCODE_GO_2_API_KEY": "sk-go2-fixture"}):
            result = OpenCodeAdapter(runner=runner).invoke(
                request(self.workspace),
                route(1, "opencode_go_2", "muse-spark-1.3-contributor"),
            )

        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "opencode_go_2")
        self.assertEqual(
            runner.command[4:6],
            ["--model", "opencode-go/muse-spark-1.3-contributor"],
        )
        self.assertNotIn("sk-go2-fixture", " ".join(runner.command))
        config = json.loads(runner.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        self.assertEqual(
            config["provider"]["opencode-go"]["options"]["apiKey"],
            "{env:OPENCODE_GO_2_API_KEY}",
        )

    def test_the_first_go_account_keeps_the_saved_login(self):
        runner = FakeCliRunner(stdout=SUCCESS_JSONL)
        OpenCodeAdapter(runner=runner).invoke(
            request(self.workspace),
            route(0, "opencode_go", "muse-spark-1.3-contributor"),
        )
        self.assertNotIn("OPENCODE_CONFIG_CONTENT", runner.kwargs.get("env") or {})

    def test_the_second_go_account_probe_checks_its_key_without_the_cli(self):
        runner = FakeCliRunner(stdout="")
        adapter = OpenCodeAdapter(runner=runner)
        go2 = route(1, "opencode_go_2", "muse-spark-1.3-contributor")
        with patch.dict(os.environ, {"OPENCODE_GO_2_API_KEY": "sk-go2-fixture"}):
            self.assertTrue(adapter.probe(go2).ready)
        with patch.dict(os.environ, {"OPENCODE_GO_2_API_KEY": ""}):
            health = adapter.probe(go2)
        self.assertFalse(health.ready)
        self.assertEqual(health.failure, FailureKind.AUTH)
        self.assertIsNone(runner.command)

    def test_a_rejected_native_call_is_not_retried(self):
        rejected = "\n".join(
            [
                '{"type":"tool_use","sessionID":"ses_a","part":{"type":"tool",'
                '"tool":"read","state":{"status":"error","error":"boom"}}}',
                '{"type":"text","part":{"type":"text","text":"done"}}',
                '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
            ]
        )
        runner = SequencedCliRunner([rejected])

        result = OpenCodeAdapter(runner=runner).invoke(
            request(self.workspace),
            route(0, "opencode_go", "muse-spark-1.3-contributor"),
        )

        self.assertTrue(result.success)
        self.assertEqual(len(runner.calls), 1)

    def test_the_zai_coding_plan_slot_is_served_through_the_cli(self):
        """The subscription the HTTP adapter reached without tools, now with them.

        The provider prefix is what OpenCode lists for that credential, so the
        model arrives with its read, grep and shell tools bound to the
        workspace, like every other CLI-backed slot.
        """
        runner = FakeCliRunner(stdout=SUCCESS_JSONL)
        adapter = OpenCodeAdapter(runner=runner)

        result = adapter.invoke(
            request(self.workspace), route(1, "opencode_zai", "glm-5.3-flash")
        )

        self.assertTrue(result.success)
        self.assertIn("zai-coding-plan/glm-5.3-flash", runner.command)
        self.assertEqual(result.billing_mode, "subscription")

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

    def test_exit_zero_without_a_valid_envelope_is_invalid_terminal(self):
        """The vendor answered, just not in the terminal contract.

        ``invalid_terminal`` is the kind that says the vendor responded and
        delivered nothing usable, without claiming the task itself failed.
        """
        adapter = OpenCodeAdapter(
            runner=FakeCliRunner(
                stdout='{"type":"part","part":{"type":"text","text":"partial"}}'
            )
        )

        result = adapter.invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)

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
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
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
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)

    def test_malformed_exit_zero_cannot_be_reclassified_as_transport(self):
        output = f"{SUCCESS_JSONL}\nprovider.connection_error raw-secret"

        result = OpenCodeAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route(0, "opencode_go", "muse")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
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
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)

    def test_failures_are_classified_without_leaking_output(self):
        cases = [
            (1, "error: unauthorized subscription", FailureKind.AUTH),
            (1, "insufficient balance on plan", FailureKind.QUOTA),
            (1, "rate limit exceeded, retry later", FailureKind.RATE_LIMIT),
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

    def test_bare_status_numbers_in_model_text_are_not_vendor_failures(self):
        # 2026-09-15: classify_failure searched the whole stdout+stderr for a
        # bare "400"/"404"/"429". A model that merely writes such a number — a
        # CSS width, an item count, a "404 page" — was misclassified as an
        # auth/quota/model failure, which then poisoned the router health cache
        # and skipped a healthy account for later stages.
        for text in (
            "The mobile nav uses a 400 breakpoint across 429 items.",
            "Add a friendly 404 page and cap the list at 401 rows.",
            "There are 403 components and 400 tests in this module.",
        ):
            self.assertEqual(
                classify_failure(1, text, ""), FailureKind.TASK_FAILURE, text
            )

    def test_a_disabled_model_is_model_unavailable_not_auth(self):
        # 2026-09-16: opencode returns {"message":"Model is disabled",
        # "statusCode":401} for a model the plan does not enable. The 401 is
        # not an account auth failure — the account authenticated and answered.
        # Reading it as model_unavailable lets the slot advance without marking
        # the whole account dead and skipping it where it uses a good model.
        disabled = (
            '{"type":"error","error":{"message":"Model is disabled",'
            '"statusCode":401,"isRetryable":false}}'
        )
        self.assertEqual(
            classify_failure(1, disabled, ""), FailureKind.MODEL_UNAVAILABLE
        )
        self.assertEqual(
            classify_failure(1, "", "error: model is disabled"),
            FailureKind.MODEL_UNAVAILABLE,
        )

    def test_kimi_weekly_usage_limit_is_quota_not_auth(self):
        # 2026-09-20: kimi wraps a weekly plan limit as provider.auth_error
        # with HTTP 403. A bare 403 in stderr was AUTH and marked the whole
        # account disconnected. The vendor answered about quota; the slot
        # should advance as quota, same as a 429.
        stderr = (
            "error: failed to run prompt: provider.auth_error: 403 "
            "You've reached your weekly (7-day) usage limit. "
            "Your quota will reset when the current 7-day window ends."
        )
        self.assertEqual(classify_failure(1, "", stderr), FailureKind.QUOTA)
        self.assertEqual(
            classify_failure(1, "", "HTTP 403 Forbidden"), FailureKind.AUTH
        )

    def test_a_throttle_is_not_a_spent_plan(self):
        # 2026-10-15: every throttle read as quota and benched the account
        # for 15 minutes. These are the vendors' own words.
        throttles = [
            "429 Output token rate limit exceeded. Please retry after a brief wait.",
            "Too Many Requests",
            "Error: 429",
        ]
        for text in throttles:
            for stdout, stderr in ((text, ""), ("", text)):
                with self.subTest(text=text, stderr=bool(stderr)):
                    self.assertEqual(
                        classify_failure(1, stdout, stderr), FailureKind.RATE_LIMIT
                    )
        self.assertEqual(
            classify_failure(1, "", "server overloaded, try again in a moment"),
            FailureKind.RATE_LIMIT,
        )
        spent = [
            "The usage limit has been reached.",
            "You've reached your weekly (7-day) usage limit.",
            "Monthly request limit reached for this plan.",
            # Both kinds of words: the spent plan wins.
            "429 Too Many Requests: you exceeded your current quota",
            "Rate limit: insufficient balance, top up to continue",
            # No throttle cue: the old reading (advance as quota) stands.
            "request limit exceeded",
        ]
        for text in spent:
            with self.subTest(text=text):
                self.assertEqual(classify_failure(1, "", text), FailureKind.QUOTA)

    def test_a_throttle_that_names_the_plans_reset_is_a_spent_plan(self):
        # A limit that resets hours from now, or is the plan's own, is the
        # window spent in throttle words: advance at once, bench 15 minutes.
        for text in (
            "Rate limit exceeded for your plan. Resets at 3:00 PM.",
            "429 Too Many Requests: limit resets in 5h 12m",
            "Error 429: try again next billing period",
        ):
            for stdout, stderr in ((text, ""), ("", text)):
                with self.subTest(text=text, stderr=bool(stderr)):
                    self.assertEqual(
                        classify_failure(1, stdout, stderr), FailureKind.QUOTA
                    )
        # The reset words alone are not a vendor failure in the model's text.
        self.assertEqual(
            classify_failure(1, "The counter resets at midnight.", ""),
            FailureKind.TASK_FAILURE,
        )
        # A reset the router would wait for is the throttle itself; one past its
        # wait cap is a spent window.
        for text, kind in (
            ("Rate limit exceeded, resets in 30 seconds", FailureKind.RATE_LIMIT),
            ("Rate limit exceeded, resets in 1 minute", FailureKind.RATE_LIMIT),
            ("Rate limit exceeded, resets in 2 min", FailureKind.RATE_LIMIT),
            ("Rate limit exceeded, resets in 3 minutes", FailureKind.QUOTA),
            ("Rate limit exceeded, resets in 2 fortnights", FailureKind.QUOTA),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(1, "", text), kind)

    def test_the_short_throttle_cap_is_the_routers_wait_cap(self):
        from subscriptions import router

        self.assertEqual(
            router._RATE_LIMIT_MAX_WAIT_SECONDS, adapters.SHORT_THROTTLE_SECONDS
        )

    def test_a_throttle_carries_the_vendors_wait_and_nothing_else(self):
        cases = [
            ("Rate limit exceeded. Retry after 30 seconds. raw-secret", 30.0),
            ("429 Too Many Requests; try again in 1.5s raw-secret", 1.5),
            ("rate limited, retry-after: 2 minutes raw-secret", 120.0),
            ("rate limit hit, retry after 500ms raw-secret", 0.5),
            ("Rate limit exceeded, retry after 4 hours raw-secret", 14400.0),
            ("Rate limit exceeded. Try again in 2 hours raw-secret", 7200.0),
            ("rate limit exceeded, try again in 3h raw-secret", 10800.0),
            ("rate limit exceeded, retry-after: 45; raw-secret", 45.0),
            # A unit it cannot read is no wait at all, never seconds.
            ("rate limit exceeded, retry after 3 attempts raw-secret", None),
            (
                "429 Output token rate limit exceeded. "
                "Please retry after a brief wait. raw-secret",
                None,
            ),
        ]
        for stderr, wait in cases:
            with self.subTest(stderr=stderr):
                result = OpenCodeAdapter(
                    runner=FakeCliRunner(stderr=stderr, returncode=1)
                ).invoke(request(self.workspace), route(0, "opencode_go", "muse"))
                self.assertEqual(result.failure, FailureKind.RATE_LIMIT)
                self.assertEqual(result.retry_after_seconds, wait)
                self.assertNotIn("raw-secret", repr(result))
        spent = OpenCodeAdapter(
            runner=FakeCliRunner(
                stderr="quota spent; retry after 3600 seconds", returncode=1
            )
        ).invoke(request(self.workspace), route(0, "opencode_go", "muse"))
        self.assertEqual(
            (spent.failure, spent.retry_after_seconds), (FailureKind.QUOTA, None)
        )

    def test_throttle_words_in_the_models_own_prose_are_not_a_throttle(self):
        # stdout is the model talking: "slow down" or "retry-after" there is a
        # sentence about the task, as a bare 429 there is a count (2026-09-15).
        # The vendor's own channel keeps reading the same words as a throttle.
        for prose in (
            "I will slow down and re-check the failing test before continuing.",
            "Added retry-after handling to the HTTP client as requested.",
        ):
            with self.subTest(prose=prose):
                self.assertEqual(
                    classify_failure(1, prose, ""), FailureKind.TASK_FAILURE
                )
                self.assertEqual(classify_failure(1, "", prose), FailureKind.RATE_LIMIT)

    def test_bare_status_number_signals_in_stderr_but_not_in_stdout(self):
        # stderr is the vendor's error channel: a bare code there is real.
        # stdout is the model's generated text: a bare code there is a width,
        # a row count or a "404 page", never a vendor failure.
        self.assertEqual(classify_failure(1, "", "401 blocked"), FailureKind.AUTH)
        self.assertEqual(
            classify_failure(1, "wrote 401 rows and a 404 page", ""),
            FailureKind.TASK_FAILURE,
        )

    def test_status_codes_in_http_context_still_classify(self):
        cases = [
            ("Error: 401 Unauthorized", FailureKind.AUTH),
            ("request failed with status code 429", FailureKind.RATE_LIMIT),
            ("HTTP 403 Forbidden", FailureKind.AUTH),
            ("server returned 404 not found", FailureKind.MODEL_UNAVAILABLE),
            ("responded 400 bad request", FailureKind.INVALID_REQUEST),
            ('{"statusCode":401,"message":"nope"}', FailureKind.AUTH),
        ]
        for stderr, expected in cases:
            self.assertEqual(classify_failure(1, "", stderr), expected, stderr)

    def test_word_signals_still_classify_without_a_status_number(self):
        self.assertEqual(
            classify_failure(1, "", "unauthorized: token expired"), FailureKind.AUTH
        )
        self.assertEqual(
            classify_failure(1, "", "quota exceeded for this plan"),
            FailureKind.QUOTA,
        )
        self.assertEqual(
            classify_failure(1, "", "model mystery-x is not available"),
            FailureKind.MODEL_UNAVAILABLE,
        )

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

    def test_invalid_terminal_at_exit_zero_keeps_the_stream_evidence(self):
        stream = '{"type":"step_finish","part":{"type":"step-finish","reason":"error"}}'
        result = OpenCodeAdapter(
            runner=FakeCliRunner(stdout=stream, stderr="opencode crashed", returncode=0)
        ).invoke(request(self.workspace), route(0, "opencode_go", "k3"))

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
        self.assertEqual(
            result.text, "opencode_go invocation failed (invalid_terminal)"
        )
        self.assertIn("exit 0", result.detail)
        self.assertIn("opencode crashed", result.detail)

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


class LongStreamTests(unittest.TestCase):
    """A real stage outruns the evidence cap; that is not a task failure."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()

    def long_success_stream(self) -> str:
        filler = [
            '{"type":"tool_use","part":{"type":"tool","tool":"read",'
            f'"callID":"call_{index:05d}","output":"{"x" * 400}"}}}}'
            for index in range(120)
        ]
        lines = [
            '{"type":"session","sessionID":"sess_long"}',
            *filler,
            '{"type":"part","part":{"type":"text","text":"finished the change"}}',
            '{"type":"step_finish","part":{"type":"step-finish","reason":"stop"}}',
        ]
        stream = "\n".join(lines)
        assert len(stream) > MAX_OUTPUT_CHARS, len(stream)
        return stream

    def test_a_stream_longer_than_the_evidence_cap_still_completes(self):
        runner = FakeCliRunner(stdout=self.long_success_stream())

        result = OpenCodeAdapter(runner=runner).invoke(
            request(self.workspace),
            route(0, "opencode_go", "muse-spark-1.3-contributor"),
        )

        self.assertTrue(result.success, result.failure)
        self.assertEqual(result.text, "finished the change")
        self.assertEqual(result.session_id, "sess_long")

    def test_evidence_stays_capped_even_though_parsing_sees_it_all(self):
        stream = self.long_success_stream()

        evidence = run_cli(
            ["opencode"], "", self.workspace, 30, runner=FakeCliRunner(stdout=stream)
        )

        self.assertLessEqual(len(evidence.stdout), MAX_OUTPUT_CHARS + 64)
        self.assertIn("truncated", evidence.stdout)
        self.assertEqual(evidence.stream, stream)


if __name__ == "__main__":
    unittest.main()
