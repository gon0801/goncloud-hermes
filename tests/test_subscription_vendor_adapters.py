from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import time
from dataclasses import replace
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

from subscriptions.adapters import build_adapter_registry  # noqa: E402
from subscriptions.adapters._live_probe import PROBE_MAX_TOKENS  # noqa: E402
from subscriptions.adapters._http import (  # noqa: E402
    MAX_BODY_CHARS,
    request_json,
    SubscriptionHttpError,
)
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.state import SubscriptionStateStore  # noqa: E402
from subscriptions.adapters.claude import ClaudeAdapter  # noqa: E402
from subscriptions.adapters.codex import CodexAdapter  # noqa: E402
from subscriptions.adapters.deepseek import DeepSeekAdapter  # noqa: E402
from subscriptions.adapters.grok import GrokAdapter  # noqa: E402
from subscriptions.adapters.kimi import (  # noqa: E402
    KIMI_SENTINEL,
    KimiAdapter,
    kimi_agent_definition,
    parse_kimi_jsonl,
)
from subscriptions.adapters.muse import MuseAdapter  # noqa: E402
from subscriptions.adapters.opencode import OpenCodeAdapter  # noqa: E402
from subscriptions.adapters.zai import ZAIAdapter  # noqa: E402
from subscriptions.types import (  # noqa: E402
    FailureKind,
    InvocationRequest,
    RouteSpec,
)

GROK_ACP = "\n".join(
    [
        '{"type":"thought","data":"."}',
        '{"type":"text","data":"grok "}',
        '{"type":"text","data":"answered"}',
        '{"type":"end","stopReason":"end_turn","sessionId":"grok_s1"}',
    ]
)

KIMI_STREAM = "\n".join(
    [
        '{"type":"system","subtype":"init","session_id":"kimi_s1"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"kimi reply"}]}}',
        '{"type":"result","subtype":"success","result":"kimi reply"}',
    ]
)

# What kimi 0.34.0 actually prints in prompt mode.
KIMI_034_STREAM = "\n".join(
    [
        '{"role":"meta","type":"system.version","version":"0.34.0"}',
        '{"role":"assistant","content":"kimi reply"}',
        '{"role":"meta","type":"session.resume_hint","session_id":"session_034",'
        '"command":"kimi -r session_034","content":"To resume this session: kimi -r session_034"}',
    ]
)

# What kimi 0.34.0 prints when the turn uses a tool: an assistant line that
# carries tool_calls instead of content, then a role:"tool" result line, then
# the assistant answer and the closing resume_hint. Captured from a live run.
KIMI_034_TOOL_STREAM = "\n".join(
    [
        '{"role":"meta","type":"system.version","version":"0.34.0"}',
        '{"role":"assistant","tool_calls":[{"type":"function","id":"tool_1",'
        '"function":{"name":"Read","arguments":"{\\"path\\":\\"sample.txt\\"}"}}]}',
        '{"role":"tool","tool_call_id":"tool_1","content":"1\\tline one\\n2\\tline two"}',
        '{"role":"assistant","content":"The file has 3 lines."}',
        '{"role":"meta","type":"session.resume_hint","session_id":"session_tool",'
        '"command":"kimi -r session_tool","content":"To resume: kimi -r session_tool"}',
    ]
)
# Fixture markers used only to prove redaction; they are not real secrets.
DEEPSEEK_FIXTURE_KEY = "sk-fixture-deepseek-key"
ZAI_FIXTURE_TOKEN = "fixture-zai-token"


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


class AgentFileReadingRunner(FakeCliRunner):
    """Reads the private agent file while it still exists (run_cli unlinks it)."""

    agent_file = ""

    def __call__(self, command, **kwargs):
        command = list(command)
        if "--agent-file" in command:
            path = Path(command[command.index("--agent-file") + 1])
            self.agent_file = path.read_text(encoding="utf-8")
        return super().__call__(command, **kwargs)


def route_for(adapter, model, slot=5, billing="subscription"):
    return RouteSpec(
        slot=slot,
        label=f"vendor/{model}",
        adapter=adapter,
        model=model,
        billing_mode=billing,
    )


def request(
    workspace, prompt="Summarize the stage evidence.", mode="start", changed=()
):
    return InvocationRequest(
        workflow_id="wf-5",
        stage_id="stage-verifier",
        role="verifier",
        prompt=prompt,
        workspace=workspace,
        start_revision="r0",
        current_revision="r0",
        mode=mode,
        changed_files=changed,
    )


