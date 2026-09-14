from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
HERMES_ROOT = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
sys.path.insert(0, str(HERMES_ROOT))
sys.path.insert(0, str(PLUGIN_DIR))

if "goncloud_team" not in sys.modules:
    _SPEC = importlib.util.spec_from_file_location(
        "goncloud_team",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    _PLUGIN = importlib.util.module_from_spec(_SPEC)
    sys.modules["goncloud_team"] = _PLUGIN
    _SPEC.loader.exec_module(_PLUGIN)

import yaml  # noqa: E402

from hermes_cli.kanban_db import complete_task, list_comments  # noqa: E402
from hermes_cli.kanban_db_connect import connect_closing  # noqa: E402
from subscriptions.types import InvocationResult  # noqa: E402
from workflow import WorkflowError, WorkflowService  # noqa: E402

SOURCES = [
    {
        "reference": "owner instruction in session tool-7",
        "observed_at": "2026-09-13T21:00:00Z",
    }
]


def payload(**overrides):
    base = {
        "workflow_id": "wf-any",
        "task_id": "t-any",
        "instruction": "Inspect and summarize the repository.",
        "repository": r"C:\dev\repo",
        "revision": "0123456789abcdef",
        "sources": SOURCES,
        "max_steps": 40,
    }
    base.update(overrides)
    return base


class FakeSubscriptionService:
    def __init__(self, profile_name):
        self.profile_name = profile_name
        self.calls = []

    def subscription_run(self, **kwargs):
        self.calls.append(("run", kwargs))
        return {"profile": self.profile_name, "success": True, "text": "done"}

    def subscription_status(self, **kwargs):
        self.calls.append(("status", kwargs))
        return {"profile": self.profile_name, "routes": []}


class FakeContext:
    def __init__(self, profile_name="claw", service=None):
        self.profile_name = profile_name
        self.service = service or FakeSubscriptionService(profile_name)
        self.tools = {}

    def register_tool(self, *, name, toolset, schema, handler, emoji=None):
        self.tools[name] = types.SimpleNamespace(
            schema=schema, handler=handler, emoji=emoji, toolset=toolset
        )

    def register_hook(self, *args, **kwargs):
        del args, kwargs

    def call(self, name, payload_args):
        return json.loads(self.tools[name].handler(dict(payload_args)))


class PluginRegistrationTests(unittest.TestCase):
    def test_tool_binds_profile_from_context(self):
        ctx = FakeContext(profile_name="implementer")
        _PLUGIN._build_service = lambda context: ctx.service

        _PLUGIN.register(ctx)

        schema = ctx.tools["goncloud_subscription_run"].schema
        self.assertNotIn("profile", schema["parameters"]["properties"])
        self.assertEqual(
            set(schema["parameters"]["required"]),
            {
                "workflow_id",
                "task_id",
                "instruction",
                "repository",
                "revision",
                "sources",
                "max_steps",
            },
        )
        result = ctx.call("goncloud_subscription_run", payload())
        self.assertEqual(result["profile"], ctx.profile_name)
        self.assertEqual(ctx.service.calls[0][0], "run")
        status_schema = ctx.tools["goncloud_subscription_status"].schema
        self.assertNotIn("profile", status_schema["parameters"]["properties"])
        self.assertEqual(
            ctx.call("goncloud_subscription_status", {})["profile"],
            ctx.profile_name,
        )

    def test_handler_errors_return_json_error_not_crash(self):
        class ExplodingService:
            def subscription_run(self, **kwargs):
                raise WorkflowError("stage identity mismatch")

        ctx = FakeContext(profile_name="claw")
        _PLUGIN._build_service = lambda context: ExplodingService()
        _PLUGIN.register(ctx)

        result = ctx.call("goncloud_subscription_run", payload())

        self.assertIn("error", result)
        self.assertIn("stage identity", result["error"])

    def test_manifest_advertises_subscription_tools(self):
        manifest = yaml.safe_load(
            (PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8")
        )

        self.assertIn("goncloud_subscription_run", manifest["provides_tools"])
        self.assertIn("goncloud_subscription_status", manifest["provides_tools"])


class ScriptedRouter:
    def __init__(self, result):
        self.result = result
        self.requests = []
        self.routing_source = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )

    def run(self, request, routes):
        self.requests.append((request, routes))
        return self.result

    def status(self, profile, **kwargs):
        return {"profile": profile, "routes": []}


class SubscriptionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db_path = self.root / "kanban.db"
        import workflow as workflow_module

        self.workflow_module = workflow_module
        self._original_factory = workflow_module._build_subscription_router
        self.result = InvocationResult(
            success=True,
            text="stage deliverable",
            adapter="claude",
            model="claude-sonnet-5",
            billing_mode="subscription",
            session_id="sess_1",
        )
        self.router = ScriptedRouter(self.result)
        workflow_module._build_subscription_router = lambda **kwargs: self.router

    def tearDown(self) -> None:
        self.workflow_module._build_subscription_router = self._original_factory

    def start(self, request_key="owner-7"):
        return self.service("claw").start(
            request_key=request_key,
            repository=str(self.repo),
            revision="0123456789abcdef",
            instruction="Wire the router into the chain.",
            sources=SOURCES,
        )

    def service(self, role="scout"):
        return WorkflowService(db_path=self.db_path, actor_role=role)

    def test_run_puts_adapter_slot_model_and_billing_in_handoff(self):
        started = self.start()
        task_id = started["stages"]["scout"]

        result = self.service("scout").subscription_run(
            workflow_id=started["workflow_id"],
            task_id=task_id,
            instruction="Investigate the constraint.",
            repository=str(self.repo),
            revision="0123456789abcdef",
            sources=SOURCES,
            max_steps=40,
        )

        self.assertTrue(result["success"])
        self.assertEqual(result["profile"], "scout")
        self.assertEqual(result["adapter"], "claude")
        self.assertEqual(result["model"], "claude-sonnet-5")
        self.assertEqual(result["billing_mode"], "subscription")
        request, routes = self.router.requests[0]
        self.assertEqual(request.workflow_id, started["workflow_id"])
        self.assertEqual(request.stage_id, task_id)
        self.assertEqual(request.workspace, self.repo.resolve())
        self.assertEqual([route.slot for route in routes], list(range(8)))
        self.assertEqual(routes[0].adapter, "opencode_go")
        with connect_closing(self.db_path) as conn:
            complete_task(conn, task_id, summary="scout done")

    def test_run_rejects_mismatched_assignee_workspace_and_revision(self):
        started = self.start()
        scout_id = started["stages"]["scout"]
        other_repo = self.root / "elsewhere"
        other_repo.mkdir()

        with self.assertRaisesRegex(WorkflowError, "not assigned"):
            self.service("implementer").subscription_run(
                workflow_id=started["workflow_id"],
                task_id=scout_id,
                instruction="Not my stage.",
                repository=str(self.repo),
                revision="0123456789abcdef",
                sources=SOURCES,
                max_steps=10,
            )
        with self.assertRaisesRegex(WorkflowError, "workspace"):
            self.service("scout").subscription_run(
                workflow_id=started["workflow_id"],
                task_id=scout_id,
                instruction="Wrong place.",
                repository=str(other_repo),
                revision="0123456789abcdef",
                sources=SOURCES,
                max_steps=10,
            )
        with self.assertRaisesRegex(WorkflowError, "revision"):
            self.service("scout").subscription_run(
                workflow_id=started["workflow_id"],
                task_id=scout_id,
                instruction="Wrong revision.",
                repository=str(self.repo),
                revision="deadbeef",
                sources=SOURCES,
                max_steps=10,
            )
        self.assertEqual(self.router.requests, [])

    def test_result_from_other_stage_is_rejected(self):
        started = self.start()
        engineer_id = started["stages"]["ingenieria"]

        with self.assertRaisesRegex(WorkflowError, "stage identity"):
            self.service("scout").attach_subscription_result(
                {
                    "workflow_id": started["workflow_id"],
                    "stage_id": engineer_id,
                    "profile": "scout",
                    "adapter": "claude",
                    "model": "claude-sonnet-5",
                    "billing_mode": "subscription",
                    "success": True,
                    "text": "deliverable",
                }
            )

    def test_attach_records_durable_evidence_on_the_stage(self):
        started = self.start()
        scout_id = started["stages"]["scout"]

        attached = self.service("scout").attach_subscription_result(
            {
                "workflow_id": started["workflow_id"],
                "stage_id": scout_id,
                "profile": "scout",
                "adapter": "claude",
                "model": "claude-sonnet-5",
                "billing_mode": "subscription",
                "success": True,
                "text": "deliverable",
                "slot": 2,
            }
        )

        self.assertTrue(attached["attached"])
        with connect_closing(self.db_path) as conn:
            comments = list_comments(conn, scout_id)
        self.assertTrue(
            any("GONCLOUD_SUBSCRIPTION" in comment.body for comment in comments)
        )


if __name__ == "__main__":
    unittest.main()
