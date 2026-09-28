from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

PROVIDER_DIR = ROOT / "plugins" / "model-providers" / "goncloud-subscriptions"


def load_provider_module():
    name = "_goncloud_subscription_provider_test"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(
        name,
        PROVIDER_DIR / "__init__.py",
        submodule_search_locations=[str(PROVIDER_DIR)],
    )
    if spec is None or spec.loader is None:
        raise AssertionError("provider module could not be loaded")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeConversationRouter:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    def complete(self, profile, messages, tools, tool_choice, workspace):
        self.calls.append(
            {
                "profile": profile,
                "messages": messages,
                "tools": tools,
                "tool_choice": tool_choice,
                "workspace": workspace,
            }
        )
        return SimpleNamespace(
            success=True,
            text=self.text,
            adapter="claude",
            model="claude-opus-5",
            billing_mode="subscription",
            session_id="claude-session",
            failure=None,
            slot=1,
            route_label="anthropic/claude-opus-5 (xhigh)",
            reasoning_effort="xhigh",
            skipped_routes=(),
        )


class SubscriptionProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_provider_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()

    def test_profile_is_local_subscription_provider_without_api_credentials(self):
        profile = self.module.goncloud_subscriptions

        self.assertEqual(profile.name, "goncloud-subscriptions")
        self.assertEqual(profile.auth_type, "external_process")
        self.assertEqual(profile.env_vars, ())
        self.assertEqual(profile.base_url, "acp://goncloud-subscriptions")
        self.assertEqual(profile.process_command, "hermes")
        self.assertEqual(profile.process_args, ())
        self.assertEqual(profile.fallback_models, ("main",))

    def test_nonstreaming_completion_exposes_terminal_route_metadata(self):
        conversation = FakeConversationRouter("final answer")
        client = self.module.goncloud_subscriptions.create_client(
            conversation_router=conversation,
            workspace=self.workspace,
        )

        completion = client.chat.completions.create(
            model="main",
            messages=[{"role": "user", "content": "hello"}],
        )

        self.assertEqual(completion.choices[0].message.content, "final answer")
        self.assertEqual(completion.choices[0].finish_reason, "stop")
        self.assertEqual(completion.model, "claude-opus-5")
        self.assertEqual(completion.hermes_provider, "goncloud-subscriptions")
        self.assertEqual(completion.hermes_requested_model, "main")
        self.assertEqual(completion.hermes_route_slot, 1)
        self.assertEqual(completion.hermes_reasoning_effort, "xhigh")
        self.assertEqual(completion.hermes_billing_mode, "subscription")
        self.assertEqual(conversation.calls[0]["profile"], "main")

    def test_tool_call_and_streaming_shapes_are_equivalent(self):
        tool_block = {
            "id": "call_weather",
            "type": "function",
            "function": {
                "name": "weather",
                "arguments": json.dumps({"city": "Lima"}, separators=(",", ":")),
            },
        }
        text = (
            "I will check.\n<tool_call>"
            + json.dumps(tool_block, separators=(",", ":"))
            + "</tool_call>"
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Read weather",
                    "parameters": {"type": "object"},
                },
            }
        ]
        conversation = FakeConversationRouter(text)
        client = self.module.goncloud_subscriptions.create_client(
            conversation_router=conversation,
            workspace=self.workspace,
        )
        kwargs = {
            "model": "main",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": tools,
            "tool_choice": "auto",
        }

        completion = client.chat.completions.create(**kwargs)
        chunks = client.chat.completions.create(**kwargs, stream=True)

        message = completion.choices[0].message
        delta = chunks[0].choices[0].delta
        self.assertEqual(completion.choices[0].finish_reason, "tool_calls")
        self.assertEqual(chunks[0].choices[0].finish_reason, "tool_calls")
        self.assertEqual(message.content, "I will check.")
        self.assertEqual(delta.content, message.content)
        self.assertEqual(delta.tool_calls[0].function.name, "weather")
        self.assertEqual(
            delta.tool_calls[0].function.arguments,
            message.tool_calls[0].function.arguments,
        )
        self.assertEqual(chunks[0].model, "claude-opus-5")
        self.assertEqual(chunks.hermes_provider, "goncloud-subscriptions")
        self.assertIn(
            "Available tools (OpenAI function schema)",
            conversation.calls[0]["messages"][0]["content"],
        )

    def test_tool_schemas_travel_once(self):
        """Live 2026-09-27: every turn carried the 38 schemas twice (developer
        section plus TOOLS=), ~62k of a 203k-char prompt."""
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "Read weather",
                    "parameters": {"type": "object"},
                },
            }
        ]
        conversation = FakeConversationRouter("ok")
        client = self.module.goncloud_subscriptions.create_client(
            conversation_router=conversation,
            workspace=self.workspace,
        )

        client.chat.completions.create(
            model="main",
            messages=[{"role": "user", "content": "weather?"}],
            tools=tools,
            tool_choice="auto",
        )

        call = conversation.calls[0]
        self.assertIn("Read weather", call["messages"][0]["content"])
        self.assertIsNone(call["tools"])
        self.assertIsNone(call["tool_choice"])

    def test_claude_invoke_markup_becomes_a_tool_call(self):
        """Live 2026-09-27: Claude answered with <invoke name=...> markup,
        Hermes parsed no call and the turn stopped at "Reviso..."."""
        text = (
            "Reviso el campo.\n\n<function_calls>\n"
            '<invoke name="browser_exec">\n'
            '<parameter name="code">print(js("1 < 2"))</parameter>\n'
            '<parameter name="timeout_s">120</parameter>\n'
            "</invoke>\n</function_calls>"
        )
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "browser_exec",
                    "description": "Run browser code",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "timeout_s": {"type": "integer"},
                        },
                    },
                },
            }
        ]
        client = self.module.goncloud_subscriptions.create_client(
            conversation_router=FakeConversationRouter(text),
            workspace=self.workspace,
        )

        completion = client.chat.completions.create(
            model="main",
            messages=[{"role": "user", "content": "entra"}],
            tools=tools,
        )

        message = completion.choices[0].message
        self.assertEqual(completion.choices[0].finish_reason, "tool_calls")
        self.assertEqual(message.content, "Reviso el campo.")
        self.assertEqual(message.tool_calls[0].function.name, "browser_exec")
        self.assertEqual(
            json.loads(message.tool_calls[0].function.arguments),
            {"code": 'print(js("1 < 2"))', "timeout_s": 120},
        )

    def test_history_tool_calls_are_shown_in_the_text_contract(self):
        """A history of native tool_calls taught the model to call natively,
        which the tool-less CLI rejects (live 2026-09-27)."""
        conversation = FakeConversationRouter("ok")
        client = self.module.goncloud_subscriptions.create_client(
            conversation_router=conversation,
            workspace=self.workspace,
        )
        call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "weather", "arguments": '{"city":"Lima"}'},
        }
        messages = [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": "Checking.", "tool_calls": [call]},
            {"role": "tool", "tool_call_id": "call_1", "content": "sunny"},
        ]

        client.chat.completions.create(model="main", messages=messages)

        sent = conversation.calls[0]["messages"][1]
        self.assertNotIn("tool_calls", sent)
        self.assertTrue(sent["content"].startswith("Checking.\n<tool_call>"))
        self.assertIn('"name": "weather"', sent["content"])
        # The caller's history is not mutated.
        self.assertIn("tool_calls", messages[1])


if __name__ == "__main__":
    unittest.main()