class FakeEndpoint:
    """Local HTTP stand-in for the DeepSeek and Z.AI endpoints."""

    def __init__(self, status=200, body="{}", *, malformed_chunked=False):
        self.status = status
        self.body = body
        self.requests: list[dict] = []
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                endpoint.requests.append(
                    {
                        "method": self.command,
                        "path": self.path,
                        "authorization": self.headers.get("Authorization", ""),
                        "version": self.headers.get("anthropic-version", ""),
                        "body": self.rfile.read(length).decode("utf-8", "replace"),
                    }
                )
                payload = endpoint.body.encode("utf-8")
                self.send_response(endpoint.status)
                self.send_header("Content-Type", "application/json")
                if malformed_chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                else:
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _serve
            do_POST = _serve

            def log_message(self, *_args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def start(self) -> str:
        self.thread.start()
        return f"http://127.0.0.1:{self._server.server_port}"

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeEndpoint":
        self._url = self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    @property
    def url(self) -> str:
        return self._url


def deepseek_ok_body(text="deepseek answer"):
    return json.dumps(
        {
            "id": "chatcmpl-1",
            "choices": [{"message": {"role": "assistant", "content": text}}],
        }
    )


def zai_ok_body(text="zai answer"):
    return json.dumps({"id": "msg_1", "content": [{"type": "text", "text": text}]})


class DripEndpoint:
    """A real server whose progress is faster than the socket idle timeout."""

    def __init__(self, *, body, status=200, drip_headers=False):
        self.active = 0
        self.max_active = 0
        self.started = threading.Event()
        self.finished = threading.Event()
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                with endpoint.lock:
                    endpoint.active += 1
                    endpoint.max_active = max(endpoint.max_active, endpoint.active)
                endpoint.started.set()
                payload = body.encode("utf-8").ljust(240, b" ")
                headers = (
                    f"HTTP/1.0 {status} Response\r\n"
                    f"Content-Length: {len(payload)}\r\n"
                    "Content-Type: application/json\r\n\r\n"
                ).encode("ascii")
                try:
                    if not drip_headers:
                        self.wfile.write(headers)
                    for byte in headers if drip_headers else payload:
                        if endpoint.stop_event.wait(0.02):
                            break
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                    if drip_headers and not endpoint.stop_event.is_set():
                        self.wfile.write(payload)
                except OSError:
                    pass  # The bounded client closes its socket at the deadline.
                finally:
                    with endpoint.lock:
                        endpoint.active -= 1
                        if not endpoint.active:
                            endpoint.finished.set()

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01)
        )

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_args):
        self.stop_event.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.finished.wait(5)


class BillingModeTests(unittest.TestCase):
    def test_only_deepseek_is_api_billed(self):
        adapters = build_adapter_registry()

        self.assertEqual(
            [adapters[name].billing_mode for name in ("kimi", "zai", "grok")],
            ["subscription"] * 3,
        )
        self.assertEqual(adapters["deepseek"].billing_mode, "api")
        self.assertEqual(adapters["opencode_go"].billing_mode, "subscription")
        self.assertEqual(adapters["opencode_zen"].billing_mode, "subscription")
        self.assertEqual(adapters["claude"].billing_mode, "subscription")
        self.assertEqual(adapters["codex"].billing_mode, "subscription")
        self.assertEqual(adapters["muse"].billing_mode, "subscription")
        self.assertEqual(
            set(adapters),
            {
                "opencode_go",
                "opencode_zen",
                "muse",
                "claude",
                "codex",
                "kimi",
                "zai",
                "grok",
                "deepseek",
            },
        )


class KimiGrokTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()

    def test_kimi_assignment_travels_in_a_private_agent_file_never_in_argv(self):
        # kimi 0.34 only takes a prompt as an argument and never reads stdin,
        # and it rejects --auto with -p. The assignment therefore rides in a
        # private agent file while argv carries one fixed sentence only.
        runner = AgentFileReadingRunner(stdout=KIMI_STREAM)

        result = KimiAdapter(runner=runner).invoke(
            request(self.workspace), route_for("kimi", "k3", slot=3)
        )

        self.assertEqual(runner.command[:2], ["kimi", "--agent-file"])
        self.assertTrue(runner.command[2].endswith(".txt"))
        self.assertEqual(runner.command[3:5], ["-p", KIMI_SENTINEL])
        self.assertNotIn("Summarize the stage evidence.", " ".join(runner.command))
        self.assertIn("Summarize the stage evidence.", runner.agent_file)
        self.assertTrue(runner.agent_file.startswith("---\n"))
        self.assertIsNone(runner.kwargs["input"])
        self.assertEqual(
            runner.command[runner.command.index("--output-format") + 1],
            "stream-json",
        )
        # Bare table models are qualified with kimi's configured alias prefix.
        self.assertEqual(
            runner.command[runner.command.index("--model") + 1], "kimi-code/k3"
        )
        self.assertFalse(runner.kwargs["shell"])
        self.assertTrue(result.success)
        self.assertEqual(result.text, "kimi reply")
        self.assertEqual(result.session_id, "kimi_s1")
        self.assertEqual(result.billing_mode, "subscription")

    def test_kimi_agent_definition_carries_the_frontmatter_kimi_requires(self):
        definition = kimi_agent_definition("Do the bounded task.")

        # kimi refuses an agent file without frontmatter ("Missing frontmatter").
        self.assertRegex(definition, r"^---\nname: [\w-]+\ndescription: .+\n---\n")
        self.assertTrue(definition.endswith("Do the bounded task.\n"))

    def test_kimi_034_stream_completes_on_the_resume_hint(self):
        # kimi 0.34 stream-json: role/content lines, no "result" event; the
        # session.resume_hint meta line is what closes prompt mode.
        result = KimiAdapter(runner=FakeCliRunner(stdout=KIMI_034_STREAM)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertTrue(result.success, result.failure)
        self.assertEqual(result.text, "kimi reply")
        self.assertEqual(result.session_id, "session_034")

    def test_kimi_034_tool_events_do_not_invalidate_the_turn(self):
        # 2026-09-15: a real kimi task that uses a tool emits a role:"tool"
        # result line and an assistant line carrying tool_calls instead of
        # content. The parser flagged both as malformed, so every tool-using
        # stage failed as task_failure even though kimi finished the work and
        # exited zero with a session.resume_hint.
        parsed = parse_kimi_jsonl(KIMI_034_TOOL_STREAM)
        self.assertTrue(parsed["terminal_valid"])
        self.assertIn("3 lines", parsed["text"])

        result = KimiAdapter(runner=FakeCliRunner(stdout=KIMI_034_TOOL_STREAM)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )
        self.assertTrue(result.success, result.failure)
        self.assertEqual(result.session_id, "session_tool")

    def test_kimi_034_retrying_without_resume_hint_is_not_a_completed_turn(self):
        stream = "\n".join(
            [
                '{"role":"meta","type":"system.version","version":"0.34.0"}',
                '{"role":"meta","type":"turn.step.retrying","failed_attempt":1,'
                '"error_name":"APIConnectionError","error_message":"Connection error."}',
                '{"role":"assistant","content":"partial"}',
            ]
        )

        exit_zero = KimiAdapter(runner=FakeCliRunner(stdout=stream)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )
        exit_one = KimiAdapter(
            runner=FakeCliRunner(
                stdout=stream,
                stderr="error: failed to run prompt: provider.connection_error",
                returncode=1,
            )
        ).invoke(request(self.workspace), route_for("kimi", "k3"))

        self.assertFalse(exit_zero.success)
        self.assertEqual(exit_zero.failure, FailureKind.TASK_FAILURE)
        self.assertFalse(exit_one.success)
        self.assertEqual(exit_one.failure, FailureKind.TRANSPORT)

    def test_kimi_connection_error_is_transport_failure(self):
        result = KimiAdapter(
            runner=FakeCliRunner(stderr="provider.connection_error", returncode=1)
        ).invoke(request(self.workspace), route_for("kimi", "k3"))

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TRANSPORT)

    def test_kimi_parser_exposes_valid_terminal_only_for_success_result(self):
        parsed = parse_kimi_jsonl(KIMI_STREAM)
        rejected = parse_kimi_jsonl(
            '{"type":"result","subtype":"error","result":"failed"}'
        )

        self.assertTrue(parsed["terminal_valid"])
        self.assertFalse(rejected["terminal_valid"])

    def test_kimi_malformed_jsonl_after_terminal_invalidates_parser_and_invoke(self):
        output = f"{KIMI_STREAM}\nnot-json-secret"
        parsed = parse_kimi_jsonl(output)
        result = KimiAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertFalse(parsed["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("not-json-secret", result.text)

    def test_kimi_invalid_message_shape_invalidates_stream_without_leaking_raw_data(
        self,
    ):
        output = "\n".join(
            [
                '{"type":"assistant","message":"raw-secret"}',
                '{"type":"result","subtype":"success","result":"answer"}',
            ]
        )
        parsed = parse_kimi_jsonl(output)
        result = KimiAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertFalse(parsed["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("raw-secret", repr(result))

    def test_kimi_scalar_content_block_invalidates_stream_without_leaking_raw_data(
        self,
    ):
        output = "\n".join(
            [
                '{"type":"assistant","message":{"content":["raw-block-secret"]}}',
                '{"type":"result","subtype":"success","result":"answer"}',
            ]
        )
        parsed = parse_kimi_jsonl(output)
        result = KimiAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertFalse(parsed["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("raw-block-secret", repr(result))

    def test_kimi_scalar_terminal_subtype_invalidates_without_leaking_raw_data(
        self,
    ):
        output = "\n".join(
            [
                '{"type":"assistant","message":{"content":[{"type":"text","text":"raw-subtype-secret"}]}}',
                '{"type":"result","subtype":[],"result":"answer"}',
            ]
        )
        parsed = parse_kimi_jsonl(output)
        result = KimiAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertFalse(parsed["terminal_valid"])
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("raw-subtype-secret", repr(result))

    def test_kimi_malformed_exit_zero_cannot_be_reclassified_as_transport(self):
        output = "\n".join(
            [
                '{"type":"assistant","message":{"content":["provider.connection_error raw-secret"]}}',
                '{"type":"result","subtype":"success","result":"answer"}',
            ]
        )

        result = KimiAdapter(runner=FakeCliRunner(stdout=output)).invoke(
            request(self.workspace), route_for("kimi", "k3")
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertNotIn("raw-secret", repr(result))

    def test_kimi_error_result_cannot_approve_partial_text(self):
        result = KimiAdapter(
            runner=FakeCliRunner(
                stdout="\n".join(
                    [
                        '{"type":"assistant","message":{"content":[{"type":"text","text":"partial"}]}}',
                        '{"type":"result","subtype":"error","result":"failed"}',
                    ]
                )
            )
        ).invoke(request(self.workspace), route_for("kimi", "k3"))

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)

    def test_grok_command_shape_keeps_sandbox_and_private_prompt(self):
        captured: dict = {}

        class CaptureRunner(FakeCliRunner):
            def __call__(self, command, **kwargs):
                result = super().__call__(command, **kwargs)
                index = command.index("--prompt-file")
                path = Path(command[index + 1])
                captured["text"] = path.read_text(encoding="utf-8")
                captured["path"] = path
                return result

        runner = CaptureRunner(stdout=GROK_ACP)

        result = GrokAdapter(runner=runner).invoke(
            replace(request(self.workspace), max_steps=17),
            route_for("grok", "grok-4.6", slot=6),
        )

        command = runner.command
        self.assertEqual(command[0], "grok")
        self.assertIn("--max-turns", command)
        self.assertEqual(command[command.index("--max-turns") + 1], "17")
        self.assertIn("Maximum autonomous steps: 17", captured["text"])
        self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(command[command.index("--sandbox") + 1], "workspace")
        self.assertEqual(
            command[command.index("--output-format") + 1], "streaming-json"
        )
        self.assertEqual(command[command.index("--model") + 1], "grok-4.6")
        self.assertIn("--prompt-file", command)
        self.assertNotIn("Summarize", " ".join(command))
        self.assertIsNone(runner.kwargs.get("input"))
        self.assertFalse(runner.kwargs["shell"])
        self.assertIn("Summarize the stage evidence.", captured["text"])
        self.assertFalse(captured["path"].exists())
        self.assertTrue(result.success)
        self.assertEqual(result.text, "grok answered")
        self.assertEqual(result.session_id, "grok_s1")
        self.assertEqual(result.billing_mode, "subscription")

    def test_grok_requires_nonempty_text_and_uncontradicted_success_terminal(self):
        for output in (
            GROK_ACP.rsplit("\n", 1)[0],
            '{"type":"end","stopReason":"end_turn"}',
            GROK_ACP.replace("grok ", " ").replace("answered", " "),
            GROK_ACP.replace("end_turn", "error"),
            GROK_ACP.replace('"stopReason":"end_turn",', ""),
            GROK_ACP + '\n{"type":"error","message":"failed"}',
            GROK_ACP + '\n{"type":"end","stopReason":"cancelled"}',
            GROK_ACP + "\nnot-json",
            GROK_ACP + '\n{"type":[]}',
            GROK_ACP + '\n{"sessionUpdate":{"update":["agent_message"],"content":"x"}}',
            GROK_ACP + '\n{"type":"end","sessionId":{"private":"value"}}',
        ):
            with self.subTest(case=output[-50:]):
                try:
                    result = GrokAdapter(runner=FakeCliRunner(stdout=output)).invoke(
                        request(self.workspace), route_for("grok", "grok-4.6")
                    )
                except Exception:
                    self.fail("malformed Grok frame escaped the adapter")
                self.assertFalse(result.success)
                self.assertEqual(result.failure, FailureKind.TASK_FAILURE)


class HttpAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.workspace = Path(self.tmp.name).resolve()

    def test_known_http_error_keeps_status_when_chunked_body_is_invalid(self):
        raw = "raw-private-fixture-not-a-chunk-size\r\n"
        with FakeEndpoint(status=429, body=raw, malformed_chunked=True) as endpoint:
            status, body = request_json("POST", endpoint.url, headers={}, payload={})
        self.assertEqual(status, 429)
        self.assertEqual(body, "")
        # A malformed success response must still fail, rather than become success.
        with FakeEndpoint(status=200, body=raw, malformed_chunked=True) as endpoint:
            with self.assertRaises(SubscriptionHttpError) as caught:
                request_json("POST", endpoint.url, headers={}, payload={})
        self.assertFalse(caught.exception.timeout)
        self.assertNotIn("raw-private-fixture", str(caught.exception))

    def test_http_boundary_and_adapters_redact_header_environment_and_identity(self):
        sensitive = (
            "opaque-header-fixture",
            "opaque-inherited-fixture",
            "sk-synthetic-pattern",
            "fixture@example.test",
        )
        for adapter_type, credentials, body_fn in (
            (ZAIAdapter, {"auth_token": sensitive[0]}, zai_ok_body),
            (DeepSeekAdapter, {"api_key": sensitive[0]}, deepseek_ok_body),
        ):
            with (
                self.subTest(adapter=adapter_type.__name__),
                patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive[1]}),
                FakeEndpoint(body=body_fn(" ".join(sensitive))) as endpoint,
            ):
                _, body = request_json(
                    "POST",
                    endpoint.url,
                    headers={"Authorization": f"Bearer {sensitive[0]}"},
                    payload={},
                )
                result = adapter_type(**credentials, base_url=endpoint.url).invoke(
                    request(self.workspace), route_for("zai", "fixture-model")
                )
            self.assertTrue(
                all(value not in repr((body, result)) for value in sensitive),
                "HTTP exposed synthetic sensitive content",
            )

    def test_http_redacts_before_evidence_truncation(self):
        sentinel = "opaque-boundary-fixture"
        raw = "x" * (MAX_BODY_CHARS - 10) + sentinel + "tail"
        with (
            patch.dict(os.environ, {"SERVICE_PASSWORD": sentinel}),
            FakeEndpoint(body=raw) as endpoint,
        ):
            _, body = request_json("GET", endpoint.url, headers={})
        self.assertTrue(
            sentinel[:10] not in body, "HTTP truncation exposed a credential prefix"
        )
        self.assertLessEqual(len(body), MAX_BODY_CHARS)

    def test_http_discards_oversized_response_before_returning_partial_secret(self):
        sentinel = "opaque-large-fixture"
        raw = "x" * (MAX_BODY_CHARS * 4 - 10) + sentinel
        with patch.dict(os.environ, {"SERVICE_PASSWORD": sentinel}):
            with FakeEndpoint(body=raw) as endpoint:
                with self.assertRaises(SubscriptionHttpError):
                    request_json("GET", endpoint.url, headers={})
            with FakeEndpoint(status=429, body=raw) as endpoint:
                status, body = request_json("GET", endpoint.url, headers={})
        self.assertEqual(status, 429)
        self.assertEqual(body, "")

    def test_zai_router_cannot_generate_health_while_adapter_is_owned(self):
        with FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as endpoint:
            adapter = ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
            router = SubscriptionRouter(
                adapters={"zai": adapter}, db_path=self.workspace / "probe-lock.db"
            )
            with router.store.dispatch_lock("zai"):
                result = router.run(
                    request(self.workspace), [route_for("zai", "glm-5.3")]
                )
            self.assertFalse(result.success)
            self.assertEqual(endpoint.requests, [])
            self.assertIsNone(router.store.health("zai"))

    def test_zai_router_denial_precedes_generative_health_probe(self):
        def denied():
            raise ValueError("revoked synthetic authority")

        with FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as endpoint:
            adapter = ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
            router = SubscriptionRouter(
                adapters={"zai": adapter}, db_path=self.workspace / "probe-denied.db"
            )
            result = router.run(
                replace(request(self.workspace), pre_invoke=denied),
                [route_for("zai", "glm-5.3")],
            )
            self.assertEqual(result.failure, FailureKind.INVALID_REQUEST)
            self.assertEqual(endpoint.requests, [])
            self.assertIsNone(router.store.health("zai"))
            attempts = router.store.stage_attempts("wf-5", "stage-verifier")
            self.assertEqual([row["disposition"] for row in attempts], ["denied"])

    def test_zai_health_authorization_can_publish_quarantine_without_nested_lock(self):
        with FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as endpoint:
            adapter = ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
            router = SubscriptionRouter(
                adapters={"zai": adapter}, db_path=self.workspace / "probe-publish.db"
            )

            def publish():
                router.store.save_live_health(
                    "zai", ready=False, account_class="subscription", failure="auth"
                )

            result = router.run(
                replace(request(self.workspace), pre_invoke=publish),
                [route_for("zai", "glm-5.3")],
            )
            self.assertEqual(result.failure, FailureKind.AUTH)
            self.assertEqual(endpoint.requests, [])
            self.assertTrue(router.store.health("zai")["quarantined"])

    def test_zai_cached_health_expiry_cannot_bypass_probe_authorization(self):
        def denied():
            raise ValueError("revoked")

        with FakeEndpoint(body=zai_ok_body("GONCLOUD_PROBE_OK")) as endpoint:
            adapter = ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
            router = SubscriptionRouter(
                adapters={"zai": adapter}, db_path=self.workspace / "probe-expiry.db"
            )
            with patch.object(
                router, "_cached_health", side_effect=[{"ready": True}, None]
            ):
                result = router.run(
                    replace(request(self.workspace), pre_invoke=denied),
                    [route_for("zai", "glm-5.3")],
                )
            self.assertEqual(result.failure, FailureKind.INVALID_REQUEST)
            self.assertEqual(len(endpoint.requests), 0)

    def test_zai_probe_requires_exact_generated_marker(self):
        for body, ready in (
            (zai_ok_body("GONCLOUD_PROBE_OK"), True),
            (zai_ok_body("READY"), False),
            (zai_ok_body(""), False),
            ("{}", False),
        ):
            with self.subTest(body=body), FakeEndpoint(body=body) as endpoint:
                adapter = ZAIAdapter(
                    auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url
                )
                health = adapter.probe(route_for("zai", "glm-5.3"))
            self.assertEqual(health.ready, ready)
            self.assertEqual(len(endpoint.requests), 1)
            seen = endpoint.requests[0]
            self.assertEqual(seen["method"], "POST")
            self.assertEqual(seen["path"], "/v1/messages")
            payload = json.loads(seen["body"])
            self.assertEqual(payload["model"], "glm-5.3")
            # Minimal, but long enough for a reasoning model to finish
            # thinking and still emit the marker.
            self.assertLessEqual(payload["max_tokens"], PROBE_MAX_TOKENS)
            self.assertLessEqual(PROBE_MAX_TOKENS, 2048)
            self.assertIn("GONCLOUD_PROBE_OK", payload["messages"][0]["content"])
            self.assertNotIn("tools", payload)
            self.assertNotIn(ZAI_FIXTURE_TOKEN, repr(health))

    def test_live_probes_enforce_exact_marker_billing_and_redact_failures(self):
        for name, adapter_type, key, body_fn in (
            ("zai", ZAIAdapter, {"auth_token": ZAI_FIXTURE_TOKEN}, zai_ok_body),
            (
                "deepseek",
                DeepSeekAdapter,
                {"api_key": DEEPSEEK_FIXTURE_KEY},
                deepseek_ok_body,
            ),
        ):
            for text, success in (
                ("GONCLOUD_PROBE_OK", True),
                ("GONCLOUD_PROBE_OK\n", False),
                ("sk-sensitive owner@example.test", False),
            ):
                with (
                    self.subTest(adapter=name, success=success),
                    FakeEndpoint(body=body_fn(text)) as endpoint,
                ):
                    adapter = adapter_type(**key, base_url=endpoint.url)
                    result = adapter.live_probe(route_for(name, "fixture-model"))
                self.assertEqual(result.success, success)
                self.assertEqual(
                    result.billing_mode, "api" if name == "deepseek" else "subscription"
                )
                self.assertEqual(len(endpoint.requests), 1)
                self.assertLessEqual(
                    json.loads(endpoint.requests[0]["body"])["max_tokens"],
                    PROBE_MAX_TOKENS,
                )
                self.assertNotIn("sk-sensitive", repr(result))
                self.assertNotIn("owner@example.test", repr(result))
            for status, failure in (
                (401, FailureKind.AUTH),
                (429, FailureKind.QUOTA),
                (500, FailureKind.TRANSPORT),
            ):
                with (
                    self.subTest(adapter=name, status=status),
                    FakeEndpoint(
                        status=status, body=body_fn("GONCLOUD_PROBE_OK")
                    ) as endpoint,
                ):
                    result = adapter_type(**key, base_url=endpoint.url).live_probe(
                        route_for(name, "fixture-model")
                    )
                self.assertFalse(result.success)
                self.assertEqual(result.failure, failure)

    def test_live_http_probe_deadline_uses_bounded_worker_without_mutation(self):
        for name, adapter_type, key, body in (
            (
                "zai",
                ZAIAdapter,
                {"auth_token": ZAI_FIXTURE_TOKEN},
                zai_ok_body("GONCLOUD_PROBE_OK"),
            ),
            (
                "deepseek",
                DeepSeekAdapter,
                {"api_key": DEEPSEEK_FIXTURE_KEY},
                deepseek_ok_body("GONCLOUD_PROBE_OK"),
            ),
        ):
            with (
                self.subTest(adapter=name),
                DripEndpoint(body=body.ljust(600)) as endpoint,
            ):
                adapter = adapter_type(**key, base_url=endpoint.url)
                adapter.probe_timeout_seconds = 0.5
                start = time.monotonic()
                result = adapter.live_probe(route_for(name, "fixture-model"))
                self.assertEqual(result.failure, FailureKind.TIMEOUT)
                self.assertLess(time.monotonic() - start, 5)
                self.assertTrue(endpoint.finished.wait(1))
                self.assertEqual(endpoint.active, 0)

    def test_invalid_chunked_quota_response_cools_down_and_advances_both_adapters(self):
        raw = "raw-private-fixture-not-a-chunk-size\r\n"
        for first_name in ("zai", "deepseek"):
            next_name = "deepseek" if first_name == "zai" else "zai"
            next_body = deepseek_ok_body() if next_name == "deepseek" else zai_ok_body()
            with (
                self.subTest(adapter=first_name),
                FakeEndpoint(status=429, body=raw, malformed_chunked=True) as broken,
                FakeEndpoint(body=next_body) as healthy,
            ):
                urls = {first_name: broken.url, next_name: healthy.url}
                adapters = {
                    "zai": ZAIAdapter(
                        auth_token=ZAI_FIXTURE_TOKEN, base_url=urls["zai"]
                    ),
                    "deepseek": DeepSeekAdapter(
                        api_key=DEEPSEEK_FIXTURE_KEY, base_url=urls["deepseek"]
                    ),
                }
                router = SubscriptionRouter(
                    adapters=adapters, db_path=self.workspace / f"quota-{first_name}.db"
                )
                for name in adapters:
                    router.store.save_health(name, ready=True, account_class=name)
                routes = [
                    route_for(
                        name,
                        "test-model",
                        slot=slot,
                        billing=adapters[name].billing_mode,
                    )
                    for slot, name in enumerate((first_name, next_name))
                ]
                result = router.run(request(self.workspace), routes)
                attempts = router.store.stage_attempts("wf-5", "stage-verifier")
                self.assertTrue(result.success)
                self.assertEqual(result.adapter, next_name)
                self.assertEqual(attempts[0]["failure"], "quota")
                self.assertGreater(
                    router.store.cooldown_until(first_name), datetime.now(timezone.utc)
                )
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[1]["success"], 1)
                self.assertEqual(len(broken.requests), 1)
                self.assertEqual(len(healthy.requests), 1)
                self.assertNotIn("raw-private-fixture", repr(result))
                self.assertNotIn("raw-private-fixture", repr(attempts))

    def test_dripping_http_cannot_outlive_stage_lease_or_overlap_another_owner(self):
        for name, body in (("zai", zai_ok_body()), ("deepseek", deepseek_ok_body())):
            with (
                self.subTest(adapter=name),
                DripEndpoint(body=body.ljust(600)) as endpoint,
            ):
                adapter = (
                    ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
                    if name == "zai"
                    else DeepSeekAdapter(
                        api_key=DEEPSEEK_FIXTURE_KEY, base_url=endpoint.url
                    )
                )
                adapter.timeout_seconds = 0.5
                db_path = self.workspace / f"{name}.db"
                store = SubscriptionStateStore(db_path)
                store.save_health(name, ready=True, account_class=name)
                routers = [
                    SubscriptionRouter(adapters={name: adapter}, db_path=db_path)
                    for _ in range(2)
                ]
                route = route_for(name, "test-model", billing=adapter.billing_mode)

                def run(router):
                    started = time.monotonic()
                    result = router.run(request(self.workspace), [route])
                    return result, time.monotonic() - started

                # Scale invocation down while retaining the process boundary's
                # real four-second cleanup allowance and Git inspection time.
                with (
                    patch("subscriptions.router._STAGE_LEASE_SECONDS", 6),
                    ThreadPoolExecutor(max_workers=2) as pool,
                ):
                    first = pool.submit(run, routers[0])
                    self.assertTrue(endpoint.started.wait(5))
                    time.sleep(6.1)
                    second = pool.submit(run, routers[1])
                    first_result, elapsed = first.result(timeout=10)
                    second.result(timeout=10)
                self.assertEqual(endpoint.max_active, 1)
                self.assertFalse(first_result.success)
                self.assertEqual(
                    first_result.failure, FailureKind.TIMEOUT, repr(first_result)
                )
                self.assertLess(elapsed, 6)
                self.assertTrue(endpoint.finished.wait(1))
                self.assertEqual(endpoint.active, 0)

    def test_http_total_deadline_covers_dripping_headers_and_error_body(self):
        for status, headers in ((200, True), (429, False)):
            with (
                self.subTest(status=status),
                DripEndpoint(
                    body="{}", status=status, drip_headers=headers
                ) as endpoint,
            ):
                started = time.monotonic()
                with self.assertRaises(SubscriptionHttpError) as caught:
                    request_json(
                        "POST", endpoint.url, headers={}, payload={}, timeout=0.5
                    )
                self.assertTrue(caught.exception.timeout)
                self.assertLess(time.monotonic() - started, 5)
                self.assertTrue(endpoint.finished.wait(1))
                self.assertEqual(endpoint.active, 0)

    def test_deepseek_calls_api_billed_endpoint_and_parses_choice(self):
        with FakeEndpoint(body=deepseek_ok_body()) as endpoint:
            adapter = DeepSeekAdapter(
                api_key=DEEPSEEK_FIXTURE_KEY, base_url=endpoint.url
            )
            result = adapter.invoke(
                request(self.workspace),
                route_for("deepseek", "deepseek-flash", slot=7, billing="api"),
            )
            seen = endpoint.requests

        self.assertTrue(result.success)
        self.assertEqual(result.text, "deepseek answer")
        self.assertEqual(result.billing_mode, "api")
        self.assertEqual(result.adapter, "deepseek")
        self.assertEqual(seen[0]["path"], "/chat/completions")
        self.assertEqual(seen[0]["authorization"], f"Bearer {DEEPSEEK_FIXTURE_KEY}")
        payload = json.loads(seen[0]["body"])
        self.assertEqual(payload["model"], "deepseek-flash")
        self.assertFalse(payload.get("stream", False))
        self.assertNotIn(DEEPSEEK_FIXTURE_KEY, result.text)
        self.assertNotIn(DEEPSEEK_FIXTURE_KEY, repr(result))

    def test_deepseek_classifies_auth_and_balance(self):
        with FakeEndpoint(status=401, body='{"error":"auth"}') as endpoint:
            result = DeepSeekAdapter(
                api_key=DEEPSEEK_FIXTURE_KEY, base_url=endpoint.url
            ).invoke(
                request(self.workspace),
                route_for("deepseek", "deepseek-flash", billing="api"),
            )
        self.assertEqual(result.failure, FailureKind.AUTH)

        with FakeEndpoint(
            status=402, body='{"error":"insufficient balance"}'
        ) as endpoint:
            result = DeepSeekAdapter(
                api_key=DEEPSEEK_FIXTURE_KEY, base_url=endpoint.url
            ).invoke(
                request(self.workspace),
                route_for("deepseek", "deepseek-flash", billing="api"),
            )
        self.assertEqual(result.failure, FailureKind.QUOTA)

    def test_deepseek_probe_uses_non_generative_models_list(self):
        with FakeEndpoint(body='{"data":[{"id":"deepseek-flash"}]}') as endpoint:
            adapter = DeepSeekAdapter(
                api_key=DEEPSEEK_FIXTURE_KEY, base_url=endpoint.url
            )
            health = adapter.probe(route_for("deepseek", "deepseek-flash"))
            seen = endpoint.requests

        self.assertTrue(health.ready)
        self.assertEqual(health.account_class, "deepseek_api")
        self.assertEqual(seen[0]["method"], "GET")
        self.assertEqual(seen[0]["path"], "/models")

    def test_zai_posts_anthropic_compatible_messages_and_parses_text(self):
        with FakeEndpoint(body=zai_ok_body()) as endpoint:
            adapter = ZAIAdapter(auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url)
            result = adapter.invoke(
                request(self.workspace), route_for("zai", "glm-5.3", slot=5)
            )
            seen = endpoint.requests

        self.assertTrue(result.success)
        self.assertEqual(result.text, "zai answer")
        self.assertEqual(result.billing_mode, "subscription")
        self.assertEqual(seen[0]["path"], "/v1/messages")
        self.assertEqual(seen[0]["authorization"], f"Bearer {ZAI_FIXTURE_TOKEN}")
        self.assertEqual(seen[0]["version"], "2023-06-01")
        payload = json.loads(seen[0]["body"])
        self.assertEqual(payload["model"], "glm-5.3")
        self.assertNotIn(ZAI_FIXTURE_TOKEN, result.text)
        self.assertNotIn(ZAI_FIXTURE_TOKEN, repr(result))

    def test_zai_classifies_auth_and_quota(self):
        with FakeEndpoint(status=401, body="{}") as endpoint:
            result = ZAIAdapter(
                auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url
            ).invoke(request(self.workspace), route_for("zai", "glm-5.3"))
        self.assertEqual(result.failure, FailureKind.AUTH)

        with FakeEndpoint(status=429, body="rate limit") as endpoint:
            result = ZAIAdapter(
                auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url
            ).invoke(request(self.workspace), route_for("zai", "glm-5.3"))
        self.assertEqual(result.failure, FailureKind.QUOTA)

    def test_unreachable_endpoint_is_task_failure_not_crash(self):
        adapter = DeepSeekAdapter(
            api_key=DEEPSEEK_FIXTURE_KEY, base_url="http://127.0.0.1:1"
        )
        result = adapter.invoke(
            request(self.workspace),
            route_for("deepseek", "deepseek-flash", billing="api"),
        )
        self.assertFalse(result.success)
        self.assertIn(result.failure, {FailureKind.TASK_FAILURE, FailureKind.TIMEOUT})


class ThinkingEndpoint:
    """An Anthropic-compatible model that reasons before it answers.

    Below the budget it spends the whole allowance thinking and stops on
    ``max_tokens``; above it the thinking block is followed by the marker.
    """

    def __init__(self, *, budget):
        self.budget = budget
        self.observed_max_tokens = None
        endpoint = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                request_body = json.loads(self.rfile.read(length) or b"{}")
                allowance = int(request_body.get("max_tokens") or 0)
                endpoint.observed_max_tokens = allowance
                content = [{"type": "thinking", "thinking": "deciding"}]
                stop = "max_tokens"
                if allowance >= endpoint.budget:
                    content.append({"type": "text", "text": "GONCLOUD_PROBE_OK"})
                    stop = "end_turn"
                payload = json.dumps(
                    {"id": "msg_1", "content": content, "stop_reason": stop}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()


class ReasoningProbeBudgetTests(unittest.TestCase):
    """A reasoning model must be able to finish thinking and still answer."""

    def test_zai_probe_allows_a_thinking_block_before_the_marker(self):
        with ThinkingEndpoint(budget=512) as endpoint:
            result = ZAIAdapter(
                auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url
            ).live_probe(route_for("zai", "glm-5.3", slot=5))

        self.assertGreaterEqual(endpoint.observed_max_tokens, 512)
        self.assertTrue(result.success)
        self.assertIsNone(result.failure)

    def test_a_probe_truncated_by_our_own_budget_never_blocks_the_slot(self):
        from subscriptions.router import _ADVANCING_FAILURES

        with ThinkingEndpoint(budget=10**6) as endpoint:
            result = ZAIAdapter(
                auth_token=ZAI_FIXTURE_TOKEN, base_url=endpoint.url
            ).live_probe(route_for("zai", "glm-5.3", slot=5))

        self.assertFalse(result.success)
        self.assertIn(result.failure, _ADVANCING_FAILURES)


class TimingOutRunner:
    """Every vendor CLI launch exceeds its deadline instead of answering."""

    def __call__(self, command, **kwargs):
        raise subprocess.TimeoutExpired(list(command), kwargs.get("timeout", 1))


class ProbeFailureClassificationTests(unittest.TestCase):
    """A probe the vendor never answered must advance the slot, not block it."""

    def probes(self):
        runner = TimingOutRunner()
        return {
            "claude": (ClaudeAdapter(runner=runner), route_for("claude", "m", slot=2)),
            "codex": (CodexAdapter(runner=runner), route_for("codex", "m", slot=7)),
            "grok": (GrokAdapter(runner=runner), route_for("grok", "m", slot=6)),
            "kimi": (KimiAdapter(runner=runner), route_for("kimi", "m", slot=3)),
            "muse": (MuseAdapter(runner=runner), route_for("muse", "m", slot=4)),
            "opencode_go": (
                OpenCodeAdapter(runner=runner),
                route_for("opencode_go", "m", slot=0),
            ),
            "opencode_zen": (
                OpenCodeAdapter(runner=runner),
                route_for("opencode_zen", "m", slot=1),
            ),
        }

    def test_every_cli_probe_reports_a_vendor_timeout_as_a_timeout(self):
        observed = {}
        for name, (adapter, route) in self.probes().items():
            health = adapter.probe(route)
            observed[name] = (health.ready, health.failure)

        self.assertEqual(
            observed,
            {name: (False, FailureKind.TIMEOUT) for name in observed},
        )

    def test_a_probe_timeout_is_a_failure_the_router_advances_past(self):
        from subscriptions.router import _ADVANCING_FAILURES

        for name, (adapter, route) in self.probes().items():
            with self.subTest(adapter=name):
                self.assertIn(adapter.probe(route).failure, _ADVANCING_FAILURES)


if __name__ == "__main__":
    unittest.main()
