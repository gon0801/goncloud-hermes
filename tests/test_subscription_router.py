from __future__ import annotations

import subprocess
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import closing
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
    SkippedRoute,
)

SOURCE = {
    "profile_names": {"main": "claw"},
    "profile_inheritance": {"entrega": "main"},
    "chains": {
        "main": [
            "opencode-go/deepseek-v4.1-flash (off)",
            "anthropic/claude-sonnet-5",
            "meta/muse-spark-1.3 (max)",
            "xai/grok-4.6",
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


def primary_and_next():
    """Primary and the next distinct subscription (v6 table, 2026-10-17)."""
    routes = routes_for_main()
    return [routes[0], routes[1]]


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
                "opencode_zai",
                "claude",
                "kimi",
                "muse",
                "zai",
                "grok",
                "codex",
                "deepseek",
            )
        }

    def git_workspace(self) -> None:
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

    def router(self, routing_source=SOURCE):
        self.sleeps = []
        router = SubscriptionRouter(
            adapters=self.fake_adapters,
            db_path=self.db_path,
            routing_source=routing_source,
            sleep=self.sleeps.append,
        )
        router.calls = []
        original_invoke = router._invoke_route

        def invoke_and_record(request, route):
            router.calls.append(route.adapter)
            return original_invoke(request, route)

        router._invoke_route = invoke_and_record
        return router


class FailoverTests(RouterTestCase):
    def test_adapter_exception_without_transform_is_fixed_durable_failure(self):
        router = self.router()

        def explode(request, route):
            raise RuntimeError("untrusted vendor diagnostics")

        self.fake_adapters["opencode_go"].result = explode
        try:
            result = router.run(request(self.workspace), routes_for_main())
        except Exception:
            self.fail("adapter diagnostics escaped the public router")
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertNotIn("untrusted vendor diagnostics", repr((result, attempts)))

    def test_live_quarantine_published_during_probe_prevents_invocation(self):
        router = self.router()
        entered, release = threading.Event(), threading.Event()
        other = SubscriptionStateStore(self.db_path)
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")

        def probe(route):
            entered.set()
            self.assertTrue(release.wait(10))
            return AdapterHealth(True, "subscription")

        self.fake_adapters["opencode_go"].probe = probe
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                router.run, request(self.workspace), primary_and_next()
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
        self.assertEqual(result.adapter, "claude")
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])

    def test_live_quarantine_at_authorization_boundary_records_skipped(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")

        def publish():
            router.store.save_live_health(
                "opencode_go",
                ready=False,
                account_class="subscription",
                failure="task_failure",
            )

        result = router.run(
            replace(request(self.workspace), pre_invoke=publish), primary_and_next()
        )
        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "claude")
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
        self.fake_adapters["claude"].result = completed(adapter="claude")
        assignment = replace(
            request(self.workspace), result_transform=lambda value: value
        )
        availability = router._availability

        def crash(route):
            if route.adapter == "claude":
                raise KeyboardInterrupt
            return availability(route)

        with patch.object(router, "_availability", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                router.run(assignment, primary_and_next())
        resumed = self.router()
        result = resumed.run(assignment, primary_and_next())
        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "claude")
        self.assertEqual(len(self.fake_adapters["claude"].requests), 1)
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

    def test_authorization_guard_errors_during_recovery_are_terminal_and_sanitized(
        self,
    ):
        router = self.router()
        self.fake_adapters["opencode_go"].result = completed("done", "opencode_go")
        first = router.run(request(self.workspace), routes_for_main())
        self.assertTrue(first.success)

        def guard():
            raise RuntimeError("fixture-private-authorization-error")

        # Replaying a recorded success meets the same authorization boundary
        # as a live dispatch: a refusal, never an exception that blocks.
        denied = router.run(
            replace(request(self.workspace), pre_invoke=guard), routes_for_main()
        )
        self.assertFalse(denied.success)
        self.assertEqual(denied.failure, FailureKind.INVALID_REQUEST)
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)
        self.assertNotIn("fixture-private", repr(denied))
        self.assertIsNotNone(router.store.latest_success("wf-6", "st-6"))
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
        self.fake_adapters["claude"].result = completed("zen", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "claude")
        self.assertEqual(go.requests, [])
        health = router.store.health("opencode_go")
        status = router.status("claw", workflow_id="wf-6", stage_id="st-6")
        self.assertNotIn("sk-sensitive", repr(health))
        self.assertNotIn("sk-sensitive", repr(status))

    def test_assignment_prompt_is_redacted_from_detail_attempts_and_status(self):
        # The failure detail is the one evidence channel that carries vendor
        # output; sanitize_result must keep stripping the assignment prompt
        # from it at every boundary a reader can reach. Dropping "detail"
        # from its redaction tuple turns this test red.
        prompt = "Move the fixture-echo-prompt file into place."
        marker = "fixture-echo-prompt"
        router = self.router()
        self.fake_adapters["opencode_go"].result = replace(
            failed(FailureKind.TIMEOUT, "opencode_go"),
            detail=(
                "opencode_go invocation failed (timeout)\n"
                f"stderr tail: vendor died reading the assignment: {prompt}"
            ),
        )

        result = router.run(
            replace(request(self.workspace), prompt=prompt), routes_for_main()[:1]
        )
        attempts = router.store.stage_attempts("wf-6", "st-6")
        status = router.status("claw", workflow_id="wf-6", stage_id="st-6")

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TIMEOUT)
        # The evidence survives redacted, the prompt does not survive at all.
        self.assertIn("stderr tail:", result.detail)
        self.assertIn("[redacted]", result.detail)
        self.assertNotIn(marker, result.detail)
        self.assertNotIn(marker, repr(attempts))
        self.assertNotIn(marker, repr(status))

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
        self.fake_adapters["claude"].result = completed("zen", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "claude")
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
        self.fake_adapters["claude"].result = completed("zen", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "claude")
        self.assertEqual(go.probes, [])
        self.assertEqual(go.requests, [])
        self.assertEqual(
            router.store.stage_attempts("wf-6", "st-6")[0]["failure"], "quota"
        )

    def test_auth_and_quota_advance_in_order(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.AUTH)
        self.fake_adapters["claude"].result = failed(FailureKind.QUOTA)
        self.fake_adapters["muse"].result = completed("ok", adapter="muse")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.text, "ok")
        self.assertEqual(router.calls, ["opencode_go", "claude", "muse"])

    def test_a_timeout_failure_blocks_the_stage_instead_of_advancing(self):
        # Stages have no deadline anymore (owner, 2026-10-19), so no route may
        # hand the stage to the next model "for time". A timeout result is
        # owner evidence, never an automatic advance.
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TIMEOUT)
        self.fake_adapters["claude"].result = completed("must not run", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TIMEOUT)
        self.assertEqual(router.calls, ["opencode_go"])

    def test_transport_failure_without_mutation_advances(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TRANSPORT)
        self.fake_adapters["claude"].result = completed("zen ok", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "claude"])

    def test_transport_failure_after_mutation_blocks_the_stage(self):
        self.git_workspace()
        router = self.router()

        def mutate_then_transport(request, route):
            (request.workspace / "partial.py").write_text("partial\n", encoding="utf-8")
            return failed(FailureKind.TRANSPORT, route.adapter)

        self.fake_adapters["opencode_go"].result = mutate_then_transport
        self.fake_adapters["claude"].result = completed("must not run", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TRANSPORT)
        self.assertEqual(router.calls, ["opencode_go"])

    def test_task_failure_without_mutation_advances_to_the_next_slot(self):
        # Live 2026-10-17 (t_bc4ddc09): an executed attempt that came back
        # task_failure with a clean workspace blocked the stage, so one dead
        # route pinned the whole lane. Nothing usable was delivered and no
        # verdict was reported, so the next position must try.
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TASK_FAILURE)
        self.fake_adapters["claude"].result = completed("next ok", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(result.text, "next ok")
        self.assertEqual(result.slot, 1)
        self.assertEqual(router.calls, ["opencode_go", "claude"])
        self.assertEqual(
            result.skipped_routes,
            (SkippedRoute(0, "opencode_go", FailureKind.TASK_FAILURE),),
        )

    def test_task_failure_keeps_a_shared_adapter_ready_for_the_next_position(self):
        # The v6 lanes repeat a family across positions (dirigir/dirigir-claw
        # has claude at 0 and 1), so the advance is only real if the health
        # bench does not skip the position that shares the adapter.
        lane = {
            **SOURCE,
            "chains": {
                "main": [
                    "anthropic/claude-sonnet-5",
                    "anthropic/claude-opus-5",
                    "meta/muse-spark-1.3 (max)",
                    "xai/grok-4.6",
                ]
            },
        }
        router = self.router(routing_source=lane)

        def fail_first_then_succeed(request, route):
            if route.slot == 0:
                return failed(FailureKind.TASK_FAILURE, route.adapter)
            return completed("same adapter ok", route.adapter)

        self.fake_adapters["claude"].result = fail_first_then_succeed

        result = router.run(
            request(self.workspace), compile_profile_routes("claw", lane)
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "same adapter ok")
        self.assertEqual(result.slot, 1)
        self.assertEqual(router.calls, ["claude", "claude"])
        self.assertEqual(
            [
                row.get("disposition")
                for row in router.store.stage_attempts("wf-6", "st-6")
            ],
            ["executed", "executed"],
        )
        self.assertTrue(router.store.health("claude")["ready"])

    def test_task_failure_on_every_slot_exhausts_the_lane_with_evidence(self):
        router = self.router()
        lane = ("opencode_go", "claude", "muse", "grok")
        for name in lane:
            self.fake_adapters[name].result = failed(FailureKind.TASK_FAILURE, name)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(router.calls, list(lane))
        self.assertEqual(
            result.skipped_routes,
            tuple(
                SkippedRoute(slot, name, FailureKind.TASK_FAILURE)
                for slot, name in enumerate(lane)
            ),
        )
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual([row["failure"] for row in attempts], ["task_failure"] * 4)
        self.assertEqual([row.get("disposition") for row in attempts], ["executed"] * 4)

    def test_task_failure_after_mutation_blocks_the_stage(self):
        self.git_workspace()
        router = self.router()

        def mutate_then_task_failure(request, route):
            (request.workspace / "partial.py").write_text("partial\n", encoding="utf-8")
            return failed(FailureKind.TASK_FAILURE, route.adapter)

        self.fake_adapters["opencode_go"].result = mutate_then_task_failure
        self.fake_adapters["claude"].result = completed("must not run", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TASK_FAILURE)
        self.assertEqual(router.calls, ["opencode_go"])

    def test_quota_then_rejected_envelope_advances_to_the_third_position(self):
        # The chain still advances through advancing failures across slots;
        # a timeout is not one of them anymore (it blocks, see above).
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.QUOTA)
        self.fake_adapters["claude"].result = failed(FailureKind.INVALID_TERMINAL)
        self.fake_adapters["muse"].result = completed("muse ok", "muse")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(result.text, "muse ok")
        self.assertEqual(result.slot, 2)
        self.assertEqual(router.calls, ["opencode_go", "claude", "muse"])
        self.assertEqual(
            result.skipped_routes,
            (
                SkippedRoute(0, "opencode_go", FailureKind.QUOTA),
                SkippedRoute(1, "claude", FailureKind.INVALID_TERMINAL),
            ),
        )

    def test_non_advancing_failure_blocks_the_stage(self):
        # task_failure advances on a clean workspace now; the kinds left out
        # of _ADVANCING_FAILURES (a refused request) still block outright.
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.INVALID_REQUEST)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_REQUEST)
        self.assertEqual(router.calls, ["opencode_go"])

    def test_exhausted_chain_blocks_with_owner_facing_evidence(self):
        router = self.router()
        for name in ("opencode_go", "claude", "muse", "grok"):
            self.fake_adapters[name].result = failed(FailureKind.AUTH, name)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(router.calls, ["opencode_go", "claude", "muse", "grok"])
        self.assertIn("all 4 slots primary..f3", result.text.lower())
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(
            [row.get("disposition") for row in attempts],
            ["executed", "executed", "executed", "executed"],
        )

    def test_quota_cooldown_skips_adapter(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.AUTH)
        self.fake_adapters["claude"].result = failed(FailureKind.QUOTA)
        self.fake_adapters["muse"].result = completed("muse ok", adapter="muse")
        router.store.set_cooldown("claude", hours=1)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "muse"])

    def test_model_unavailable_keeps_the_account_ready(self):
        # 2026-09-16: a disabled/unknown model is the vendor answering about
        # the model, not the account. It must advance the slot without marking
        # the account unhealthy, or a later stage that uses a good model on the
        # same account gets skipped for the whole health window (this is why
        # opencode_zen looked "dead": a disabled deepseek model on one slot
        # poisoned the account it shares with the working muse model).
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.MODEL_UNAVAILABLE, adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "claude")
        self.assertEqual(result.slot, 1)
        self.assertEqual(result.route_label, "anthropic/claude-sonnet-5")
        self.assertEqual(
            result.skipped_routes,
            (SkippedRoute(0, "opencode_go", FailureKind.MODEL_UNAVAILABLE),),
        )
        self.assertTrue(router.store.health("opencode_go")["ready"])

    def test_a_timeout_failure_leaves_no_cooldown_bench(self):
        # The bench existed so a hung endpoint would not re-eat the old 300 s
        # deadline at slot 0 of every stage. With no stage deadline there is
        # no hung-at-the-deadline pattern left, and time never advances a
        # stage: the timeout result itself is the owner's evidence.
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.TIMEOUT, adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.TIMEOUT)
        self.assertEqual(router.calls, ["opencode_go"])
        self.assertIsNone(router.store.cooldown_until("opencode_go"))

    def test_a_persistent_throttle_benches_the_account_briefly_and_keeps_it_ready(
        self,
    ):
        # 2026-10-15: a few-second OpenCode Go throttle was read as quota, so
        # the primary account sat out 15 minutes of later stages and the work
        # fell through weaker fallbacks until the chain ran out.
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.RATE_LIMIT, adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()
        before = datetime.now(timezone.utc)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "claude")
        self.assertEqual(router.calls, ["opencode_go"] * 3 + ["claude"])
        self.assertEqual(self.sleeps, [20, 60])
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(
            [(row["slot"], row["attempt"], row["failure"]) for row in attempts],
            [
                (0, 0, "rate_limit"),
                (0, 1, "rate_limit"),
                (0, 2, "rate_limit"),
                (1, 0, None),
            ],
        )
        self.assertEqual(
            result.skipped_routes,
            (SkippedRoute(0, "opencode_go", FailureKind.RATE_LIMIT),),
        )
        bench = router.store.cooldown_until("opencode_go") - before
        self.assertGreaterEqual(bench, timedelta(minutes=2))
        self.assertLess(bench, timedelta(minutes=2, seconds=30))
        health = router.store.health("opencode_go")
        self.assertEqual((health["ready"], health["failure"]), (True, "rate_limit"))
        self.assertFalse(health["quarantined"])

        router.run(
            replace(request(self.workspace), workflow_id="wf-7", stage_id="st-7"),
            routes_for_main(),
        )
        skipped = router.store.stage_attempts("wf-7", "st-7")[0]
        self.assertEqual(
            (skipped["disposition"], skipped["failure"]), ("skipped", "rate_limit")
        )

    def test_a_passing_throttle_is_retried_on_the_same_route(self):
        router = self.router()
        replies = iter(
            [
                failed(FailureKind.RATE_LIMIT, "opencode_go"),
                completed("same route", "opencode_go"),
            ]
        )
        self.fake_adapters["opencode_go"].result = lambda request, route: next(replies)
        self.fake_adapters["claude"].result = completed(adapter="claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual((result.text, result.slot), ("same route", 0))
        self.assertEqual(result.skipped_routes, ())
        self.assertEqual(router.calls, ["opencode_go", "opencode_go"])
        self.assertEqual(self.sleeps, [20])
        self.assertIsNone(router.store.cooldown_until("opencode_go"))
        self.assertTrue(router.store.health("opencode_go")["ready"])
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(
            [(row["slot"], row["attempt"], row["failure"]) for row in attempts],
            [(0, 0, "rate_limit"), (0, 1, None)],
        )

    def test_a_throttle_waits_as_long_as_the_vendor_asks_within_the_lease(self):
        hint = replace(
            failed(FailureKind.RATE_LIMIT, "opencode_go"), retry_after_seconds=7
        )
        self.fake_adapters["opencode_go"].result = hint
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        router.run(request(self.workspace), routes_for_main())

        self.assertEqual(self.sleeps, [7, 7])
        self.assertEqual(router.calls, ["opencode_go"] * 3 + ["claude"])

    def test_a_throttle_wait_never_outlasts_the_stage_lease(self):
        router = self.router()
        lease_left = []

        def lease_seconds():
            with closing(sqlite3.connect(self.db_path)) as conn:
                (expires,) = conn.execute(
                    "SELECT expires_at FROM subscription_stage_leases"
                ).fetchone()
            remaining = datetime.fromisoformat(expires) - datetime.now(timezone.utc)
            return remaining.total_seconds()

        def slow_throttle(request, route):
            # A vendor call that used up all but 5 s of the lease.
            with closing(sqlite3.connect(self.db_path)) as conn, conn:
                conn.execute(
                    "UPDATE subscription_stage_leases SET expires_at = ?",
                    ((datetime.now(timezone.utc) + timedelta(seconds=5)).isoformat(),),
                )
            return failed(FailureKind.RATE_LIMIT, route.adapter)

        self.fake_adapters["opencode_go"].result = slow_throttle
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router.sleep = lambda seconds: lease_left.append((seconds, lease_seconds()))

        router.run(request(self.workspace), routes_for_main())

        self.assertEqual([seconds for seconds, _ in lease_left], [20, 60])
        for seconds, remaining in lease_left:
            self.assertGreater(remaining, seconds)

    def test_a_throttle_longer_than_the_lease_allows_advances_without_waiting(self):
        # A wait must end well inside the stage lease renewed before it; a
        # vendor asking for minutes is a spent window in all but name. Later
        # stages skip the account for the wait it named, up to the quota
        # window, and the account stays ready.
        for hint, expected in (
            (300, timedelta(minutes=5)),
            (7200, timedelta(minutes=15)),
        ):
            with self.subTest(hint=hint):
                self.setUp()
                self.fake_adapters["opencode_go"].result = replace(
                    failed(FailureKind.RATE_LIMIT, "opencode_go"),
                    retry_after_seconds=hint,
                )
                self.fake_adapters["claude"].result = completed(adapter="claude")
                router = self.router()
                before = datetime.now(timezone.utc)

                result = router.run(request(self.workspace), routes_for_main())

                self.assertEqual(result.adapter, "claude")
                self.assertEqual(self.sleeps, [])
                self.assertEqual(router.calls, ["opencode_go", "claude"])
                bench = router.store.cooldown_until("opencode_go") - before
                self.assertGreaterEqual(bench, expected)
                self.assertLess(bench, expected + timedelta(seconds=30))
                self.assertTrue(router.store.health("opencode_go")["ready"])

    def test_a_throttle_on_every_slot_stops_waiting_once_the_stage_budget_is_spent(
        self,
    ):
        # Per-slot retries alone held a stage 80 s per throttled slot (480 s
        # on this chain) before the owner saw it blocked.
        for name in ("opencode_go", "claude", "muse", "grok", "opencode_zai", "codex"):
            self.fake_adapters[name].result = failed(FailureKind.RATE_LIMIT, name)
        router = self.router()

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(self.sleeps, [20, 60, 20, 60, 20])
        self.assertEqual(
            router.calls,
            ["opencode_go"] * 3 + ["claude"] * 3 + ["muse"] * 2 + ["grok"],
        )
        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.RATE_LIMIT)
        self.assertIn("all 4 slots primary..f3 are exhausted", result.text.lower())

    def test_plan_exhaustion_advances_at_once_and_benches_the_account(self):
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.QUOTA, adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()
        before = datetime.now(timezone.utc)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertEqual(result.adapter, "claude")
        self.assertEqual(router.calls, ["opencode_go", "claude"])
        self.assertEqual(self.sleeps, [])
        bench = router.store.cooldown_until("opencode_go") - before
        self.assertGreaterEqual(bench, timedelta(minutes=15))
        self.assertLess(bench, timedelta(minutes=15, seconds=30))
        health = router.store.health("opencode_go")
        self.assertEqual((health["ready"], health["failure"]), (False, "quota"))

    def test_ordinary_cooldown_is_recorded_with_quota_and_exact_duplicate_positions(
        self,
    ):
        router = self.router()
        router.store.set_cooldown("opencode_go", hours=1)
        self.fake_adapters["claude"].result = completed("zen", "claude")
        first, second = primary_and_next()
        routes = [
            first,
            replace(first, slot=1, label=first.label + " dup"),
            replace(second, slot=2),
        ]
        result = router.run(request(self.workspace), routes)
        self.assertTrue(result.success)
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual([row["slot"] for row in attempts], [0, 1, 2])
        self.assertEqual(
            [row["disposition"] for row in attempts], ["skipped", "skipped", "executed"]
        )
        self.assertEqual([row["failure"] for row in attempts[:2]], ["quota", "quota"])
        self.assertEqual(
            [row["model"] for row in attempts[:2]], [first.model, first.model]
        )
        self.assertEqual(self.fake_adapters["opencode_go"].probes, [])
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])

    def test_crash_after_cooldown_skip_resumes_next_slot_with_or_without_transform(
        self,
    ):
        for transformed in (False, True):
            with self.subTest(transformed=transformed):
                self.db_path = self.workspace / f"cooldown-{transformed}.db"
                router = self.router()
                router.store.set_cooldown("opencode_go", hours=1)
                self.fake_adapters["claude"].result = completed("zen", "claude")
                assignment = replace(
                    request(self.workspace),
                    result_transform=(lambda result: result) if transformed else None,
                )
                availability = router._availability

                def crash(route):
                    if route.slot == 1:
                        raise KeyboardInterrupt
                    return availability(route)

                with patch.object(router, "_availability", side_effect=crash):
                    with self.assertRaises(KeyboardInterrupt):
                        router.run(assignment, primary_and_next())
                resumed = self.router()
                self.assertTrue(resumed.run(assignment, primary_and_next()).success)
                attempts = resumed.store.stage_attempts("wf-6", "st-6")
                self.assertEqual([row["slot"] for row in attempts], [0, 1])
                self.assertEqual(
                    [row["disposition"] for row in attempts], ["skipped", "executed"]
                )

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
        self.fake_adapters["claude"].result = completed("must not run")
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

    def test_recovered_failure_keeps_diagnostics_out_of_the_public_text(self):
        # A blocking failure recovered from the durable record must return the
        # short public text; the vendor diagnostics ride in detail and stay in
        # the normalized attempt row.
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        def commit_then_fail(request, route):
            (self.git_repo / "changed.py").write_text("new\n", encoding="utf-8")
            self._run_git("add", "changed.py")
            self._run_git("commit", "-q", "-m", "changed")
            return replace(
                failed(FailureKind.TRANSPORT, route.adapter),
                detail=(
                    "opencode_go invocation failed (transport)\n"
                    "stderr tail: connection reset by peer"
                ),
            )

        self.fake_adapters["opencode_go"].result = commit_then_fail
        assignment = replace(
            request(self.git_repo),
            start_revision=start,
            current_revision=start,
            result_transform=lambda result: result,
        )
        blocked = self.router().run(assignment, routes_for_main())
        self.assertFalse(blocked.success)

        resumed_router = self.router()
        resumed = resumed_router.run(assignment, routes_for_main())

        self.assertFalse(resumed.success)
        self.assertEqual(resumed.failure, FailureKind.TRANSPORT)
        self.assertEqual(resumed.text, "opencode_go invocation failed (transport)")
        self.assertIn("connection reset by peer", resumed.detail)
        attempt = resumed_router.store.stage_attempts("wf-6", "st-6")[-1]
        self.assertIn("invocation failed (transport)", attempt["detail"])
        self.assertIn("connection reset by peer", attempt["detail"])

    def test_a_throttle_after_a_mutation_retries_the_route_as_a_continuation(self):
        router = self.router()
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        def edit_then_throttle(request, route):
            if request.mode == "start":
                (request.workspace / "half-done.txt").write_text("x", encoding="utf-8")
                return failed(FailureKind.RATE_LIMIT, route.adapter)
            return completed("resumed", route.adapter)

        self.fake_adapters["opencode_go"].result = edit_then_throttle
        initial = replace(
            request(self.git_repo), start_revision=start, current_revision=start
        )

        result = router.run(initial, routes_for_main())

        self.assertEqual((result.text, result.slot), ("resumed", 0))
        first, retry = self.fake_adapters["opencode_go"].requests
        self.assertEqual(first.mode, "start")
        self.assertEqual(retry.mode, "continue")
        self.assertEqual(retry.changed_files, ("half-done.txt",))
        self.assertEqual(retry.current_revision, start)
        self.assertEqual(
            [row["mode"] for row in router.store.stage_attempts("wf-6", "st-6")],
            ["start", "continue"],
        )

    def test_empty_commit_advances_auth_as_continuation_and_survives_restart(self):
        router = self.router()
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        def commit_then_fail(request, route):
            self._run_git("commit", "--allow-empty", "-q", "-m", "empty mutation")
            return failed(FailureKind.AUTH, route.adapter)

        self.fake_adapters["opencode_go"].result = commit_then_fail
        self.fake_adapters["claude"].result = failed(FailureKind.TASK_FAILURE)
        initial = replace(
            request(self.git_repo), start_revision=start, current_revision=start
        )
        router.run(initial, routes_for_main())

        continuation = self.fake_adapters["claude"].requests[0]
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
        self.fake_adapters["claude"].result = completed("continued", "claude")

        router.run(
            request(self.git_repo, workflow_id="wf-mut", stage_id="st-mut"),
            routes_for_main(),
        )

        second = self.fake_adapters["claude"].requests[0]
        self.assertEqual(second.mode, "continue")
        self.assertEqual(second.changed_files, ("changed.py",))
        attempt = router.store.stage_attempts("wf-mut", "st-mut")[0]
        self.assertEqual(attempt["changed_files"], ["changed.py"])

    def test_owner_uncommitted_edits_are_not_a_continuation(self):
        """An owner's dirty tree is not evidence that a worker mutated anything.

        Reporting it as a continuation tells the next model a previous attempt
        already changed these files, on attempt 0, when none ever ran.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "owner-wip.py").write_text("my own edit", encoding="utf-8")

        absorbed = self.router()._absorb_workspace_state(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            )
        )

        self.assertEqual(absorbed.mode, "start")

    def test_pre_existing_dirt_advances_when_the_attempt_touches_nothing(self):
        """Attempt 0 tells the truth about the tree, and a clean failure advances.

        Two separate questions. The owner's uncommitted files must never be
        reported to the model as a previous worker's mutation, and they must
        not read as THIS attempt's mutation either: the attempt's verdict is
        its own delta against the baseline it started from (owner,
        2026-10-19; live t_464d20e7, uv.lock dirty before the attempt). What
        still blocks is an attempt that changed something and then failed.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "README.md").write_text("owner edit", encoding="utf-8")
        (self.git_repo / "owner-wip.py").write_text("my own edit", encoding="utf-8")

        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE, "opencode_go"
        )
        self.fake_adapters["claude"].result = completed("clean failover", "claude")

        router = self.router()
        result = router.run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            ),
            primary_and_next(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "clean failover")
        self.assertEqual(router.calls, ["opencode_go", "claude"])
        attempt = router.store.stage_attempts("wf-6", "st-6")[0]
        self.assertEqual(attempt["failure"], "task_failure")
        # The part that matters: no invented continuation on the first attempt.
        self.assertEqual(attempt["mode"], "start")

    def test_inherited_stage_changes_no_longer_block_a_clean_failed_attempt(self):
        """A stage that inherits another stage's work advances on a clean failure.

        Live 2026-10-19 (t_9df6342d verifier over grok; t_464d20e7 scout): the
        attempt carried the whole inherited file list in its record, read it
        back as its own mutation, and blocked the stage although the attempt
        touched nothing. The inherited files stay in the record as evidence;
        only the attempt's own delta decides advance or block.
        """
        (self.git_repo / "implemented.py").write_text("stage work\n", encoding="utf-8")
        self._run_git("add", "implemented.py")
        self._run_git("commit", "-q", "-m", "implementer stage")
        start = self._run_git("rev-parse", "HEAD").stdout.strip()

        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.TASK_FAILURE, "opencode_go"
        )
        self.fake_adapters["claude"].result = completed("verifier next slot", "claude")

        router = self.router()
        result = router.run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
                mode="continue",
                changed_files=("implemented.py", "uv.lock"),
            ),
            primary_and_next(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "verifier next slot")
        self.assertEqual(router.calls, ["opencode_go", "claude"])
        attempt = router.store.stage_attempts("wf-6", "st-6")[0]
        self.assertEqual(attempt["failure"], "task_failure")
        # The evidence keeps the inherited union even though the verdict used
        # only this attempt's delta (nothing).
        self.assertEqual(attempt["changed_files"], ["implemented.py", "uv.lock"])
        # The record names the attempt's own baseline, not the stage's.
        self.assertEqual(attempt["start_revision"], start)

    def test_a_worker_rewriting_an_already_dirty_file_counts_as_mutation(self):
        """A worker editing an already-dirty file must not read as "nothing changed".

        The owner left README.md dirty. Slot 0 edits that same file and then
        returns a rejected envelope. A name-only comparison sees an identical
        set before and after and hands the next slot a workspace it would redo
        work on top of, so the rule stays conservative: any dirt counts.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        owned = self.git_repo / "README.md"
        owned.write_text("owner edit", encoding="utf-8")

        def rewrite_then_reject(request, route):
            owned.write_text("worker edit", encoding="utf-8")
            return completed(text="not an envelope", adapter="opencode_go")

        self.fake_adapters["opencode_go"].result = rewrite_then_reject
        self.fake_adapters["claude"].result = completed("must not run", "claude")

        def transform(result):
            if result.adapter == "opencode_go":
                raise ValueError("invalid or oversized terminal envelope")
            return result

        router = self.router()
        result = router.run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
                result_transform=transform,
            ),
            primary_and_next(),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
        self.assertEqual(self.fake_adapters["claude"].requests, [])
        attempt = router.store.stage_attempts("wf-6", "st-6")[0]
        self.assertIn("README.md", attempt["changed_files"])

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
        self.fake_adapters["claude"].result = completed("recovered", "claude")
        second = fresh.run(
            request(self.git_repo, workflow_id="wf-r", stage_id="st-r"),
            routes_for_main(),
        )

        self.assertEqual(second.text, "recovered")
        zen_request = self.fake_adapters["claude"].requests[0]
        self.assertEqual(zen_request.mode, "continue")
        self.assertIn("half-done.txt", zen_request.changed_files)


class StatusTests(RouterTestCase):
    def test_status_matrix_reports_billing_and_health(self):
        router = self.router()

        status = router.status("claw")

        self.assertEqual(len(status["routes"]), 4)
        deepseekless = [r for r in status["routes"] if r["adapter"] == "grok"]
        self.assertEqual(deepseekless[0]["slot"], 3)
        self.assertTrue(
            all(r["billing_mode"] == "subscription" for r in status["routes"])
        )

    def test_status_reports_deepseek_flash_on_the_go_subscription(self):
        # Owner, 2026-10-17: no table position bills as API anymore; DeepSeek
        # Flash rides the OpenCode Go subscription on every lane.
        router = self.router()
        source = {
            "chains": {
                "scout": [
                    "xai/grok-4.6",
                    "kimi/k3",
                    "zai/glm-5.3",
                    "opencode-go/deepseek-v4.1-flash (off)",
                ]
            }
        }
        router.routing_source = source

        status = router.status("scout")

        self.assertEqual(status["routes"][3]["adapter"], "opencode_go")
        self.assertEqual(status["routes"][3]["billing_mode"], "subscription")
        self.assertTrue(
            all(route["billing_mode"] == "subscription" for route in status["routes"])
        )

    def test_status_reports_stage_evidence(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.QUOTA, "opencode_go"
        )
        self.fake_adapters["claude"].result = completed("done", "claude")
        router.run(request(self.workspace), routes_for_main())

        status = router.status("claw", workflow_id="wf-6", stage_id="st-6")

        self.assertEqual(status["stage"]["workflow_id"], "wf-6")
        self.assertEqual(status["stage"]["attempts"][0]["slot"], 0)
        self.assertEqual(status["stage"]["attempts"][0]["failure"], "quota")
        self.assertEqual(status["stage"]["attempts"][1]["success"], 1)
        self.assertEqual(status["stage"]["attempts"][1]["billing_mode"], "subscription")


class StateStoreTests(RouterTestCase):
    def test_a_rearming_attempt_without_a_failure_kind_keeps_the_cause(self):
        store = SubscriptionStateStore(self.db_path)
        store.save_live_health(
            "opencode_go", ready=False, account_class="subscription", failure="quota"
        )

        store.save_health(
            "opencode_go",
            ready=False,
            account_class="opencode_go",
            failure=None,
            detail="live attempt failed",
            attempt_failed=True,
        )

        health = store.health("opencode_go")
        self.assertTrue(health["quarantined"])
        # A re-arm never erases why the account is held.
        self.assertEqual(health["failure"], "quota")

    def test_store_redacts_each_evidence_write_boundary(self):
        sensitive = (
            "opaque-state-fixture",
            "sk-synthetic-pattern",
            "ghp_syntheticfixture0123456789ab",
        )
        text = " ".join(sensitive)
        store = SubscriptionStateStore(self.db_path)
        with patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive[0]}):
            self.assertTrue(store.claim_stage("wf", "stage", "owner", 60))
            store.record_attempt(
                workflow_id="wf",
                stage_id="stage",
                slot=0,
                attempt=0,
                adapter="muse",
                model="model",
                billing_mode="subscription",
                mode="start",
                success=False,
                failure=None,
                session_id=text,
                start_revision="r0",
                end_revision="r0",
                changed_files=[text],
                detail=text,
                status="running",
                owner="owner",
            )
            for operation in ("record", "finish", "normalize"):
                if operation == "finish":
                    self.assertTrue(
                        store.finish_attempt(
                            workflow_id="wf",
                            stage_id="stage",
                            slot=0,
                            attempt=0,
                            owner="owner",
                            success=True,
                            failure=None,
                            session_id=text,
                            end_revision="r0",
                            changed_files=[text],
                            detail=text,
                        )
                    )
                elif operation == "normalize":
                    attempt_id = store.stage_attempts("wf", "stage")[0]["id"]
                    self.assertTrue(
                        store.normalize_completed_attempt(
                            workflow_id="wf",
                            stage_id="stage",
                            attempt_id=attempt_id,
                            owner="owner",
                            success=True,
                            failure=None,
                            session_id=text,
                            detail=text,
                        )
                    )
                with self.subTest(operation=operation), store._connect() as conn:
                    raw = [
                        tuple(row)
                        for row in conn.execute(
                            "SELECT detail, session_id, changed_files FROM subscription_attempts"
                        )
                    ]
                    self.assertTrue(
                        all(value not in repr(raw) for value in sensitive),
                        "SQLite write retained synthetic sensitive content",
                    )

    def test_legacy_cached_success_is_redacted_and_repaired_without_a_transform(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = completed("safe", "opencode_go")
        assignment = request(self.workspace)
        router.run(assignment, routes_for_main())
        sensitive = "opaque-legacy-fixture"
        with router.store._connect() as conn:
            conn.execute(
                "UPDATE subscription_attempts SET detail=?, session_id=?",
                (sensitive, sensitive),
            )
            conn.commit()
        with patch.dict(os.environ, {"SERVICE_PASSWORD": sensitive}):
            result = router.run(assignment, routes_for_main())
            status = router.status("claw", workflow_id="wf-6", stage_id="st-6")
        with router.store._connect() as conn:
            raw = [
                tuple(row)
                for row in conn.execute(
                    "SELECT detail, session_id FROM subscription_attempts"
                )
            ]
        self.assertTrue(
            sensitive not in repr((result, status, raw)),
            "legacy success exposed synthetic content",
        )
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)

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

    def test_a_lease_with_an_unreadable_expiry_counts_as_live(self):
        # Live review F7: claim_stage treats an unparsable expires_at as a
        # live lease (it refuses to take the stage over); active_leases —
        # the side that authorizes the close — must agree instead of
        # dropping the lease and failing open.
        store = SubscriptionStateStore(self.db_path)
        self.assertTrue(store.claim_stage("wf", "st", "owner", 60))
        with store._connect() as conn:
            conn.execute(
                "UPDATE subscription_stage_leases SET expires_at = 'not-a-date'"
            )
            conn.commit()
        self.assertEqual(
            [item["owner"] for item in store.active_leases("wf")], ["owner"]
        )
        self.assertFalse(store.claim_stage("wf", "st", "other", 60))

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

    def test_reclaiming_a_stage_closes_running_attempts_of_dead_owners_as_abandoned(
        self,
    ):
        """A running attempt whose owner no longer exists closes with a cause.

        Live 2026-10-19 (claw point 9, t_70e4ed2c kimi slot 1): rows stayed
        success=0, failure NULL, detail "attempt running" forever when their
        owner vanished without a lease row to name. Reopening the stage must
        close them as abandoned with a readable reason, never NULL.
        """
        store = SubscriptionStateStore(self.db_path)
        # A row found in the wild: running, no failure, and no lease row left
        # naming its owner (an expired lease of another owner would already
        # interrupt its own rows; this owner matches nothing).
        with store._connect() as conn:
            conn.execute(
                "INSERT INTO subscription_attempts (workflow_id, stage_id, slot, "
                "attempt, adapter, model, billing_mode, mode, success, failure, "
                "session_id, start_revision, end_revision, changed_files, detail, "
                "status, owner, disposition) VALUES "
                "('wf', 'st', 1, 0, 'kimi', 'k3', 'subscription', 'start', 0, "
                "NULL, '', 'r0', 'r0', '[]', 'attempt running', 'running', "
                "'ghost', 'executed')"
            )
            conn.commit()

        self.assertTrue(store.claim_stage("wf", "st", "fresh", 60))

        row = store.stage_attempts("wf", "st")[0]
        self.assertEqual(row["status"], "abandoned")
        self.assertEqual(row["failure"], "task_failure")
        self.assertIn("abandoned", row["detail"])
        self.assertNotEqual(row["detail"], "attempt running")

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
        self.addCleanup(cleanup_temp_directory, self.tmp)
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


class InvalidTerminalTests(RouterTestCase):
    """A vendor that answers but not in the terminal contract did not do the task.

    That is a route failure, not a task failure: with the workspace untouched the
    next table position gets its turn. Once the workspace changed, the stage
    blocks with evidence instead, exactly like a transport failure would.
    """

    def rejecting_transform(self):
        def transform(result):
            if result.adapter == "opencode_go":
                raise ValueError("invalid or oversized terminal envelope")
            return result

        return transform

    def test_a_rejected_envelope_advances_to_the_next_slot_when_nothing_changed(self):
        self.fake_adapters["opencode_go"].result = completed(
            text="not an envelope", adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(
            text="```json\n{}\n```", adapter="claude"
        )
        router = self.router()

        result = router.run(
            replace(
                request(self.workspace), result_transform=self.rejecting_transform()
            ),
            primary_and_next(),
        )

        self.assertTrue(result.success)
        self.assertEqual(result.adapter, "claude")
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(attempts[0]["failure"], "invalid_terminal")
        self.assertEqual(attempts[0]["disposition"], "executed")
        self.assertEqual(len(self.fake_adapters["claude"].requests), 1)
        # The vendor answered, so the account stays ready for the next stage.
        self.assertTrue(router.store.health("opencode_go")["ready"])

    def test_a_rejected_envelope_blocks_once_the_workspace_was_mutated(self):
        self.fake_adapters["opencode_go"].result = completed(
            text="not an envelope", adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")

        class MutatedGit:
            """A tree that moves between the attempt's baseline and its verdict."""

            def __init__(self):
                self.reads = 0

            def current_revision(self, workspace):
                return "r1"

            def resolve_revision(self, workspace, revision):
                return "r0"

            def changed_files(self, workspace):
                return ("a.txt",)

            def mutation_since(self, workspace, revision):
                return "r1", ("a.txt",), True

            def tree_fingerprint(self, workspace):
                # First read: the attempt's baseline. Second: the verdict, a
                # file changed in between.
                self.reads += 1
                if self.reads == 1:
                    return "r0", ()
                return "r1", (("a.txt", "d"),)

        router = SubscriptionRouter(
            adapters=self.fake_adapters,
            db_path=self.db_path,
            routing_source=SOURCE,
            git=MutatedGit(),
        )

        result = router.run(
            replace(
                request(self.workspace), result_transform=self.rejecting_transform()
            ),
            primary_and_next(),
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
        self.assertEqual(self.fake_adapters["claude"].requests, [])


class QuarantineExpiryTests(RouterTestCase):
    """Quota comes and goes; a quarantine must expire and re-prove itself.

    The proof standard does not change: only a real successful inference lifts
    the quarantine. What changes is that the router is allowed to try again
    after a bounded horizon instead of skipping the account forever.
    """

    def quarantine(self, adapter, failure, *, age_seconds=0):
        store = SubscriptionStateStore(self.db_path)
        store.save_live_health(
            adapter, ready=False, account_class="subscription", failure=failure
        )
        if age_seconds:
            stamp = (
                datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
            ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            # `with` on a connection commits but never closes it, and an open
            # handle keeps Windows from deleting the temp directory.
            with closing(sqlite3.connect(self.db_path)) as conn:
                conn.execute(
                    "UPDATE subscription_health SET updated_at=?, quarantined_at=? "
                    "WHERE adapter=?",
                    (stamp, stamp, adapter),
                )
                conn.commit()

    def test_a_fresh_quota_quarantine_still_skips_the_route(self):
        self.quarantine("opencode_go", "quota")
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")

        result = self.router().run(request(self.workspace), primary_and_next())

        self.assertEqual(result.adapter, "claude")
        self.assertEqual(self.fake_adapters["opencode_go"].requests, [])

    def test_an_expired_quota_quarantine_is_retried_and_lifted_by_real_success(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        self.quarantine(
            "opencode_go", "quota", age_seconds=_QUARANTINE_RETRY_SECONDS + 60
        )
        self.fake_adapters["opencode_go"].result = completed(adapter="opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        result = router.run(request(self.workspace), primary_and_next())

        self.assertEqual(result.adapter, "opencode_go")
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)
        health = router.store.health("opencode_go")
        self.assertTrue(health["ready"])
        self.assertFalse(health["quarantined"])
        self.assertIsNone(health["failure"])

    def test_a_throttle_on_an_expired_quarantine_does_not_rearm_it(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        self.quarantine(
            "opencode_go", "quota", age_seconds=_QUARANTINE_RETRY_SECONDS + 60
        )
        armed_at = SubscriptionStateStore(self.db_path).health("opencode_go")[
            "quarantined_at"
        ]
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.RATE_LIMIT, "opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        result = router.run(request(self.workspace), primary_and_next())

        self.assertEqual(result.adapter, "claude")
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 3)
        self.assertEqual(router.store.health("opencode_go")["quarantined_at"], armed_at)

    def test_an_expired_quarantine_that_fails_again_is_rearmed(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        self.quarantine(
            "opencode_go", "quota", age_seconds=_QUARANTINE_RETRY_SECONDS + 60
        )
        go = self.fake_adapters["opencode_go"]
        go.result = failed(FailureKind.QUOTA, "opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        first = router.run(request(self.workspace), primary_and_next())
        second = router.run(
            request(self.workspace, stage_id="st-7"), primary_and_next()
        )

        self.assertEqual(first.adapter, "claude")
        self.assertEqual(second.adapter, "claude")
        # One real retry, then the quarantine is armed again with a fresh clock.
        self.assertEqual(len(go.requests), 1)
        health = router.store.health("opencode_go")
        self.assertFalse(health["ready"])
        self.assertTrue(health["quarantined"])
        self.assertEqual(health["failure"], "quota")

    def test_every_quarantine_kind_expires_and_is_reproven(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        for kind in ("auth", "timeout", "transport", "model_unavailable"):
            with self.subTest(kind=kind):
                # One store and one fresh adapter per kind; no setUp re-entry.
                self.db_path = self.workspace / f"router-{kind}.db"
                self.fake_adapters["opencode_go"] = ScriptedAdapter("opencode_go")
                self.quarantine(
                    "opencode_go", kind, age_seconds=_QUARANTINE_RETRY_SECONDS + 60
                )
                self.fake_adapters["opencode_go"].result = completed(
                    adapter="opencode_go"
                )
                router = self.router()

                result = router.run(request(self.workspace), routes_for_main()[:1])

                self.assertEqual(result.adapter, "opencode_go")
                self.assertFalse(router.store.health("opencode_go")["quarantined"])

    def test_an_expired_quarantine_rearms_on_every_failure_kind(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        kinds = (
            FailureKind.INVALID_TERMINAL,
            FailureKind.TASK_FAILURE,
            FailureKind.INVALID_REQUEST,
            FailureKind.AUTH,
            # A timeout is absent on purpose: a stage no longer advances "for
            # time" (owner, 2026-10-19), so a timeout result blocks the stage
            # instead of retrying through the expired quarantine here.
            FailureKind.TRANSPORT,
            FailureKind.MODEL_UNAVAILABLE,
        )
        for kind in kinds:
            with self.subTest(kind=kind.value):
                self.db_path = self.workspace / f"rearm-{kind.value}.db"
                self.fake_adapters["opencode_go"] = ScriptedAdapter("opencode_go")
                self.fake_adapters["claude"] = ScriptedAdapter("claude")
                self.quarantine(
                    "opencode_go", "quota", age_seconds=_QUARANTINE_RETRY_SECONDS + 60
                )
                go = self.fake_adapters["opencode_go"]
                go.result = failed(kind, "opencode_go")
                self.fake_adapters["claude"].result = completed(adapter="claude")
                router = self.router()

                for stage_id in ("st-6", "st-7", "st-8"):
                    router.run(
                        request(self.workspace, stage_id=stage_id),
                        primary_and_next(),
                    )

                # An expired quarantine buys exactly one real retry per hour,
                # whatever the failure looks like: only success is proof.
                self.assertEqual(len(go.requests), 1)
                health = router.store.health("opencode_go")
                self.assertTrue(health["quarantined"])
                self.assertFalse(health["ready"])
                self.assertEqual(health["failure"], kind.value)
                rearmed_at = datetime.strptime(
                    health["quarantined_at"], "%Y-%m-%dT%H:%M:%S.%fZ"
                ).replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - rearmed_at).total_seconds()
                self.assertLess(age, 60)
                # The re-armed quarantine is cached again: no probe per run.
                self.assertEqual(len(go.probes), 1)


class StageLeaseHeartbeatTests(RouterTestCase):
    """The lease must outlive any stage: a heartbeat renews it while the
    vendor process runs, and a dead process stops beating so the lease
    expires on its own, exactly as before."""

    def _counting_renewals(self, store):
        renewals = []
        original = store.renew_stage

        def counting(*args, **kwargs):
            renewed = original(*args, **kwargs)
            if renewed:
                renewals.append(time.monotonic())
            return renewed

        store.renew_stage = counting
        return renewals

    def test_the_lease_beats_during_a_long_invocation_and_blocks_other_owners(self):
        router = self.router()
        entered, release = threading.Event(), threading.Event()

        def unbounded(request, route):
            entered.set()
            release.wait(10)
            return completed("finally done", "opencode_go")

        self.fake_adapters["opencode_go"].result = unbounded
        renewals = self._counting_renewals(router.store)
        other = SubscriptionStateStore(self.db_path)

        with (
            patch("subscriptions.router._STAGE_LEASE_RENEWAL_SECONDS", 0.01),
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            future = pool.submit(
                router.run, request(self.workspace), routes_for_main()[:1]
            )
            try:
                self.assertTrue(entered.wait(10))
                time.sleep(0.15)
                # An outside owner cannot claim the stage while it runs.
                self.assertFalse(other.claim_stage("wf-6", "st-6", "thief", 600))
                self.assertGreaterEqual(len(renewals), 5)
                beats = len(renewals)
                time.sleep(0.15)
                # The heartbeat kept beating while the invocation stayed slow.
                self.assertGreater(len(renewals), beats)
                self.assertFalse(other.claim_stage("wf-6", "st-6", "thief", 600))
            finally:
                release.set()
            result = future.result(timeout=10)

        self.assertTrue(result.success)
        # The heartbeat stops with the stage: the renewal count freezes.
        stopped = len(renewals)
        time.sleep(0.1)
        self.assertEqual(len(renewals), stopped)
        self.assertTrue(router.store.claim_stage("wf-6", "st-6", "next-owner", 60))
        router.store.release_stage("wf-6", "st-6", "next-owner")

    def test_a_dead_process_stops_beating_and_its_lease_expires_alone(self):
        from subscriptions.router import _StageLeaseHeartbeat

        store = SubscriptionStateStore(self.db_path)
        self.assertTrue(store.claim_stage("wf-9", "st-9", "owner", 0.3))
        renewals = self._counting_renewals(store)
        heartbeat = _StageLeaseHeartbeat(
            store,
            "wf-9",
            "st-9",
            "owner",
            lease_seconds=0.3,
            interval=0.05,
        )
        heartbeat.start()
        time.sleep(0.2)
        self.assertGreaterEqual(len(renewals), 2)

        # The router process dying is the thread dying: nothing renews after
        # the beat stops, and the lease lapses with no owner releasing it.
        heartbeat.stop()
        frozen = len(renewals)
        time.sleep(0.1)
        self.assertEqual(len(renewals), frozen)
        time.sleep(0.3)
        self.assertTrue(store.claim_stage("wf-9", "st-9", "next-owner", 60))
        store.release_stage("wf-9", "st-9", "next-owner")

    def test_a_heartbeat_that_cannot_start_releases_the_lease(self):
        from subscriptions.router import _StageLeaseHeartbeat

        router = self.router()
        with patch.object(
            _StageLeaseHeartbeat,
            "start",
            side_effect=RuntimeError("can't start new thread"),
        ):
            with self.assertRaises(RuntimeError):
                router.run(request(self.workspace), routes_for_main()[:1])

        # The stage is free at once — not held for the lease window with no
        # one running it.
        self.assertTrue(router.store.claim_stage("wf-6", "st-6", "next-owner", 60))
        router.store.release_stage("wf-6", "st-6", "next-owner")


if __name__ == "__main__":
    unittest.main()
