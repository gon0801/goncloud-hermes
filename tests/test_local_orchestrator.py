from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path.home() / "AppData/Local/hermes/hermes-agent"))
sys.path.insert(0, str(ROOT / "plugins/goncloud-team"))
sys.path.insert(0, str(ROOT / "tests"))

import local_orchestrator as local_orchestrator_module  # noqa: E402
import entrega_pr as entrega_pr_module  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402
from hermes_cli.kanban_db import (  # noqa: E402
    add_comment,
    archive_task,
    complete_task,
    create_task,
    get_task,
    list_comments,
    list_events,
    list_runs,
    list_tasks,
)
from hermes_cli.kanban_db_connect import connect_closing, write_txn  # noqa: E402
from local_orchestrator import (  # noqa: E402
    LocalOrchestrator,
    close_hint,
    parse_terminal_envelope,
)
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.state import SubscriptionStateStore  # noqa: E402
from subscriptions.types import (  # noqa: E402
    AdapterHealth,
    FailureKind,
    InvocationResult,
)
from workflow import WorkflowError, WorkflowService  # noqa: E402

SOURCES = [{"reference": "owner-approved local test", "observed_at": "2026-09-14"}]


def _plugin_package():
    """The plugin package, loadable by name (its directory carries a hyphen)."""
    import importlib.util

    name = "goncloud_team"
    package = sys.modules.get(name)
    if package is None:
        spec = importlib.util.spec_from_file_location(
            name,
            str(ROOT / "plugins/goncloud-team/__init__.py"),
            submodule_search_locations=[str(ROOT / "plugins/goncloud-team")],
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[name] = package
        spec.loader.exec_module(package)
    return package


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


# The test's own routing table: every position of every orchestrator stage
# lane rides the one adapter the fake registry serves, so a slot/position
# assertion keeps meaning "this position ran". Against the live table the
# fake registry holds none of the bound adapters and every position is
# skipped, which is why 26 tests failed after the v6 table landed (#24).
SOURCE = {
    "profile_names": {"dirigir-claw": "claw"},
    "profile_inheritance": {
        "claw": "dirigir-claw",
        "entrega": "dirigir",
        "ingenieria": "planear",
        "reviewer": "planear",
        "implementer": "codigo",
        "verifier": "verificar",
        "scout": "investigar",
        "adversary": "adversary",
    },
    # The v6 lane shape per stage (efforts included), rebound to the one
    # adapter under test: the deepseek family rides opencode_go.
    "chains": {
        "dirigir": [
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
        ],
        "dirigir-claw": [
            "opencode-go/deepseek-v4.1-flash (medium)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
        ],
        "planear": [
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
        ],
        "codigo": [
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (medium)",
        ],
        "verificar": [
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
        ],
        "investigar": [
            "opencode-go/deepseek-v4.1-flash (medium)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (medium)",
        ],
        "adversary": [
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
            "opencode-go/deepseek-v4.1-flash (high)",
        ],
    },
}


class StageInstructionTests(unittest.TestCase):
    def test_each_stage_is_told_the_instruction_is_the_chain_goal(self):
        # Live 2026-09-28 (t_1dbd51ce): scout read "create hola.txt and commit",
        # correctly did not implement, and then returned blocked, which stopped
        # the whole chain. Completing its own duty is scout's success.
        for role in ORDER:
            with self.subTest(role=role):
                text = LocalOrchestrator._instruction(
                    "Create hola.txt and commit.", "Do your stage.", role
                )
                self.assertIn("goal of the whole chain", text)
                self.assertIn("completed once your own stage duties are done", text)
                self.assertIn("blocked only when you cannot do your own stage", text)


class TerminalFieldTrimTests(unittest.TestCase):
    def test_a_slightly_long_field_is_trimmed_not_rejected(self):
        # Live 2026-09-28 (t_e02d3646): Go 2's correct scout report had a
        # 454-char summary against the 400 cap and was thrown away, sending the
        # stage to a model that implemented instead of investigating.
        long_report = envelope(
            summary="s" * 454,
            evidence=["e" * 130] + [f"item {n}" for n in range(9)],
        )
        parsed, _ = local_orchestrator_module._normalized_terminal(
            fenced(long_report), "scout", ""
        )

        self.assertEqual(parsed["status"], "completed")
        self.assertLessEqual(len(parsed["summary"]), 400)
        self.assertTrue(parsed["summary"].startswith("s" * 390))
        self.assertEqual(len(parsed["evidence"]), 8)
        self.assertLessEqual(len(parsed["evidence"][0]), 120)

    def test_the_total_size_cap_still_rejects(self):
        with self.assertRaises(Exception):
            local_orchestrator_module._normalized_terminal(
                fenced(envelope(summary="x" * 5000)), "scout", ""
            )


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
            with self.subTest(text=text), self.assertRaises(WorkflowError):
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
            with self.subTest(text=text), self.assertRaises(WorkflowError):
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
        ):
            with self.assertRaises(WorkflowError) as error:
                parse_terminal_envelope(text, role="scout")
            self.assertNotIn("secret-marker", str(error.exception))

    def test_redaction_cannot_shrink_original_input_envelope_or_field_limits(self):
        credential = "\u00e9" * 100
        cases = (
            ("input", "sk-fixture" * 3000 + "\n" + fenced(envelope())),
            ("envelope", fenced(envelope(evidence=[credential] * 8))),
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

    def test_over_long_fields_are_redacted_before_they_are_trimmed(self):
        # Trimming first could cut a credential mid-token, and the fragment
        # would no longer match the exact-value redaction pass.
        credential = "opaque-credential-fixture-value"
        text = fenced(
            envelope(
                summary="x" * 380 + credential + "y" * 40,
                evidence=["z" * 100 + credential],
            )
        )
        with patch.dict(os.environ, {"SERVICE_PASSWORD": credential}):
            parsed, _ = local_orchestrator_module._normalized_terminal(
                text, "scout", ""
            )
        joined = parsed["summary"] + "".join(parsed["evidence"])
        self.assertNotIn("opaque-cred", joined)
        self.assertLessEqual(len(parsed["summary"]), 400)
        self.assertLessEqual(len(parsed["evidence"][0]), 120)


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
        # stages; the stage name is an identifier, so argv may carry it. The
        # launch's run id rides argv too (the driver uses it to recognize
        # its own launch's mark).
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

        self.assertEqual(launched["argv"][-2], "scout")
        self.assertTrue(launched["argv"][-1])

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


class DriverBootstrapTests(unittest.TestCase):
    """Run the real detached-driver bootstrap the way launch_detached does.

    Live 2026-10-17 (t_220bd43c): after Hermes moved its third-party packages
    behind hermes_bootstrap, the isolated (-I) driver died importing the
    plugin, wrote no result, and every chain stayed at zero attempts.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)
        self.hermes = self.root / "hermes"
        self.plugin = self.root / "plugin"
        self.deps = self.root / "deps"
        for directory in (self.hermes, self.plugin, self.deps):
            directory.mkdir()
        # El buzon de error del bootstrap usa el helper compartido del
        # plugin; el stub lo lleva junto a su local_orchestrator.
        (self.plugin / "carpetas.py").write_bytes(
            (ROOT / "plugins/goncloud-team/carpetas.py").read_bytes()
        )
        self.out = self.root / "result.json"

    def run_driver(self, *extra):
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                local_orchestrator_module._DRIVER_BOOTSTRAP,
                str(self.plugin),
                str(self.hermes),
                "t_boot",
                str(self.out),
                *extra,
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def write_plugin(self, header):
        (self.plugin / "local_orchestrator.py").write_text(
            header + "class LocalOrchestrator:\n"
            "    def resume(self, workflow_id, stop_after=None, launch_run_id=None):\n"
            "        return {'workflow_id': workflow_id, 'outcome': 'completed',\n"
            "                'stop_after': stop_after, 'launch_run_id': launch_run_id}\n",
            encoding="utf-8",
        )

    def test_driver_activates_hermes_dependencies_before_loading_the_plugin(self):
        # The plugin needs a package only hermes_bootstrap puts on sys.path,
        # exactly like kanban_db -> hermes_yaml -> ruamel in the live failure.
        (self.deps / "only_after_bootstrap.py").write_text("VALUE = 1\n")
        (self.hermes / "hermes_bootstrap.py").write_text(
            f"import sys\nsys.path.insert(0, {str(self.deps)!r})\n"
        )
        self.write_plugin("import only_after_bootstrap\n")

        # Live F1: a healthy run used to die on result['error'] (KeyError)
        # after publishing its result; the exit must prove it didn't.
        run = self.run_driver("scout")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        result = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(result["stop_after"], "scout")

    def test_an_unloadable_driver_still_leaves_an_error_result(self):
        # Failing to load must surface in goncloud_local_status as an error,
        # never as a driver that vanished with the workflow still pending.
        cases = {
            "plugin import fails": ("import missing_vendor_package\n", None),
            "bootstrap import fails": (
                "",
                "import missing_vendor_package\n",
            ),
        }
        for name, (plugin_header, bootstrap) in cases.items():
            with self.subTest(name):
                self.out.unlink(missing_ok=True)
                boot = self.hermes / "hermes_bootstrap.py"
                boot.unlink(missing_ok=True)
                if bootstrap is not None:
                    boot.write_text(bootstrap)
                self.write_plugin(plugin_header)

                self.run_driver()

                result = json.loads(self.out.read_text(encoding="utf-8"))
                self.assertEqual(result["outcome"], "error")
                self.assertEqual(result["workflow_id"], "t_boot")

    def test_an_unloadable_driver_also_leaves_the_mailbox_error_event(self):
        # Encargo H: the error stop reaches claw through the mailbox too.
        # The run never started, so the run segment is the fixed "boot".
        # The out path mirrors the production shape (nested under
        # goncloud-local) because the bootstrap derives the mailbox from it.
        self.out = self.root / "goncloud-local" / "t_boot.json"
        # Production always launches into a directory the running mark
        # already created; the bootstrap never had to make it.
        self.out.parent.mkdir()
        avisos = self.root / "goncloud-local" / "avisos"

        self.run_driver()

        self.assertEqual(
            [p.name for p in sorted(avisos.iterdir())],
            ["t_boot-boot-error.json"],
        )
        event = json.loads(
            (avisos / "t_boot-boot-error.json").read_text(encoding="utf-8")
        )
        self.assertEqual(event["workflow_id"], "t_boot")
        self.assertEqual(event["outcome"], "error")
        self.assertEqual(event["motivo"], "local driver failed; check durable status")
        self.assertTrue(event["fecha"])

        # Lead r3 (blocker): a delivered boot error brakes nothing. claw got
        # the news and relaunched — every later broken launch of the chain
        # must speak up again, or it stays silent forever after its first
        # notice. (Reverses cr 4312376346: the only brake is an event still
        # waiting unread; despertar delivers through the real
        # entregar_aviso.)
        despertar = importlib.import_module("raices.despertar")
        despertar.entregar_aviso(avisos / "t_boot-boot-error.json")
        self.out.unlink(missing_ok=True)

        self.run_driver()

        self.assertEqual(
            [p.name for p in sorted(avisos.iterdir())],
            ["t_boot-boot-error.entregado", "t_boot-boot-error.json"],
        )

        # A third broken launch while claw has not read the pending event
        # stays one event: same name, overwritten in place, no pile-up.
        self.out.unlink(missing_ok=True)
        self.run_driver()

        self.assertEqual(
            [p.name for p in sorted(avisos.iterdir())],
            ["t_boot-boot-error.entregado", "t_boot-boot-error.json"],
        )

    def test_a_healthy_delivered_run_ends_clean_with_its_result_and_aviso(self):
        # Live F1 (High): the bootstrap read result['error'] after EVERY
        # run, so the healthy delivered run — whose report carries no
        # 'error' key — died on KeyError right after publishing its result,
        # with a traceback and exit code 1. run_driver() discarded the
        # CompletedProcess, so the crash never showed. The whole real
        # bootstrap (sys.executable, the same argv launch_detached builds,
        # #44 style) must end without exception, keep the delivered result,
        # and keep the mailbox event the run's own write path published —
        # with no boot-error event, which only a bootstrap that died may
        # write. The stub resume ends the way the real _report does: the
        # result file and the event through the real module's writers.
        self.out = self.root / "goncloud-local" / "t_boot.json"
        self.out.parent.mkdir()
        real = ROOT / "plugins" / "goncloud-team"
        (self.plugin / "local_orchestrator.py").write_text(
            "import importlib.util\n"
            "import sys\n"
            f"sys.path.insert(0, {str(real)!r})\n"
            "spec = importlib.util.spec_from_file_location(\n"
            f"    'driver_real', {str(real / 'local_orchestrator.py')!r})\n"
            "driver = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(driver)\n"
            "\n"
            "\n"
            "class LocalOrchestrator:\n"
            "    def resume(self, workflow_id, stop_after=None, launch_run_id=None):\n"
            "        report = {'workflow_id': workflow_id, 'outcome': 'delivered',\n"
            "                  'run_id': 'feedc0de', 'stages': {}}\n"
            "        driver._write_result(workflow_id, report, home="
            f"{str(self.root)!r})\n"
            "        driver._write_aviso(\n"
            "            workflow_id, 'feedc0de', 'delivered', report,\n"
            f"            home={str(self.root)!r}, repo='', instruction='')\n"
            "        return report\n",
            encoding="utf-8",
        )

        run = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                local_orchestrator_module._DRIVER_BOOTSTRAP,
                str(self.plugin),
                str(ROOT / "tests" / "fake_hermes_cli"),
                "t_boot",
                str(self.out),
            ],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=60,
        )

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        result = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(result["outcome"], "delivered")
        self.assertNotIn("error", result)
        avisos = self.root / "goncloud-local" / "avisos"
        self.assertEqual(
            [p.name for p in sorted(avisos.iterdir())],
            ["t_boot-feedc0de-delivered.json"],
        )
        event = json.loads(
            (avisos / "t_boot-feedc0de-delivered.json").read_text(encoding="utf-8")
        )
        self.assertEqual(event["outcome"], "delivered")

    def test_a_hermes_tree_without_a_bootstrap_still_drives(self):
        # Older Hermes trees have no hermes_bootstrap; the driver must not
        # turn its absence into a failure. Live F1: the exit must prove the
        # healthy run ends clean, not crashed on result['error'].
        self.write_plugin("")

        run = self.run_driver()

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stderr, "")
        result = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(result["outcome"], "completed")


class CarpetasPrivadasTests(unittest.TestCase):
    """H-carpetas: en Windows, mkdir(mode=0o700) deja la carpeta ilegible
    para el gateway (DAC protegida sin herencia desde Python 3.13): el cron
    despertar-raices veia el buzon VACIO y los avisos nunca llegaron a claw.
    En Windows las carpetas privadas se crean sin mode (heredan la DACL del
    padre); en POSIX conservan 0o700. Un solo helper para todos los sitios.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)

    def carpetas(self):
        import carpetas

        return importlib.reload(carpetas)

    def test_en_windows_la_carpeta_se_crea_sin_mode(self):
        carpetas = self.carpetas()
        ruta = self.root / "avisos"
        stub = Mock()
        stub.name = "nt"
        with (
            patch.object(carpetas, "os", stub),
            patch.object(Path, "mkdir", autospec=True) as mkdir,
        ):
            carpetas.mkdir_privado(ruta)

        # Sin mode: la carpeta hereda la DACL del padre.
        mkdir.assert_called_once_with(ruta, parents=True, exist_ok=True)

    def test_en_posix_la_carpeta_conserva_0o700(self):
        carpetas = self.carpetas()
        ruta = self.root / "avisos"
        stub = Mock()
        stub.name = "posix"
        with (
            patch.object(carpetas, "os", stub),
            patch.object(Path, "mkdir", autospec=True) as mkdir,
        ):
            carpetas.mkdir_privado(ruta)

        mkdir.assert_called_once_with(ruta, mode=0o700, parents=True, exist_ok=True)

    def test_write_result_y_write_aviso_usan_el_helper(self):
        with patch.object(
            local_orchestrator_module,
            "mkdir_privado",
            side_effect=local_orchestrator_module.mkdir_privado,
        ) as carpeta:
            local_orchestrator_module._write_result(
                "t_carpetas", {"outcome": "running"}, home=self.root
            )
            local_orchestrator_module._write_aviso(
                "t_carpetas",
                "run-1",
                "error",
                {"outcome": "error", "workflow_id": "t_carpetas"},
                home=self.root,
            )

        self.assertEqual(carpeta.call_count, 2)
        self.assertTrue((self.root / "goncloud-local" / "t_carpetas.json").is_file())
        self.assertEqual(
            [
                p.name
                for p in sorted((self.root / "goncloud-local" / "avisos").iterdir())
            ],
            ["t_carpetas-run-1-error.json"],
        )


