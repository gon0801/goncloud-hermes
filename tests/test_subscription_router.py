from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))

from subscriptions.catalog import compile_profile_routes  # noqa: E402
from subscriptions.router import GitInspector, SubscriptionRouter  # noqa: E402
from subscriptions.state import SubscriptionStateStore  # noqa: E402
from subscriptions.types import (  # noqa: E402
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
)

SOURCE = {
    "profile_names": {"main": "claw"},
    "profile_inheritance": {"entrega": "main"},
    "chains": {
        "main": [
            "opencode-go/muse-spark-1.3-contributor",
            "opencode/muse-spark-1.3-contributor-free",
            "anthropic/claude-sonnet-5",
            "kimi/k3",
            "opencode/muse-spark-1.3-contributor-free dup",
            "zai/glm-5.3",
            "xai/grok-4.6",
            "opencode/gpt-6-astra",
        ]
    },
}


class ScriptedAdapter:
    billing_mode = "subscription"

    def __init__(self, name):
        self.name = name
        self.result = None
        self.requests = []

    def probe(self, route):
        return AdapterHealth(ready=True, account_class=f"{self.name}_test")

    def invoke(self, request, route):
        self.requests.append(request)
        if callable(self.result):
            return self.result(request, route)
        return self.result


def completed(text="ok", adapter="claude"):
    return InvocationResult(
        success=True,
        text=text,
        adapter=adapter,
        model="model-x",
        billing_mode="subscription",
        session_id="s1",
    )


def failed(kind, adapter="adapter", text=""):
    return InvocationResult(
        success=False,
        text=text or f"{adapter} invocation failed ({kind.value})",
        adapter=adapter,
        model="model-x",
        billing_mode="subscription",
        failure=kind,
    )


def routes_for_main():
    return compile_profile_routes("claw", SOURCE)


def request(workspace, workflow_id="wf-6", stage_id="st-6", prompt="Do it once."):
    return InvocationRequest(
        workflow_id=workflow_id,
        stage_id=stage_id,
        role="implementer",
        prompt=prompt,
        workspace=workspace,
        start_revision="",
        current_revision="",
    )


class RouterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.db_path = self.workspace / "router.db"
        self.fake_adapters = {
            name: ScriptedAdapter(name)
            for name in (
                "opencode_go",
                "opencode_zen",
                "claude",
                "kimi",
                "muse",
                "zai",
                "grok",
                "codex",
                "deepseek",
            )
        }

    def router(self):
        router = SubscriptionRouter(
            adapters=self.fake_adapters,
            db_path=self.db_path,
            routing_source=SOURCE,
        )
        router.calls = []
        original_invoke = router._invoke_route

        def invoke_and_record(request, route):
            router.calls.append(route.adapter)
            return original_invoke(request, route)

        router._invoke_route = invoke_and_record
        return router


