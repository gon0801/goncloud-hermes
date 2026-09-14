from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.adapters import build_adapter_registry  # noqa: E402
from subscriptions.adapters._http import request_json, SubscriptionHttpError  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.state import SubscriptionStateStore  # noqa: E402
from subscriptions.adapters.deepseek import DeepSeekAdapter  # noqa: E402
from subscriptions.adapters.grok import GrokAdapter  # noqa: E402
from subscriptions.adapters.kimi import KimiAdapter, parse_kimi_jsonl  # noqa: E402
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

    def __init__(self, status=200, body="{}"):
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
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_kimi_uses_prompt_mode_stream_json_and_stdin(self):
        # kimi 0.34.0 rejects --auto combined with -p; prompt mode is already
        # the autonomous non-interactive mode, so autonomy is preserved.
        runner = FakeCliRunner(stdout=KIMI_STREAM)

        result = KimiAdapter(runner=runner).invoke(
            request(self.workspace), route_for("kimi", "k3", slot=3)
        )

        self.assertEqual(runner.command[:3], ["kimi", "-p", "-"])
        self.assertEqual(
            runner.command[runner.command.index("--output-format") + 1],
            "stream-json",
        )
        # Bare table models are qualified with kimi's configured alias prefix.
        self.assertEqual(
            runner.command[runner.command.index("--model") + 1], "kimi-code/k3"
        )
        self.assertIn("Summarize the stage evidence.", runner.kwargs["input"])
        self.assertFalse(runner.kwargs["shell"])
        self.assertTrue(result.success)
        self.assertEqual(result.text, "kimi reply")
        self.assertEqual(result.session_id, "kimi_s1")
        self.assertEqual(result.billing_mode, "subscription")

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
            request(self.workspace), route_for("grok", "grok-4.6", slot=6)
        )

        command = runner.command
        self.assertEqual(command[0], "grok")
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


class HttpAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

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


if __name__ == "__main__":
    unittest.main()