class _OrchestratorHarness(unittest.TestCase):
    """Shared mount: SOURCE table, fake adapter, fake git, patched router."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(cleanup_temp_directory, self.tmp)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.db = self.root / "kanban.db"
        # Live review F9: without this, close and close_hint resolve the
        # machine's real subscription store through default_db_path() and
        # the suite runs its migrations and locks the live router. The
        # isolated store is the same one the patched router below uses.
        subscriptions = patch.dict(
            os.environ, {"GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db")}
        )
        subscriptions.start()
        self.addCleanup(subscriptions.stop)
        # The mailbox (encargo H) and the result file follow HERMES_HOME when
        # resume runs without an explicit home; pin it to the mount so a run
        # never drops either into the machine's real Hermes home.
        hermes_home = patch.dict(os.environ, {"HERMES_HOME": str(self.root)})
        hermes_home.start()
        self.addCleanup(hermes_home.stop)
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
                "tree_fingerprint": lambda _, workspace: ("baseline", ()),
            },
        )()
        self.router = SubscriptionRouter(
            adapters={"opencode_go": self},
            db_path=self.root / "routes.db",
            routing_source=SOURCE,
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

    def write_driver_outcome(self, workflow_id, outcome):
        """The result file the detached driver writes when it stops."""
        results = self.root / "goncloud-local"
        results.mkdir(exist_ok=True)
        (results / f"{workflow_id}.json").write_text(
            json.dumps({"workflow_id": workflow_id, "outcome": outcome}),
            encoding="utf-8",
        )

    def write_mark(self, workflow_id, mark):
        results = self.root / "goncloud-local"
        results.mkdir(exist_ok=True)
        (results / f"{workflow_id}.json").write_text(json.dumps(mark), encoding="utf-8")

    @staticmethod
    def running_mark(pid, *, started_at=None):
        return {
            "workflow_id": "wf",
            "outcome": "running",
            "run_id": "mark-run",
            "pid": pid,
            "started_at": started_at
            or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

    def sleeper(self):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"]
        )
        # Cleanups unwind LIFO: kill first, then the reaping wait — the
        # other order waited out the sleeper's full sleep in every test
        # that used it.
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        return process

    @staticmethod
    def recording_popen(spawned, pid=4242):
        """A Popen double that records argv and survives subprocess.run:
        the ps query inside the pid check opens its own Popen in a with
        and talks to it (communicate/kill)."""

        def double(argv, **kwargs):
            spawned.append(argv)
            return type(
                "P",
                (),
                {
                    "pid": pid,
                    "returncode": 0,
                    "args": argv,
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *exc: None,
                    "communicate": lambda self, input=None, timeout=None: (
                        "",
                        "",
                    ),
                    "kill": lambda self: None,
                    "wait": lambda self, timeout=None: 0,
                    "poll": lambda self: 0,
                },
            )()

        return double

    def close(self, workflow_id, **kwargs):
        kwargs.setdefault("home", self.root)
        return self.orchestrator.close(workflow_id, **kwargs)

    def paused(self, **kwargs):
        """A chain stopped at its scout checkpoint: outcome paused, root todo."""
        first = self.start(stop_after="scout", **kwargs)
        self.assertEqual(len(self.requests), 1)
        self.write_driver_outcome(first["workflow_id"], "paused")
        return first


class LocalOrchestratorTests(_OrchestratorHarness):
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

    def _real_git_repo(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("probe\n", encoding="utf-8")
        git("add", "README.md")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "init",
        )
        # La cadena arranca en SU worktree desde esta revisión real: con
        # review F8/F9 una revisión que no resuelve es un error claro, así
        # que los tests pasan el SHA real, no un símbolo suelto.
        return git("rev-parse", "HEAD").stdout.strip()

    def test_a_planning_stage_that_edits_the_repository_stops_the_chain(self):
        # Live 2026-09-28 (t_2748d636): ingenieria wrote and committed the
        # change itself; implementer then only confirmed it. Planning stages
        # must leave the repository exactly as they found it.
        for role in ("scout", "ingenieria"):
            with self.subTest(role=role):
                self.setUp()
                sha = self._real_git_repo()
                invoke = LocalOrchestratorTests.invoke.__get__(self)

                def editing_invoke(request, route, role=role):
                    if request.role == role:
                        (Path(request.workspace) / "hola.txt").write_text(
                            "hola\n", encoding="utf-8"
                        )
                    return invoke(request, route)

                self.invoke = editing_invoke
                result = self.start(revision=sha)

                self.assertEqual(result["outcome"], "blocked")
                self.assertEqual(result["stages"][role]["status"], "blocked")
                self.assertNotIn("implementer", [r.role for r in self.requests])

    def test_a_planning_stage_that_leaves_the_repository_alone_proceeds(self):
        sha = self._real_git_repo()
        result = self.start(revision=sha)
        self.assertEqual(result["outcome"], "delivered")

    def _git(self, *args):
        subprocess.run(
            ["git", "-C", str(self.repo), *args], check=True, capture_output=True
        )

    def _origen_pelado(self):
        """Un origin pelado local para el repo real: git sin red."""
        origen = self.root / "origin.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(self.repo), str(origen)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "add", "origin", str(origen)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "fetch", "-q", "origin"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "remote", "set-head", "origin", "-a"],
            check=True,
            capture_output=True,
        )
        return origen

    def _aviso_delivered(self, workflow_id, run_id):
        ruta = (
            self.root
            / "goncloud-local"
            / "avisos"
            / f"{workflow_id}-{run_id}-delivered.json"
        )
        return json.loads(ruta.read_text(encoding="utf-8"))

    def test_a_delivered_chain_pushes_its_branch_and_opens_the_pr(self):
        # H-entrega-pr: al entregar, el driver sube cadena/<id> al origin y
        # abre el PR por la API; el link va al resultado durable y al aviso
        # ("Cadena X entregada: <url>"), y el token jamas sale por ningun
        # lado. Git real sin red; credencial y API con dobles.
        sha = self._real_git_repo()
        origen = self._origen_pelado()
        llamadas = []
        token = "ghp_Abc123Def456Ghi789"

        def api(metodo, ruta, token_llamada, payload=None):
            llamadas.append((metodo, ruta, payload))
            if metodo == "GET" and ruta.startswith("/repos/acme/repo/pulls"):
                return 200, []
            if metodo == "GET":
                return 200, {"private": True, "default_branch": "main"}
            return 201, {"html_url": "https://github.com/acme/repo/pull/7"}

        with (
            patch.object(entrega_pr_module, "_credencial", return_value=token),
            patch.object(entrega_pr_module, "_api", api),
            patch.object(
                entrega_pr_module, "_dueño_y_repo", return_value=("acme", "repo")
            ),
        ):
            result = self.start(revision=sha)

        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["pull_request"], "https://github.com/acme/repo/pull/7")
        # El push fue real: la rama de la cadena vive en el origen pelado.
        subido = subprocess.run(
            [
                "git",
                "-C",
                str(origen),
                "rev-parse",
                "--verify",
                f"refs/heads/cadena/{result['workflow_id']}",
            ],
            capture_output=True,
        )
        self.assertEqual(subido.returncode, 0)
        # El POST lleva head = cadena/<id> y base = donde nacio (origin/HEAD).
        metodo, ruta, payload = next(
            llamada for llamada in llamadas if llamada[0] == "POST"
        )
        self.assertEqual(payload["head"], f"cadena/{result['workflow_id']}")
        self.assertEqual(payload["base"], "main")
        evento = self._aviso_delivered(result["workflow_id"], result["run_id"])
        self.assertEqual(evento["motivo"], "https://github.com/acme/repo/pull/7")
        # El resultado durable tambien lleva el link, y el token no sale ni
        # en el resultado ni en el aviso.
        durable = json.loads(
            (self.root / "goncloud-local" / f"{result['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(durable["pull_request"], "https://github.com/acme/repo/pull/7")
        self.assertNotIn(token, json.dumps(durable))
        self.assertNotIn(token, json.dumps(evento))

    def test_without_a_credential_the_chain_stays_delivered_and_says_why(self):
        sha = self._real_git_repo()
        origen = self._origen_pelado()
        with (
            patch.object(entrega_pr_module, "_credencial", return_value=None),
            patch.object(
                entrega_pr_module, "_dueño_y_repo", return_value=("acme", "repo")
            ),
        ):
            result = self.start(revision=sha)

        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(
            result["pr_no_abierto"],
            "sin credencial de GitHub para https://github.com",
        )
        # Sin credencial no hay PR, pero el push SI: la rama queda subida.
        subido = subprocess.run(
            [
                "git",
                "-C",
                str(origen),
                "rev-parse",
                "--verify",
                f"refs/heads/cadena/{result['workflow_id']}",
            ],
            capture_output=True,
        )
        self.assertEqual(subido.returncode, 0)
        evento = self._aviso_delivered(result["workflow_id"], result["run_id"])
        self.assertEqual(
            evento["motivo"],
            "PR no abierto: sin credencial de GitHub para https://github.com",
        )

    def test_the_pr_base_is_the_branch_the_chain_was_born_from(self):
        # H-entrega-pr r2: el trabajo no siempre va contra la rama por
        # defecto. La cadena nace con el checkout en feat/x (el default del
        # remoto sigue siendo main): la base del POST es feat/x, capturada
        # al crear el worktree, no main.
        self._real_git_repo()
        self._origen_pelado()
        self._git("checkout", "-b", "feat/x")
        sha_resuelto = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        llamadas = []

        def api(metodo, ruta, token, payload=None):
            llamadas.append((metodo, ruta, payload))
            if metodo == "GET" and ruta.startswith("/repos/acme/repo/pulls"):
                return 200, []
            if metodo == "GET":
                return 200, {"private": True, "default_branch": "main"}
            return 201, {"html_url": "https://github.com/acme/repo/pull/3"}

        with (
            patch.object(
                entrega_pr_module, "_credencial", return_value="ghp_Abc123Def456Ghi789"
            ),
            patch.object(entrega_pr_module, "_api", api),
            patch.object(
                entrega_pr_module, "_dueño_y_repo", return_value=("acme", "repo")
            ),
        ):
            result = self.start(revision=sha_resuelto)

        self.assertEqual(result["outcome"], "delivered")
        payload = next(llamada for llamada in llamadas if llamada[0] == "POST")[2]
        self.assertEqual(payload["base"], "feat/x")
        self.assertEqual(payload["head"], f"cadena/{result['workflow_id']}")

    def test_a_chain_born_detached_falls_back_to_the_default_branch(self):
        # Detached no hay rama que guardar: el respaldo es la rama por
        # defecto del remoto (origin/HEAD -> main), como en r1.
        self._real_git_repo()
        self._origen_pelado()
        self._git("checkout", "--detach")
        sha_resuelto = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        llamadas = []

        def api(metodo, ruta, token, payload=None):
            llamadas.append((metodo, ruta, payload))
            if metodo == "GET" and ruta.startswith("/repos/acme/repo/pulls"):
                return 200, []
            if metodo == "GET":
                return 200, {"private": True, "default_branch": "main"}
            return 201, {"html_url": "https://github.com/acme/repo/pull/5"}

        with (
            patch.object(
                entrega_pr_module, "_credencial", return_value="ghp_Abc123Def456Ghi789"
            ),
            patch.object(entrega_pr_module, "_api", api),
            patch.object(
                entrega_pr_module, "_dueño_y_repo", return_value=("acme", "repo")
            ),
        ):
            result = self.start(revision=sha_resuelto)

        self.assertEqual(result["outcome"], "delivered")
        payload = next(llamada for llamada in llamadas if llamada[0] == "POST")[2]
        self.assertEqual(payload["base"], "main")

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

    def test_medium_reviewer_retains_evidence_and_delivers_with_pending(self):
        self.responses["reviewer"] = fenced(
            envelope(
                review={"decision": "reject", "severity": "medium"},
                evidence=["Review reproduction: failing_case.py:8"],
            )
        )
        result = self.start()
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(
            result["stages"]["reviewer"]["terminal"]["evidence"],
            ["Review reproduction: failing_case.py:8"],
        )
        self.assertEqual(
            result["pendientes_no_bloqueantes"],
            [
                {
                    "severity": "medium",
                    "summary": "Evidence verified.",
                    "round": 1,
                }
            ],
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
        with (
            patch.object(
                self.orchestrator, "_finish_stage", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.start()
        request = self.requests[0]
        result = self.orchestrator.resume(request.workflow_id)
        self.assertEqual(result["outcome"], "blocked")
        # Every position of the test lane is tried exactly once before the
        # stage turns terminal; the resume must recover the recorded evidence
        # and re-invoke none of them.
        self.assertEqual(len(self.requests), 4)
        self.assertEqual(
            len(
                self.router.store.stage_attempts(request.workflow_id, request.stage_id)
            ),
            4,
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
            self.assertRaises(KeyboardInterrupt),
        ):
            self.start()
        request = self.requests[0]
        self.router.store.save_health(
            "opencode_go", ready=True, account_class="subscription"
        )
        result = self.orchestrator.resume(request.workflow_id)
        self.assertEqual(result["outcome"], "blocked")
        self.assertEqual(len(self.requests), 1)

    def test_crash_between_advancing_slots_resumes_at_next_slot(self):
        # Slot 0 fails with a non-benched advancing failure (a timeout would
        # bench the one adapter all four positions share and skip slot 1
        # before its probe); the crash lands in slot 1's probe, and the
        # resume must continue at slot 1, never replay slot 0.
        original, availability = self.invoke, self.router._availability
        called = []

        def invoke(request, route):
            called.append(route.slot)
            result = original(request, route)
            return (
                replace(result, success=False, failure=FailureKind.TRANSPORT)
                if route.slot == 0
                else result
            )

        def interrupt(route):
            if route.slot == 1:
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
        self.assertEqual(called, [0, 1])

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
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual(result["corrective_stages"], {})
        self.assertEqual([r.role for r in self.requests], ORDER)
        self.assertEqual(
            [p["severity"] for p in result["pendientes_no_bloqueantes"]],
            ["medium"],
        )
        # The entrega stage is told the pending finding by name.
        self.assertIn("Pendientes no bloqueantes", self.requests[-1].prompt)

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
        # Encargo H punto 1: a relaunch is a retry — the resume unblocks the
        # stage, opens a fresh attempt generation and calls the model again;
        # with the model still answering blocked, the chain blocks again.
        self.orchestrator.resume(result["workflow_id"])
        self.assertEqual([r.role for r in self.requests], ["scout", "scout"])
        self.assertEqual(
            self.orchestrator.status(result["workflow_id"])["outcome"], "blocked"
        )

    def test_final_envelope_after_2000_chars_survives_restart_before_completion(self):
        self.responses["scout"] = "raw-log-secret " * 300 + "\n" + fenced(envelope())
        with (
            patch.object(
                self.orchestrator, "_finish_stage", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
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
        for secret in (instruction, "sk-private-marker"):
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


class LocalCloseTests(_OrchestratorHarness):
    """goncloud_local_close: cierra una cadena paused o blocked; nunca una running.

    Vivo 2026-10-18: un diagnostico stop_after=scout quedo outcome "paused",
    su raiz en todo, y WorkflowService.start niego toda cadena nueva en el
    repositorio ("repository already has active Goncloud workflow"); claw no
    tenia herramienta para cerrarla y el coordinador la archivo a mano con
    `hermes kanban archive`.
    """

    def test_close_works_through_the_faithful_native_archive(self):
        # Live review F6, medido por el coordinador sobre el codigo REAL de
        # Hermes 0.22: archive_task abre write_txn(conn) SIN allow_nested (el
        # write_txn real lanza "already inside a transaction" si ya hay uno
        # abierto) y add_comment es la excepcion opt-in con allow_nested=True.
        # El doble replica eso, asi que un close que envolviera los archivados
        # en su propio write_txn fallaria SIEMPRE en produccion: el cierre
        # maneja el archivo nativo tarea por tarea, fuera de cualquier
        # transaccion propia. Roja sobre 742a856 (RuntimeError anidado),
        # verde con el close reecho.
        first = self.paused()
        with connect_closing(self.db) as conn:
            with self.assertRaisesRegex(RuntimeError, "already inside a transaction"):
                with write_txn(conn):
                    archive_task(conn, first["stages"]["ingenieria"]["id"])
            # add_comment si compone anidada (referencia kanban_db.py:1698).
            with write_txn(conn):
                add_comment(conn, first["workflow_id"], "claw", "nested ok")

        receipt = self.close(first["workflow_id"], reason="archivos nativos")
        self.assertEqual(receipt["outcome"], "closed")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")
            self.assertEqual(
                get_task(conn, first["stages"]["ingenieria"]["id"]).status, "archived"
            )

    def test_closing_a_paused_chain_archives_and_frees_the_repository(self):
        first = self.paused()

        receipt = self.close(
            first["workflow_id"], reason="owner closed the paused diagnostic"
        )

        self.assertEqual(receipt["workflow_id"], first["workflow_id"])
        self.assertEqual(receipt["outcome"], "closed")
        self.assertEqual(receipt["root_status"], "archived")
        self.assertEqual(receipt["done_stages"], ["scout"])
        self.assertEqual(receipt["archived_stages"], sorted(set(ORDER) - {"scout"}))
        with connect_closing(self.db) as conn:
            root = get_task(conn, first["workflow_id"])
            self.assertEqual(root.status, "archived")
            self.assertEqual(
                get_task(conn, first["stages"]["scout"]["id"]).status, "done"
            )
            for role in ORDER[1:]:
                self.assertEqual(
                    get_task(conn, first["stages"][role]["id"]).status, "archived"
                )
            comentarios = list_comments(conn, first["workflow_id"])
        self.assertEqual(len(comentarios), 1)
        self.assertIn("owner closed the paused diagnostic", comentarios[0].body)
        self.assertIn("GONCLOUD_LOCAL_CLOSE_V1", comentarios[0].body)
        # The same-repository mutex follows the root's lifecycle: a new chain
        # in the same repository must start without the old refusal.
        created = self.orchestrator.create(
            request_key="next-chain",
            repository=str(self.repo),
            revision="baseline",
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )
        self.assertNotEqual(created["workflow_id"], first["workflow_id"])

    def test_closing_twice_returns_the_same_receipt_and_one_comment(self):
        first = self.paused()

        receipt = self.close(first["workflow_id"], reason="cerrar diagnostico")
        again = self.close(first["workflow_id"], reason="cerrar diagnostico")

        self.assertEqual(receipt, again)
        with connect_closing(self.db) as conn:
            comentarios = [c.body for c in list_comments(conn, first["workflow_id"])]
        self.assertEqual(len(comentarios), 1)

    def test_closing_a_running_chain_is_refused(self):
        first = self.paused()
        stage_id = first["stages"]["ingenieria"]["id"]

        # (a) The kernel itself shows the stage running: typed refusal, no
        # durable change at all.
        with connect_closing(self.db) as conn:
            conn.execute(
                "UPDATE tasks SET status='running' WHERE id=?", (stage_id,)
            ).close()
            conn.commit()
        with self.assertRaisesRegex(WorkflowError, "running"):
            self.close(first["workflow_id"], reason="no")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")
            self.assertEqual(get_task(conn, stage_id).status, "running")

        # (b) A live driver lease on any stage refuses the close; released,
        # the same close goes through.
        with connect_closing(self.db) as conn:
            conn.execute(
                "UPDATE tasks SET status='ready' WHERE id=?", (stage_id,)
            ).close()
            conn.commit()
        store = SubscriptionStateStore(self.root / "routes.db")
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"], stage_id, owner="driver-1", lease_seconds=900
            )
        )
        with self.assertRaisesRegex(WorkflowError, "running"):
            self.close(first["workflow_id"], reason="no", store=store)
        store.release_stage(first["workflow_id"], stage_id, "driver-1")
        receipt = self.close(first["workflow_id"], reason="driver stopped", store=store)
        self.assertEqual(receipt["outcome"], "closed")

    def test_a_blocked_chain_closes_and_keeps_done_evidence(self):
        self.reviews = [{"decision": "reject", "severity": "high"}] * 2
        first = self.start()
        self.assertEqual(first["outcome"], "blocked")
        # The driver writes its own blocked outcome when it stops on it.
        self.write_driver_outcome(first["workflow_id"], "blocked")

        receipt = self.close(first["workflow_id"], reason="blocked chain abandoned")

        self.assertEqual(
            receipt["done_stages"],
            sorted(
                ORDER[:6]
                + [
                    "round2:implementer",
                    "round2:verifier",
                    "round2:adversary",
                ]
            ),
        )
        self.assertEqual(receipt["archived_stages"], ["entrega", "round2:reviewer"])
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, first["stages"]["reviewer"]["id"]).status, "done"
            )
            self.assertEqual(
                get_task(conn, first["corrective_stages"]["reviewer"]["id"]).status,
                "archived",
            )
            self.assertEqual(
                get_task(conn, first["stages"]["scout"]["id"]).status, "done"
            )

    def test_close_rejects_unknown_malformed_or_foreign_ids(self):
        self.paused()
        for workflow_id in ("t_nope", "../escape", "", "t 1", "a" * 200):
            with self.subTest(workflow_id=workflow_id):
                with self.assertRaises(WorkflowError):
                    self.close(workflow_id, reason="x")
        with connect_closing(self.db) as conn:
            ajeno = create_task(conn, title="Tarjeta ajena", assignee="operaciones")
        with self.assertRaises(WorkflowError):
            self.close(ajeno, reason="x")

    def test_close_requires_a_bounded_reason(self):
        first = self.paused()
        for reason in ("", "   ", None, "x" * 1001):
            with self.subTest(reason=type(reason).__name__):
                with self.assertRaises(WorkflowError):
                    self.close(first["workflow_id"], reason=reason)

    def test_status_hint_points_at_goncloud_local_close(self):
        first = self.paused()
        estado = self.orchestrator.status(first["workflow_id"])

        hint = close_hint(estado, "paused")
        self.assertIn("goncloud_local_close", hint)
        # waiting and error are durable stops too (F5): announced like
        # paused and blocked.
        self.assertIn("goncloud_local_close", close_hint(estado, "waiting"))
        self.assertIn("goncloud_local_close", close_hint(estado, "error"))
        # A running, delivered or missing outcome is never advertised: the
        # driver's own durable stop is required evidence (F2).
        self.assertIsNone(close_hint(estado, "running"))
        self.assertIsNone(close_hint(estado, "delivered"))
        self.assertIsNone(close_hint(estado, None))
        # A live lease silences the hint even with a paused outcome.
        self.assertIsNone(close_hint(estado, "paused", driver_live=True))
        # A root that is done (delivered) or archived announces nothing.
        entregado = json.loads(json.dumps(estado))
        entregado["root"]["status"] = "done"
        self.assertIsNone(close_hint(entregado, "paused"))
        cerrado = json.loads(json.dumps(estado))
        cerrado["root"]["status"] = "archived"
        self.assertIsNone(close_hint(cerrado, "paused"))

    def test_a_live_driver_silences_the_hint_and_refuses_the_close(self):
        # Live review F2: nothing marks a stage running while the driver
        # works; the lease is the life signal, the result file the stop one.
        first = self.paused()
        store = SubscriptionStateStore(self.root / "routes.db")
        stage_id = first["stages"]["ingenieria"]["id"]
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"], stage_id, owner="driver-1", lease_seconds=900
            )
        )

        self.assertIsNone(
            self.orchestrator.close_hint(
                first["workflow_id"], store=store, home=self.root
            )
        )
        with self.assertRaisesRegex(WorkflowError, "running"):
            self.close(first["workflow_id"], reason="no", store=store)
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")

        store.release_stage(first["workflow_id"], stage_id, "driver-1")
        hint = self.orchestrator.close_hint(
            first["workflow_id"], store=store, home=self.root
        )
        self.assertIn("goncloud_local_close", hint)
        receipt = self.close(first["workflow_id"], reason="driver stopped", store=store)
        self.assertEqual(receipt["outcome"], "closed")

    def test_close_without_a_durable_driver_outcome_is_refused(self):
        # Between stages nothing marks a stage running, so a chain without
        # the driver's own stopped outcome may still be working: refused.
        first = self.paused()
        (self.root / "goncloud-local" / f"{first['workflow_id']}.json").unlink()
        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")
        # An outcome that is not a stop ("running" is a live driver talking,
        # not one that quit) refuses too; the paused stop goes through. The
        # mark carries a live pid — since review F12 a pidless running mark
        # reads as driverless, never as a live driver.
        live = self.sleeper()
        self.write_mark(first["workflow_id"], self.running_mark(live.pid))
        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
        # Live review F8, overturned by review F12: an illegible file is a
        # foreign state — every driver write is atomic — and it reads as no
        # driver at all, so the status announces the close and the chain
        # stays recoverable instead of jamming the repository.
        (self.root / "goncloud-local" / f"{first['workflow_id']}.json").write_text(
            "{broken", encoding="utf-8"
        )
        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )
        self.write_driver_outcome(first["workflow_id"], "paused")
        self.assertEqual(
            self.close(first["workflow_id"], reason="ya paro")["outcome"], "closed"
        )

    def test_a_relaunched_chain_refuses_the_close_while_its_mark_is_running(self):
        # Live review F8 (1): the result file survived a relaunch — after
        # resuming a paused chain (or a same-request-key run) the old
        # "paused" was still there, so the close archived a live chain.
        # Every driver start now overwrites the file with a running mark
        # (its own run id) before any stage, and the close and the hint
        # refuse until that driver's own outcome is the last write. The
        # fake driver carries this process's pid: a live one, exactly what
        # a real Popen returns.
        first = self.paused()
        with patch(
            "local_orchestrator.subprocess.Popen",
            lambda *args, **kwargs: type("P", (), {"pid": os.getpid()})(),
        ):
            self.orchestrator.launch_detached(first["workflow_id"], home=self.root)
        mark = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(mark["outcome"], "running")
        self.assertEqual(mark["workflow_id"], first["workflow_id"])
        self.assertTrue(mark["run_id"])

        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")

    def test_the_relaunched_driver_paused_outcome_closes(self):
        # Live review F8 (2): the same relaunched driver quits paused — its
        # outcome, written under the run id it started with, is now the last
        # write: the hint announces the close and the close accepts it. The
        # fake driver pid is this process: the resume guard reads its mark
        # as a previous sequential run, not another driver.
        first = self.paused()
        with patch(
            "local_orchestrator.subprocess.Popen",
            lambda *args, **kwargs: type("P", (), {"pid": os.getpid()})(),
        ):
            self.orchestrator.launch_detached(first["workflow_id"], home=self.root)
        report = self.orchestrator.resume(
            first["workflow_id"], stop_after="ingenieria", home=self.root
        )
        self.assertEqual(report["outcome"], "paused")
        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(written["outcome"], "paused")
        self.assertEqual(written["run_id"], report["run_id"])

        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )
        receipt = self.close(first["workflow_id"], reason="driver stopped")
        self.assertEqual(receipt["outcome"], "closed")
        self.assertEqual(receipt["done_stages"], ["ingenieria", "scout"])

    def test_both_driver_entries_mark_running_before_the_first_stage(self):
        # Live review F8 (3): launch_detached marks the file running before
        # spawning the driver, and the CLI resume marks it through the same
        # orchestrator resume — in both cases before the first stage runs.
        # The fake driver pid is a real, already-harvested process: the CLI
        # subprocess resume must read the mark as a stopped driver and
        # proceed (with review F19, an out-of-range pid would read
        # unchecked, i.e. live).
        first = self.paused()
        dead = self.sleeper()
        dead.kill()
        dead.wait()
        with patch.object(
            local_orchestrator_module.subprocess,
            "Popen",
            self.recording_popen([], pid=dead.pid),
        ):
            launched = self.orchestrator.launch_detached(
                first["workflow_id"], home=self.root
            )
        result_file = self.root / "goncloud-local" / f"{first['workflow_id']}.json"
        marked = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(marked["outcome"], "running")
        self.assertEqual(marked["run_id"], launched["run_id"])

        observed = {}

        def recording_stage_runner(service, **assignment):
            observed["at_stage"] = json.loads(result_file.read_text(encoding="utf-8"))
            review = (
                {"decision": "approve", "severity": None}
                if service.actor_role == "reviewer"
                else None
            )
            return {
                "success": True,
                "text": fenced(envelope(review=review)),
                "adapter": "opencode_go",
            }

        spec = importlib.util.spec_from_file_location(
            "run_local_workflow_f8", ROOT / "scripts/run_local_workflow.py"
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        orchestrator = LocalOrchestrator(
            service_factory=self.service, stage_runner=recording_stage_runner
        )
        out = io.StringIO()
        with patch.dict(os.environ, {"HERMES_HOME": str(self.root)}):
            with redirect_stdout(out):
                code = cli.main(
                    ["resume", "--workflow-id", first["workflow_id"]],
                    orchestrator_factory=lambda: orchestrator,
                )
        self.assertEqual(code, 0)
        self.assertEqual(observed["at_stage"]["outcome"], "running")
        self.assertTrue(observed["at_stage"]["run_id"])
        final = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(final["outcome"], "delivered")
        self.assertEqual(final["run_id"], observed["at_stage"]["run_id"])

    def test_a_waiting_driver_outcome_closes_and_is_announced(self):
        # Live review F5: resume also stops durably with "waiting" (a stage
        # whose parents never went ready, local_orchestrator ~880). That jam
        # held the repository exactly like paused; the close accepts it with
        # the same live-lease guard and the hint announces it.
        first = self.paused()
        self.write_driver_outcome(first["workflow_id"], "waiting")
        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )
        store = SubscriptionStateStore(self.root / "routes.db")
        stage_id = first["stages"]["ingenieria"]["id"]
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"], stage_id, owner="driver-1", lease_seconds=900
            )
        )
        with self.assertRaisesRegex(WorkflowError, "running"):
            self.close(first["workflow_id"], reason="no", store=store)
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")
        store.release_stage(first["workflow_id"], stage_id, "driver-1")

        receipt = self.close(first["workflow_id"], reason="waiting abandoned")
        self.assertEqual(receipt["outcome"], "closed")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")

    def test_an_error_driver_outcome_closes_and_is_announced(self):
        # Live review F5: the driver writes "error" when its bootstrap fails
        # (local_orchestrator ~141). F8 later made an unreadable result file
        # fail closed ("running"), so "error" here is the driver's own
        # written outcome: the process is gone; the same jam, so the close
        # accepts it and the hint announces it.
        first = self.paused()
        self.write_driver_outcome(first["workflow_id"], "error")
        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )

        receipt = self.close(first["workflow_id"], reason="driver errored for good")
        self.assertEqual(receipt["outcome"], "closed")
        self.assertEqual(receipt["done_stages"], ["scout"])
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")

    def test_a_stale_outcome_over_a_done_root_announces_and_closes_nothing(self):
        # The result file can outlive the chain: a delivered root must not
        # announce a close because of an old paused outcome (F2).
        first = self.start()
        self.assertEqual(first["outcome"], "delivered")
        self.write_driver_outcome(first["workflow_id"], "paused")

        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "delivered"):
            self.close(first["workflow_id"], reason="no")

    def test_a_triage_root_without_graph_answers_busy_until_closed(self):
        # Live review F3: workflow.py inserts the root in triage and
        # decomposes afterwards; a failure in between leaves a root with
        # no driver and no stages. Regla simple A: una raíz en triage sin
        # hijos NO es evidencia de parada — es una cadena naciendo — así
        # que start no la archiva: contesta busy con su id, sin crear
        # nada, y goncloud_local_close (que sí acepta la raíz sin driver)
        # la libera; entonces la siguiente clave arranca.
        contract = "GONCLOUD_ENGINEERING_V1\n" + json.dumps(
            {
                "request_key": "stuck-triage",
                "repository": str(self.repo.resolve()),
                "revision": "baseline",
                "instruction": "Inspect.",
                "sources": SOURCES,
                "affects_business_rules": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with connect_closing(self.db) as conn:
            root_id = create_task(
                conn,
                title="Engineering: Inspect.",
                body=contract,
                assignee="claw",
                created_by="claw",
                workspace_kind="dir",
                workspace_path=str(self.repo),
                triage=True,
                idempotency_key="goncloud-workflow:stuck-triage",
            )
        with connect_closing(self.db) as conn:
            before = len(list_tasks(conn, include_archived=True))

        busy = self.orchestrator.create(
            request_key="otra-cadena",
            repository=str(self.repo),
            revision="baseline",
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )

        self.assertTrue(busy.get("busy"))
        self.assertEqual(busy["cadena_viva"], root_id)
        with connect_closing(self.db) as conn:
            self.assertEqual(
                len(list_tasks(conn, include_archived=True)),
                before,
                "busy creates nothing",
            )
            self.assertEqual(get_task(conn, root_id).status, "triage")

        # The explicit close releases the driverless root; the next key
        # then starts clean.
        receipt = self.close(root_id, reason="descomposicion fallida")
        self.assertEqual(receipt["outcome"], "closed")
        created = self.orchestrator.create(
            request_key="otra-cadena",
            repository=str(self.repo),
            revision="baseline",
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )
        self.assertIsNone(created.get("busy"))
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, root_id).status, "archived")


class RetryAndQueueTests(_OrchestratorHarness):
    """Encargo H: relanzar = reintentar; driver muerto ≠ driver vivo.
    r6: sin cola — el repo ocupado contesta busy con la cadena viva, y el
    aviso a claw pide relanzarla.
    """

    def test_a_relaunched_blocked_stage_calls_the_model_again_and_advances(self):
        self.responses["scout"] = fenced(envelope(status="blocked"))
        first = self.start()
        self.assertEqual(first["outcome"], "blocked")
        scout_id = first["stages"]["scout"]["id"]
        self.assertEqual(
            self.router.store.current_generation(first["workflow_id"], scout_id), 0
        )
        del self.responses["scout"]

        again = self.start()

        self.assertEqual(again["workflow_id"], first["workflow_id"])
        self.assertEqual(again["outcome"], "delivered")
        self.assertEqual([r.role for r in self.requests], ["scout"] + ORDER)
        self.assertEqual(
            self.router.store.current_generation(first["workflow_id"], scout_id), 1
        )
        attempts = self.router.store.stage_attempts(first["workflow_id"], scout_id)
        self.assertEqual(len(attempts), 1, "the router sees only the new generation")

    def test_the_retry_never_touches_done_stages_or_the_review_ceiling(self):
        self.reviews = [{"decision": "reject", "severity": "high"}] * 2
        first = self.start()
        self.assertEqual(first["outcome"], "blocked")
        reviewer_id = first["stages"]["reviewer"]["id"]
        calls = len(self.requests)

        again = self.orchestrator.resume(first["workflow_id"])

        self.assertEqual(again["outcome"], "blocked")
        self.assertEqual(len(self.requests), calls)
        self.assertEqual(
            self.router.store.current_generation(first["workflow_id"], reviewer_id),
            0,
            "the reviewer's two-round ceiling is a contract, not a jam",
        )
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, first["stages"]["scout"]["id"]).status, "done"
            )

    # (b) — encargo H punto 2
    def test_a_stopped_chain_is_auto_closed_when_a_new_chain_starts(self):
        first = self.paused()

        second = self.start(request_key="second-chain")

        self.assertNotEqual(second["workflow_id"], first["workflow_id"])
        self.assertEqual(second["outcome"], "delivered")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")
            self.assertEqual(
                get_task(conn, first["stages"]["scout"]["id"]).status, "done"
            )
            self.assertEqual(
                get_task(conn, first["stages"]["entrega"]["id"]).status, "archived"
            )
            # The same native audit trail archive_task leaves (review F14):
            # one "archived" event per archived member, none for done ones.
            self.assertIn(
                "archived",
                [event["kind"] for event in list_events(conn, first["workflow_id"])],
            )
            self.assertIn(
                "archived",
                [
                    event["kind"]
                    for event in list_events(conn, first["stages"]["entrega"]["id"])
                ],
            )
            self.assertNotIn(
                "archived",
                [
                    event["kind"]
                    for event in list_events(conn, first["stages"]["scout"]["id"])
                ],
            )
            comentarios = [c.body for c in list_comments(conn, first["workflow_id"])]
        self.assertTrue(
            any("GONCLOUD_LOCAL_CLOSE_V1" in body for body in comentarios),
            "the automatic close leaves the same close receipt",
        )
        self.assertTrue(
            any("freed for a new chain" in body for body in comentarios),
            "the automatic close leaves the reason",
        )

    # (c) — encargo H punto 2
    def test_a_live_chain_answers_busy_until_it_finishes(self):
        first = self.paused()
        store = SubscriptionStateStore(self.root / "routes.db")
        stage_id = first["stages"]["ingenieria"]["id"]
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"], stage_id, owner="driver-live", lease_seconds=900
            )
        )

        busy = self.start(request_key="second-chain")

        self.assertTrue(busy["busy"], "the new chain is not created")
        self.assertEqual(busy["cadena_viva"], first["workflow_id"])
        self.assertEqual(
            [r.role for r in self.requests],
            ["scout"],
            "no model runs over a busy repository",
        )

        store.release_stage(first["workflow_id"], stage_id, "driver-live")
        second = self.start(request_key="second-chain")

        self.assertEqual(second["outcome"], "delivered")
        self.assertEqual(len(self.requests), 1 + len(ORDER))
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")
            comentarios = [c.body for c in list_comments(conn, first["workflow_id"])]
        self.assertTrue(any("GONCLOUD_LOCAL_CLOSE_V1" in body for body in comentarios))

    def test_a_missing_store_does_not_jam_the_new_chain(self):
        first = self.paused()

        with patch.dict(
            os.environ,
            {"GONCLOUD_SUBSCRIPTION_DB": str(self.root / "no-router" / "routes.db")},
        ):
            second = self.start(request_key="second-chain")

        self.assertEqual(
            second["outcome"],
            "delivered",
            "the chain starts even though the router store is absent",
        )
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")
            comentarios = [c.body for c in list_comments(conn, first["workflow_id"])]
        self.assertTrue(any("GONCLOUD_LOCAL_CLOSE_V1" in body for body in comentarios))

    # review F12 — el outcome running de _report firma su pid y uno muerto
    # reintenta; sin pid no hay driver que lo respalde y no atasca nada.
    def test_a_report_running_mark_with_a_dead_pid_retries(self):
        first = self.paused()
        report = self.orchestrator._report(
            first["workflow_id"], "running", home=self.root, run_id="busy-run"
        )
        result_file = self.root / "goncloud-local" / f"{first['workflow_id']}.json"
        written = json.loads(result_file.read_text(encoding="utf-8"))
        self.assertEqual(written["outcome"], "running")
        self.assertEqual(written["pid"], os.getpid(), "the report signs its writer")
        self.assertEqual(report["pid"], os.getpid())

        spawned = []

        def recording_popen(argv, **kwargs):
            spawned.append(argv)
            # Context-manager protocol: the ps query inside the pid check
            # goes through subprocess.run, which opens its Popen in a with.
            return type(
                "P",
                (),
                {
                    "pid": 4242,
                    "__enter__": lambda self: self,
                    "__exit__": lambda self, *exc: None,
                },
            )()

        # A live driver of the same chain (a foreign pid, like production:
        # the driver signs, the gateway launches) answers busy. The
        # liveness verdict is pinned per block (epoch 0 = a process
        # born long before its mark; None = proven gone) — the verdict
        # machinery itself has its own tests.
        live = self.sleeper()
        self.write_mark(first["workflow_id"], self.running_mark(live.pid))
        with (
            patch.object(
                local_orchestrator_module.subprocess, "Popen", recording_popen
            ),
            patch.object(
                local_orchestrator_module, "_process_start_epoch", lambda pid: 0
            ),
        ):
            busy = self.orchestrator.launch_detached(
                first["workflow_id"], home=self.root
            )
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["cadena_viva"], first["workflow_id"])
        self.assertEqual(spawned, [], "no driver spawns over a live one")

        # The driver dies — exactly what a finished driver leaves behind:
        # the same key relaunches the driver instead of answering busy
        # forever.
        live.kill()
        live.wait()
        self.write_mark(first["workflow_id"], self.running_mark(live.pid))
        with (
            patch.object(
                local_orchestrator_module.subprocess, "Popen", recording_popen
            ),
            patch.object(
                local_orchestrator_module, "_process_start_epoch", lambda pid: None
            ),
        ):
            relaunched = self.orchestrator.launch_detached(
                first["workflow_id"], home=self.root
            )
        self.assertEqual(relaunched["driver_pid"], 4242)
        self.assertEqual(len(spawned), 1, "the same-key relaunch spawns the driver")

    def test_a_pidless_running_mark_jams_nothing(self):
        # F12: una marca running sin pid ya no lee como driver vivo — la
        # clave nueva del mismo repo arranca y la vieja se cierra sola.
        first = self.paused()
        self.write_mark(
            first["workflow_id"],
            {"workflow_id": first["workflow_id"], "outcome": "running"},
        )

        second = self.start(request_key="second-chain")

        self.assertEqual(
            second["outcome"],
            "delivered",
            "the new key starts; the pidless mark is nobody's driver",
        )
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")

    # review F13 — una sola fuente de verdad: lanzada la cadena, su archivo
    # pasa a decir "running" (marca viva) y deja de leerse como cola; close
    # y close_hint leen lo mismo que el lanzador escribió.
    # F13-equivalente r6 — una cadena lanzada tiene su marca viva como
    # última escritura: ni close ni close_hint la archivan entre etapas.
    def test_a_launched_chain_refuses_the_close_while_its_driver_is_live(self):
        live = self.sleeper()
        second = self.paused(request_key="second-chain")
        self.write_mark(second["workflow_id"], self.running_mark(live.pid))

        # The launch's running mark (this double's live pid) is the life
        # signal: between stages — no lease, no running stage — the chain
        # refuses the close and the status announces nothing.
        with patch.object(
            local_orchestrator_module.subprocess,
            "Popen",
            self.recording_popen([], pid=live.pid),
        ):
            self.orchestrator.launch_detached(second["workflow_id"], home=self.root)

        self.assertIsNone(
            self.orchestrator.close_hint(second["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "refuses"):
            self.close(second["workflow_id"], reason="entre etapas")
        # Y cuando el driver de la cadena muere, el mismo archivo pasa a ser
        # la evidencia de parada: el close ya la archiva (contrato 2).
        live.kill()
        live.wait()
        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(second["workflow_id"], home=self.root),
        )
        receipt = self.close(second["workflow_id"], reason="driver muerto")
        self.assertEqual(receipt["outcome"], "closed")

    # contrato r6 (1) — repo con cadena viva: la herramienta contesta busy
    # con su id y no construye nada.
    def test_the_local_run_handler_answers_busy_without_spawning(self):
        first = self.paused()
        store = SubscriptionStateStore(self.root / "routes.db")
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"],
                first["stages"]["ingenieria"]["id"],
                owner="driver-live",
                lease_seconds=900,
            )
        )
        with connect_closing(self.db) as conn:
            before = len(list_tasks(conn, include_archived=True))
        plugin = _plugin_package()
        registered = {}

        class Ctx:
            profile_name = "claw"

            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

            def register_hook(self, name, hook):
                pass

        plugin.register(Ctx())
        handler = registered["goncloud_local_run"]["handler"]
        environment = {
            "HERMES_KANBAN_DB": str(self.db),
            "GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db"),
            "HERMES_HOME": str(self.root),
        }
        with patch.dict(os.environ, environment):
            with patch("local_orchestrator.subprocess.Popen") as popen:
                result = json.loads(
                    handler(
                        {
                            "request_key": "second-chain",
                            "repository": str(self.repo),
                            "revision": "baseline",
                            "instruction": "Inspect again.",
                            "sources": SOURCES,
                            "max_steps": 5,
                        }
                    )
                )
        popen.assert_not_called()
        self.assertTrue(result["busy"])
        self.assertEqual(result["cadena_viva"], first["workflow_id"])
        self.assertIn("relanzas", result["mensaje"])
        with connect_closing(self.db) as conn:
            self.assertEqual(
                len(list_tasks(conn, include_archived=True)),
                before,
                "busy creates nothing",
            )

    def test_launch_detached_never_spawns_over_a_live_driver_of_the_same_chain(
        self,
    ):
        first = self.paused()
        pid = self.sleeper().pid
        self.write_mark(first["workflow_id"], self.running_mark(pid))

        launched = self.orchestrator.launch_detached(
            first["workflow_id"], home=self.root
        )

        self.assertTrue(launched.get("busy"))
        self.assertEqual(launched["cadena_viva"], first["workflow_id"])
        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(written["pid"], pid, "the live mark stays untouched")

    # punto 4 — driver muerto ≠ driver vivo
    def test_a_running_mark_with_a_dead_pid_is_stop_evidence(self):
        # Marca running con un pid comprobadamente muerto (un proceso real,
        # ya cosechado): close acepta y el relanzamiento reintenta (encargo
        # H punto 4). Con review F19 el pid es real: macOS le dice
        # "process id too large" a uno fuera de rango, y eso es
        # incomprobable, no muerto.
        self.responses["scout"] = fenced(envelope(status="blocked"))
        first = self.start()
        self.assertEqual(first["outcome"], "blocked")
        dead = self.sleeper()
        dead.kill()
        dead.wait()
        self.write_mark(first["workflow_id"], self.running_mark(dead.pid))

        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )
        del self.responses["scout"]
        again = self.orchestrator.resume(first["workflow_id"], home=self.root)

        self.assertEqual(again["outcome"], "delivered")
        # La etapa scout estaba bloqueada y el relanzamiento la reabre:
        # scout corre otra vez (generación nueva) y luego el resto.
        self.assertEqual([r.role for r in self.requests], ["scout"] + ORDER)

    def test_a_recycled_pid_reads_as_a_dead_driver(self):
        # El pid existe, pero nació mucho después de la marca: el pid se
        # recicló y el driver marcado ya no está.
        first = self.paused()
        self.write_mark(
            first["workflow_id"],
            self.running_mark(os.getpid(), started_at="2000-01-01T00:00:00Z"),
        )

        receipt = self.close(first["workflow_id"], reason="pid reciclado")

        self.assertEqual(receipt["outcome"], "closed")

    def test_a_live_pid_refuses_the_close_and_answers_busy_on_relaunch(self):
        # Marca running con un pid vivo (otro proceso de verdad): el close
        # rechaza, el relanzamiento contesta busy y el archivo no se toca —
        # la marca viva sigue siendo la señal de vida (review F17).
        first = self.paused()
        sleeper = self.sleeper()
        self.write_mark(first["workflow_id"], self.running_mark(sleeper.pid))

        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
        result = self.orchestrator.resume(first["workflow_id"], home=self.root)
        self.assertEqual(result["outcome"], "busy")
        self.assertTrue(result["busy"])
        self.assertEqual(result["cadena_viva"], first["workflow_id"])
        self.assertEqual([r.role for r in self.requests], ["scout"])
        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        # Nothing overwrote the live mark: pid and outcome stay exactly as
        # the live driver wrote them.
        self.assertEqual(written["pid"], sleeper.pid)
        self.assertEqual(written["outcome"], "running")

        sleeper.kill()
        sleeper.wait()
        result = self.orchestrator.resume(first["workflow_id"], home=self.root)
        self.assertEqual(result["outcome"], "delivered")
        self.assertEqual([r.role for r in self.requests], ["scout"] + ORDER[1:])

    def test_launch_detached_without_stop_after_runs_the_driver(self):
        # Review F1 (Critical): stop_after es opcional, su hueco en argv no.
        # El run_id caía en ese hueco y todo lanzamiento sin checkpoint
        # moría en check_stop_after antes de correr una sola etapa. El
        # argv del lanzador REAL — capturado, nunca rearmado — debe llevar
        # el hueco vacío de stop_after y el run_id al final, y ese argv
        # exacto debe manejar un intérprete real por el bootstrap real.
        first = self.paused()
        eco_plugin = self.root / "eco-plugin"
        eco_hermes = self.root / "eco-hermes"
        eco_plugin.mkdir()
        eco_hermes.mkdir()
        (eco_plugin / "local_orchestrator.py").write_text(
            "class LocalOrchestrator:\n"
            "    def resume(self, workflow_id, stop_after=None, launch_run_id=None):\n"
            "        return {'workflow_id': workflow_id, 'outcome': 'completed',\n"
            "                'stop_after': stop_after, 'launch_run_id': launch_run_id}\n",
            encoding="utf-8",
        )
        captured = {}

        def capture(argv, **kwargs):
            captured["argv"] = list(argv)
            return type("Spawned", (), {"pid": 424242})()

        with patch("local_orchestrator.subprocess.Popen", capture):
            launched = self.orchestrator.launch_detached(
                first["workflow_id"], home=self.root
            )

        argv = captured["argv"]
        self.assertEqual(argv[0], sys.executable, "the real launcher boots python")
        self.assertEqual(argv[1:3], ["-I", "-c"])
        self.assertEqual(len(argv), 10, "fixed positions: no spliced slots")
        self.assertEqual(argv[8], "", "stop_after's own slot stays empty")
        self.assertEqual(argv[9], launched["run_id"])
        # The driver side is the echo double; the launch under test is the
        # launcher's own argv product.
        argv[4], argv[5] = str(eco_plugin), str(eco_hermes)
        subprocess.run(
            argv, cwd=str(self.root), capture_output=True, timeout=60, check=True
        )
        result = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(result["outcome"], "completed")
        self.assertIsNone(result["stop_after"], "no stop_after, no run_id leaking in")
        self.assertEqual(result["launch_run_id"], launched["run_id"])

    # review F2 — una cadena en cola nunca se cierra sola
    # contrato r6 (b) — un repo con cadena viva contesta busy y no crea
    # nada.
    def test_a_live_chain_answers_busy_and_creates_nothing(self):
        first = self.paused()
        store = SubscriptionStateStore(self.root / "routes.db")
        self.assertTrue(
            store.claim_stage(
                first["workflow_id"],
                first["stages"]["ingenieria"]["id"],
                owner="driver-live",
                lease_seconds=900,
            )
        )
        with connect_closing(self.db) as conn:
            before = len(list_tasks(conn, include_archived=True))

        busy = self.start(request_key="second-chain")

        self.assertTrue(busy["busy"])
        self.assertEqual(busy["cadena_viva"], first["workflow_id"])
        self.assertEqual(busy["repo"], str(self.repo.resolve()))
        self.assertIn("relanzas", busy["mensaje"])
        with connect_closing(self.db) as conn:
            self.assertEqual(
                len(list_tasks(conn, include_archived=True)),
                before,
                "busy creates nothing",
            )
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "todo")

    # review F3, contrato r6 — si el cierre automático no cierra (la vieja
    # despertó), la nueva no se crea: busy, y nada escrito.
    def test_a_chain_that_turns_alive_answers_busy_and_creates_nothing(self):
        first = self.paused()
        signals = iter([(False, True), (True, False)])
        with patch("workflow._driver_signals", side_effect=lambda _id: next(signals)):
            started = self.service().start(
                request_key="second-chain",
                repository=str(self.repo),
                revision="baseline",
                instruction="Inspect again.",
                sources=SOURCES,
            )

        self.assertTrue(started.get("busy"))
        self.assertEqual(started["cadena_viva"], first["workflow_id"])
        with connect_closing(self.db) as conn:
            self.assertNotEqual(get_task(conn, first["workflow_id"]).status, "archived")
            roots = [
                task
                for task in list_tasks(conn, include_archived=True)
                if (task.body or "").startswith("GONCLOUD_ENGINEERING_V1")
            ]
        self.assertEqual(
            len(roots),
            1,
            "the refused close creates nothing: only the old chain exists",
        )
        with connect_closing(self.db) as conn:
            comentarios = [
                comment.body for comment in list_comments(conn, first["workflow_id"])
            ]
        self.assertTrue(
            all("GONCLOUD_LOCAL_CLOSE_V1" not in body for body in comentarios)
        )

    # cr:4314695530 — una etapa de solo lectura bloqueada por el guard de
    # mutación NO se reabre con el relanzamiento: sigue bloqueada hasta que
    # el dueño revierta, y su motivo lo dice.
    def test_a_read_only_stage_blocked_by_its_guard_stays_blocked(self):
        self.responses["scout"] = fenced(
            envelope(status="completed", summary="leo y no toco")
        )
        fingerprints = iter([("base", ""), ("editado", "M archivo.py")])
        with patch.object(
            local_orchestrator_module,
            "_workspace_fingerprint",
            side_effect=lambda repo: next(fingerprints),
        ):
            first = self.start(stop_after="scout")
        self.assertEqual(first["outcome"], "blocked")
        scout_id = first["stages"]["scout"]["id"]
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, scout_id).status, "blocked")
        requests_after_block = len(self.requests)

        result = self.orchestrator.resume(first["workflow_id"], home=self.root)

        self.assertEqual(result["outcome"], "blocked")
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, scout_id).status,
                "blocked",
                "the guard's block survives a relaunch",
            )
            motivo = [
                event["payload"]["reason"]
                for event in list_events(conn, scout_id)
                if event["kind"] == "blocked"
            ][-1]
        self.assertIn("changed the repository", motivo)
        self.assertIn("owner reverts", motivo)
        self.assertEqual(
            len(self.requests),
            requests_after_block,
            "no model runs: the stage keeps waiting for the owner's revert",
        )

    # review F18 — la marca running de _report firma pid y started_at: un
    # pid reciclado (dueño nacido después de la marca) lee muerto.
    def test_the_report_signs_started_at_and_a_recycled_pid_reads_dead(self):
        first = self.paused()
        self.orchestrator._report(
            first["workflow_id"], "running", home=self.root, run_id="busy-run"
        )
        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(written["outcome"], "running")
        self.assertEqual(written["pid"], os.getpid())
        self.assertEqual(
            written["started_at"],
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "the same stamp format _running_mark uses",
        )

        # A live pid whose owner was born AFTER the mark: recycled, dead.
        live = self.sleeper()
        started_before = datetime.now(timezone.utc) - timedelta(seconds=30)
        self.write_mark(
            first["workflow_id"],
            self.running_mark(
                live.pid, started_at=started_before.isoformat(timespec="seconds")
            ),
        )
        mark = local_orchestrator_module.read_driver_mark(
            first["workflow_id"], home=self.root
        )
        self.assertFalse(
            local_orchestrator_module.driver_mark_live(mark),
            "a recycled pid is not a live driver",
        )
        self.assertIn(
            "goncloud_local_close",
            self.orchestrator.close_hint(first["workflow_id"], home=self.root),
        )

    # review F19 — solo "no existe ese proceso" (ps != 0 con stdout y
    # stderr vacíos) es pid muerto; cualquier otro fallo de ps es
    # incomprobable (vivo).
    def test_only_a_missing_process_reads_as_dead(self):
        def ps_run(returncode, stdout, stderr):
            return lambda *args, **kwargs: SimpleNamespace(
                returncode=returncode, stdout=stdout, stderr=stderr
            )

        with patch.object(
            local_orchestrator_module.subprocess, "run", ps_run(1, "", "")
        ):
            self.assertIsNone(
                local_orchestrator_module._process_start_epoch(4242),
                "empty stdout and stderr with a non-zero exit: no such process",
            )
        with patch.object(
            local_orchestrator_module.subprocess,
            "run",
            ps_run(1, "", "ps: illegal option -- o"),
        ):
            checked = local_orchestrator_module._process_start_epoch(4242)
        self.assertIsInstance(checked, local_orchestrator_module._UncheckedPid)
        self.assertIn("illegal option", checked.cause)
        # La marca con ese pid queda viva y anota la causa (review F4/F19).
        first = self.paused()
        self.write_mark(first["workflow_id"], self.running_mark(4242))
        with patch.object(
            local_orchestrator_module.subprocess,
            "run",
            ps_run(1, "", "ps: illegal option -- o"),
        ):
            mark = local_orchestrator_module.read_driver_mark(
                first["workflow_id"], home=self.root
            )
            self.assertTrue(
                local_orchestrator_module.driver_mark_live(mark),
                "an uncheckable ps never reads as a dead driver",
            )
        self.assertIn("ps refused", mark["pid_check"])

    # contrato r6 (3) — el stop_after no tiene camino que lo pierda: la
    # única puerta de lanzamiento es launch_detached, y en repo ocupado no
    # se crea nada que pudiera perderlo.
    def test_stop_after_never_loses_the_checkpoint(self):
        first = self.paused()
        self.assertEqual(first["outcome"], "paused")
        live = self.sleeper()
        self.write_mark(first["workflow_id"], self.running_mark(live.pid))

        busy = self.start(request_key="second-chain", stop_after="scout")

        self.assertTrue(busy["busy"])
        self.assertNotIn("workflow_id", busy, "nothing was created to lose it")
        self.assertNotIn("stages", busy)

    def test_relaunching_the_live_chains_own_key_answers_busy(self):
        # Contrato r6 (b): relanzar la MISMA request_key de la cadena viva
        # no lanza un segundo driver — el guard de resume con la marca viva
        # propia contesta lo mismo: busy con esa cadena.
        first = self.paused()
        live = self.sleeper()
        self.write_mark(first["workflow_id"], self.running_mark(live.pid))

        again = self.start(request_key="local-1")

        self.assertTrue(again.get("busy"))
        self.assertEqual(again["cadena_viva"], first["workflow_id"])
        self.assertEqual(
            [r.role for r in self.requests],
            ["scout"],
            "no model runs over a busy repository, same key or not",
        )
        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(written["pid"], live.pid, "the live mark stays untouched")

    def test_an_uncheckable_pid_reads_alive_and_records_the_cause(self):
        # Review F4: sin ps, con timeout o con acceso denegado, la marca
        # lee VIVA — no se cierra ni se reintenta — y la causa queda
        # registrada en la propia marca. Solo un pid comprobadamente muerto
        # o reciclado cuenta como driver parado.
        first = self.paused()
        self.write_mark(first["workflow_id"], self.running_mark(99999999))

        def sin_ps(*args, **kwargs):
            raise OSError("no hay ps")

        with patch.object(local_orchestrator_module.subprocess, "run", sin_ps):
            checked = local_orchestrator_module._process_start_epoch(99999999)
            self.assertIsInstance(checked, local_orchestrator_module._UncheckedPid)
            self.assertIn("no hay ps", checked.cause)
            mark = local_orchestrator_module.read_driver_mark(
                first["workflow_id"], home=self.root
            )
            self.assertTrue(
                local_orchestrator_module.driver_mark_live(mark),
                "an uncheckable pid is not a dead driver",
            )
            self.assertIn("ps unavailable", mark["pid_check"])
            self.assertIsNone(
                self.orchestrator.close_hint(first["workflow_id"], home=self.root)
            )
            with self.assertRaises(WorkflowError):
                self.close(first["workflow_id"], reason="pid incomprobable")
            result = self.orchestrator.resume(first["workflow_id"], home=self.root)

        self.assertEqual(
            result["outcome"], "busy", "no retry over a possibly live driver"
        )
        self.assertEqual(result["cadena_viva"], first["workflow_id"])
        self.assertEqual([r.role for r in self.requests], ["scout"])

    # Hallazgo del lead — en Windows, un proceso que ya terminó no es un
    # driver vivo: OpenProcess abre el pid mientras quede un handle y
    # GetProcessTimes contesta igual, así que el código de salida decide
    # (259 = STILL_ACTIVE; cualquier otro código, terminado).
    def test_windows_exit_code_says_whether_the_driver_is_gone(self):
        import ctypes

        class _FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", ctypes.c_ulong),
                ("dwHighDateTime", ctypes.c_ulong),
            ]

        # En macOS ctypes.wintypes no importa (es de Windows): el doble
        # aporta un módulo equivalente con los tipos reales de ctypes.
        fake_wintypes = SimpleNamespace(DWORD=ctypes.c_ulong, FILETIME=_FILETIME)

        def fake_kernel32(*, exit_code=None, fail_exit_code=False, times_ok=True):
            kernel32 = SimpleNamespace()

            def get_exit(handle, ref):
                if fail_exit_code:
                    return False
                ref._obj.value = exit_code
                return True

            def get_times(handle, creation, exit_time, kernel_time, user_time):
                if not times_ok:
                    return False
                creation._obj.dwHighDateTime = 310_000_000
                creation._obj.dwLowDateTime = 0
                return True

            kernel32.OpenProcess = lambda access, inherit, pid: 4242
            kernel32.GetExitCodeProcess = get_exit
            kernel32.GetProcessTimes = get_times
            kernel32.CloseHandle = lambda handle: None
            return kernel32

        def windows_pid_check(kernel32):
            with (
                patch("os.name", "nt"),
                patch("ctypes.WinDLL", lambda *args, **kwargs: kernel32, create=True),
                patch("ctypes.get_last_error", lambda: 0, create=True),
                patch.dict("sys.modules", {"ctypes.wintypes": fake_wintypes}),
            ):
                return local_orchestrator_module._process_start_epoch(4242)

        expected_epoch = (310_000_000 << 32) / 10_000_000 - 11_644_473_600

        # Exit code 0: el proceso ya terminó — pid muerto.
        self.assertIsNone(
            windows_pid_check(fake_kernel32(exit_code=0)),
            "a terminated process is not a live driver",
        )
        # Exit code 259 (STILL_ACTIVE): sigue vivo — la hora nace de
        # GetProcessTimes.
        epoch = windows_pid_check(fake_kernel32(exit_code=259))
        self.assertIsInstance(epoch, float)
        self.assertEqual(epoch, expected_epoch)
        # GetExitCodeProcess falla: incomprobable, la marca lee viva.
        checked = windows_pid_check(fake_kernel32(exit_code=259, fail_exit_code=True))
        self.assertIsInstance(checked, local_orchestrator_module._UncheckedPid)
        self.assertIn("GetExitCodeProcess refused", checked.cause)
        mark = {"workflow_id": "wf", "outcome": "running", "pid": 4242}
        with (
            patch("os.name", "nt"),
            patch(
                "ctypes.WinDLL",
                lambda *args, **kwargs: fake_kernel32(fail_exit_code=True),
                create=True,
            ),
            patch("ctypes.get_last_error", lambda: 0, create=True),
            patch.dict("sys.modules", {"ctypes.wintypes": fake_wintypes}),
        ):
            self.assertTrue(
                local_orchestrator_module.driver_mark_live(mark),
                "an uncheckable exit code never reads as a dead driver",
            )

    # review F6 — un resultado JSON que no es objeto no revienta
    def test_a_result_file_that_is_not_an_object_proves_no_stop(self):
        # Review F6: [1], "paused" o 5 son JSON válido y ninguna salida de
        # driver — leerlos no puede reventar driver_stopped ni close_hint,
        # y jamás cuenta como evidencia de parada.
        first = self.paused()
        result_file = self.root / "goncloud-local" / f"{first['workflow_id']}.json"
        for payload in ("[1, 2]", '"paused"', "5"):
            with self.subTest(payload=payload):
                result_file.write_text(payload, encoding="utf-8")
                self.assertFalse(
                    local_orchestrator_module.driver_stopped(json.loads(payload))
                )
                self.assertIsNone(
                    self.orchestrator.close_hint(first["workflow_id"], home=self.root)
                )
                with self.assertRaises(WorkflowError):
                    self.close(first["workflow_id"], reason="no")

    # review F7 — un spawn que falla no deja la marca del lanzador
    def test_a_failed_spawn_leaves_no_running_mark_of_the_launcher(self):
        # Review F7: la marca running se escribe antes de Popen; si el
        # spawn falla, el pid del lanzador (vivo durante días) quedaba como
        # driver vivo para siempre. El fallo de lanzamiento es la última
        # palabra del archivo: una salida parada que el siguiente start
        # cierra y reintenta.
        first = self.paused()

        with (
            patch(
                "local_orchestrator.subprocess.Popen",
                side_effect=OSError("no hay memoria"),
            ),
            self.assertRaises(OSError),
        ):
            self.orchestrator.launch_detached(first["workflow_id"], home=self.root)

        written = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(written["outcome"], "error")
        self.assertIn("launch failed", written["error"])
        self.assertNotIn("pid", written, "no launcher pid posing as a driver")

        second = self.start(request_key="second-chain")

        self.assertEqual(second["outcome"], "delivered")
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, first["workflow_id"]).status, "archived")


class AvisoMailboxTests(_OrchestratorHarness):
    """Encargo H: every stop a run lands leaves one mailbox event, redacted.

    The cron despertar-raices drains the mailbox and tells claw with zero
    model calls; these tests pin the writer side: one event per stopped run,
    bounded and sanitized words, nothing for a live "running" mark.
    """

    def avisos(self):
        return self.root / "goncloud-local" / "avisos"

    def read_event(self, name):
        return json.loads((self.avisos() / name).read_text(encoding="utf-8"))

    def test_a_blocked_run_leaves_one_mailbox_event(self):
        self.responses["implementer"] = fenced(
            envelope(
                status="blocked",
                summary="Missing credentials for the Odoo login.",
                evidence=["login step refused: 2fa"],
            )
        )
        result = self.start()
        self.assertEqual(result["outcome"], "blocked")
        name = f"{result['workflow_id']}-{result['run_id']}-blocked.json"
        event = self.read_event(name)
        self.assertEqual(
            set(event),
            {
                "workflow_id",
                "repo",
                "outcome",
                "etapa",
                "motivo",
                "instruccion",
                "fecha",
            },
        )
        self.assertEqual(event["workflow_id"], result["workflow_id"])
        self.assertEqual(event["repo"], str(self.repo.resolve()))
        self.assertEqual(event["outcome"], "blocked")
        self.assertEqual(event["etapa"], "implementer")
        self.assertEqual(event["motivo"], "Missing credentials for the Odoo login.")
        self.assertEqual(
            event["instruccion"],
            "Inspect this repository without external actions.",
        )
        self.assertTrue(event["fecha"])

    def test_a_key_shaped_reason_reaches_the_mailbox_redacted(self):
        token = "ghp_Abc123Def456Ghi789"
        self.responses["implementer"] = fenced(
            envelope(status="blocked", summary=f"push refused for {token}")
        )
        result = self.start()
        name = f"{result['workflow_id']}-{result['run_id']}-blocked.json"
        raw = (self.avisos() / name).read_text(encoding="utf-8")
        self.assertNotIn(token, raw)
        self.assertIn("[redacted]", self.read_event(name)["motivo"])

    def test_a_delivered_run_leaves_one_mailbox_event(self):
        result = self.start()
        self.assertEqual(result["outcome"], "delivered")
        event = self.read_event(
            f"{result['workflow_id']}-{result['run_id']}-delivered.json"
        )
        self.assertEqual(event["outcome"], "delivered")
        self.assertIsNone(event["etapa"])
        self.assertEqual(event["repo"], str(self.repo.resolve()))
        # One run, one event: resuming the delivered chain writes nothing new.
        self.assertEqual(len(list(self.avisos().iterdir())), 1)

    def test_a_delivered_medium_rejection_names_pending_in_the_event(self):
        self.reviews = [{"decision": "reject", "severity": "medium"}]
        result = self.start()
        self.assertEqual(result["outcome"], "delivered")
        event = self.read_event(
            f"{result['workflow_id']}-{result['run_id']}-delivered.json"
        )
        self.assertEqual(event["outcome"], "delivered")
        self.assertEqual(
            [p["severity"] for p in event["pendientes_no_bloqueantes"]],
            ["medium"],
        )

    def test_a_waiting_run_names_the_pending_stage(self):
        first = self.start(stop_after="scout")
        self.service("ingenieria").consult(
            task_id=first["stages"]["ingenieria"]["id"],
            target_role="scout",
            question="Verify dependency evidence.",
            request_key="peer-1",
        )
        result = self.orchestrator.resume(first["workflow_id"], home=self.root)
        self.assertEqual(result["outcome"], "waiting")
        event = self.read_event(
            f"{result['workflow_id']}-{result['run_id']}-waiting.json"
        )
        self.assertEqual(event["etapa"], "ingenieria")
        self.assertEqual(event["motivo"], "a stage waits on its parents.")

    def test_a_paused_checkpoint_leaves_its_own_event(self):
        result = self.start(stop_after="scout")
        self.assertEqual(result["outcome"], "paused")
        event = self.read_event(
            f"{result['workflow_id']}-{result['run_id']}-paused.json"
        )
        self.assertEqual(event["outcome"], "paused")
        self.assertEqual(event["etapa"], "ingenieria")
        self.assertEqual(event["motivo"], "paused at its stop_after checkpoint.")

    def test_a_key_shaped_repo_reaches_the_mailbox_redacted(self):
        # cr 4312376359: the repository path travels into the event under
        # the same redaction as every other word, never as a place to hide
        # a token-shaped secret.
        secret = self.root / "ghp_Abc123Def456Ghi789"
        secret.mkdir()
        result = self.start(repository=str(secret))
        self.assertEqual(result["outcome"], "delivered")
        name = f"{result['workflow_id']}-{result['run_id']}-delivered.json"
        raw = (self.avisos() / name).read_text(encoding="utf-8")
        self.assertNotIn("ghp_Abc123Def456Ghi789", raw)
        self.assertIn("[redacted]", self.read_event(name)["repo"])

    def test_a_driver_quitting_running_leaves_its_own_event(self):
        # Live review F3: the router's task_failure path is the one quit
        # that happens with a stage still live. The process ends, close
        # refuses the chain and close_hint stays silent, so the mailbox is
        # the only channel left: claw hears the jam and relaunches. (The
        # running mark a start writes goes through _write_result, never
        # through this event.)
        first = self.start(stop_after="scout")
        before = {p.name for p in self.avisos().iterdir()}
        self.assertTrue(any(name.endswith("paused.json") for name in before))
        self.orchestrator._report(
            first["workflow_id"], "running", home=self.root, run_id="liverun"
        )
        event = self.read_event(f"{first['workflow_id']}-liverun-running.json")
        self.assertEqual(event["outcome"], "running")
        self.assertEqual(event["etapa"], "ingenieria")
        self.assertEqual(
            event["motivo"], "the driver quit with the stage still unfinished."
        )

    def test_a_broken_mailbox_keeps_the_real_result(self):
        # Live F2 + cr 4312376366: the mailbox is best effort. A stop the
        # result file already published is the truth; a mailbox that cannot
        # be written (full disk, permission slip, Windows lock) is swallowed
        # — never propagated, because the detached bootstrap's catch would
        # overwrite the real outcome with its generic error.
        first = self.start(stop_after="scout")
        before = {p.name for p in self.avisos().iterdir()}
        barrier = self.root / "barrier"
        barrier.write_text("not a directory", encoding="utf-8")
        with patch.object(
            local_orchestrator_module, "_home_raiz", return_value=barrier
        ):
            result = self.orchestrator.resume(first["workflow_id"], home=self.root)
        self.assertEqual(result["outcome"], "delivered")
        durable = json.loads(
            (self.root / "goncloud-local" / f"{first['workflow_id']}.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(durable["outcome"], "delivered")
        self.assertEqual(durable["run_id"], result["run_id"])
        # The broken mailbox published nothing, and the real events stay.
        self.assertEqual({p.name for p in self.avisos().iterdir()}, before)

    def test_the_event_is_idempotent_by_name(self):
        first = self.start(stop_after="scout")
        report = {"outcome": "paused", "workflow_id": first["workflow_id"]}
        name = f"{first['workflow_id']}-same-run-paused.json"
        for _ in range(2):
            local_orchestrator_module._write_aviso(
                first["workflow_id"],
                "same-run",
                "paused",
                report,
                home=self.root,
                repo="owner/repo",
                instruction="i",
            )
            # The chain's own checkpoint event stays untouched beside it.
            self.assertEqual(
                [p.name for p in self.avisos().iterdir() if "same-run" in p.name],
                [name],
            )

    def test_a_profile_home_climbs_to_the_root_mailbox(self):
        first = self.start(stop_after="scout")
        profile = self.root / "profiles" / "claw"
        result = self.orchestrator.resume(first["workflow_id"], home=profile)
        self.assertEqual(result["outcome"], "delivered")
        # The writer's home is the profile; the event lands at the root,
        # where the cron in the default profile reads it.
        self.assertTrue(
            (
                self.avisos()
                / f"{result['workflow_id']}-{result['run_id']}-delivered.json"
            ).exists()
        )
        self.assertFalse((profile / "goncloud-local" / "avisos").exists())


class LocalCloseHandlerTests(_OrchestratorHarness):
    """The tool surface: registration, the claw-only guard, and the receipt."""

    def handlers(self, profile):
        plugin = _plugin_package()
        registered = {}
        hooks = []

        class Ctx:
            def __init__(self, profile_name):
                self.profile_name = profile_name

            def register_tool(self, **kwargs):
                registered[kwargs["name"]] = kwargs

            def register_hook(self, name, hook):
                hooks.append((name, hook))

        ctx = Ctx(profile)
        plugin.register(ctx)
        return registered, hooks

    def test_registration_declares_the_close_tool(self):
        registered, _ = self.handlers("claw")

        self.assertIn("goncloud_local_close", registered)
        tool = registered["goncloud_local_close"]
        self.assertEqual(tool["toolset"], "goncloud_team")
        self.assertEqual(tool["schema"]["name"], "goncloud_local_close")
        self.assertEqual(
            set(tool["schema"]["parameters"]["required"]),
            {"workflow_id", "reason"},
        )

    def test_only_the_claw_director_may_close(self):
        for profile in ("scout", "operaciones", "reviewer", "entrega"):
            registered, _ = self.handlers(profile)
            handler = registered["goncloud_local_close"]["handler"]
            with self.subTest(profile=profile):
                result = json.loads(handler({"workflow_id": "t_x", "reason": "r"}))
                self.assertIn("error", result)
                self.assertIn("claw", result["error"])

    def test_the_claw_handler_closes_and_status_announces_the_close(self):
        # paused() already wrote the driver's own paused outcome file.
        first = self.paused()
        registered, _ = self.handlers("claw")
        close_handler = registered["goncloud_local_close"]["handler"]
        status_handler = registered["goncloud_local_status"]["handler"]
        environment = {
            "HERMES_KANBAN_DB": str(self.db),
            "GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db"),
            "HERMES_HOME": str(self.root),
        }
        with patch.dict(os.environ, environment):
            status = json.loads(status_handler({"workflow_id": first["workflow_id"]}))
            self.assertIn("goncloud_local_close", status["close_hint"])

            receipt = json.loads(
                close_handler(
                    {
                        "workflow_id": first["workflow_id"],
                        "reason": "owner closed",
                    }
                )
            )
            self.assertEqual(receipt["outcome"], "closed")

            closed = json.loads(status_handler({"workflow_id": first["workflow_id"]}))
            self.assertIsNone(closed["close_hint"])

    def test_status_reports_the_pid_check_cause(self):
        # Regla E (review F11): the uncheckable-pid cause rides on what
        # goncloud_local_status returns — not on a discarded local copy.
        first = self.paused()
        results = self.root / "goncloud-local"
        results.mkdir(exist_ok=True)
        (results / f"{first['workflow_id']}.json").write_text(
            json.dumps(
                {
                    "workflow_id": first["workflow_id"],
                    "outcome": "running",
                    "run_id": "mark-run",
                    "pid": 99999999,
                    "started_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                }
            ),
            encoding="utf-8",
        )

        def sin_ps(*args, **kwargs):
            raise OSError("no hay ps")

        registered, _ = self.handlers("claw")
        status_handler = registered["goncloud_local_status"]["handler"]
        environment = {
            "HERMES_KANBAN_DB": str(self.db),
            "GONCLOUD_SUBSCRIPTION_DB": str(self.root / "routes.db"),
            "HERMES_HOME": str(self.root),
        }
        with patch.dict(os.environ, environment):
            with patch.object(local_orchestrator_module.subprocess, "run", sin_ps):
                reported = json.loads(
                    status_handler({"workflow_id": first["workflow_id"]})
                )

        self.assertEqual(reported["driver"]["outcome"], "running")
        self.assertIn("ps unavailable", reported["driver"]["pid_check"])


class _ChainWorktreeHarnessMixin:
    """Helpers de worktree sin pruebas propias (L2): las clases de
    pruebas los mezclan sin heredar ni re-correr la batería ajena."""

    def _git_repo(self):
        """A real temp git repo with one commit; returns its base sha."""
        self.responses = {}

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        git("add", "README.md")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "init",
        )
        sha = git("rev-parse", "HEAD").stdout.strip()
        self.base_sha = sha
        return sha

    def worktree_root(self):
        return self.root / "wt"

    def original_is_untouched(self, sha):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        self.assertEqual(
            git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip(), "main"
        )
        self.assertEqual(git("rev-parse", "HEAD").stdout.strip(), sha)
        self.assertEqual(git("status", "--porcelain").stdout.strip(), "")

    def committing_invoke(self):
        """An invoke whose implementer writes and commits inside the
        workspace the router handed it — and records that workspace."""
        original = self.invoke
        worked_in = []

        def invoke(request, route):
            if request.role != "implementer":
                return original(request, route)
            workspace = Path(request.workspace)
            worked_in.append(str(workspace))
            (workspace / "cambio.txt").write_text("cambio\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(workspace), "add", "cambio.txt"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(workspace),
                    "-c",
                    "user.name=t",
                    "-c",
                    "user.email=t@example.invalid",
                    "commit",
                    "-qm",
                    "trabajo de la cadena",
                ],
                check=True,
                capture_output=True,
            )
            return original(request, route)

        return invoke, worked_in

    def _common_exclude_path(self):
        comun = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--git-common-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        comun_path = Path(comun)
        if not comun_path.is_absolute():
            comun_path = self.repo / comun_path
        return comun_path / "info" / "exclude"

    def _common_exclude_lines(self):
        return self._common_exclude_path().read_text(encoding="utf-8").splitlines()

    def _git_status(self, cwd):
        return subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain", "-uall"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout


class ChainWorktreeTests(_ChainWorktreeHarnessMixin, _OrchestratorHarness):
    """r-H-cadena-worktree: cada cadena trabaja en SU worktree (rama
    cadena/<id>), jamás en el checkout que le pasan. Repos git reales
    temporales."""

    # (a) — la cadena crea SU worktree en rama cadena/<id> y el checkout
    # original no cambia de rama, HEAD ni estado tras etapas que escriben
    # y commitean.
    def test_a_chain_works_in_its_own_worktree(self):
        sha = self._git_repo()
        invoke, worked_in = self.committing_invoke()
        self.invoke = invoke

        result = self.start(revision=sha)

        self.assertEqual(result["outcome"], "delivered")
        worktree = result["worktree"]
        self.assertEqual(
            Path(worktree).resolve(),
            (
                self.worktree_root() / (f"{self.repo.name}-{result['workflow_id']}")
            ).resolve(),
        )
        branch = subprocess.run(
            ["git", "-C", worktree, "rev-parse", "--abbrev-ref", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(branch, f"cadena/{result['workflow_id']}")
        self.assertTrue(worked_in, "the stages ran")
        for workspace in worked_in:
            self.assertEqual(
                Path(workspace).resolve(),
                Path(worktree).resolve(),
                f"the stage worked outside the chain worktree: {workspace}",
            )
        self.original_is_untouched(sha)
        status = self.orchestrator.status(result["workflow_id"])
        self.assertEqual(status["worktree"], worktree)
        self.assertEqual(status["worktree_branch"], branch)

    # (b) — dos cadenas sobre el mismo repo tienen worktrees distintos.
    def test_two_chains_on_the_same_repo_get_distinct_worktrees(self):
        sha = self._git_repo()

        first = self.start(request_key="cadena-1", revision=sha)
        self.assertEqual(first["outcome"], "delivered")
        second = self.start(request_key="cadena-2", revision=sha)
        self.assertEqual(second["outcome"], "delivered")

        self.assertTrue(first["worktree"])
        self.assertTrue(second["worktree"])
        self.assertNotEqual(first["worktree"], second["worktree"])
        self.assertTrue(Path(first["worktree"]).is_dir())
        self.assertTrue(Path(second["worktree"]).is_dir())

    # (c) — cierre con worktree limpio lo quita; con commits propios lo
    # conserva.
    def test_closing_removes_a_clean_worktree_and_keeps_committed_ones(self):
        sha = self._git_repo()

        clean = self.paused(request_key="limpia", revision=sha)
        clean_wt = Path(clean["worktree"])
        self.assertTrue(clean_wt.is_dir())
        receipt = self.close(clean["workflow_id"], reason="sin trabajo")
        self.assertTrue(receipt["worktree"]["removed"])
        self.assertFalse(clean_wt.exists(), "the clean worktree is removed")

        invoke, _worked_in = self.committing_invoke()
        self.invoke = invoke
        committed = self.start(
            request_key="con-commits", revision=sha, stop_after="scout"
        )
        self.assertEqual(committed["outcome"], "paused")
        committed_wt = Path(committed["worktree"])
        self.assertTrue(committed_wt.is_dir())
        # El trabajo propio de la cadena: un commit en su rama (lo deja el
        # implementer cuando corre; aquí se simula duradero).
        (committed_wt / "trabajo.txt").write_text("trabajo\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(committed_wt), "add", "trabajo.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(committed_wt),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "-qm",
                "trabajo de la cadena",
            ],
            check=True,
            capture_output=True,
        )
        receipt = self.close(committed["workflow_id"], reason="tiene trabajo")
        self.assertFalse(
            receipt["worktree"]["removed"],
            "the chain's own commits keep the worktree alive",
        )
        self.assertEqual(receipt["worktree"]["reason"], "tiene commits sin publicar")
        self.assertTrue(committed_wt.is_dir())

    # F1 — si el worktree no se puede crear, la raíz recién insertada se
    # archiva: el repo queda libre y el siguiente start arranca (no busy).
    def test_a_failed_worktree_leaves_no_root_and_the_repo_free(self):
        sha = self._git_repo()
        bloqueo = self.root / "bloqueo"
        bloqueo.write_text("un archivo, no una carpeta\n", encoding="utf-8")
        started = None
        with patch.dict(os.environ, {"GONCLOUD_WORKTREE_ROOT": str(bloqueo / "wt")}):
            with self.assertRaises(WorkflowError):
                started = self.start(request_key="f1", revision=sha)

        self.assertIsNone(started, "nothing is returned for a failed chain")
        with connect_closing(self.db) as conn:
            roots = [
                task
                for task in list_tasks(conn, include_archived=True)
                if (task.body or "").startswith("GONCLOUD_ENGINEERING_V1")
            ]
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].status, "archived", "the fresh root is archived")

        siguiente = self.start(request_key="f1-siguiente", revision=sha)
        self.assertEqual(
            siguiente["outcome"],
            "delivered",
            "the next start runs: the failed one left no busy jam",
        )

    # F2 — la ronda correctiva apunta al worktree de la cadena.
    def test_the_corrective_round_points_at_the_worktree(self):
        sha = self._git_repo()
        self.reviews = [
            {"decision": "reject", "severity": "high"},
            {"decision": "approve", "severity": None},
        ]
        result = self.start(revision=sha)

        self.assertEqual(result["outcome"], "delivered")
        worktree = result["worktree"]
        self.assertTrue(result["corrective_stages"], "a corrective round opened")
        with connect_closing(self.db) as conn:
            for stage in result["corrective_stages"].values():
                card = get_task(conn, stage["id"])
                self.assertEqual(
                    Path(card.workspace_path).resolve(),
                    Path(worktree).resolve(),
                    "the corrective round works in the chain's worktree",
                )
                self.assertIn(str(worktree), card.body)

    # F3 — el cierre mide contra el SHA base real: revision="HEAD" con
    # commits propios en cadena/<id> se CONSERVA.
    def test_closing_with_head_revision_keeps_committed_worktrees(self):
        self._git_repo()
        chain = self.paused(request_key="head-rev", revision="HEAD")
        worktree = Path(chain["worktree"])
        (worktree / "trabajo.txt").write_text("trabajo\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(worktree), "add", "trabajo.txt"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "-qm",
                "trabajo propio",
            ],
            check=True,
            capture_output=True,
        )
        head_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        receipt = self.close(chain["workflow_id"], reason="tiene trabajo")

        self.assertFalse(
            receipt["worktree"]["removed"],
            "own commits against the real base SHA keep the worktree",
        )
        self.assertEqual(receipt["worktree"]["reason"], "tiene commits sin publicar")
        self.assertTrue(worktree.is_dir())
        kept_sha = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(kept_sha, head_sha)

    # F5 — rama cadena/<id> ya creada (restart concurrente o relanzamiento
    # tras limpiar la ruta): el worktree se añade SIN -b y la cadena corre.
    def test_a_start_reuses_an_existing_branch_without_minus_b(self):
        sha = self._git_repo()
        first = self.start(request_key="relanzada", revision=sha)
        worktree = Path(first["worktree"])
        branch = f"cadena/{first['workflow_id']}"
        # La ruta desaparece (limpieza manual) pero la rama queda: el
        # relanzamiento debe añadir el worktree SIN -b, no reventar.
        subprocess.run(
            ["git", "-C", str(self.repo), "worktree", "remove", str(worktree)],
            check=True,
            capture_output=True,
        )
        self.assertFalse(worktree.exists())
        branch_alive = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
        )
        self.assertEqual(branch_alive.returncode, 0)

        again = self.start(request_key="relanzada", revision=sha)

        self.assertEqual(again["outcome"], "delivered")
        self.assertTrue(worktree.is_dir(), "the worktree is re-added")

    # F12 — relanzar una cadena viva cuyo worktree falla NO archiva la
    # raíz preexistente: queda intacta y el error se propaga.
    def test_relaunch_failure_keeps_the_preexisting_root_alive(self):
        sha = self._git_repo()
        first = self.orchestrator.create(
            request_key="viva",
            repository=str(self.repo),
            revision=sha,
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )
        self.assertTrue(Path(first["worktree"]).is_dir())
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, first["workflow_id"]).status,
                "todo",
                "the chain exists alive: created, never launched",
            )

        bloqueo = self.root / "bloqueo"
        bloqueo.write_text("archivo\n", encoding="utf-8")
        with patch.dict(os.environ, {"GONCLOUD_WORKTREE_ROOT": str(bloqueo / "wt")}):
            with self.assertRaises(WorkflowError):
                self.orchestrator.create(
                    request_key="viva",
                    repository=str(self.repo),
                    revision=sha,
                    instruction="Inspect again.",
                    sources=SOURCES,
                    max_steps=5,
                )

        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, first["workflow_id"]).status,
                "todo",
                "the preexisting live root stays intact",
            )
            vivas = [
                task
                for task in list_tasks(conn, include_archived=True)
                if (task.body or "").startswith("GONCLOUD_ENGINEERING_V1")
            ]
        self.assertEqual(len(vivas), 1, "no extra roots, nothing archived")

        # Con la raíz buena, el mismo key corre: el worktree se reutiliza.
        again = self.orchestrator.start(
            request_key="viva",
            repository=str(self.repo),
            revision=sha,
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )
        self.assertEqual(again["outcome"], "delivered")

    # F13 — un probe de git que FALLA no degrada al checkout original:
    # error claro, nada activo se crea, y el repo no queda jamado.
    def test_a_failing_git_probe_creates_nothing(self):
        sha = self._git_repo()

        def timed_out(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=60)

        with patch("workflow.subprocess.run", timed_out):
            with self.assertRaises(WorkflowError) as ctx:
                self.start(request_key="f13", revision=sha)
        self.assertIn("git probe failed", str(ctx.exception))
        with connect_closing(self.db) as conn:
            active = [
                task
                for task in list_tasks(conn, include_archived=True)
                if (task.body or "").startswith("GONCLOUD_ENGINEERING_V1")
                and task.status not in {"done", "archived"}
            ]
        self.assertEqual(active, [], "nothing active is created")
        result = self.start(request_key="f13-despues", revision=sha)
        self.assertEqual(result["outcome"], "delivered", "the repo is not jammed")

    # F14 — archivos ignorados: el worktree se conserva y el cierre lo
    # dice (sin commits propios, vía .git/info/exclude).
    def test_a_worktree_with_ignored_files_is_kept_and_said(self):
        sha = self._git_repo()
        chain = self.paused(request_key="ignorados", revision=sha)
        worktree = Path(chain["worktree"])
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("ignorado.txt\n", encoding="utf-8")
        (worktree / "ignorado.txt").write_text("cache\n", encoding="utf-8")
        sanity = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain", "--ignored"],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertIn("ignorado.txt", sanity.stdout)

        receipt = self.close(chain["workflow_id"], reason="solo ignorados")

        self.assertFalse(
            receipt["worktree"]["removed"],
            "ignored files keep the worktree alive",
        )
        self.assertIn("ignorados", receipt["worktree"]["reason"])
        self.assertTrue(worktree.is_dir())

    # R4 — el exclude del git común trae las carpetas de estado CLI una
    # sola vez aunque se creen dos cadenas, y git status no las ve.
    def test_el_exclude_comun_ignora_el_estado_cli_una_sola_vez(self):
        sha = self._git_repo()
        first = self.start(request_key="exclude-1", revision=sha)
        second = self.start(request_key="exclude-2", revision=sha)
        self.assertEqual(first["outcome"], "delivered")
        self.assertEqual(second["outcome"], "delivered")
        lineas = self._common_exclude_lines()
        for linea in (
            ".claude/memory/",
            ".claude/sessions/",
            ".claude/state/",
            ".codex/",
            ".gemini/",
            ".grok/",
            ".kimi/",
            ".muse/",
            ".qwen/",
        ):
            self.assertEqual(
                lineas.count(linea),
                1,
                f"{linea} must appear exactly once in the common exclude",
            )
        for linea in (".claude/", ".cursor/", ".opencode/"):
            self.assertEqual(
                lineas.count(linea),
                0,
                f"{linea} must not hide versionable config in the common exclude",
            )
        worktree = Path(first["worktree"])
        estado = worktree / ".claude" / "sessions" / "x"
        estado.parent.mkdir(parents=True, exist_ok=True)
        estado.write_text("estado del CLI\n", encoding="utf-8")
        estado_principal = self.repo / ".claude" / "sessions" / "x"
        estado_principal.parent.mkdir(parents=True, exist_ok=True)
        estado_principal.write_text("estado del CLI\n", encoding="utf-8")
        for cwd in (worktree, self.repo):
            self.assertNotIn(".claude", self._git_status(cwd))

    # R4b — `.claude/` entero no va en el exclude: una skill nueva sí se
    # ve en git status; el estado de sesión, no.
    def test_el_exclude_comun_deja_visible_una_skill_nueva(self):
        sha = self._git_repo()
        result = self.start(request_key="exclude-visible", revision=sha)
        self.assertEqual(result["outcome"], "delivered")
        worktree = Path(result["worktree"])
        skill = worktree / ".claude" / "skills" / "x.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text("skill nueva\n", encoding="utf-8")
        sesion = worktree / ".claude" / "sessions" / "y"
        sesion.parent.mkdir(parents=True, exist_ok=True)
        sesion.write_text("estado del CLI\n", encoding="utf-8")
        estado_git = self._git_status(worktree)
        self.assertIn(".claude/skills/x.md", estado_git)
        self.assertNotIn(".claude/sessions/y", estado_git)

    # R4b — un exclude viejo de r2 con `.claude/` entero bajo el marcador
    # se migra a las líneas nuevas al arrancar la siguiente cadena.
    def test_el_exclude_viejo_con_claude_entero_se_migra(self):
        sha = self._git_repo()
        exclude = self._common_exclude_path()
        exclude.parent.mkdir(parents=True, exist_ok=True)
        viejas = [
            "# goncloud: carpetas de estado de los CLI",
            ".claude/",
            ".codex/",
            ".cursor/",
            ".gemini/",
            ".grok/",
            ".kimi/",
            ".muse/",
            ".opencode/",
            ".qwen/",
        ]
        exclude.write_text("".join(f"{linea}\n" for linea in viejas), encoding="utf-8")
        result = self.start(request_key="exclude-migra", revision=sha)
        self.assertEqual(result["outcome"], "delivered")
        lineas = self._common_exclude_lines()
        for linea in (".claude/", ".cursor/", ".opencode/"):
            self.assertEqual(lineas.count(linea), 0, f"{linea} must be migrated out")
        for linea in (
            ".claude/memory/",
            ".claude/sessions/",
            ".claude/state/",
            ".codex/",
            ".gemini/",
            ".grok/",
            ".kimi/",
            ".muse/",
            ".qwen/",
        ):
            self.assertEqual(
                lineas.count(linea),
                1,
                f"{linea} must appear exactly once after migration",
            )

    # R5 — las tarjetas hijas apuntan su workspace_path al worktree de
    # la cadena; la raíz sigue en el repo original.
    def test_las_hijas_apuntan_su_workspace_al_worktree(self):
        sha = self._git_repo()
        result = self.start(revision=sha)
        self.assertEqual(result["outcome"], "delivered")
        worktree = Path(result["worktree"]).resolve()
        with connect_closing(self.db) as conn:
            raiz = get_task(conn, result["workflow_id"])
            self.assertEqual(Path(raiz.workspace_path).resolve(), self.repo.resolve())
            for stage in result["stages"].values():
                tarjeta = get_task(conn, stage["id"])
                self.assertEqual(
                    Path(tarjeta.workspace_path).resolve(),
                    worktree,
                    "the child card must work in the chain worktree",
                )

    # (d) — un repository que no es git conserva la conducta de hoy.
    def test_a_non_git_repository_keeps_todays_behavior(self):
        result = self.start()

        self.assertEqual(result["outcome"], "delivered")
        self.assertIsNone(result.get("worktree"))
        self.assertEqual([r.role for r in self.requests], ORDER)
        self.assertFalse(self.worktree_root().exists(), "no worktree root is created")


class ReadOnlyHarnessStateTests(_ChainWorktreeHarnessMixin, _OrchestratorHarness):
    """Encargo H-readonly-harness: el estado que el propio CLI crea en su
    cwd (.claude/, .codex/, ...) no es trabajo de la etapa de solo
    lectura: no bloquea, no sale en la evidencia y se limpia al cerrar
    la etapa; un archivo seguido bajo esas carpetas sí cuenta."""

    def _git_repo(self, *, tracked_claude=False):
        sha = super()._git_repo()
        if not tracked_claude:
            return sha
        guia = self.repo / ".claude" / "skills" / "guia.md"
        guia.parent.mkdir(parents=True)
        guia.write_text("guia base\n", encoding="utf-8")

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("add", ".claude/skills/guia.md")
        git(
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.invalid",
            "commit",
            "-qm",
            "skill seguida",
        )
        return git("rev-parse", "HEAD").stdout.strip()

    def _scout_writes(self, *relpaths):
        original = self.invoke

        def invoke(request, route):
            if request.role == "scout":
                workspace = Path(request.workspace)
                for rel in relpaths:
                    destino = workspace / rel
                    destino.parent.mkdir(parents=True, exist_ok=True)
                    destino.write_text("estado del CLI\n", encoding="utf-8")
            return original(request, route)

        self.invoke = invoke

    def _scout_edits(self, rel, texto):
        original = self.invoke

        def invoke(request, route):
            if request.role == "scout":
                (Path(request.workspace) / rel).write_text(texto, encoding="utf-8")
            return original(request, route)

        self.invoke = invoke

    # (1) — solo estado del CLI: no bloquea y lo nuevo desaparece.
    def test_solo_estado_del_cli_no_bloquea_y_se_borra(self):
        sha = self._git_repo()
        self._scout_writes(".claude/sessions/x")

        result = self.start(revision=sha, stop_after="scout")

        self.assertEqual(result["outcome"], "paused")
        worktree = Path(result["worktree"])
        self.assertFalse(
            (worktree / ".claude" / "sessions" / "x").exists(),
            "the CLI state created by the stage is cleaned at stage close",
        )
        self.assertFalse(
            (worktree / ".claude").exists(),
            "folders left empty by the cleanup go too",
        )

    # F6 — una skill nueva bajo .claude/ NO es estado: bloquea y el
    # archivo se queda (la lista única ya no trata .claude/ entero ni
    # .cursor/ ni .opencode/ como estado).
    def test_una_skill_nueva_bajo_claude_si_bloquea_y_no_se_borra(self):
        sha = self._git_repo()
        self._scout_writes(".claude/skills/x.md")

        result = self.start(revision=sha, stop_after="scout")

        self.assertEqual(result["outcome"], "blocked")
        skill = Path(result["worktree"]) / ".claude" / "skills" / "x.md"
        self.assertTrue(skill.exists(), "versionable config is never cleaned")
        self.assertEqual(skill.read_text(encoding="utf-8"), "estado del CLI\n")

    # F6 (unidad) — .cursor/ y .opencode/ cuentan como cambio y no se
    # limpian; .claude/sessions/ sigue siendo estado: no cuenta y se borra.
    def test_cursor_y_opencode_cuentan_y_las_sesiones_no(self):
        self._git_repo()
        huella = local_orchestrator_module._workspace_fingerprint
        for rel in (
            ".claude/skills/x.md",
            ".cursor/rules/x.md",
            ".opencode/agente.md",
        ):
            destino = self.repo / rel
            destino.parent.mkdir(parents=True, exist_ok=True)
            destino.write_text("config nueva\n", encoding="utf-8")
        con_config = huella(self.repo)
        self.assertIsNotNone(con_config)
        sesion = self.repo / ".claude" / "sessions" / "y"
        sesion.parent.mkdir(parents=True, exist_ok=True)
        sesion.write_text("estado del CLI\n", encoding="utf-8")
        self.assertEqual(
            con_config,
            huella(self.repo),
            "CLI session state must not change the fingerprint",
        )
        estado = local_orchestrator_module._cli_state_untracked(self.repo)
        self.assertEqual(estado, {".claude/sessions/y"})
        local_orchestrator_module._clean_new_cli_state(self.repo, set())
        self.assertFalse(sesion.exists(), "new CLI state is cleaned")
        for rel in (
            ".claude/skills/x.md",
            ".cursor/rules/x.md",
            ".opencode/agente.md",
        ):
            self.assertTrue(
                (self.repo / rel).exists(), f"{rel} must survive the cleanup"
            )

    # M1 — un nombre no-UTF-8 bajo el estado no tumba _cli_state_untracked:
    # la salida -z se decodifica utf-8/surrogateescape y un
    # UnicodeDecodeError (ValueError) se lee como None. Simulado: APFS no
    # admite nombres no-UTF-8 en disco.
    def test_cli_state_untracked_tolera_un_nombre_no_utf8(self):
        capturado = {}

        def fake_run(*args, **kwargs):
            capturado.update(kwargs)
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="?? .claude/sessions/y\0!! .codex/state/z\0",
                stderr="",
            )

        with patch.object(local_orchestrator_module.subprocess, "run", fake_run):
            estado = local_orchestrator_module._cli_state_untracked(self.repo)

        self.assertEqual(estado, {".claude/sessions/y", ".codex/state/z"})
        self.assertEqual(capturado.get("encoding"), "utf-8")
        self.assertEqual(capturado.get("errors"), "surrogateescape")
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        with patch.object(
            local_orchestrator_module.subprocess, "run", side_effect=error
        ):
            self.assertIsNone(local_orchestrator_module._cli_state_untracked(self.repo))

    # L3 — un symlink dentro del estado que apunta fuera del repo: la
    # limpieza no sigue el enlace y el archivo externo sobrevive.
    def test_un_enlace_en_el_estado_no_borra_el_archivo_externo(self):
        sha = self._git_repo()
        externo = self.root / "fuera-del-repo.txt"
        externo.write_text("no tocar\n", encoding="utf-8")
        original = self.invoke

        def invoke(request, route):
            if request.role == "scout":
                destino = Path(request.workspace) / ".claude" / "sessions" / "evil"
                destino.parent.mkdir(parents=True, exist_ok=True)
                try:
                    destino.symlink_to(externo)
                except OSError as exc:
                    self.skipTest(f"sin symlinks en esta plataforma: {exc}")
            return original(request, route)

        self.invoke = invoke

        result = self.start(revision=sha, stop_after="scout")

        self.assertEqual(result["outcome"], "paused")
        self.assertTrue(externo.exists(), "the file outside the repo survives")
        self.assertEqual(externo.read_text(encoding="utf-8"), "no tocar\n")

    # (2) — editar un archivo real sigue bloqueando (control).
    def test_editar_un_archivo_real_si_bloquea(self):
        sha = self._git_repo()
        self._scout_edits("README.md", "tocado por scout\n")

        result = self.start(revision=sha, stop_after="scout")

        self.assertEqual(result["outcome"], "blocked")
        worktree = Path(result["worktree"])
        self.assertEqual(
            (worktree / "README.md").read_text(encoding="utf-8"),
            "tocado por scout\n",
            "a real edit is left for the owner to revert, never cleaned",
        )

    # (3) — un archivo SEGUIDO bajo .claude/ sí cuenta como cambio.
    def test_modificar_un_seguido_bajo_claude_si_bloquea(self):
        sha = self._git_repo(tracked_claude=True)
        self._scout_edits(".claude/skills/guia.md", "guia tocada\n")

        result = self.start(revision=sha, stop_after="scout")

        self.assertEqual(result["outcome"], "blocked")

    # (4) — lo que ya estaba antes de la etapa no se toca; lo nuevo sí
    # se borra, en cualquier carpeta de estado.
    def test_estado_previo_se_queda_y_el_nuevo_se_borra(self):
        sha = self._git_repo()
        created = self.orchestrator.create(
            request_key="local-1",
            repository=str(self.repo),
            revision=sha,
            instruction="Inspect this repository without external actions.",
            sources=SOURCES,
            max_steps=17,
        )
        worktree = Path(created["worktree"])
        previo = worktree / ".claude" / "sessions" / "viejo"
        previo.parent.mkdir(parents=True)
        previo.write_text("ya estaba\n", encoding="utf-8")
        self._scout_writes(".claude/sessions/nuevo", ".codex/state/y")

        result = self.orchestrator.resume(
            created["workflow_id"], stop_after="scout", home=self.root
        )

        self.assertEqual(result["outcome"], "paused")
        self.assertTrue(previo.exists(), "pre-existing CLI state is untouched")
        self.assertFalse(
            (worktree / ".claude" / "sessions" / "nuevo").exists(),
            "state created during the stage is cleaned",
        )
        self.assertFalse(
            (worktree / ".codex").exists(),
            "folders left empty by the cleanup go too",
        )

    # R1 — la huella ignora lo no seguido del harness (y su evidencia
    # tampoco lo lista), pero no un seguido bajo .claude/.
    def test_la_huella_ignora_el_estado_no_seguido_y_no_al_seguido(self):
        self._git_repo(tracked_claude=True)
        antes = local_orchestrator_module._workspace_fingerprint(self.repo)
        self.assertIsNotNone(antes)
        sesiones = self.repo / ".claude" / "sessions"
        sesiones.mkdir(parents=True)
        (sesiones / "x").write_text("estado del CLI\n", encoding="utf-8")

        despues = local_orchestrator_module._workspace_fingerprint(self.repo)

        self.assertEqual(antes, despues, "untracked CLI state is not a change")
        bloqueo = local_orchestrator_module._edited_by_read_only_stage(
            "scout", antes, despues
        )
        self.assertNotIn(".claude", " ".join(bloqueo["evidence"]))

        (self.repo / ".claude" / "skills" / "guia.md").write_text(
            "guia tocada\n", encoding="utf-8"
        )
        tocada = local_orchestrator_module._workspace_fingerprint(self.repo)
        self.assertNotEqual(despues, tocada, "a tracked file under .claude counts")
        self.assertIn(".claude/skills/guia.md", tocada[1])

    # L1 — probe_workspace tolera un cleanup que falla (en Windows un
    # archivo abierto lanza PermissionError): no debe escapar de probe().
    def test_probe_workspace_tolera_un_cleanup_que_falla(self):
        from subscriptions import process as process_module

        workspace = process_module.probe_workspace()
        self.assertTrue(
            workspace._ignore_cleanup_errors,
            "probe_workspace must ignore cleanup errors",
        )
        (Path(workspace.name) / "probe.txt").write_text("x", encoding="utf-8")

        def rmtree_que_falla(path, ignore_errors=False):
            if not ignore_errors:
                raise PermissionError("archivo abierto (simulado)")

        with patch.object(
            process_module.tempfile.TemporaryDirectory,
            "_rmtree",
            staticmethod(rmtree_que_falla),
        ):
            workspace.cleanup()

    # R3/L4 — los probes de los 6 adaptadores corren en un directorio
    # temporal, no en el cwd del proceso (que puede ser el checkout
    # principal de la tarjeta). Muse incluido su camino WSL y opencode
    # incluido _major().
    def test_los_probes_corren_en_un_directorio_temporal(self):
        from subscriptions.adapters.claude import ClaudeAdapter
        from subscriptions.adapters.codex import CodexAdapter
        from subscriptions.adapters.grok import GrokAdapter
        from subscriptions.adapters.kimi import KimiAdapter
        from subscriptions.adapters.muse import MuseAdapter
        from subscriptions.adapters.opencode import OpenCodeAdapter
        from subscriptions.types import RouteSpec

        def grabadora(respuestas):
            llamadas = []

            def runner(command, **kwargs):
                llamadas.append((list(command), kwargs.get("cwd")))
                salida = respuestas(list(command))
                return subprocess.CompletedProcess(command, 0, stdout=salida, stderr="")

            return llamadas, runner

        def ruta(adapter, model):
            return RouteSpec(
                slot=1,
                label=f"{adapter}/x",
                adapter=adapter,
                model=model,
                billing_mode="subscription",
            )

        oauth = json.dumps(
            {
                "loggedIn": True,
                "authMethod": "oauth_token",
                "apiProvider": "firstParty",
            }
        )
        casos = [
            ("claude", ClaudeAdapter, "claude", "x", lambda cmd: oauth),
            (
                "codex",
                CodexAdapter,
                "codex",
                "x",
                lambda cmd: "Logged in using ChatGPT\n",
            ),
            (
                "grok",
                GrokAdapter,
                "grok",
                "grok-4.6",
                lambda cmd: "logged in, grok-4.6 disponible\n",
            ),
            ("kimi", KimiAdapter, "kimi", "x", lambda cmd: ""),
            ("muse", MuseAdapter, "muse", "x", lambda cmd: "muse 1.0\n"),
            (
                "opencode",
                OpenCodeAdapter,
                "opencode_go",
                "x",
                lambda cmd: (
                    "opencode v2.0.29\n"
                    if cmd[:2] == ["opencode", "--version"]
                    else "opencode go\n"
                ),
            ),
        ]
        for nombre, clase, adapter, model, respuestas in casos:
            with self.subTest(adapter=nombre):
                llamadas, runner = grabadora(respuestas)
                instancia = clase(runner=runner)
                health = instancia.probe(ruta(adapter, model))
                self.assertTrue(health.ready, f"{nombre}: {health}")
                if nombre == "opencode":
                    self.assertEqual(instancia._major(), 2)
                self.assertTrue(llamadas, f"{nombre}: el probe no corrio")
                for comando, cwd in llamadas:
                    self.assertIsNotNone(cwd, f"{nombre}: probe sin cwd")
                    self.assertNotEqual(
                        Path(cwd).resolve(),
                        Path.cwd().resolve(),
                        f"{nombre}: the probe must not run in the process cwd",
                    )
                    self.assertFalse(
                        Path(cwd).exists(),
                        f"{nombre}: the probe directory is gone afterwards",
                    )
                if nombre == "muse":
                    self.assertEqual(
                        llamadas[0][0][:4],
                        ["wsl.exe", "-d", "Ubuntu", "--exec"],
                        "muse probe must take its WSL path",
                    )


if __name__ == "__main__":
    unittest.main()
