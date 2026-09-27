from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from subscriptions.conversation import (  # noqa: E402
    ConversationRouter,
    render_conversation_prompt,
)
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.types import (  # noqa: E402
    AdapterHealth,
    FailureKind,
    InvocationResult,
    SkippedRoute,
)


SOURCE = json.loads((ROOT / "model-routing.source.json").read_text(encoding="utf-8"))


class ScriptedAdapter:
    probe_is_generative = False

    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls
        self.result = None

    def probe(self, route):
        return AdapterHealth(True, f"{self.name}_subscription")

    def invoke(self, request, route):
        self.calls.append(route.adapter)
        if callable(self.result):
            return self.result(request, route)
        return self.result


def success(request, route):
    return InvocationResult(
        True,
        "terminal answer",
        route.adapter,
        route.model,
        route.billing_mode,
    )


def failure(kind):
    def result(request, route):
        return InvocationResult(
            False,
            f"{route.adapter} invocation failed ({kind.value})",
            route.adapter,
            route.model,
            route.billing_mode,
            failure=kind,
        )

    return result


class ConversationRouterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()
        self.calls: list[str] = []
        names = {
            "opencode_go",
            "claude",
            "muse",
            "grok",
            "opencode_zai",
            "codex",
            "kimi",
            "deepseek",
        }
        self.adapters = {name: ScriptedAdapter(name, self.calls) for name in names}
        self.subscription_router = SubscriptionRouter(
            adapters=self.adapters,
            db_path=self.workspace / "router.db",
            routing_source=SOURCE,
        )
        self.router = ConversationRouter(self.subscription_router)

    def complete(self, messages=None, tools=None, tool_choice=None):
        return self.router.complete(
            "claw",
            messages or [{"role": "user", "content": "Help me."}],
            tools,
            tool_choice,
            self.workspace,
        )

    def test_main_advances_from_opencode_go_to_claude_xhigh_not_kimi(self):
        self.adapters["opencode_go"].result = failure(FailureKind.MODEL_UNAVAILABLE)
        self.adapters["claude"].result = success
        self.adapters["kimi"].result = success

        result = self.complete()

        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["opencode_go", "claude"])
        self.assertEqual(result.adapter, "claude")
        self.assertEqual(result.model, "claude-opus-5")
        self.assertEqual(result.slot, 1)
        self.assertEqual(result.route_label, "anthropic/claude-opus-5 (xhigh)")
        self.assertEqual(result.reasoning_effort, "xhigh")
        self.assertEqual(
            result.skipped_routes,
            (
                SkippedRoute(
                    slot=0,
                    adapter="opencode_go",
                    failure=FailureKind.MODEL_UNAVAILABLE,
                ),
            ),
        )

    def test_prompt_preserves_tool_schema_call_and_result_order(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Read weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                },
            }
        ]
        messages = [
            {"role": "system", "content": "Be exact."},
            {"role": "user", "content": "Weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "weather",
                            "arguments": '{"city":"Lima"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": '{"temperature":22}',
            },
        ]

        prompt = render_conversation_prompt(
            messages, tools, {"type": "function", "function": {"name": "weather"}}
        )

        positions = [
            prompt.index('"role":"system"'),
            prompt.index('"role":"user"'),
            prompt.index('"role":"assistant"'),
            prompt.index('"role":"tool"'),
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('"name":"weather"', prompt)
        self.assertIn(r"\"city\":\"Lima\"", prompt)
        self.assertIn('"tool_call_id":"call_1"', prompt)

    def test_task_and_invalid_request_failures_are_terminal(self):
        for kind in (FailureKind.TASK_FAILURE, FailureKind.INVALID_REQUEST):
            with self.subTest(kind=kind):
                self.calls.clear()
                self.adapters["opencode_go"].result = failure(kind)
                self.adapters["claude"].result = success

                result = self.complete()

                self.assertFalse(result.success)
                self.assertEqual(result.failure, kind)
                self.assertEqual(self.calls, ["opencode_go"])
                self.assertEqual(result.slot, 0)

    def test_cooldown_is_sanitized_skip_evidence_before_next_position(self):
        self.subscription_router.store.set_cooldown("opencode_go", hours=1)
        self.adapters["claude"].result = success

        result = self.complete()

        self.assertTrue(result.success)
        self.assertEqual(self.calls, ["claude"])
        self.assertEqual(
            result.skipped_routes,
            (SkippedRoute(0, "opencode_go", FailureKind.QUOTA),),
        )
        self.assertNotIn("subscription", repr(result.skipped_routes))

    def test_malformed_tool_arguments_are_terminal_before_any_subscription(self):
        result = self.complete(
            messages=[
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_bad",
                            "type": "function",
                            "function": {
                                "name": "weather",
                                "arguments": "{broken",
                            },
                        }
                    ],
                }
            ]
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_REQUEST)
        self.assertEqual(result.adapter, "router")
        self.assertEqual(self.calls, [])

    def test_identical_completion_calls_get_fresh_stage_identities(self):
        self.adapters["opencode_go"].result = success

        first = self.complete()
        second = self.complete()

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(self.calls, ["opencode_go", "opencode_go"])


if __name__ == "__main__":
    unittest.main()