class FailoverTests(RouterTestCase):
    def test_auth_and_quota_advance_in_order(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.AUTH)
        self.fake_adapters["opencode_zen"].result = failed(FailureKind.QUOTA)
        self.fake_adapters["claude"].result = completed("ok")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.text, "ok")
        self.assertEqual(router.calls, ["opencode_go", "opencode_zen", "claude"])

    def test_timeout_without_mutation_advances(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TIMEOUT)
        self.fake_adapters["opencode_zen"].result = completed("zen ok", "opencode_zen")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "opencode_zen"])

    def test_non_advancing_failure_blocks_the_stage(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TASK_FAILURE)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(router.calls, ["opencode_go"])

    def test_exhausted_chain_blocks_with_owner_facing_evidence(self):
        router = self.router()
        for name in (
            "opencode_go",
            "opencode_zen",
            "claude",
            "kimi",
            "muse",
            "zai",
            "grok",
            "codex",
        ):
            self.fake_adapters[name].result = failed(FailureKind.AUTH, name)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(len(router.calls), 8)
        self.assertIn("all eight", result.text.lower())
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 8)

    def test_quota_cooldown_skips_adapter(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.AUTH)
        self.fake_adapters["opencode_zen"].result = failed(FailureKind.QUOTA)
        self.fake_adapters["claude"].result = completed("claude ok")
        router.store.set_cooldown("opencode_zen", hours=1)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "claude"])

    def test_run_is_idempotent_after_success(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = completed("first ok", "opencode_go")

        first = router.run(request(self.workspace), routes_for_main())
        router.calls.clear()
        second = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(first.text, "first ok")
        self.assertEqual(second.text, "first ok")
        self.assertEqual(router.calls, [])


class MutationContinuationTests(RouterTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.git_repo = self.workspace / "repo"
        self.git_repo.mkdir()
        self._run_git("init", "-q")
        self._run_git("config", "user.email", "test@example.invalid")
        self._run_git("config", "user.name", "Router Test")
        (self.git_repo / "README.md").write_text("baseline\n", encoding="utf-8")
        self._run_git("add", "README.md")
        self._run_git("commit", "-q", "-m", "baseline")

    def _run_git(self, *args):
        return subprocess.run(
            ["git", *args],
            cwd=self.git_repo,
            capture_output=True,
            text=True,
            check=True,
        )

    def test_mutation_turns_next_attempt_into_continuation(self):
        router = self.router()

        def fail_after_edit(request, route):
            (request.workspace / "changed.py").write_text(
                "partial work\n", encoding="utf-8"
            )
            return failed(FailureKind.AUTH, route.adapter)

        self.fake_adapters["opencode_go"].result = fail_after_edit
        self.fake_adapters["opencode_zen"].result = completed(
            "continued", "opencode_zen"
        )

        router.run(
            request(self.git_repo, workflow_id="wf-mut", stage_id="st-mut"),
            routes_for_main(),
        )

        second = self.fake_adapters["opencode_zen"].requests[0]
        self.assertEqual(second.mode, "continue")
        self.assertEqual(second.changed_files, ("changed.py",))
        attempt = router.store.stage_attempts("wf-mut", "st-mut")[0]
        self.assertEqual(attempt["changed_files"], ["changed.py"])

    def test_restart_continues_from_recorded_state_not_blind_replay(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE, "opencode_go"
        )

        def edit_then_fail(request, route):
            (request.workspace / "half-done.txt").write_text("x", encoding="utf-8")
            return failed(FailureKind.TASK_FAILURE, route.adapter)

        self.fake_adapters["opencode_go"].result = edit_then_fail

        first = router.run(
            request(self.git_repo, workflow_id="wf-r", stage_id="st-r"),
            routes_for_main(),
        )
        self.assertFalse(first.success)

        # The owner retries the stage; slot 0 now fails with an advancing
        # AUTH error, so the chain must reach zen as a continuation.
        fresh = self.router()
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.AUTH, "opencode_go"
        )
        self.fake_adapters["opencode_zen"].result = completed(
            "recovered", "opencode_zen"
        )
        second = fresh.run(
            request(self.git_repo, workflow_id="wf-r", stage_id="st-r"),
            routes_for_main(),
        )

        self.assertEqual(second.text, "recovered")
        zen_request = self.fake_adapters["opencode_zen"].requests[0]
        self.assertEqual(zen_request.mode, "continue")
        self.assertIn("half-done.txt", zen_request.changed_files)


class StatusTests(RouterTestCase):
    def test_status_matrix_reports_billing_and_health(self):
        router = self.router()

        status = router.status("claw")

        self.assertEqual(len(status["routes"]), 8)
        deepseekless = [r for r in status["routes"] if r["adapter"] == "codex"]
        self.assertEqual(deepseekless[0]["slot"], 7)
        self.assertTrue(
            all(r["billing_mode"] == "subscription" for r in status["routes"])
        )

    def test_status_reports_api_billing_for_deepseek_profile(self):
        router = self.router()
        source = {
            "chains": {
                "scout": [
                    "zai/glm-5.3",
                    "kimi/k3",
                    "xai/grok-4.6",
                    "anthropic/claude-sonnet-5",
                    "opencode/gpt-6-astra",
                    "opencode/muse-spark-1.3-contributor-free",
                    "opencode/claude-fable-5-1",
                    "deepseek/deepseek-flash",
                ]
            }
        }
        router.routing_source = source

        status = router.status("scout")

        self.assertEqual(status["routes"][7]["billing_mode"], "api")
        self.assertEqual(status["routes"][7]["adapter"], "deepseek")

    def test_status_reports_stage_evidence(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.QUOTA, "opencode_go"
        )
        self.fake_adapters["opencode_zen"].result = completed("done", "opencode_zen")
        router.run(request(self.workspace), routes_for_main())

        status = router.status("claw", workflow_id="wf-6", stage_id="st-6")

        self.assertEqual(status["stage"]["workflow_id"], "wf-6")
        self.assertEqual(status["stage"]["attempts"][0]["slot"], 0)
        self.assertEqual(status["stage"]["attempts"][0]["failure"], "quota")
        self.assertEqual(status["stage"]["attempts"][1]["success"], 1)
        self.assertEqual(status["stage"]["attempts"][1]["billing_mode"], "subscription")


class StateStoreTests(RouterTestCase):
    def test_concurrent_attempt_recording_keeps_unique_keys(self):
        store = SubscriptionStateStore(self.db_path)
        errors = []

        def record(worker):
            try:
                for attempt in range(20):
                    store.record_attempt(
                        workflow_id="wf-c",
                        stage_id="st-c",
                        slot=worker,
                        attempt=attempt,
                        adapter="kimi",
                        model="k3",
                        billing_mode="subscription",
                        mode="start",
                        success=False,
                        failure=FailureKind.AUTH,
                        session_id="",
                        start_revision="",
                        end_revision="",
                        changed_files=[],
                        detail="",
                    )
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=record, args=(w,)) for w in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        rows = store.stage_attempts("wf-c", "st-c")
        self.assertEqual(len(rows), 80)
        per_slot = {}
        for row in rows:
            per_slot.setdefault(row["slot"], set()).add(row["attempt"])
        self.assertEqual(
            {slot: attempts for slot, attempts in per_slot.items()},
            {slot: set(range(20)) for slot in range(4)},
        )

    def test_store_survives_restart(self):
        SubscriptionStateStore(self.db_path).record_attempt(
            workflow_id="wf-x",
            stage_id="st-x",
            slot=3,
            attempt=0,
            adapter="grok",
            model="grok-4.6",
            billing_mode="subscription",
            mode="start",
            success=True,
            failure=None,
            session_id="sess",
            start_revision="r0",
            end_revision="r1",
            changed_files=["a.py"],
            detail="",
        )

        reopened = SubscriptionStateStore(self.db_path)
        rows = reopened.stage_attempts("wf-x", "st-x")

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["changed_files"], ["a.py"])
        self.assertEqual(rows[0]["success"], 1)
        self.assertTrue(reopened.latest_success("wf-x", "st-x")["adapter"] == "grok")


class GitInspectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()

    def _git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.repo, capture_output=True, text=True, check=True
        )

    def test_non_git_workspace_reports_no_mutation(self):
        inspector = GitInspector()

        self.assertEqual(inspector.current_revision(self.repo), "")
        self.assertEqual(inspector.changed_files(self.repo), ())

    def test_porcelain_changes_are_detected(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "t")
        (self.repo / "base.txt").write_text("hi", encoding="utf-8")
        self._git("add", "base.txt")
        self._git("commit", "-q", "-m", "base")
        inspector = GitInspector()

        self.assertNotEqual(inspector.current_revision(self.repo), "")
        self.assertEqual(inspector.changed_files(self.repo), ())
        (self.repo / "new.py").write_text("x", encoding="utf-8")
        self.assertEqual(inspector.changed_files(self.repo), ("new.py",))


if __name__ == "__main__":
    unittest.main()
