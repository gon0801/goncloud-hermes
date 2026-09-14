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
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path.home() / "AppData/Local/hermes/hermes-agent"))
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))

from hermes_cli.kanban_db import (  # noqa: E402
    complete_task,
    get_task,
    list_runs,
    list_tasks,
)
from hermes_cli.kanban_db_connect import connect_closing  # noqa: E402
from workflow import WorkflowError, WorkflowService  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.types import AdapterHealth, InvocationResult  # noqa: E402
from local_orchestrator import LocalOrchestrator, parse_terminal_envelope  # noqa: E402
from tests.test_subscription_router import cleanup_temp_directory  # noqa: E402

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
