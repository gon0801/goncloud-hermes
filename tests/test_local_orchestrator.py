from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path.home() / "AppData/Local/hermes/hermes-agent"))
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
sys.path.insert(0, str(ROOT / "tests"))

from hermes_cli.kanban_db import (  # noqa: E402
    complete_task,
    get_task,
    list_runs,
    list_tasks,
)
from hermes_cli.kanban_db_connect import connect_closing  # noqa: E402
from workflow import WorkflowError, WorkflowService  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.types import AdapterHealth, FailureKind, InvocationResult  # noqa: E402
from local_orchestrator import LocalOrchestrator, parse_terminal_envelope  # noqa: E402
import local_orchestrator as local_orchestrator_module  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

SOURCES = [{"reference": "owner-approved local test", "observed_at": "2026-09-14"}]
ORDER = [
    "scout",
    "ingenieria",
    "implementer",
    "verifier",
    "adversary",
    "reviewer",
    "entrega",
]


def envelope(
    *, status="completed", review=None, summary="Evidence verified.", evidence=None
):
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence or [],
        "review": review,
    }


def fenced(value):
    return "```json\n" + json.dumps(value) + "\n```"


class TerminalParserTests(unittest.TestCase):
    def test_only_last_complete_json_fence_has_authority(self):
        approved = envelope(review={"decision": "approve", "severity": None})
        rejected = envelope(review={"decision": "reject", "severity": "high"})
        self.assertEqual(
            parse_terminal_envelope(
                fenced(approved) + "\n" + fenced(rejected), role="reviewer"
            ),
            rejected,
        )

    def test_prose_glued_to_the_opening_fence_is_still_the_terminal(self):
        # Vendors differ on whether they break the line before the fence, and a
        # missing newline is not evidence that the stage failed.
        expected = envelope(summary="Plan ready.", evidence=["base revision clean"])
        for text in (
            "Plan on the way." + fenced(expected),
            "Plan on the way. " + fenced(expected),
            "Plan on the way.\n" + fenced(expected),
        ):
            with self.subTest(text=text):
                self.assertEqual(
                    parse_terminal_envelope(text, role="ingenieria"), expected
                )

    def test_a_glued_fence_still_obeys_every_other_rule(self):
        valid = envelope()
        for text in (
            "Prose." + fenced(valid) + "\nAPPROVED",
            "Prose." + "```json\n" + json.dumps({"status": "completed"}) + "\n```",
            "Prose." + "```python\n{}\n```",
        ):
            with self.subTest(text=text):
                with self.assertRaises(WorkflowError):
                    parse_terminal_envelope(text, role="ingenieria")

    def test_malformed_trailing_multiple_objects_and_wrong_fences_fail_closed(self):
        valid = fenced(envelope())
        for text in (
            "",
            "{}",
            valid + "\nAPPROVED",
            valid + "\n```json\n{",
            "```python\n{}\n```",
            "```json\n{} {}\n```",
            "```json\n[]\n```",
            "```json\n{bad}\n```",
            valid + "\n```text\nOK\n```",
            valid + "\n```json\n{}\n```",
        ):
            with self.subTest(text=text):
                with self.assertRaises(WorkflowError):
                    parse_terminal_envelope(text, role="scout")

    def test_schema_types_duplicate_keys_and_nonfinite_values_are_rejected(self):
        invalid = [
            envelope(status=True),
            envelope(status="approve"),
            envelope(summary=1),
            envelope(summary=" "),
            envelope(evidence="command"),
            envelope(evidence=[1]),
            envelope(review={"decision": "approve", "severity": None}),
            {**envelope(), "extra": "ignored"},
            {"status": "completed"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(WorkflowError):
                parse_terminal_envelope(fenced(value), role="scout")
        for text in (
            '{"status":"blocked","status":"completed","summary":"x","evidence":[],"review":null}',
            '{"status":"completed","summary":NaN,"evidence":[],"review":null}',
        ):
            with self.assertRaises(WorkflowError):
                parse_terminal_envelope("```json\n" + text + "\n```", role="scout")

    def test_reviewer_requires_exact_decision_severity_contract(self):
        for review in (
            None,
            True,
            {},
            {"decision": "approve"},
            {"decision": "approve", "severity": "high"},
            {"decision": "reject", "severity": None},
            {"decision": "APPROVE", "severity": None},
            {"decision": "approve", "severity": None, "approved": True},
        ):
            with self.subTest(review=review), self.assertRaises(WorkflowError):
                parse_terminal_envelope(
                    fenced(envelope(review=review)), role="reviewer"
                )

    def test_input_and_envelope_caps_reject_without_echoing_data(self):
        for text in (
            "x" * 100000 + fenced(envelope()),
            fenced(envelope(summary="secret-marker" * 1000)),
            fenced(envelope(evidence=["x"] * 100)),
        ):
            with self.assertRaises(WorkflowError) as error:
                parse_terminal_envelope(text, role="scout")
            self.assertNotIn("secret-marker", str(error.exception))

    def test_redaction_cannot_shrink_original_input_envelope_or_field_limits(self):
        credential = "\u00e9" * 100
        cases = (
            ("input", "sk-fixture" * 3000 + "\n" + fenced(envelope())),
            ("envelope", fenced(envelope(evidence=[credential] * 8))),
            ("summary", fenced(envelope(summary="sk-fixture" * 50))),
            ("evidence", fenced(envelope(evidence=["sk-fixture" * 13]))),
        )
        with patch.dict(os.environ, {"SERVICE_PASSWORD": credential}):
            for name, text in cases:
                with self.subTest(limit=name):
                    with self.assertRaises(WorkflowError):
                        parse_terminal_envelope(text, role="scout")
                    result = InvocationResult(
                        True, text, "fixture", "fixture", "subscription"
                    )
                    with self.assertRaises(WorkflowError):
                        parse_terminal_envelope(result.text, role="scout")


class TransportEntryPointTests(unittest.TestCase):
    """A messaging transport creates the durable chain and detaches the driver.

    The gateway must answer at once with a workflow id, never block for the
    whole chain, and never put the assignment in a command line.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db = self.root / "kanban.db"
        self.stage_calls = []
        self.orchestrator = LocalOrchestrator(
            service_factory=lambda role: WorkflowService(
                actor_role=role, db_path=self.db
            ),
            stage_runner=lambda service, **assignment: self.stage_calls.append(
                assignment
            ),
        )

    def assignment(self, **overrides):
        base = dict(
            request_key="tg-42",
            repository=str(self.repo),
            revision="baseline",
            instruction="Inspect the repository. Do not modify files.",
            sources=SOURCES,
            max_steps=20,
        )
        base.update(overrides)
        return base

    def test_create_registers_the_chain_without_running_a_single_stage(self):
        created = self.orchestrator.create(**self.assignment())
        again = self.orchestrator.create(**self.assignment())

        self.assertEqual(created["workflow_id"], again["workflow_id"])
        self.assertEqual(self.stage_calls, [])
        status = self.orchestrator.status(created["workflow_id"])
        self.assertEqual(
            {stage["status"] for stage in status["stages"].values()} - {"ready"},
            {"todo"},
        )

    def test_create_names_a_missing_repository_before_anything_else(self):
        # The director mangled a path once and got "revision is required"
        # back, because the revision was derived from a directory that does
        # not exist. The repository is checked first and named in the error.
        with self.assertRaisesRegex(WorkflowError, "repository") as raised:
            self.orchestrator.create(
                **self.assignment(
                    repository=str(self.root / "missing" / "repo"), revision=None
                )
            )

        self.assertNotIn("revision", str(raised.exception))
        self.assertEqual(self.stage_calls, [])

    def test_create_keeps_the_local_entry_point_bounds(self):
        with self.assertRaises(WorkflowError):
            self.orchestrator.create(**self.assignment(max_steps=0))
        with self.assertRaises(WorkflowError):
            self.orchestrator.create(**self.assignment(instruction="x" * 40000))
        self.assertEqual(self.stage_calls, [])

    def test_launch_detached_never_exposes_the_assignment_and_detaches(self):
        created = self.orchestrator.create(**self.assignment())
        launched = {}

        def fake_popen(argv, **kwargs):
            launched["argv"] = list(argv)
            launched["kwargs"] = kwargs
            return type("P", (), {"pid": 4242})()

        with patch("local_orchestrator.subprocess.Popen", fake_popen):
            result = self.orchestrator.launch_detached(
                created["workflow_id"], home=self.root
            )

        self.assertEqual(result["workflow_id"], created["workflow_id"])
        self.assertEqual(result["driver_pid"], 4242)
        argv_text = " ".join(launched["argv"])
        self.assertNotIn("Inspect the repository", argv_text)
        self.assertIn(created["workflow_id"], argv_text)
        kwargs = launched["kwargs"]
        self.assertIsNone(kwargs.get("stdin"))
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
        else:
            self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(str(result["result_file"]).startswith(str(self.root)))

    def test_launch_detached_carries_a_stop_after_stage_to_the_driver(self):
        # A read-only inspection stops after scout instead of running seven
        # stages; the stage name is an identifier, so argv may carry it.
        created = self.orchestrator.create(**self.assignment())
        launched = {}

        def fake_popen(argv, **kwargs):
            launched["argv"] = list(argv)
            return type("P", (), {"pid": 1})()

        with patch("local_orchestrator.subprocess.Popen", fake_popen):
            self.orchestrator.launch_detached(
                created["workflow_id"], home=self.root, stop_after="scout"
            )
            with self.assertRaises(WorkflowError):
                self.orchestrator.launch_detached(
                    created["workflow_id"], home=self.root, stop_after="nope"
                )

        self.assertEqual(launched["argv"][-1], "scout")

    def test_create_derives_the_revision_from_the_expanded_repository_path(self):
        home = self.root / "home"
        repo = home / "repo"
        repo.mkdir(parents=True)
        for command in (
            ["git", "init", "-q"],
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=fixture@example.test",
                "commit",
                "-q",
                "--allow-empty",
                "-m",
                "baseline",
            ],
        ):
            subprocess.run(command, cwd=repo, check=True, capture_output=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        # The path the owner typed passes validation once expanded; deriving
        # the revision from the raw form surfaced as "revision is required".
        with patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}):
            created = self.orchestrator.create(
                **self.assignment(repository="~/repo", revision=None)
            )

        contract = WorkflowService(actor_role="claw", db_path=self.db).start_contract(
            request_key="tg-42"
        )
        self.assertIn("scout", created["stages"])
        self.assertEqual(contract["revision"], head)

    def test_malformed_workflow_ids_never_reach_the_driver_or_the_result_path(self):
        with patch.object(local_orchestrator_module.subprocess, "Popen") as popen:
            for bad in ("../escape", "t_1/../x", "", "t 1", ".hidden", "a" * 200):
                with self.subTest(workflow_id=bad):
                    with self.assertRaises(WorkflowError):
                        self.orchestrator.driver_result(bad, home=self.root)
                    with self.assertRaises(WorkflowError):
                        self.orchestrator.launch_detached(bad, home=self.root)
        popen.assert_not_called()
        self.assertFalse((self.root / "goncloud-local").exists())


class LocalOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db = self.root / "kanban.db"
        self.requests = []
        self.responses = {}
        self.reviews = []
        self.git = type(
            "Git",
            (),
            {
                "current_revision": lambda _, workspace: "baseline",
                "resolve_revision": lambda _, workspace, revision: "baseline",
                "changed_files": lambda _, workspace: (),
                "mutation_since": lambda _, workspace, revision: (
                    "baseline",
                    (),
                    False,
                ),
            },
        )()
        self.router = SubscriptionRouter(
            adapters={"opencode_go": self},
            db_path=self.root / "routes.db",
            git=self.git,
        )
        self.addCleanup(patch.stopall)
        patch("workflow._build_subscription_router", return_value=self.router).start()
        self.orchestrator = LocalOrchestrator(service_factory=self.service)

    def service(self, role="claw"):
        return WorkflowService(actor_role=role, db_path=self.db)

    def probe(self, route):
        return AdapterHealth(True, "subscription")

    def invoke(self, request, route):
        self.requests.append(request)
        review = None
        if request.role == "reviewer":
            review = (
                self.reviews.pop(0)
                if self.reviews
                else {"decision": "approve", "severity": None}
            )
        text = self.responses.get(request.role, fenced(envelope(review=review)))
        if isinstance(text, Exception):
            raise text
        return InvocationResult(
            True, text, route.adapter, route.model, route.billing_mode
        )

    def start(self, **kwargs):
        return self.orchestrator.start(
            **{
                "request_key": "local-1",
                "repository": str(self.repo),
                "revision": "baseline",
                "instruction": "Inspect this repository without external actions.",
                "sources": SOURCES,
                "max_steps": 17,
                **kwargs,
            }
        )

    def test_exact_order_and_durable_delivery_evidence(self):
        result = self.start()
        self.assertEqual([r.role for r in self.requests], ORDER)
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["root"]["status"], "done")
        self.assertEqual(result["stages"]["reviewer"]["review"]["decision"], "approve")
        for stage in result["stages"].values():
            self.assertEqual(stage["status"], "done")
            self.assertEqual(stage["attempts"][0]["slot"], 0)
            self.assertEqual(stage["attempts"][0]["billing_mode"], "subscription")
        self.assertEqual(
            self.orchestrator.resume(result["workflow_id"])["outcome"], "delivered"
        )
        self.assertEqual(len(self.requests), 7)

    def test_cli_reviewer_envelope_without_successful_vendor_terminal_cannot_approve(
        self,
    ):
        from subscriptions.adapters.codex import CodexAdapter
        from subscriptions.adapters.grok import GrokAdapter

        approved = fenced(envelope(review={"decision": "approve", "severity": None}))
        for index, (adapter_type, events) in enumerate(
            (
                (
                    CodexAdapter,
                    [
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": approved},
                        }
                    ],
                ),
                (
                    CodexAdapter,
                    [
                        {
                            "type": "item.completed",
                            "item": {"type": "agent_message", "text": approved},
                        },
                        {"type": "turn.failed"},
                    ],
                ),
                (GrokAdapter, [{"type": "text", "data": approved}]),
                (
                    GrokAdapter,
                    [
                        {"type": "text", "data": approved},
                        {"type": "end", "stopReason": "error"},
                    ],
                ),
            )
        ):
            with self.subTest(adapter=adapter_type.__name__, case=index):
                self.db = self.root / f"invalid-cli-review-{index}.db"
                self.router.adapters["opencode_go"] = self
                first = self.start(
                    request_key=f"invalid-cli-review-{index}", stop_after="adversary"
                )

                def runner(command, **kwargs):
                    return subprocess.CompletedProcess(
                        command,
                        0,
                        stdout="\n".join(json.dumps(event) for event in events),
                        stderr="",
                    )

                self.router.adapters["opencode_go"] = adapter_type(runner=runner)
                result = self.orchestrator.resume(first["workflow_id"])
                self.assertEqual(result["outcome"], "blocked")
                self.assertNotEqual(result["stages"]["reviewer"]["status"], "done")
                self.assertNotEqual(
                    (result["stages"]["reviewer"].get("review") or {}).get("decision"),
                    "approve",
                )
                self.assertNotEqual(result["stages"]["entrega"]["status"], "done")

    def test_legacy_handoff_is_normalized_before_status_and_downstream_prompt(self):
        first = self.start(stop_after="scout")
        metadata = {
            "goncloud_local": {
                "terminal": envelope(summary="sk-legacy-private owner@example.test"),
                "attempts": [],
            }
        }
        with connect_closing(self.db) as conn:
            conn.execute(
                "UPDATE task_runs SET metadata=? WHERE task_id=?",
                (json.dumps(metadata), first["stages"]["scout"]["id"]),
            ).close()
            conn.commit()
        status = self.orchestrator.status(first["workflow_id"])
        self.orchestrator.resume(first["workflow_id"], stop_after="ingenieria")
        public = repr(status) + self.requests[-1].prompt
        self.assertNotIn("sk-legacy-private", public)
        self.assertNotIn("owner@example.test", public)

    def test_blocked_reviewer_retains_terminal_evidence_in_status(self):
        self.responses["reviewer"] = fenced(
            envelope(
                review={"decision": "reject", "severity": "medium"},
                evidence=["Review reproduction: failing_case.py:8"],
            )
        )
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(
            result["stages"]["reviewer"]["terminal"]["evidence"],
            ["Review reproduction: failing_case.py:8"],
        )

    def test_owner_block_during_invocation_cannot_be_completed(self):
        original = self.invoke

        def block_during_invoke(request, route):
            result = original(request, route)
            with connect_closing(self.db) as conn:
                conn.execute(
                    "UPDATE tasks SET status='blocked' WHERE id=?", (request.stage_id,)
                ).close()
                conn.commit()
            return result

        with patch.object(self, "invoke", side_effect=block_during_invoke):
            with self.assertRaises(WorkflowError):
                self.start()
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, self.requests[0].stage_id).status, "blocked"
            )

    def test_status_change_between_check_and_completion_is_fenced(self):
        import local_orchestrator as local

        native = local.complete_task

        def block_before_update(conn, task_id, **kwargs):
            with connect_closing(self.db) as other:
                other.execute(
                    "UPDATE tasks SET status='blocked' WHERE id=?", (task_id,)
                ).close()
                other.commit()
            return native(conn, task_id, **kwargs)

        with patch.object(local, "complete_task", side_effect=block_before_update):
            with self.assertRaises(WorkflowError):
                self.start()
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, self.requests[0].stage_id).status, "blocked"
            )

    def prepare_unexecuted_workflow(self):
        with patch.object(self.orchestrator, "resume", return_value={}):
            self.start()
        with connect_closing(self.db) as conn:
            root = next(task for task in list_tasks(conn) if task.assignee == "claw")
        return self.service().status(workflow_id=root.id)

    def test_cached_direct_service_success_is_normalized_before_resume_handoff(self):
        initial = self.prepare_unexecuted_workflow()
        self.responses["scout"] = fenced(
            envelope(
                summary="sk-cached-private owner@example.test",
                evidence=["validator.py:42"],
            )
        )
        self.service("scout").subscription_run(
            workflow_id=initial["workflow_id"],
            task_id=initial["stages"]["scout"]["id"],
            instruction="Inspect.",
            repository=str(self.repo),
            revision="baseline",
            sources=SOURCES,
            max_steps=17,
        )
        result = self.orchestrator.resume(
            initial["workflow_id"], stop_after="ingenieria"
        )
        self.assertEqual([r.role for r in self.requests], ["scout", "ingenieria"])
        public = (
            repr(result)
            + self.requests[-1].prompt
            + repr(
                self.router.store.stage_attempts(
                    initial["workflow_id"], initial["stages"]["scout"]["id"]
                )
            )
        )
        self.assertNotIn("sk-cached-private", public)
        self.assertNotIn("owner@example.test", public)

    def test_cached_malformed_success_becomes_durable_failure_without_reinvocation(
        self,
    ):
        initial = self.prepare_unexecuted_workflow()
        self.responses["scout"] = "sk-cached-private owner@example.test"
        self.service("scout").subscription_run(
            workflow_id=initial["workflow_id"],
            task_id=initial["stages"]["scout"]["id"],
            instruction="Inspect.",
            repository=str(self.repo),
            revision="baseline",
            sources=SOURCES,
            max_steps=17,
        )
        result = self.orchestrator.resume(initial["workflow_id"])
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(self.requests), 1)
        attempts = self.router.store.stage_attempts(
            initial["workflow_id"], initial["stages"]["scout"]["id"]
        )
        self.assertFalse(attempts[0]["success"])
        self.assertNotIn("sk-cached-private", repr(attempts))

    def test_a_failed_stage_keeps_the_adapter_failure_kind_as_evidence(self):
        """Overwriting the adapter text erases which route failed and why.

        The adapter's own failure text is synthetic, built from the route and
        the failure kind, so it carries no vendor output to leak. Replacing it
        with a fixed string left the durable row unable to tell a bad terminal
        envelope apart from a dead account.
        """
        transform = local_orchestrator_module._terminal_transform(
            "scout", "the instruction"
        )
        rejected = InvocationResult(
            success=False,
            text="opencode_go invocation failed (invalid_terminal)",
            adapter="opencode_go",
            model="deepseek-v4.1-flash",
            billing_mode="subscription",
            session_id="sess_private",
            failure=FailureKind.INVALID_TERMINAL,
        )

        kept = transform(rejected)

        self.assertIn("invalid_terminal", kept.text)
        self.assertIn("opencode_go", kept.text)
        self.assertEqual(kept.session_id, "")

    def test_terminal_task_failure_survives_crash_without_reinvocation(self):
        self.responses["scout"] = "malformed"
        with patch.object(
            self.orchestrator, "_finish_stage", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.start()
        request = self.requests[0]
        result = self.orchestrator.resume(request.workflow_id)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(
            len(
                self.router.store.stage_attempts(request.workflow_id, request.stage_id)
            ),
            1,
        )

    def test_exhausted_advancing_failure_survives_crash_without_reinvocation(self):
        original = self.invoke

        def timeout(request, route):
            return replace(
                original(request, route),
                success=False,
                failure=FailureKind.TIMEOUT,
                text="private timeout",
            )

        with (
            patch.object(self, "invoke", side_effect=timeout),
            patch.object(
                self.orchestrator, "_finish_stage", side_effect=KeyboardInterrupt
            ),
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.start()
        request = self.requests[0]
        self.router.store.save_health(
            "opencode_go", ready=True, account_class="subscription"
        )
        result = self.orchestrator.resume(request.workflow_id)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(self.requests), 1)

    def test_crash_between_advancing_slots_resumes_at_next_slot(self):
        # Scout F1 is the Z.AI Coding Plan served through the OpenCode CLI.
        self.router.adapters["opencode_zai"] = self
        original, availability = self.invoke, self.router._availability
        called = []

        def invoke(request, route):
            called.append(route.adapter)
            result = original(request, route)
            return (
                replace(result, success=False, failure=FailureKind.TIMEOUT)
                if route.adapter == "opencode_go"
                else result
            )

        def interrupt(route):
            if route.adapter == "opencode_zai":
                raise KeyboardInterrupt
            return availability(route)

        with patch.object(self, "invoke", side_effect=invoke):
            with patch.object(self.router, "_availability", side_effect=interrupt):
                with self.assertRaises(KeyboardInterrupt):
                    self.start()
            self.router.store.save_health(
                "opencode_go", ready=True, account_class="subscription"
            )
            result = self.orchestrator.resume(
                self.requests[0].workflow_id, stop_after="scout"
            )
        self.assertEqual(result["outcome"], "paused")
        self.assertEqual(called, ["opencode_go", "opencode_zai"])

    def test_interrupted_attempt_can_resume_with_a_new_attempt(self):
        original = self.invoke

        def interrupt(request, route):
            original(request, route)
            raise KeyboardInterrupt

        with patch.object(self, "invoke", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.start()
        result = self.orchestrator.resume(
            self.requests[0].workflow_id, stop_after="scout"
        )
        self.assertEqual(result["outcome"], "paused")
        self.assertEqual(len(self.requests), 2)

    def test_downstream_assignment_contains_durable_predecessor_evidence(self):
        self.responses["scout"] = fenced(
            envelope(
                summary="Found the invariant in validator.py.",
                evidence=["validator.py:42"],
            )
        )
        first = self.start(stop_after="scout")
        LocalOrchestrator(service_factory=self.service).resume(
            first["workflow_id"], stop_after="ingenieria"
        )
        self.assertIn("Found the invariant in validator.py.", self.requests[-1].prompt)
        self.assertIn("validator.py:42", self.requests[-1].prompt)

    def test_adapter_and_transform_exceptions_are_durable_sanitized_failures(self):
        self.responses["scout"] = RuntimeError(
            "raw-error sk-private-marker owner@example.test"
        )
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        attempt = self.router.store.stage_attempts(
            result["workflow_id"], result["stages"]["scout"]["id"]
        )[0]
        self.assertEqual(attempt["status"], "completed")
        self.assertEqual(attempt["failure"], "task_failure")
        self.assertNotIn("raw-error", repr(attempt))
        self.assertNotIn("sk-private-marker", repr(attempt))

    def test_medium_review_rejection_never_creates_a_corrective_round(self):
        self.reviews = [{"decision": "reject", "severity": "medium"}]
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["corrective_stages"], {})
        self.assertEqual([r.role for r in self.requests], ORDER[:-1])

    def test_resume_at_an_already_completed_checkpoint_does_not_advance(self):
        first = self.start(stop_after="scout")
        result = self.orchestrator.resume(first["workflow_id"], stop_after="scout")
        self.assertEqual(result["outcome"], "paused")
        self.assertEqual([r.role for r in self.requests], ["scout"])

    def test_changed_step_limit_is_rejected_without_resuming(self):
        self.start(stop_after="scout")
        with self.assertRaises(WorkflowError):
            self.start(max_steps=18)
        self.assertEqual([r.role for r in self.requests], ["scout"])

    def test_unfinished_peer_dependency_prevents_next_stage_execution(self):
        first = self.start(stop_after="scout")
        self.service("ingenieria").consult(
            task_id=first["stages"]["ingenieria"]["id"],
            target_role="scout",
            question="Verify dependency evidence.",
            request_key="peer-1",
        )
        result = self.orchestrator.resume(first["workflow_id"])
        self.assertEqual(result["outcome"], "waiting")
        self.assertEqual([r.role for r in self.requests], ["scout"])

    def test_blocked_director_is_visible_and_never_resumed(self):
        first = self.start(stop_after="scout")
        with connect_closing(self.db) as conn:
            # An operator can pause the director while it waits on children;
            # native block_task only accepts running/ready cards.
            conn.execute(
                "UPDATE tasks SET status=? WHERE id=?",
                ("blocked", first["workflow_id"]),
            ).close()
            conn.commit()
        self.assertEqual(
            self.orchestrator.status(first["workflow_id"])["outcome"], "blocked"
        )
        self.assertEqual(
            self.orchestrator.resume(first["workflow_id"])["outcome"], "blocked"
        )
        self.assertEqual([r.role for r in self.requests], ["scout"])

    def test_reassignment_during_execution_cannot_be_closed_by_previous_role(self):
        original = self.invoke

        def reassigned(request, route):
            result = original(request, route)
            with connect_closing(self.db) as conn:
                conn.execute(
                    "UPDATE tasks SET assignee=? WHERE id=?",
                    ("ingenieria", request.stage_id),
                ).close()
                conn.commit()
            return result

        with patch.object(self, "invoke", side_effect=reassigned):
            with self.assertRaises(WorkflowError):
                self.start(stop_after="scout")
        with connect_closing(self.db) as conn:
            stage = get_task(conn, self.requests[0].stage_id)
            self.assertEqual(stage.status, "ready")
            self.assertEqual(stage.assignee, "ingenieria")

    def test_reassignment_between_check_and_completion_is_fenced(self):
        import local_orchestrator as local

        native = local.complete_task

        def changed(conn, task_id, **kwargs):
            with connect_closing(self.db) as other:
                other.execute(
                    "UPDATE tasks SET assignee=? WHERE id=?", ("ingenieria", task_id)
                ).close()
                other.commit()
            return native(conn, task_id, **kwargs)

        with patch.object(local, "complete_task", side_effect=changed):
            with self.assertRaises(WorkflowError):
                self.start(stop_after="scout")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, self.requests[0].stage_id).status, "ready")

    def test_reassignment_between_check_and_block_is_fenced(self):
        import local_orchestrator as local

        native = local.block_task
        self.responses["scout"] = fenced(envelope(status="blocked"))

        def changed(conn, task_id, **kwargs):
            with connect_closing(self.db) as other:
                other.execute(
                    "UPDATE tasks SET assignee=? WHERE id=?", ("ingenieria", task_id)
                ).close()
                other.commit()
            return native(conn, task_id, **kwargs)

        with patch.object(local, "block_task", side_effect=changed):
            with self.assertRaises(WorkflowError):
                self.start()
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, self.requests[0].stage_id).status, "ready")

    def test_root_close_fences_reassigned_director(self):
        import local_orchestrator as local

        native = local.complete_task

        def changed(conn, task_id, **kwargs):
            if get_task(conn, task_id).assignee == "claw":
                with connect_closing(self.db) as other:
                    other.execute(
                        "UPDATE tasks SET assignee=? WHERE id=?",
                        ("operaciones", task_id),
                    ).close()
                    other.commit()
            return native(conn, task_id, **kwargs)

        with patch.object(local, "complete_task", side_effect=changed):
            with self.assertRaises(WorkflowError):
                self.start()
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, self.requests[0].workflow_id).status, "ready"
            )

    def test_root_close_fences_removed_review_approval(self):
        import local_orchestrator as local

        native = local.complete_task

        def changed(conn, task_id, **kwargs):
            if get_task(conn, task_id).assignee == "claw":
                reviewer = next(r for r in self.requests if r.role == "reviewer")
                with connect_closing(self.db) as other:
                    other.execute(
                        "UPDATE task_runs SET metadata='{}' WHERE task_id=?",
                        (reviewer.stage_id,),
                    ).close()
                    other.commit()
            return native(conn, task_id, **kwargs)

        with patch.object(local, "complete_task", side_effect=changed):
            with self.assertRaises(WorkflowError):
                self.start()

    def _assert_review_history_change_fenced(
        self, target_role, *, change="insert", decision="reject"
    ):
        import local_orchestrator as local

        first = self.start(stop_after="reviewer")
        reviewer_id = first["stages"]["reviewer"]["id"]
        with connect_closing(self.db) as conn:
            record = self.service()._recorded_review_run(conn, reviewer_id)
            latest = max(run.started_at for run in list_runs(conn, reviewer_id))
            record = {
                **record,
                "decision": decision,
                "severity": "high" if decision == "reject" else None,
                "result": {
                    **record["result"],
                    "outcome": "blocked" if decision == "reject" else "approved",
                },
            }
            metadata = json.dumps({"goncloud_review": record})
            run_id = None
            if change in {"reorder", "close"}:
                cursor = conn.execute(
                    "INSERT INTO task_runs (task_id,status,started_at,ended_at,metadata) VALUES (?,?,?,?,?)",
                    (
                        reviewer_id,
                        "done",
                        latest - 1 if change == "reorder" else latest + 1,
                        latest if change == "reorder" else None,
                        metadata,
                    ),
                )
                run_id = cursor.lastrowid
                cursor.close()
                conn.commit()
            self.assertEqual(
                self.service()._recorded_review_run(conn, reviewer_id)["decision"],
                "approve",
            )
        native = local.complete_task
        interleaved = []

        def changed(conn, task_id, **kwargs):
            if get_task(conn, task_id).assignee == target_role:
                with connect_closing(self.db) as other:
                    if change == "insert":
                        # Same timestamp: the higher run ID determines authority.
                        other.execute(
                            "INSERT INTO task_runs (task_id,status,started_at,ended_at,metadata) VALUES (?,?,?,?,?)",
                            (reviewer_id, "done", latest, latest, metadata),
                        ).close()
                    elif change == "reorder":
                        other.execute(
                            "UPDATE task_runs SET started_at=? WHERE id=?",
                            (latest + 1, run_id),
                        ).close()
                    else:
                        other.execute(
                            "UPDATE task_runs SET ended_at=? WHERE id=?",
                            (latest, run_id),
                        ).close()
                    other.commit()
                    self.assertEqual(
                        self.service()._recorded_review_run(other, reviewer_id)[
                            "decision"
                        ],
                        decision,
                    )
                interleaved.append(task_id)
            return native(conn, task_id, **kwargs)

        with patch.object(local, "complete_task", side_effect=changed):
            with self.assertRaisesRegex(WorkflowError, "authorization changed"):
                self.orchestrator.resume(first["workflow_id"])
        self.assertEqual(len(interleaved), 1)
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, interleaved[0]).status, "ready")
            self.assertNotEqual(get_task(conn, first["workflow_id"]).status, "done")
        self.assertNotEqual(
            self.orchestrator.status(first["workflow_id"])["outcome"], "delivered"
        )

    def test_delivery_fences_new_rejection_with_same_timestamp(self):
        self._assert_review_history_change_fenced("entrega")

    def test_root_close_fences_new_rejection_with_same_timestamp(self):
        self._assert_review_history_change_fenced("claw")

    def test_delivery_fences_new_approval_with_same_timestamp(self):
        self._assert_review_history_change_fenced("entrega", decision="approve")

    def test_root_close_fences_new_approval_with_same_timestamp(self):
        self._assert_review_history_change_fenced("claw", decision="approve")

    def test_delivery_fences_changed_approve_reject_order(self):
        self._assert_review_history_change_fenced("entrega", change="reorder")

    def test_root_close_fences_changed_approve_reject_order(self):
        self._assert_review_history_change_fenced("claw", change="reorder")

    def test_delivery_fences_newly_closed_rejection(self):
        self._assert_review_history_change_fenced("entrega", change="close")

    def test_root_close_fences_newly_closed_rejection(self):
        self._assert_review_history_change_fenced("claw", change="close")

    def test_review_fence_treats_large_metadata_as_data(self):
        first = self.start(stop_after="reviewer")
        reviewer_id = first["stages"]["reviewer"]["id"]
        with connect_closing(self.db) as conn:
            earliest = min(run.started_at for run in list_runs(conn, reviewer_id))
            conn.execute(
                "INSERT INTO task_runs (task_id,status,started_at,ended_at,metadata) VALUES (?,?,?,?,?)",
                (
                    reviewer_id,
                    "done",
                    earliest - 1,
                    earliest,
                    json.dumps({"untrusted": "'); DROP TABLE tasks; --" * 10000}),
                ),
            ).close()
            conn.commit()
        self.assertEqual(
            self.orchestrator.resume(first["workflow_id"])["outcome"], "delivered"
        )
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "done")

    def test_real_cli_status_and_completed_resume_use_persisted_state(self):
        first = self.start()
        environment = {
            **os.environ,
            "HERMES_KANBAN_DB": str(self.db),
            "GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db"),
        }
        for command in ("status", "resume"):
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/run_local_workflow.py"),
                    command,
                    "--workflow-id",
                    first["workflow_id"],
                    "--json",
                ],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(json.loads(result.stdout)["outcome"], "delivered")
            self.assertNotIn("Inspect this repository", result.stdout)

    def test_real_cli_repeated_start_reuses_implicit_sources_and_revision(self):
        first = self.start(stop_after="scout")
        instruction = self.root / "instruction.txt"
        instruction.write_text(
            "Inspect this repository without external actions.", encoding="utf-8"
        )
        environment = {
            **os.environ,
            "HERMES_KANBAN_DB": str(self.db),
            "GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db"),
        }
        argv = [
            sys.executable,
            str(ROOT / "scripts/run_local_workflow.py"),
            "start",
            "--repository",
            str(self.repo),
            "--request-key",
            "local-1",
            "--instruction-file",
            str(instruction),
            "--max-steps",
            "17",
            "--stop-after",
            "scout",
            "--json",
        ]
        for _ in range(2):
            result = subprocess.run(
                argv, env=environment, capture_output=True, text=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            self.assertEqual(
                json.loads(result.stdout)["workflow_id"], first["workflow_id"]
            )
            self.assertEqual(json.loads(result.stdout)["outcome"], "paused")
        result = subprocess.run(
            argv + ["--revision", "changed"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 2)

    def test_reviewer_evidence_survives_review_and_reaches_corrective_implementer(self):
        self.reviews = [
            {"decision": "reject", "severity": "high"},
            {"decision": "approve", "severity": None},
        ]
        original = self.invoke

        def with_evidence(request, route):
            result = original(request, route)
            if (
                request.role == "reviewer"
                and sum(r.role == "reviewer" for r in self.requests) == 1
            ):
                result = replace(
                    result,
                    text=fenced(
                        envelope(
                            summary="Fix the documented race.",
                            review={"decision": "reject", "severity": "high"},
                            evidence=["Proof: concurrency_case.py:17"],
                        )
                    ),
                )
            return result

        with patch.object(self, "invoke", side_effect=with_evidence):
            result = self.start()
        correction = [r for r in self.requests if r.role == "implementer"][1]
        self.assertIn("Proof: concurrency_case.py:17", correction.prompt)
        self.assertEqual(
            result["stages"]["reviewer"]["terminal"]["evidence"],
            ["Proof: concurrency_case.py:17"],
        )

    def test_fresh_instance_resumes_without_repeating_scout_or_losing_limit(self):
        first = self.start(stop_after="scout")
        self.assertEqual(first["outcome"], "paused")
        self.assertEqual([r.role for r in self.requests], ["scout"])
        result = LocalOrchestrator(service_factory=self.service).resume(
            first["workflow_id"]
        )
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual([r.role for r in self.requests], ORDER)
        self.assertEqual({r.max_steps for r in self.requests}, {17})
        self.assertTrue(
            all(
                "Inspect this repository without external actions." in r.prompt
                for r in self.requests
            )
        )

    def test_completed_reviewer_without_service_review_never_releases_delivery(self):
        first = self.start(stop_after="adversary")
        with connect_closing(self.db) as conn:
            self.assertTrue(
                complete_task(conn, first["stages"]["reviewer"]["id"], summary="bypass")
            )
        result = self.orchestrator.resume(first["workflow_id"])
        self.assertEqual(result["outcome"], "blocked")
        self.assertNotIn("entrega", [r.role for r in self.requests])
        observed = self.orchestrator.status(first["workflow_id"])
        self.assertEqual(observed["outcome"], "blocked")
        self.assertTrue(observed["delivery_blocked"])

    def test_exit_zero_without_reviewer_decision_blocks(self):
        self.responses["reviewer"] = fenced(envelope())
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(result["stages"]["reviewer"]["status"], "blocked")
        self.assertNotIn("entrega", [r.role for r in self.requests])

    def test_high_rejection_opens_exactly_one_round_then_delivers(self):
        self.reviews = [
            {"decision": "reject", "severity": "high"},
            {"decision": "approve", "severity": None},
        ]
        result = self.start()
        self.assertEqual(
            [r.role for r in self.requests],
            ORDER[:-1]
            + ["implementer", "verifier", "adversary", "reviewer", "entrega"],
        )
        self.assertEqual(result["outcome"], "delivered")
        with connect_closing(self.db) as conn:
            self.assertEqual(len(list_tasks(conn)), 12)
            run = list_runs(conn, result["corrective_stages"]["reviewer"]["id"])[-1]
            self.assertEqual(run.metadata["goncloud_review"]["decision"], "approve")

    def test_second_high_rejection_blocks_without_third_round_or_retry(self):
        self.reviews = [{"decision": "reject", "severity": "high"}] * 2
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(self.requests), 10)
        self.orchestrator.resume(result["workflow_id"])
        self.assertEqual(len(self.requests), 10)
        with connect_closing(self.db) as conn:
            self.assertEqual(len(list_tasks(conn)), 12)

    def test_blocked_stage_stops_the_loop_and_resume(self):
        self.responses["scout"] = fenced(envelope(status="blocked"))
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual([r.role for r in self.requests], ["scout"])
        self.orchestrator.resume(result["workflow_id"])
        self.assertEqual(len(self.requests), 1)

    def test_final_envelope_after_2000_chars_survives_restart_before_completion(self):
        self.responses["scout"] = "raw-log-secret " * 300 + "\n" + fenced(envelope())
        with patch.object(
            self.orchestrator, "_finish_stage", side_effect=KeyboardInterrupt
        ):
            with self.assertRaises(KeyboardInterrupt):
                self.start(stop_after="scout")
        with connect_closing(self.db) as conn:
            root = next(task for task in list_tasks(conn) if task.assignee == "claw")
        result = LocalOrchestrator(service_factory=self.service).resume(root.id)
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual([r.role for r in self.requests], ORDER)
        attempts = self.router.store.stage_attempts(
            root.id, result["stages"]["scout"]["id"]
        )
        self.assertNotIn("raw-log-secret", repr(attempts))
        self.assertLess(len(attempts[0]["detail"]), 2000)

    def test_oversize_results_block_without_raw_evidence(self):
        self.responses["scout"] = fenced(envelope(summary="sk-private-marker" * 2000))
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        attempts = self.router.store.stage_attempts(
            result["workflow_id"], result["stages"]["scout"]["id"]
        )
        self.assertNotIn("sk-private-marker", repr(attempts) + repr(result))

    def test_status_and_durable_terminal_do_not_expose_prompt_or_credentials(self):
        instruction = "PRIVATE INSTRUCTION MUST NOT BE ECHOED"
        self.responses["scout"] = fenced(
            envelope(
                summary="token sk-private-marker owner@example.test",
                evidence=[instruction],
            )
        )
        result = self.start(instruction=instruction, stop_after="scout")
        result = self.orchestrator.status(result["workflow_id"])
        attempts = self.router.store.stage_attempts(
            result["workflow_id"], result["stages"]["scout"]["id"]
        )
        public = repr(result) + repr(attempts)
        for secret in (instruction, "sk-private-marker", "owner@example.test"):
            self.assertNotIn(secret, public)

    def test_invalid_assignment_and_stop_role_never_invoke_vendor(self):
        for overrides in (
            {"max_steps": True},
            {"max_steps": 201},
            {"sources": []},
            {"repository": "relative"},
            {"revision": ""},
            {"stop_after": "claw"},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(WorkflowError):
                self.start(**overrides)
        self.assertEqual(self.requests, [])


class LocalCliTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "run_local_workflow", ROOT / "scripts/run_local_workflow.py"
        )
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)

    def test_stdin_is_forwarded_privately_and_stdout_is_clean_json(self):
        calls = []

        class Orchestrator:
            def start(self, **kwargs):
                calls.append(kwargs)
                print("raw vendor output sk-private")
                print("raw stderr sk-private", file=sys.stderr)
                return {"workflow_id": "wf", "outcome": "paused"}

        out, err = io.StringIO(), io.StringIO()
        argv = [
            "start",
            "--repository",
            str(ROOT),
            "--revision",
            "baseline",
            "--instruction-file",
            "-",
            "--stop-after",
            "scout",
            "--json",
        ]
        with (
            redirect_stdout(out),
            redirect_stderr(err),
            patch("sys.stdin", io.StringIO("PRIVATE INSTRUCTION")),
        ):
            code = self.cli.main(argv, orchestrator_factory=Orchestrator)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["instruction"], "PRIVATE INSTRUCTION")
        self.assertEqual(
            json.loads(out.getvalue()), {"workflow_id": "wf", "outcome": "paused"}
        )
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn("PRIVATE INSTRUCTION", repr(argv))

    def test_cli_argument_and_runtime_errors_never_echo_values(self):
        for argv in (
            ["start", "--instruction", "sk-private"],
            ["status", "--workflow-id", "sk-private"],
        ):

            class Orchestrator:
                def status(self, workflow_id):
                    raise RuntimeError("sk-private owner@example.test")

            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = self.cli.main(argv, orchestrator_factory=Orchestrator)
            self.assertNotEqual(code, 0)
            self.assertEqual(json.loads(out.getvalue())["outcome"], "error")
            self.assertNotIn("sk-private", out.getvalue() + err.getvalue())

    def test_explicit_null_sources_file_is_rejected(self):
        calls = []

        class Orchestrator:
            def start(self, **kwargs):
                calls.append(kwargs)
                return {"outcome": "paused"}

        with tempfile.TemporaryDirectory() as directory:
            sources = Path(directory) / "sources.json"
            sources.write_text("null", encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out), patch("sys.stdin", io.StringIO("Inspect.")):
                code = self.cli.main(
                    [
                        "start",
                        "--repository",
                        str(ROOT),
                        "--revision",
                        "baseline",
                        "--sources-file",
                        str(sources),
                    ],
                    orchestrator_factory=Orchestrator,
                )
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_instruction_file_and_sources_are_read_without_argument_echo(self):
        calls = []

        class Orchestrator:
            def start(self, **kwargs):
                calls.append(kwargs)
                return {"outcome": "paused"}

        with tempfile.TemporaryDirectory() as directory:
            instruction_path = Path(directory) / "assignment.txt"
            instruction_path.write_text(
                "PRIVATE FILE INSTRUCTION", encoding="utf-8-sig"
            )
            sources_path = Path(directory) / "sources.json"
            sources_path.write_text(json.dumps(SOURCES), encoding="utf-8")
            argv = [
                "start",
                "--repository",
                str(ROOT),
                "--revision",
                "baseline",
                "--instruction-file",
                str(instruction_path),
                "--sources-file",
                str(sources_path),
            ]
            out = io.StringIO()
            with redirect_stdout(out):
                code = self.cli.main(argv, orchestrator_factory=Orchestrator)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["instruction"], "PRIVATE FILE INSTRUCTION")
        self.assertEqual(calls[0]["sources"], SOURCES)
        self.assertNotIn("PRIVATE FILE INSTRUCTION", out.getvalue() + repr(argv))


if __name__ == "__main__":
    unittest.main()
