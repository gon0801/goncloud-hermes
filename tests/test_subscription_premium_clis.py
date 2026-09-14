from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.adapters.claude import ClaudeAdapter  # noqa: E402
from subscriptions.adapters.codex import CodexAdapter  # noqa: E402
from subscriptions.adapters.muse import MuseAdapter  # noqa: E402
from subscriptions.types import (  # noqa: E402
    FailureKind,
    InvocationRequest,
    RouteSpec,
)

CLAUDE_STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","session_id":"sess_c1"}',
        '{"type":"assistant","message":{"model":"claude-sonnet-5","content":'
        '[{"type":"text","text":"partial "}]}}',
        '{"type":"assistant","message":{"model":"claude-sonnet-5","content":'
        '[{"type":"text","text":"answer"}]}}',
        '{"type":"result","subtype":"success","session_id":"sess_c1",'
        '"result":"partial answer"}',
    ]
)

CODEX_JSONL = "\n".join(
    [
        '{"type":"thread.started","thread_id":"thr_9"}',
        '{"type":"item.completed","item":{"type":"agent_message","text":"codex says hi"}}',
        '{"type":"turn.completed"}',
    ]
)

MUSE_JSONL = "\n".join(
    [
        '{"stream":{"kind":"session","id":"muse_s1"},"payload_type":'
        '"run.model.configured","payload":{"model_id":"muse-spark-1.3-contributor-free"}}',
        '{"stream":{"kind":"session","id":"muse_s1"},"payload_type":'
        '"run.terminal.completed","payload":{"terminal":"completed","text":"muse done"}}',
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

    def added_env(self):
        return set(self.kwargs["env"]) - set(os.environ)


def route_for(adapter, model, slot=4, billing="subscription"):
    return RouteSpec(
        slot=slot,
        label=f"opencode/{model}",
        adapter=adapter,
        model=model,
        billing_mode=billing,
    )


def request(
    workspace, prompt="Produce the stage deliverable.", mode="start", changed=()
):
    return InvocationRequest(
        workflow_id="wf-4",
        stage_id="stage-implementer",
        role="implementer",
        prompt=prompt,
        workspace=workspace,
        start_revision="r0",
        current_revision="r0",
        mode=mode,
        changed_files=changed,
    )


class MuseAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def _muse_runner(self):
        class MuseCaptureRunner(FakeCliRunner):
            def __call__(self, command, **kwargs):
                index = command.index("--prompt-file")
                path = Path(command[index + 1].replace("/mnt/c/", "C:/"))
                self.prompt_text = path.read_text(encoding="utf-8")
                self.prompt_path = path
                return super().__call__(command, **kwargs)

        return MuseCaptureRunner(stdout=MUSE_JSONL)

    def test_muse_reuses_bridge_command_and_reports_subscription(self):
        runner = self._muse_runner()

        result = MuseAdapter(runner=runner).invoke(
            request(self.workspace),
            route_for("muse", "muse-spark-1.3-contributor-free"),
        )

        self.assertEqual(
            runner.command[:5],
            ["wsl.exe", "-d", "Ubuntu", "--exec", "/home/goncloud/.local/bin/muse"],
        )
        self.assertIn("Produce the stage deliverable.", runner.prompt_text)
        self.assertFalse(runner.prompt_path.exists())
        self.assertTrue(result.success)
        self.assertEqual(result.text, "muse done")
        self.assertEqual(result.session_id, "muse_s1")
        self.assertEqual(result.adapter, "muse")
        self.assertEqual(result.billing_mode, "subscription")

    def test_muse_failure_is_classified_from_bridge_detail(self):
        runner = FakeCliRunner(stderr="auth session expired", returncode=1)

        result = MuseAdapter(runner=runner).invoke(
            request(self.workspace),
            route_for("muse", "muse-spark-1.3-contributor-free"),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.AUTH)


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_claude_uses_max_session_without_api_budget(self):
        runner = FakeCliRunner(stdout=CLAUDE_STREAM)

        ClaudeAdapter(runner=runner).invoke(
            request(self.workspace), route_for("claude", "claude-sonnet-5", slot=2)
        )

        self.assertEqual(runner.command[:2], ["claude", "--print"])
        self.assertIn("--permission-mode", runner.command)
        self.assertNotIn("--dangerously-skip-permissions", runner.command)
        self.assertNotIn("--max-budget-usd", runner.command)
        self.assertNotIn("--max-tokens-unlimited", runner.command)
        self.assertNotIn("ANTHROPIC_API_KEY", runner.added_env())
        index = runner.command.index("--output-format")
        self.assertEqual(runner.command[index + 1], "stream-json")
        index = runner.command.index("--model")
        self.assertEqual(runner.command[index + 1], "claude-sonnet-5")
        self.assertIn("Produce the stage deliverable.", runner.kwargs["input"])
        self.assertFalse(runner.kwargs["shell"])

    def test_claude_result_parses_stream_and_session(self):
        result = ClaudeAdapter(runner=FakeCliRunner(stdout=CLAUDE_STREAM)).invoke(
            request(self.workspace), route_for("claude", "claude-fable-5-1", slot=6)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "partial answer")
        self.assertEqual(result.session_id, "sess_c1")
        self.assertEqual(result.billing_mode, "subscription")

    def test_claude_error_result_is_failure(self):
        stream = CLAUDE_STREAM.replace(
            '"subtype":"success"', '"subtype":"error_during_execution"'
        )
        result = ClaudeAdapter(runner=FakeCliRunner(stdout=stream)).invoke(
            request(self.workspace), route_for("claude", "claude-sonnet-5", slot=2)
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)

    def test_claude_probe_requires_oauth_subscription_login(self):
        subscribed = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "oauth_token",
                "apiProvider": "firstParty",
            }
        )
        api_key = json.dumps(
            {"loggedIn": True, "authMethod": "api_key", "apiProvider": "anthropic"}
        )

        ready = ClaudeAdapter(runner=FakeCliRunner(stdout=subscribed)).probe(
            route_for("claude", "claude-sonnet-5")
        )
        refused = ClaudeAdapter(runner=FakeCliRunner(stdout=api_key)).probe(
            route_for("claude", "claude-sonnet-5")
        )

        self.assertTrue(ready.ready)
        self.assertEqual(ready.account_class, "claude_max")
        self.assertFalse(refused.ready)
        self.assertEqual(refused.failure, FailureKind.AUTH)
        # A credential file alone never proves the Max tier; live inference does.
        self.assertIn("live", ready.detail)


class CodexAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_codex_uses_chatgpt_session_and_sandbox(self):
        runner = FakeCliRunner(stdout=CODEX_JSONL)

        CodexAdapter(runner=runner).invoke(
            request(self.workspace), route_for("codex", "gpt-6-astra", slot=7)
        )

        self.assertEqual(runner.command[:2], ["codex", "exec"])
        self.assertIn("--json", runner.command)
        sandbox_at = runner.command.index("--sandbox")
        self.assertEqual(
            runner.command[sandbox_at : sandbox_at + 2],
            ["--sandbox", "workspace-write"],
        )
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", runner.command)
        cd_at = runner.command.index("--cd")
        self.assertEqual(runner.command[cd_at + 1], str(self.workspace))
        model_at = runner.command.index("--model")
        self.assertEqual(runner.command[model_at + 1], "gpt-6-astra")
        self.assertIn("Produce the stage deliverable.", runner.kwargs["input"])
        self.assertFalse(runner.kwargs["shell"])
        self.assertEqual(runner.command[2], "-")

    def test_codex_result_parses_thread_and_text(self):
        result = CodexAdapter(runner=FakeCliRunner(stdout=CODEX_JSONL)).invoke(
            request(self.workspace), route_for("codex", "gpt-6-astra")
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "codex says hi")
        self.assertEqual(result.session_id, "thr_9")
        self.assertEqual(result.billing_mode, "subscription")

    def test_codex_probe_requires_chatgpt_login(self):
        chatgpt = FakeCliRunner(stdout="Logged in using ChatGPT\n")
        api_key = FakeCliRunner(stdout="Logged in using an OpenAI API key\n")

        ready = CodexAdapter(runner=chatgpt).probe(route_for("codex", "gpt-6-astra"))
        refused = CodexAdapter(runner=api_key).probe(route_for("codex", "gpt-6-astra"))

        self.assertTrue(ready.ready)
        self.assertEqual(ready.account_class, "chatgpt")
        self.assertFalse(refused.ready)
        self.assertEqual(refused.failure, FailureKind.AUTH)


if __name__ == "__main__":
    unittest.main()
