from __future__ import annotations

import subprocess
import os
import sqlite3
import sys
import tempfile
import threading
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
from subscriptions.adapters.opencode import OpenCodeAdapter  # noqa: E402
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
            "anthropic/claude-sonnet-5",
            "meta/muse-spark-1.3 (max)",
            "meta/muse-spark-1.3 (max)",
            "xai/grok-4.6",
            "zai/glm-5.3",
            "openai/gpt-5.6-terra (xhigh)",
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
            replace(request(self.workspace), pre_invoke=publish), routes_for_main()[:2]
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
                router.run(assignment, routes_for_main()[:2])
        resumed = self.router()
        result = resumed.run(assignment, routes_for_main()[:2])
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

    def test_timeout_without_mutation_advances(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TIMEOUT)
        self.fake_adapters["claude"].result = completed("zen ok", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "claude"])

    def test_transport_failure_without_mutation_advances(self):
        router = self.router()
        self.fake_adapters["opencode_go"].result = failed(FailureKind.TRANSPORT)
        self.fake_adapters["claude"].result = completed("zen ok", "claude")

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertEqual(router.calls, ["opencode_go", "claude"])

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
        self.fake_adapters["claude"].result = completed("must not run", "claude")

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
            "claude",
            "muse",
            "grok",
            "zai",
            "codex",
        ):
            self.fake_adapters[name].result = failed(FailureKind.AUTH, name)

        result = router.run(request(self.workspace), routes_for_main())

        self.assertFalse(result.success)
        # Slots 2 and 3 share Muse; AUTH on the first quarantines the account
        # so the repeated position is recorded without a second invocation.
        self.assertEqual(
            router.calls,
            ["opencode_go", "claude", "muse", "grok", "zai", "codex"],
        )
        self.assertIn("all seven", result.text.lower())
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(len(attempts), 7)
        self.assertEqual(
            [row.get("disposition") for row in attempts],
            [
                "executed",
                "executed",
                "executed",
                "skipped",
                "executed",
                "executed",
                "executed",
            ],
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
        self.assertTrue(router.store.health("opencode_go")["ready"])

    def test_timeout_cools_the_account_so_a_hung_endpoint_is_skipped(self):
        # opencode_go hangs to its 300s deadline at slot 0 of nearly every
        # chain. A timeout must cool the account down like quota so the next
        # stage skips it instead of hanging the full deadline again.
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.TIMEOUT, adapter="opencode_go"
        )
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        result = router.run(request(self.workspace), routes_for_main())

        self.assertTrue(result.success)
        self.assertIsNotNone(router.store.cooldown_until("opencode_go"))

    def test_ordinary_cooldown_is_recorded_with_quota_and_exact_duplicate_positions(
        self,
    ):
        router = self.router()
        router.store.set_cooldown("opencode_go", hours=1)
        self.fake_adapters["claude"].result = completed("zen", "claude")
        first, second = routes_for_main()[:2]
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
                        router.run(assignment, routes_for_main()[:2])
                resumed = self.router()
                self.assertTrue(resumed.run(assignment, routes_for_main()[:2]).success)
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

    def test_bad_envelope_at_slot_zero_advances_although_the_tree_started_dirty(self):
        """The owner's own uncommitted files must not pin the stage to slot 0.

        Two defects combined here. The CLI exits 0 with no terminal event, which
        was classified ``task_failure`` and never advances; and the dirty tree
        made attempt 0 a continuation, which forced ``mutated`` true and blocked
        the advancing kinds too. Either one alone leaves every healthy account
        untried.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "owner-wip.py").write_text("my own edit", encoding="utf-8")
        partial = '{"type":"part","part":{"type":"text","text":"partial"}}'

        def exits_zero_without_terminal(command, **kwargs):
            return subprocess.CompletedProcess(
                list(command), 0, stdout=partial, stderr=""
            )

        self.fake_adapters["opencode_go"] = OpenCodeAdapter(
            runner=exits_zero_without_terminal
        )
        self.fake_adapters["claude"].result = completed("recovered", "claude")
        SubscriptionStateStore(self.db_path).save_health(
            "opencode_go", ready=True, account_class="subscription"
        )

        router = self.router()
        result = router.run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
                result_transform=lambda value: value,
            ),
            routes_for_main()[:2],
        )

        self.assertTrue(result.success)
        self.assertEqual(result.text, "recovered")
        attempts = router.store.stage_attempts("wf-6", "st-6")
        self.assertEqual(attempts[0]["failure"], "invalid_terminal")
        self.assertEqual(attempts[0]["mode"], "start")

    def test_a_worker_rewriting_an_already_dirty_file_counts_as_mutation(self):
        """Names alone cannot show a rewrite.

        The owner left README.md dirty. Slot 0 edits that same file and then
        returns a rejected envelope. The set of changed NAMES is identical
        before and after, so a name-only comparison reads "nothing changed" and
        hands the next slot a workspace it would redo work on top of.
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
            routes_for_main()[:2],
        )

        self.assertFalse(result.success)
        self.assertEqual(result.failure, FailureKind.INVALID_TERMINAL)
        self.assertEqual(self.fake_adapters["claude"].requests, [])
        attempt = router.store.stage_attempts("wf-6", "st-6")[0]
        self.assertIn("README.md", attempt["changed_files"])

    def test_owner_dirt_is_not_recorded_as_the_attempts_own_change(self):
        """The durable row must name only what this attempt touched.

        Recording the owner's files makes a later resume read them back as a
        worker mutation, which blocks the very failure kinds that should
        advance, and tells the next slot it is continuing work nobody did.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "owner-wip.txt").write_text("mine", encoding="utf-8")
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.AUTH, "opencode_go"
        )
        self.fake_adapters["claude"].result = completed("recovered", "claude")

        router = self.router()
        result = router.run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            ),
            routes_for_main()[:2],
        )

        self.assertTrue(result.success)
        attempt = router.store.stage_attempts("wf-6", "st-6")[0]
        self.assertEqual(attempt["changed_files"], [])
        second = self.fake_adapters["claude"].requests[0]
        self.assertEqual(second.mode, "start")
        self.assertEqual(second.changed_files, ())

    def test_a_restart_after_an_attempt_that_changed_nothing_is_not_a_continuation(
        self,
    ):
        """A recorded attempt is not by itself evidence of a mutation.

        The owner's tree is dirty and slot 0 failed without touching anything.
        Treating the mere existence of that attempt as a continuation puts the
        stage back where the fix found it: mutated, so nothing advances.
        """
        start = self._run_git("rev-parse", "HEAD").stdout.strip()
        (self.git_repo / "owner-wip.txt").write_text("mine", encoding="utf-8")
        self.fake_adapters["opencode_go"].result = failed(
            FailureKind.AUTH, "opencode_go"
        )
        self.router().run(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            ),
            routes_for_main()[:1],
        )

        absorbed = self.router()._absorb_workspace_state(
            replace(
                request(self.git_repo),
                start_revision=start,
                current_revision=start,
            )
        )

        self.assertEqual(absorbed.mode, "start")

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

        self.assertEqual(len(status["routes"]), 7)
        deepseekless = [r for r in status["routes"] if r["adapter"] == "codex"]
        self.assertEqual(deepseekless[0]["slot"], 6)
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
                    "deepseek/deepseek-flash",
                ]
            }
        }
        router.routing_source = source

        status = router.status("scout")

        self.assertEqual(status["routes"][6]["billing_mode"], "api")
        self.assertEqual(status["routes"][6]["adapter"], "deepseek")

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
            "fixture@example.test",
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
            routes_for_main()[:2],
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
        mutated_git = type(
            "Git",
            (),
            {
                "current_revision": lambda _, workspace: "r1",
                "resolve_revision": lambda _, workspace, revision: "r0",
                "changed_files": lambda _, workspace: ("a.txt",),
                "dirty_digest": lambda _, workspace: {"a.txt": "same-before-and-after"},
                "mutation_since": lambda _, workspace, revision: (
                    "r1",
                    ("a.txt",),
                    True,
                ),
            },
        )()
        router = SubscriptionRouter(
            adapters=self.fake_adapters,
            db_path=self.db_path,
            routing_source=SOURCE,
            git=mutated_git,
        )

        result = router.run(
            replace(
                request(self.workspace), result_transform=self.rejecting_transform()
            ),
            routes_for_main()[:2],
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

        result = self.router().run(request(self.workspace), routes_for_main()[:2])

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

        result = router.run(request(self.workspace), routes_for_main()[:2])

        self.assertEqual(result.adapter, "opencode_go")
        self.assertEqual(len(self.fake_adapters["opencode_go"].requests), 1)
        health = router.store.health("opencode_go")
        self.assertTrue(health["ready"])
        self.assertFalse(health["quarantined"])
        self.assertIsNone(health["failure"])

    def test_an_expired_quarantine_that_fails_again_is_rearmed(self):
        from subscriptions.router import _QUARANTINE_RETRY_SECONDS

        self.quarantine(
            "opencode_go", "quota", age_seconds=_QUARANTINE_RETRY_SECONDS + 60
        )
        go = self.fake_adapters["opencode_go"]
        go.result = failed(FailureKind.QUOTA, "opencode_go")
        self.fake_adapters["claude"].result = completed(adapter="claude")
        router = self.router()

        first = router.run(request(self.workspace), routes_for_main()[:2])
        second = router.run(
            request(self.workspace, stage_id="st-7"), routes_for_main()[:2]
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
            FailureKind.TIMEOUT,
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
                        routes_for_main()[:2],
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


if __name__ == "__main__":
    unittest.main()
