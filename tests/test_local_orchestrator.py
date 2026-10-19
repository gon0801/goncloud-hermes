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
    add_comment,
    archive_task,
    complete_task,
    create_task,
    get_task,
    list_comments,
    list_runs,
    list_tasks,
)
from hermes_cli.kanban_db_connect import connect_closing, write_txn  # noqa: E402
from workflow import WorkflowError, WorkflowService  # noqa: E402
from subscriptions.router import SubscriptionRouter  # noqa: E402
from subscriptions.state import SubscriptionStateStore  # noqa: E402
from subscriptions.types import AdapterHealth, FailureKind, InvocationResult  # noqa: E402
from local_orchestrator import (  # noqa: E402
    LocalOrchestrator,
    close_hint,
    parse_terminal_envelope,
)
import local_orchestrator as local_orchestrator_module  # noqa: E402
from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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
            "    def resume(self, workflow_id, stop_after=None):\n"
            "        return {'workflow_id': workflow_id, 'outcome': 'completed',\n"
            "                'stop_after': stop_after}\n",
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
            "    def resume(self, workflow_id, stop_after=None):\n"
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
            subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
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

    def test_a_planning_stage_that_edits_the_repository_stops_the_chain(self):
        # Live 2026-09-28 (t_2748d636): ingenieria wrote and committed the
        # change itself; implementer then only confirmed it. Planning stages
        # must leave the repository exactly as they found it.
        for role in ("scout", "ingenieria"):
            with self.subTest(role=role):
                self.setUp()
                self._real_git_repo()
                invoke = LocalOrchestratorTests.invoke.__get__(self)

                def editing_invoke(request, route, role=role):
                    if request.role == role:
                        (Path(request.workspace) / "hola.txt").write_text(
                            "hola\n", encoding="utf-8"
                        )
                    return invoke(request, route)

                self.invoke = editing_invoke
                result = self.start()

                self.assertEqual(result["outcome"], "blocked")
                self.assertEqual(result["stages"][role]["status"], "blocked")
                self.assertNotIn("implementer", [r.role for r in self.requests])

    def test_a_planning_stage_that_leaves_the_repository_alone_proceeds(self):
        self._real_git_repo()
        result = self.start()
        self.assertEqual(result["outcome"], "delivered")

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
        self.reviews = [{"decision": "reject", "severity": "medium"}]
        first = self.start()
        self.assertEqual(first["outcome"], "blocked")
        # The driver writes its own blocked outcome when it stops on it.
        self.write_driver_outcome(first["workflow_id"], "blocked")

        receipt = self.close(first["workflow_id"], reason="blocked chain abandoned")

        self.assertEqual(receipt["done_stages"], sorted(ORDER[:5]))
        self.assertEqual(receipt["archived_stages"], ["entrega", "reviewer"])
        with connect_closing(self.db) as conn:
            self.assertEqual(
                get_task(conn, first["stages"]["reviewer"]["id"]).status, "archived"
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
        # not one that quit) refuses too; the paused stop goes through.
        self.write_driver_outcome(first["workflow_id"], "running")
        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
        # Live review F8: an illegible file proves nothing either — the
        # close fails closed, like a driver that may still be running.
        (self.root / "goncloud-local" / f"{first['workflow_id']}.json").write_text(
            "{broken", encoding="utf-8"
        )
        self.assertIsNone(
            self.orchestrator.close_hint(first["workflow_id"], home=self.root)
        )
        with self.assertRaisesRegex(WorkflowError, "driver"):
            self.close(first["workflow_id"], reason="no")
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
        # refuse until that driver's own outcome is the last write.
        first = self.paused()
        with patch(
            "local_orchestrator.subprocess.Popen",
            lambda *args, **kwargs: type("P", (), {"pid": 4242})(),
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
        # write: the hint announces the close and the close accepts it.
        first = self.paused()
        with patch(
            "local_orchestrator.subprocess.Popen",
            lambda *args, **kwargs: type("P", (), {"pid": 4242})(),
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
        first = self.paused()
        with patch(
            "local_orchestrator.subprocess.Popen",
            lambda *args, **kwargs: type("P", (), {"pid": 4242})(),
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

    def test_a_triage_root_without_graph_closes_and_frees_the_repository(self):
        # Live review F3: workflow.py inserts the root in triage and
        # decomposes afterwards; a failure in between leaves a root that
        # blocks the repository forever, with no driver and no stages.
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
        with self.assertRaisesRegex(WorkflowError, "already has active"):
            self.start(request_key="otra-cadena")

        self.assertIsNone(self.orchestrator.close_hint(root_id, home=self.root))
        receipt = self.close(root_id, reason="descomposicion fallida")
        self.assertEqual(receipt["outcome"], "closed")
        self.assertEqual(receipt["archived_stages"], [])
        self.assertEqual(receipt["done_stages"], [])
        with connect_closing(self.db) as conn:
            self.assertEqual(get_task(conn, root_id).status, "archived")
        # The repository accepts new chains again.
        created = self.orchestrator.create(
            request_key="next-after-triage",
            repository=str(self.repo),
            revision="baseline",
            instruction="Inspect again.",
            sources=SOURCES,
            max_steps=5,
        )
        self.assertNotEqual(created["workflow_id"], root_id)


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


if __name__ == "__main__":
    unittest.main()
