from __future__ import annotations

import subprocess
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "plugins" / "goncloud-team"
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(ROOT / "tests"))

from goncloud_test_support import cleanup_temp_directory  # noqa: E402

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
        self.probes = []
        self.health = AdapterHealth(ready=True, account_class=f"{self.name}_test")

    def probe(self, route):
        self.probes.append(route)
        return self.health

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
        self.addCleanup(cleanup_temp_directory, self.tmp)
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
    def test_live_quarantine_published_during_probe_prevents_invocation(self):
        router = self.router()
        entered, release = threading.Event(), threading.Event()
        other = SubscriptionStateStore(self.db_path)
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["opencode_zen"].result = completed(adapter="opencode_zen")

        def probe(route):
            entered.set()
            self.assertTrue(release.wait(10))
            return AdapterHealth(True, "subscription")

        self.fake_adapters["opencode_go"].probe = probe
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                router.run, request(self.workspace), routes_for_main()[:2]
            )
            try:
                self.assertTrue(entered.wait(10))
                other.save_live_health(
                    "opencode_go",
                    ready=False,
                    account_class="subscription",
                    failure="task_failure",
                )
            finally:
                release.set()
            result = future.result(timeout=15)
        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])

    def test_live_quarantine_at_authorization_boundary_records_skipped(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["opencode_zen"].result = completed(adapter="opencode_zen")

        def publish():
            router.store.save_live_health(
                "opencode_go",
                ready=False,
                account_class="subscription",
                failure="task_failure",
            )

        result = router.run(
            replace(request(self.workspace), pre_invoke=publish), routes_for_main()[:2]
        )
        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])
        self.assertEqual(
            router.store.stage_attempts("wf-6", "st-6")[0].get("disposition"), "skipped"
        )

    def test_dispatch_holds_publication_lock_until_vendor_returns(self):
        from subscriptions.state import SubscriptionBusy

        router = self.router()
        entered, release = threading.Event(), threading.Event()

        def invoke(assignment, route):
            entered.set()
            self.assertTrue(release.wait(10))
            return completed(adapter="opencode_go")

        self.fake_adapters["opencode_go"].result = invoke
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                router.run, request(self.workspace), routes_for_main()[:1]
            )
            try:
                self.assertTrue(entered.wait(10))
                with self.assertRaises(SubscriptionBusy):
                    SubscriptionStateStore(self.db_path).save_live_health(
                        "opencode_go",
                        ready=False,
                        account_class="subscription",
                        failure="auth",
                    )
            finally:
                release.set()
            self.assertTrue(future.result(timeout=15).success)
        router.store.save_live_health(
            "opencode_go", ready=False, account_class="subscription", failure="auth"
        )
        self.assertTrue(router.store.health("opencode_go")["quarantined"])

    def test_crash_after_skipped_task_failure_resumes_next_slot_once(self):
        router = self.router()
        router.store.save_live_health(
            "opencode_go",
            ready=False,
            account_class="subscription",
            failure="task_failure",
        )
        self.fake_adapters["opencode_zen"].result = completed(adapter="opencode_zen")
        assignment = replace(
            request(self.workspace), result_transform=lambda value: value
        )
        availability = router._availability

        def crash(route):
            if route.adapter == "opencode_zen":
                raise KeyboardInterrupt
            return availability(route)

        with patch.object(router, "_availability", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                router.run(assignment, routes_for_main()[:2])
        resumed = self.router()
        result = resumed.run(assignment, routes_for_main()[:2])
        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(len(self.fake_adapters["opencode_zen"].requests), 1)
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])
        attempts = resumed.store.stage_attempts("wf-6", "st-6")
        self.assertEqual([row["slot"] for row in attempts], [0, 1])
        self.assertEqual(
            [row.get("disposition") for row in attempts], ["skipped", "executed"]
        )

    def test_expiry_during_guard_fences_old_owner_after_second_router_claims(self):
        old_router, new_router = self.router(), self.router()
        clock = [datetime.now(timezone.utc)]
        self.fake_adapters["opencode_go"].result = completed(
            "new owner result", "opencode_go"
        )
        new_results = []

        def delayed_guard():
            clock[0] += timedelta(seconds=601)
            new_results.append(
                new_router.run(request(self.workspace), routes_for_main())
            )

        with patch("subscriptions.state._utcnow", side_effect=lambda: clock[0]):
            result = old_router.run(
                replace(request(self.workspace), pre_invoke=delayed_guard),
                routes_for_main(),
            )

        self.assertTrue(new_results[0].success)
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(old_router.calls, [])
        self.assertEqual(new_router.calls, ["opencode_go"])
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)
        attempts = old_router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(
            [row["status"] for row in attempts], ["interrupted", "completed"]
        )
        self.assertEqual([row["success"] for row in attempts], [False, True])
        self.assertEqual(attempts[1]["detail"], "new owner result")
        with old_router.store._connect() as conn:
            owners = conn.execute(
                "SELECT owner FROM subscription_attempts ORDER BY id"
            ).fetchall()
        self.assertNotEqual(owners[0]["owner"], owners[1]["owner"])

    def test_guard_delay_renews_full_invocation_and_cleanup_budget(self):
        router = self.router()
        clock = [datetime.now(timezone.utc)]

        def delayed_guard():
            clock[0] += timedelta(seconds=400)

        def bounded_invocation(request, route):
            with router.store._connect() as conn:
                lease = conn.execute(
                    "SELECT expires_at FROM subscription_stage_leases "
                    "WHERE workflow_id = ? AND stage_id = ?",
                    ("wf-6", "st-6"),
                ).fetchone()
            remaining = (
                datetime.fromisoformat(lease["expires_at"]) - clock[0]
            ).total_seconds()
            self.assertGreaterEqual(remaining, 305)
            clock[0] += timedelta(seconds=305)
            return completed("bounded invocation completed", route.adapter)

        self.fake_adapters["opencode_go"].result = bounded_invocation
        with patch("subscriptions.state._utcnow", side_effect=lambda: clock[0]):
            result = router.run(
                replace(request(self.workspace), pre_invoke=delayed_guard),
                routes_for_main(),
            )
        self.assertTrue(result.success)
        self.assertEqual(
            router.store.stage_attempts("wf-6", "st-6")[0]["status"], "completed"
        )

    def test_guard_outliving_lease_without_takeover_refuses_and_remains_resumable(self):
        router = self.router()
        clock = [datetime.now(timezone.utc)]
        self.fake_adapters["opencode_go"].result = completed("resumed", "opencode_go")

        def delayed_guard():
            clock[0] += timedelta(seconds=720)

        with patch("subscriptions.state._utcnow", side_effect=lambda: clock[0]):
            result = router.run(
                replace(request(self.workspace), pre_invoke=delayed_guard),
                routes_for_main(),
            )
            self.assertFalse(result.success)
            self.assertEqual(router.calls, [])
            self.assertEqual(
                router.store.stage_attempts("wf-6", "st-6")[0]["status"], "interrupted"
            )
            resumed = self.router().run(request(self.workspace), routes_for_main())
        self.assertTrue(resumed.success)
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)

    def test_authorization_guard_errors_are_terminal_and_sanitized(self):
        router = self.router()

        def guard():
            raise RuntimeError("fixture-private-authorization-error")

        denied = router.run(
            replace(request(self.workspace), pre_invoke=guard), routes_for_main()
        )
        self.assertFalse(denied.success)
        self.assertEqual(denied.failure, FailureKind.INVALID_REQUEST)
        self.assertEqual(router.calls, [])
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertEqual(attempts[0]["failure"], "invalid_request")
        self.assertNotIn("fixture-private", repr(attempts))
        self.assertNotIn("fixture-private", repr(denied))
        self.assertEqual(
            router.store.health("opencode_go")["account_class"], "opencode_go_test"
        )
        self.assertIsNone(router.store.cooldown_until("opencode_go"))
        self.assertTrue(router.store.claim_stage("wf-6", "st-6", "next-owner", 10))
        router.store.release_stage("wf-6", "st-6", "next-owner")

    def test_concurrent_stage_runs_invoke_vendor_once(self):
        routers = [self.router(), self.router()]
        start = threading.Barrier(2)
        entered = threading.Event()
        release = threading.Event()

        def blocked(request, route):
            entered.set()
            if not release.wait(10):
                raise TimeoutError("test did not release invocation")
            return completed("done", route.adapter)

        self.fake_adapters["opencode_go"].result = blocked

        def run(router):
            start.wait(10)
            return router.run(request(self.workspace), routes_for_main())

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(run, router) for router in routers]
            try:
                self.assertTrue(entered.wait(10))
                done, _ = wait(futures, timeout=2, return_when=FIRST_COMPLETED)
                self.assertEqual(len(done), 1, "contender must return while owner runs")
                contender = next(iter(done)).result()
                self.assertFalse(contender.success)
                self.assertIn("stage already running", contender.text)
                running = routers[0].store.stage_attempts("wf-6", "st-6")
                self.assertEqual(len(running), 1)
                self.assertEqual(running[0]["status"], "running")
            finally:
                release.set()
            results = [future.result() for future in futures]

        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)
        self.assertEqual(sum(result.success for result in results), 1)
        attempts = routers[0].store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")

    def test_interrupted_attempt_is_durable_and_releases_stage(self):
        router = self.router()

        def interrupt(request, route):
            raise KeyboardInterrupt()

        self.fake_adapters["opencode_go"].result = interrupt
        with self.assertRaises(KeyboardInterrupt):
            router.run(request(self.workspace), routes_for_main())

        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "interrupted")
        self.assertFalse(attempts[0]["success"])
        self.fake_adapters["opencode_go"].result = completed("recovered")
        self.assertTrue(
            self.router().run(request(self.workspace), routes_for_main()).success
        )
        self.assertEqual(len(router.store.stage_attempts("wf-6", "st-6")), 2)

    def test_quarantined_probe_detail_is_redacted_from_durable_status(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        go.health = AdapterHealth(
            ready=False,
            account_class="opencode_go",
            failure=FailureKind.AUTH,
            detail="token sk-sensitive owner@example.test",
        )
        go.result = failed(FailureKind.AUTH, "opencode_go")
        self.fake_adapters["opencode_zen"].result = completed("zen", "opencode_zen")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(go.requests, [])
        health = router.store.health("opencode_go")
        status = router.status("claw", workflow_id="wf-6", stage_id="st-6")
        self.assertNotIn("sk-sensitive", repr(health))
        self.assertNotIn("owner@example.test", repr(health))
        self.assertNotIn("sk-sensitive", repr(status))
        self.assertNotIn("owner@example.test", repr(status))

    def test_absent_health_probes_once_and_persists_result(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        go.health = AdapterHealth(ready=True, account_class="opencode_go")

        availability = router._availability(routes_for_main()[0])

        self.assertTrue(availability.ready)
        self.assertEqual(len(go.probes), 1)
        self.assertEqual(
            router.store.health("opencode_go")["account_class"], "opencode_go"
        )

    def test_expired_health_probes_once_and_updates_persisted_result(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        router.store.save_health(
            "opencode_go",
            ready=False,
            account_class="opencode_go",
            failure="quota",
        )
        with router.store._connect() as conn:
            conn.execute(
                "UPDATE subscription_health SET updated_at = ? WHERE adapter = ?",
                (
                    (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat(),
                    "opencode_go",
                ),
            )
            conn.commit()
        go.health = AdapterHealth(ready=True, account_class="opencode_go")

        availability = router._availability(routes_for_main()[0])

        self.assertTrue(availability.ready)
        self.assertEqual(len(go.probes), 1)
        self.assertTrue(router.store.health("opencode_go")["ready"])

    def test_fresh_ready_health_skips_probe_and_invokes(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        router.store.save_health("opencode_go", ready=True, account_class="opencode_go")
        go.health = AdapterHealth(
            ready=False,
            account_class="opencode_go",
            failure=FailureKind.AUTH,
        )
        go.result = completed("go", "opencode_go")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "opencode_go")
        self.assertEqual(go.probes, [])
        self.assertEqual(len(go.requests), 1)

    def test_quarantined_route_is_recorded_without_invocation(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        go.health = AdapterHealth(
            ready=False,
            account_class="opencode_go",
            failure=FailureKind.AUTH,
            detail="login required",
        )
        go.result = failed(FailureKind.AUTH, "opencode_go")
        self.fake_adapters["opencode_zen"].result = completed("zen", "opencode_zen")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(go.requests, [])
        self.assertEqual(
            router.store.stage_attempts("wf-6", "st-6")[0]["failure"], "auth"
        )

    def test_fresh_unready_health_skips_probe_and_invoke(self):
        router = self.router()
        go = self.fake_adapters["opencode_go"]
        router.store.save_health(
            "opencode_go",
            ready=False,
            account_class="opencode_go",
            failure="quota",
            detail="quarantined",
        )
        go.result = failed(FailureKind.QUOTA, "opencode_go")
        self.fake_adapters["opencode_zen"].result = completed("zen", "opencode_zen")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "opencode_zen")
        self.assertEqual(go.probes, [])
        self.assertEqual(go.requests, [])
        self.assertEqual(
            router.store.stage_attempts("wf-6", "st-6")[0]["failure"], "quota"
        )

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

    def test_transport_failure_without_mutation_advances(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TRANSPORT)
        self.fake_adapters["opencode_zen"].result = completed("zen ok", "opencode_zen")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "opencode_zen"])

    def test_transport_failure_after_mutation_blocks_the_stage(self):
        subprocess.run(["git", "init", "-q"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.invalid"],
            cwd=self.workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.workspace,
            check=True,
        )
        router = self.router()

        def mutate_then_transport(request, route):
            (request.workspace / "partial.py").write_text("partial\n", encoding="utf-8")
            return failed(FailureKind.TRANSPORT, route.adapter)

        self.fake_adapters["opencode_go"].result = mutate_then_transport
        self.fake_adapters["opencode_zen"].result = completed(
            "must not run", "opencode_zen"
        )

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TRANSPORT)
        self.assertEqual(router.calls, ["opencode_go"])

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

    def test_clean_commit_causes_continuation(self):
        router = self.router()
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "changed.py").write_text("new\n", encoding="utf-8")
        self._run_git("add", "changed.py")
        self._run_git("commit", "-q", "-m", "changed")
        self.assertEqual(self._run_git("status", "--porcelain").stdout, "")

        absorbed = router._absorb_workspace_state(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            )
        )

        self.assertEqual(absorbed.mode, "continue")
        self.assertEqual(absorbed.changed_files, ("changed.py",))
        self.assertEqual(
            absorbed.current_revision, self._run_git("rev-parse", "HEAD").stdout.strip()
        )

    def test_interrupted_clean_commit_recovery_keeps_historical_baseline(self):
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        router = self.router()

        def commit_then_interrupt(request, route):
            (self.git_repo / "committed.py").write_text("partial\n", encoding="utf-8")
            self._run_git("add", "committed.py")
            self._run_git("commit", "-q", "-m", "interrupted mutation")
            raise KeyboardInterrupt()

        self.fake_adapters["opencode_go"].result = commit_then_interrupt
        with self.assertRaises(KeyboardInterrupt):
            router.run(
                replace(
                    request(self.git_repo), start_revision=start, current_revision=start
                ),
                routes_for_main(),
            )
        current = self._run_git("rev-parse", "HEAD").stdout.strip()
        self.assertEqual(self._run_git("status", "--porcelain").stdout, "")
        self.fake_adapters["opencode_go"].requests.clear()
        self.fake_adapters["opencode_go"].result = completed("continued", "opencode_go")
        self.router().run(
            replace(
                request(self.git_repo), start_revision=current, current_revision=current
            ),
            routes_for_main(),
        )
        resumed = self.fake_adapters["opencode_go"].requests[0]
        self.assertEqual(resumed.mode, "continue")
        self.assertEqual(resumed.start_revision, start)
        self.assertEqual(resumed.current_revision, current)
        self.assertEqual(resumed.changed_files, ("committed.py",))

    def test_revision_option_cannot_create_output_file(self):
        # Before --end-of-options, diff interprets the supplied baseline as a flag.
        inspector = GitInspector()
        current, files, mutated = inspector.mutation_since(
            self.git_repo, "--output=owned"
        )
        self.assertEqual(list(self.git_repo.glob("owned*")), [])
        self.assertEqual(files, ())
        self.assertTrue(mutated)
        self.assertEqual(current, self._run_git("rev-parse", "HEAD").stdout.strip())

    def test_clean_commit_transport_failure_blocks_with_durable_evidence(self):
        router = self.router()
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        def commit_then_fail(request, route):
            (self.git_repo / "changed.py").write_text("new\n", encoding="utf-8")
            self._run_git("add", "changed.py")
            self._run_git("commit", "-q", "-m", "changed")
            return failed(FailureKind.TRANSPORT, route.adapter)

        self.fake_adapters["opencode_go"].result = commit_then_fail
        self.fake_adapters["opencode_zen"].result = completed("must not run")
        result = router.run(
            replace(
                request(self.git_repo), start_revision=start, current_revision=start
            ),
            routes_for_main(),
        )

        self.assertFalse(result.success)
        self.assertEqual(router.calls, ["opencode_go"])
        evidence = SubscriptionStateStore(self.db_path).stage_mutation("wf-6", "st-6")
        self.assertEqual(evidence["changed_files"], ["changed.py"])
        self.assertEqual(
            evidence["end_revision"], self._run_git("rev-parse", "HEAD").stdout.strip()
        )

    def test_empty_commit_advances_auth_as_continuation_and_survives_restart(self):
        router = self.router()
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        def commit_then_fail(request, route):
            self._run_git("commit", "--allow-empty", "-q", "-m", "empty mutation")
            return failed(FailureKind.AUTH, route.adapter)

        self.fake_adapters["opencode_go"].result = commit_then_fail
        self.fake_adapters["opencode_zen"].result = failed(FailureKind.TASK_FAILURE)
        initial = replace(
            request(self.git_repo), start_revision=start, current_revision=start
        )
        router.run(initial, routes_for_main())

        continuation = self.fake_adapters["opencode_zen"].requests[0]
        self.assertEqual(continuation.mode, "continue")
        self.assertEqual(continuation.changed_files, ())
        self.assertNotEqual(continuation.current_revision, start)
        evidence = SubscriptionStateStore(self.db_path).stage_mutation("wf-6", "st-6")
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["end_revision"], continuation.current_revision)
        # Even a caller supplying today's revision must retain the recorded mutation.
        absorbed = self.router()._absorb_workspace_state(
            replace(
                initial,
                start_revision=continuation.current_revision,
                current_revision=continuation.current_revision,
            )
        )
        self.assertEqual(absorbed.mode, "continue")

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
    def test_concurrent_open_migrates_existing_attempts_without_losing_success(self):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript("""
                CREATE TABLE subscription_attempts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workflow_id TEXT NOT NULL, stage_id TEXT NOT NULL,
                    slot INTEGER NOT NULL, attempt INTEGER NOT NULL,
                    adapter TEXT NOT NULL, model TEXT NOT NULL,
                    billing_mode TEXT NOT NULL, mode TEXT NOT NULL,
                    success INTEGER NOT NULL, failure TEXT,
                    session_id TEXT NOT NULL DEFAULT '',
                    start_revision TEXT NOT NULL DEFAULT '',
                    end_revision TEXT NOT NULL DEFAULT '',
                    changed_files TEXT NOT NULL DEFAULT '[]',
                    detail TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
                    UNIQUE(workflow_id, stage_id, slot, attempt)
                );
                INSERT INTO subscription_attempts
                (workflow_id, stage_id, slot, attempt, adapter, model, billing_mode,
                 mode, success, detail)
                VALUES ('wf', 'st', 0, 0, 'kimi', 'k3', 'subscription', 'start', 1, 'done');
            """)
        finally:
            conn.close()
        start = threading.Barrier(2)

        def reopen(_):
            start.wait(10)
            return SubscriptionStateStore(self.db_path).latest_success("wf", "st")

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reopen, range(2)))
        self.assertEqual([row["detail"] for row in results], ["done", "done"])
        self.assertEqual([row["status"] for row in results], ["completed", "completed"])

    def test_reclaimed_lease_marks_crashed_attempt_and_fences_old_owner(self):
        store = SubscriptionStateStore(self.db_path)
        self.assertTrue(store.claim_stage("wf", "st", "crashed", 60))
        values = dict(
            workflow_id="wf",
            stage_id="st",
            slot=0,
            attempt=0,
            adapter="kimi",
            model="k3",
            billing_mode="subscription",
            mode="start",
            success=False,
            failure=None,
            session_id="",
            start_revision="r0",
            end_revision="r0",
            changed_files=[],
            detail="attempt running",
            status="running",
            owner="crashed",
        )
        store.record_attempt(**values)
        with store._connect() as conn:
            conn.execute(
                "UPDATE subscription_stage_leases SET expires_at = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
            conn.commit()
        reopened = SubscriptionStateStore(self.db_path)
        self.assertTrue(reopened.claim_stage("wf", "st", "recovered", 60))
        self.assertEqual(
            reopened.stage_attempts("wf", "st")[0]["status"], "interrupted"
        )
        self.assertFalse(store.renew_stage("wf", "st", "crashed", 60))
        self.assertFalse(
            store.finish_attempt(
                workflow_id="wf",
                stage_id="st",
                slot=0,
                attempt=0,
                owner="crashed",
                success=True,
                failure=None,
                session_id="s",
                end_revision="r1",
                changed_files=["wrong.py"],
                detail="stale success",
            )
        )
        self.assertIsNone(store.latest_success("wf", "st"))
        values.update(attempt=1, owner="recovered")
        reopened.record_attempt(**values)
        store.release_stage("wf", "st", "crashed")
        self.assertEqual(reopened.stage_attempts("wf", "st")[1]["status"], "running")
        self.assertTrue(
            reopened.finish_attempt(
                workflow_id="wf",
                stage_id="st",
                slot=0,
                attempt=1,
                owner="recovered",
                success=True,
                failure=None,
                session_id="s",
                end_revision="r1",
                changed_files=["done.py"],
                detail="done",
            )
        )
        self.assertEqual(store.latest_success("wf", "st")["detail"], "done")

    def test_expired_lease_is_recoverable_and_wrong_owner_cannot_release(self):
        store = SubscriptionStateStore(self.db_path)
        self.assertTrue(store.claim_stage("wf", "st", "old", 60))
        reopened = SubscriptionStateStore(self.db_path)
        self.assertFalse(reopened.claim_stage("wf", "st", "new", 60))
        reopened.release_stage("wf", "st", "new")
        self.assertFalse(reopened.claim_stage("wf", "st", "new", 60))
        with store._connect() as conn:
            conn.execute(
                "UPDATE subscription_stage_leases SET expires_at = ?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),),
            )
            conn.commit()
        self.assertTrue(reopened.claim_stage("wf", "st", "new", 60))
        store.release_stage("wf", "st", "old")
        self.assertFalse(store.claim_stage("wf", "st", "third", 60))
        reopened.release_stage("wf", "st", "new")
        self.assertTrue(store.claim_stage("wf", "st", "third", 60))

    def test_duplicate_attempt_is_rejected_instead_of_silently_hidden(self):
        store = SubscriptionStateStore(self.db_path)
        values = dict(
            workflow_id="wf",
            stage_id="st",
            slot=0,
            attempt=0,
            adapter="kimi",
            model="k3",
            billing_mode="subscription",
            mode="start",
            success=False,
            failure="auth",
            session_id="",
            start_revision="",
            end_revision="",
            changed_files=[],
            detail="",
        )
        store.record_attempt(**values)
        with self.assertRaises(sqlite3.IntegrityError):
            store.record_attempt(**values)

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

    def test_unstaged_and_renamed_paths_preserve_spaces_and_unicode(self):
        self._git("init", "-q")
        self._git("config", "user.email", "t@example.invalid")
        self._git("config", "user.name", "t")
        name = " space ñ.txt"
        (self.repo / name).write_text("baseline", encoding="utf-8")
        self._git("add", "--", name)
        self._git("commit", "-q", "-m", "baseline")
        (self.repo / name).write_text("dirty", encoding="utf-8")
        inspector = GitInspector()
        self.assertEqual(inspector.changed_files(self.repo), (name,))
        self._git("restore", "--", name)
        self._git("mv", "--", name, " renamed ñ.txt")
        self.assertEqual(inspector.changed_files(self.repo), (" renamed ñ.txt",))


if __name__ == "__main__":
    unittest.main()
