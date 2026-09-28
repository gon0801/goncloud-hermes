from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sqlite3
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
HERMES_ROOT = Path.home() / "AppData" / "Local" / "hermes" / "hermes-agent"
sys.path.insert(0, str(HERMES_ROOT))
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

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

from hermes_cli.kanban_db import block_task, complete_task, list_comments  # noqa: E402
from hermes_cli.kanban_db_connect import connect_closing  # noqa: E402
from subscriptions.adapters import build_assignment_prompt  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.types import AdapterHealth, InvocationRequest, InvocationResult  # noqa: E402
from goncloud_team.schemas import SUBSCRIPTION_RUN  # noqa: E402
from workflow import WorkflowError, WorkflowService  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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


class FakeOrchestrator:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"workflow_id": "t_local"}

    def launch_detached(self, workflow_id, **kwargs):
        self.calls.append(("launch", workflow_id, kwargs))
        return {"workflow_id": workflow_id, "driver_pid": 7, "result_file": "x"}

    def status(self, workflow_id):
        self.calls.append(("status", workflow_id))
        return {"workflow_id": workflow_id, "outcome": "running"}


class TransportToolTests(unittest.TestCase):
    def test_local_run_creates_then_detaches_and_only_claw_may_call(self):
        ctx = FakeContext(profile_name="claw")
        orchestrator = FakeOrchestrator()
        _PLUGIN._build_orchestrator = lambda context: orchestrator

        _PLUGIN.register(ctx)
        result = ctx.call(
            "goncloud_local_run",
            {
                "request_key": "tg-1",
                "repository": "C:/repo",
                "instruction": "Inspect.",
                "sources": [
                    {"reference": "owner", "observed_at": "2026-09-14T00:00:00Z"}
                ],
                "max_steps": 20,
            },
        )

        self.assertEqual(result["workflow_id"], "t_local")
        self.assertEqual(result["driver_pid"], 7)
        self.assertEqual([call[0] for call in orchestrator.calls], ["create", "launch"])
        self.assertEqual(
            ctx.tools["goncloud_local_run"].schema["parameters"]["required"],
            ["request_key", "repository", "instruction", "sources"],
        )

        worker = FakeContext(profile_name="scout")
        _PLUGIN.register(worker)
        refused = worker.call("goncloud_local_run", {"request_key": "tg-2"})
        self.assertIn("error", refused)

    def test_local_run_forwards_stop_after_for_read_only_requests(self):
        ctx = FakeContext(profile_name="claw")
        orchestrator = FakeOrchestrator()
        _PLUGIN._build_orchestrator = lambda context: orchestrator

        _PLUGIN.register(ctx)
        ctx.call(
            "goncloud_local_run",
            {
                "request_key": "tg-3",
                "repository": "C:/repo",
                "instruction": "Inspect only.",
                "sources": [
                    {"reference": "owner", "observed_at": "2026-09-14T00:00:00Z"}
                ],
                "stop_after": "scout",
            },
        )

        launch = next(call for call in orchestrator.calls if call[0] == "launch")
        self.assertEqual(launch[2].get("stop_after"), "scout")
        schema = ctx.tools["goncloud_local_run"].schema["parameters"]["properties"]
        self.assertIn("scout", schema["stop_after"]["enum"])

    def test_local_run_rejects_a_bad_stop_after_before_creating_the_chain(self):
        ctx = FakeContext(profile_name="claw")
        orchestrator = FakeOrchestrator()
        _PLUGIN._build_orchestrator = lambda context: orchestrator

        _PLUGIN.register(ctx)
        result = ctx.call(
            "goncloud_local_run",
            {
                "request_key": "tg-4",
                "repository": "C:/repo",
                "instruction": "Inspect only.",
                "sources": [
                    {"reference": "owner", "observed_at": "2026-09-14T00:00:00Z"}
                ],
                "stop_after": "Scout",
            },
        )

        self.assertIn("error", result)
        # Creating first would leave a seven-card chain nobody drives and no
        # workflow id in the answer to reach it by.
        self.assertEqual(orchestrator.calls, [])

    def test_local_status_reads_the_driver_view(self):
        ctx = FakeContext(profile_name="claw")
        orchestrator = FakeOrchestrator()
        _PLUGIN._build_orchestrator = lambda context: orchestrator

        _PLUGIN.register(ctx)
        result = ctx.call("goncloud_local_status", {"workflow_id": "t_local"})

        self.assertEqual(result["outcome"], "running")
        self.assertEqual(orchestrator.calls, [("status", "t_local")])


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
        self.run_count = 0
        self.routing_source = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )

    def run(self, request, routes):
        self.run_count += 1
        self.requests.append((request, routes))
        return self.result

    def status(self, profile, **kwargs):
        return {"profile": profile, "routes": []}


class SubscriptionRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
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

    def assignment(self, started, **overrides):
        return payload(
            workflow_id=started["workflow_id"],
            task_id=started["stages"]["scout"],
            **{"repository": str(self.repo), **overrides},
        )

    def test_subscription_schema_matches_execution_step_bounds(self):
        schema = SUBSCRIPTION_RUN["parameters"]["properties"]["max_steps"]
        self.assertEqual((schema["minimum"], schema["maximum"]), (1, 200))
        started = self.start()
        self.service().subscription_run(
            **self.assignment(started, max_steps=schema["maximum"])
        )
        self.assertEqual(self.router.requests[0][0].max_steps, 200)
        self.assert_rejected_before_routing(
            self.service(), self.assignment(started, max_steps=201), "max_steps"
        )

    def changed_assignment_case(self, phase, change):
        repository = self.root / f"{phase}-{change}"
        repository.mkdir()
        started = self.service("claw").start(
            request_key=f"{phase}-{change}",
            repository=str(repository),
            revision="0123456789abcdef",
            instruction="Inspect.",
            sources=SOURCES,
        )
        return started, self.assignment(started, repository=str(repository))

    def change_authorization(self, started, change):
        with connect_closing(self.db_path) as conn:
            stage_id = started["stages"]["scout"]
            if change == "director-assignee":
                conn.execute(
                    "UPDATE tasks SET assignee='implementer' WHERE id=?",
                    (started["workflow_id"],),
                ).close()
            elif change.startswith("director-"):
                conn.execute(
                    "UPDATE tasks SET status=? WHERE id=?",
                    (change.removeprefix("director-"), started["workflow_id"]),
                ).close()
            elif change == "blocked":
                block_task(conn, stage_id, reason="Owner revoked authorization.")
            elif change == "assignee":
                conn.execute(
                    "UPDATE tasks SET assignee = ? WHERE id = ?",
                    ("implementer", stage_id),
                ).close()
            elif change == "repository":
                conn.execute(
                    "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                    (str(self.root), stage_id),
                ).close()
            else:
                root = self.workflow_module.get_task(conn, started["workflow_id"])
                contract = self.service()._contract_of(root)
                if change == "revision":
                    contract["revision"] = "new-approved-baseline"
                else:
                    contract["sources"] = [
                        {"reference": "Changed authority.", "observed_at": "2026-09-14"}
                    ]
                body = self.workflow_module._CONTRACT_MARKER + json.dumps(contract)
                conn.execute(
                    "UPDATE tasks SET body = ? WHERE id = ?", (body, root.id)
                ).close()
            conn.commit()

    def test_changes_during_compilation_revoke_before_router_run(self):
        compile_routes = self.workflow_module.compile_profile_routes
        for change in (
            "blocked",
            "assignee",
            "repository",
            "revision",
            "sources",
            "director-blocked",
            "director-assignee",
        ):
            with self.subTest(change=change):
                started, assignment = self.changed_assignment_case("compile", change)
                self.router.run_count = 0

                def compile_and_change(*args):
                    routes = compile_routes(*args)
                    self.change_authorization(started, change)
                    return routes

                with patch.object(
                    self.workflow_module, "compile_profile_routes", compile_and_change
                ):
                    with self.assertRaises(WorkflowError):
                        self.service().subscription_run(**assignment)
                self.assertEqual(self.router.run_count, 0)

    def test_changes_during_probe_leave_terminal_denial_and_release_lease(self):
        for change in (
            "blocked",
            "assignee",
            "repository",
            "revision",
            "sources",
            "director-blocked",
            "director-assignee",
        ):
            with self.subTest(change=change):
                started, assignment = self.changed_assignment_case("probe", change)
                invoked = []

                def probe(route):
                    self.change_authorization(started, change)
                    return AdapterHealth(ready=True, account_class="subscription")

                def invoke(request, route):
                    invoked.append(request)
                    return replace(
                        self.result, adapter=route.adapter, model=route.model
                    )

                router = SubscriptionRouter(
                    adapters={
                        "opencode_go": types.SimpleNamespace(probe=probe, invoke=invoke)
                    },
                    db_path=self.root / f"router-{change}.db",
                    routing_source=self.router.routing_source,
                )
                with patch.object(
                    self.workflow_module,
                    "_build_subscription_router",
                    return_value=router,
                ):
                    try:
                        result = self.service().subscription_run(**assignment)
                    except WorkflowError:
                        result = None  # Reassignment also forbids attaching to the old stage.
                self.assertEqual(invoked, [])
                if result is not None:
                    self.assertFalse(result["success"])
                    self.assertEqual(result["failure"], "invalid_request")
                attempts = router.store.stage_attempts(
                    started["workflow_id"], assignment["task_id"]
                )
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["status"], "completed")
                self.assertEqual(attempts[0]["failure"], "invalid_request")
                self.assertEqual(
                    attempts[0]["detail"],
                    "subscription assignment authorization changed; request refused",
                )
                health = router.store.health("opencode_go")
                self.assertTrue(health["ready"])
                self.assertEqual(health["detail"], "ready")
                self.assertEqual(health["account_class"], "subscription")
                self.assertIsNone(router.store.cooldown_until("opencode_go"))
                self.assertTrue(
                    router.store.claim_stage(
                        started["workflow_id"], assignment["task_id"], "next-owner", 10
                    )
                )
                router.store.release_stage(
                    started["workflow_id"], assignment["task_id"], "next-owner"
                )

    def test_unchanged_authorization_releases_kanban_transaction_before_invoke(self):
        started = self.start()
        invoked = []

        def invoke(request, route):
            # This separate writer fails immediately if the authorization read
            # holds a Kanban transaction across inference.
            with closing(sqlite3.connect(self.db_path, timeout=0)) as conn:
                conn.execute("BEGIN IMMEDIATE").close()
                conn.rollback()
            self.assertNotIn("pre_invoke", repr(request))
            invoked.append(request)
            return replace(self.result, adapter=route.adapter, model=route.model)

        router = SubscriptionRouter(
            adapters={
                "opencode_go": types.SimpleNamespace(
                    probe=lambda route: AdapterHealth(
                        ready=True, account_class="subscription"
                    ),
                    invoke=invoke,
                )
            },
            db_path=self.root / "authorized-router.db",
            routing_source=self.router.routing_source,
        )
        with patch.object(
            self.workflow_module, "_build_subscription_router", return_value=router
        ):
            result = self.service().subscription_run(**self.assignment(started))
        self.assertTrue(result["success"])
        self.assertEqual(len(invoked), 1)

    def assert_rejected_before_routing(self, service, assignment, message):
        self.router.run_count = 0
        with patch.object(
            self.workflow_module, "compile_profile_routes"
        ) as compile_routes:
            with self.assertRaisesRegex(WorkflowError, message):
                service.subscription_run(**assignment)
            compile_routes.assert_not_called()
        self.assertEqual(self.router.run_count, 0)

    def test_foreign_workflow_stage_is_rejected_before_routes_are_compiled(self):
        started = self.start()
        other_repo = self.root / "other"
        other_repo.mkdir()
        foreign = self.service("claw").start(
            request_key="other",
            repository=str(other_repo),
            revision="0123456789abcdef",
            instruction="Other task.",
            sources=SOURCES,
        )
        assignment = self.assignment(started, repository=str(other_repo))
        assignment["task_id"] = foreign["stages"]["scout"]
        self.assert_rejected_before_routing(
            self.service(), assignment, "not a stage of workflow"
        )

    def test_all_assignment_mismatches_precede_route_compilation(self):
        started = self.start()
        cases = [
            ("unknown", {}, "unknown Goncloud role"),
            ("implementer", {}, "not assigned"),
            ("scout", {"workflow_id": started["stages"]["scout"]}, "not a Goncloud"),
            ("scout", {"repository": str(self.root)}, "workspace"),
            ("scout", {"revision": "different"}, "revision"),
            ("scout", {"sources": []}, "sources"),
            ("scout", {"sources": [{"reference": "x"}]}, "observed_at"),
        ]
        for role, overrides, message in cases:
            with self.subTest(role=role, overrides=overrides):
                assignment = self.assignment(started)
                assignment.update(overrides)
                self.assert_rejected_before_routing(
                    self.service(role), assignment, message
                )

    def test_non_executable_stages_are_rejected_before_routing(self):
        started = self.start()
        assignment = self.assignment(started)
        assignment["task_id"] = started["stages"]["ingenieria"]
        self.assert_rejected_before_routing(
            self.service("ingenieria"), assignment, "status"
        )
        assignment["task_id"] = started["stages"]["scout"]
        with connect_closing(self.db_path) as conn:
            block_task(conn, assignment["task_id"], reason="owner paused")
        self.assert_rejected_before_routing(self.service(), assignment, "status")

    def test_revoked_director_is_rejected_by_initial_snapshot_before_any_effect(self):
        for change in (
            "director-blocked",
            "director-done",
            "director-archived",
            "director-assignee",
        ):
            with self.subTest(change=change):
                started, assignment = self.changed_assignment_case("initial", change)
                self.change_authorization(started, change)
                self.assert_rejected_before_routing(
                    self.service(), assignment, "director"
                )

    def test_contract_repository_mismatch_is_rejected_before_routing(self):
        started = self.start()
        with connect_closing(self.db_path) as conn:
            root = self.workflow_module.get_task(conn, started["workflow_id"])
            contract = self.workflow_module._CONTRACT_MARKER + json.dumps(
                {
                    **self.service()._contract_of(root),
                    "repository": str(self.root),
                }
            )
            conn.execute("UPDATE tasks SET body = ? WHERE id = ?", (contract, root.id))
            conn.commit()
        self.assert_rejected_before_routing(
            self.service(), self.assignment(started), "repository"
        )

    def test_root_workspace_mismatch_is_rejected_before_routing(self):
        started = self.start()
        with connect_closing(self.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET workspace_path = ? WHERE id = ?",
                (str(self.root), started["workflow_id"]),
            ).close()
            conn.commit()
        self.assert_rejected_before_routing(
            self.service(), self.assignment(started), "workspace"
        )

    def test_step_limit_endpoints_are_accepted(self):
        started = self.start()
        for value in (1, 200):
            with self.subTest(value=value):
                self.service().subscription_run(
                    **self.assignment(started, max_steps=value)
                )
                self.assertEqual(self.router.requests[-1][0].max_steps, value)

    def test_public_subscription_run_redacts_before_return_sqlite_and_status_without_transform(
        self,
    ):
        from subscriptions.process import run_cli

        sensitive = (
            "opaque-inherited-fixture",
            "sk-synthetic-pattern",
            "ghp_syntheticfixture0123456789ab",
            "private synthetic assignment",
        )

        def invoke(assignment, route):
            def runner(command, **kwargs):
                return subprocess.CompletedProcess(
                    command, 0, stdout=" ".join(sensitive), stderr=""
                )

            evidence = run_cli(
                ["synthetic-vendor"], "", assignment.workspace, 5, runner=runner
            )
            return InvocationResult(
                True,
                evidence.stdout,
                route.adapter,
                route.model,
                route.billing_mode,
                session_id=sensitive[0],
            )

        started = self.start()
        router = SubscriptionRouter(
            adapters={
                "opencode_go": types.SimpleNamespace(
                    probe=lambda route: AdapterHealth(True, "subscription"),
                    invoke=invoke,
                )
            },
            db_path=self.root / "redaction-router.db",
            routing_source=self.router.routing_source,
        )
        with (
            patch.object(
                self.workflow_module, "_build_subscription_router", return_value=router
            ),
            patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive[0]}),
        ):
            result = self.service().subscription_run(
                **self.assignment(started, instruction=sensitive[-1])
            )
        status = router.status(
            "scout",
            workflow_id=started["workflow_id"],
            stage_id=started["stages"]["scout"],
        )
        with closing(sqlite3.connect(router.store.db_path)) as conn:
            persisted = conn.execute(
                "SELECT detail, session_id FROM subscription_attempts"
            ).fetchall()
        self.assertTrue(result["success"])
        self.assertTrue(
            all(value not in repr((result, persisted, status)) for value in sensitive),
            "public evidence exposed synthetic sensitive content",
        )

    def test_public_subscription_run_sanitizes_a_result_created_before_environment_scope(
        self,
    ):
        sensitive = "opaque-delayed-fixture"
        self.router.result = replace(self.result, text=sensitive, session_id=sensitive)
        started = self.start()
        with patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive}):
            result = self.service().subscription_run(**self.assignment(started))
        self.assertTrue(
            sensitive not in repr(result),
            "public scripted result exposed a synthetic secret",
        )

    def test_completed_stage_is_not_executed_again(self):
        started = self.start()
        with connect_closing(self.db_path) as conn:
            complete_task(conn, started["stages"]["scout"], summary="done")
        self.assert_rejected_before_routing(
            self.service(), self.assignment(started), "status"
        )

    def test_ready_corrective_stage_keeps_membership_and_step_limit(self):
        started = self.start()
        with connect_closing(self.db_path) as conn:
            for role in ("scout", "ingenieria", "implementer", "verifier", "adversary"):
                complete_task(conn, started["stages"][role], summary="done")
        correction = self.service("reviewer").review(
            workflow_id=started["workflow_id"],
            task_id=started["stages"]["reviewer"],
            decision="reject",
            severity="high",
            summary="Fix race.",
        )
        assignment = self.assignment(started, max_steps=17)
        assignment["task_id"] = correction["corrective_stages"]["implementer"]
        result = self.service("implementer").subscription_run(**assignment)
        self.assertTrue(result["success"])
        self.assertEqual(self.router.requests[0][0].max_steps, 17)

    def test_max_steps_reaches_request_and_bounded_prompt(self):
        started = self.start()
        self.service().subscription_run(**self.assignment(started, max_steps=17))
        request, _ = self.router.requests[0]
        self.assertEqual(getattr(request, "max_steps", None), 17)
        self.assertIn("Maximum autonomous steps: 17", build_assignment_prompt(request))
        continued = replace(request, mode="continue", current_revision="next")
        self.assertIn(
            "Maximum autonomous steps: 17", build_assignment_prompt(continued)
        )

    def test_invalid_step_limits_are_rejected_before_routing(self):
        started = self.start()
        for value in (0, 201, 500, True, False, 1.5, "17", "17; whoami", None):
            with self.subTest(value=value):
                self.assert_rejected_before_routing(
                    self.service(),
                    self.assignment(started, max_steps=value),
                    "max_steps",
                )

    def test_direct_request_rejects_invalid_step_limits(self):
        for value in (0, 201, True, False, 1.5, "17", "17; whoami", None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_steps"):
                    InvocationRequest(
                        workflow_id="wf",
                        stage_id="stage",
                        role="scout",
                        prompt="inspect",
                        workspace=self.repo,
                        start_revision="rev",
                        current_revision="rev",
                        max_steps=value,
                    )

    def test_attach_remains_defensive_and_idempotent(self):
        started = self.start()
        evidence = {
            "workflow_id": started["workflow_id"],
            "stage_id": started["stages"]["scout"],
            "profile": "scout",
            "success": True,
        }
        service = self.service()
        self.assertTrue(service.attach_subscription_result(evidence)["attached"])
        self.assertTrue(service.attach_subscription_result(evidence)["duplicate"])
        with connect_closing(self.db_path) as conn:
            records = [
                c
                for c in list_comments(conn, evidence["stage_id"])
                if c.body.startswith("GONCLOUD_SUBSCRIPTION")
            ]
        self.assertEqual(len(records), 1)
        evidence["profile"] = "implementer"
        with self.assertRaisesRegex(WorkflowError, "stage identity"):
            service.attach_subscription_result(evidence)

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
        self.assertEqual(routes[1].adapter, "opencode_go_2")
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


class RecordingContext(FakeContext):
    def __init__(self, profile_name):
        super().__init__(profile_name)
        self.hooks = {}

    def register_hook(self, event, handler):
        self.hooks[event] = handler


class ReviewerCompletionGuardTests(unittest.TestCase):
    """The only hook the plugin registers must fail closed for reviewer cards."""

    def setUp(self):
        self.calls = []
        tests = self

        class FakeWorkflowService:
            def __init__(self, *, actor_role):
                self.actor_role = actor_role

            def guard_native_completion(self, task_id):
                tests.calls.append((self.actor_role, task_id))
                return {"action": "block", "message": "blocked"}

        self.addCleanup(patch.stopall)
        patch.object(_PLUGIN, "WorkflowService", FakeWorkflowService).start()
        patch.dict(os.environ, {}, clear=False).start()
        os.environ.pop("HERMES_KANBAN_TASK", None)

    def hook(self, profile_name):
        ctx = RecordingContext(profile_name)
        _PLUGIN.register(ctx)
        return ctx.hooks["pre_tool_call"]

    def test_reviewer_completion_without_a_resolvable_id_is_blocked(self):
        hook = self.hook("reviewer")
        for args, kwargs in (
            ({}, {}),
            ({"task_id": "   "}, {}),
            ({"card_id": "t_1"}, {"task_id": ""}),
        ):
            with self.subTest(args=args, kwargs=kwargs):
                verdict = hook("kanban_complete", args, **kwargs)
                self.assertEqual(verdict["action"], "block")
        self.assertEqual(self.calls, [])

    def test_resolved_ids_reach_the_durable_guard_and_other_roles_stay_outside(
        self,
    ):
        hook = self.hook("reviewer")
        self.assertEqual(hook("kanban_complete", {"task_id": "t_1"})["action"], "block")
        self.assertEqual(hook("kanban_complete", {}, task_id="t_2")["action"], "block")
        with patch.dict(os.environ, {"HERMES_KANBAN_TASK": "t_3"}):
            self.assertEqual(hook("kanban_complete", {})["action"], "block")
        self.assertEqual(
            self.calls, [("reviewer", "t_1"), ("reviewer", "t_2"), ("reviewer", "t_3")]
        )
        self.assertIsNone(hook("kanban_list", {}))
        # Other roles and ordinary cards are deliberately outside the policy.
        self.assertIsNone(self.hook("implementer")("kanban_complete", {}))


if __name__ == "__main__":
    unittest.main()
