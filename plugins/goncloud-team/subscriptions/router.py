"""Mutation-aware subscription failover router."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from .adapters import build_adapter_registry
from .catalog import compile_profile_routes, load_routing_source
from .process import run_cli
from .state import SubscriptionStateStore
from .types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)

#: Failures that may advance to the next slot automatically.
_ADVANCING_FAILURES = {
    FailureKind.AUTH,
    FailureKind.QUOTA,
    FailureKind.MODEL_UNAVAILABLE,
    FailureKind.TIMEOUT,
    FailureKind.TRANSPORT,
}
_QUOTA_COOLDOWN_MINUTES = 15


def default_db_path() -> Path:
    """Private, Git-ignored location shared by every profile process."""
    configured = os.environ.get("GONCLOUD_SUBSCRIPTION_DB")
    if configured:
        return Path(configured)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes" / "subscription-router.db"
    return Path.home() / ".goncloud" / "subscription-router.db"


class GitInspector:
    """Read-only Git evidence for one workspace (never mutates)."""

    def _git(self, workspace: Path, *args: str) -> tuple[int, str]:
        try:
            evidence = run_cli(
                ["git", *args],
                "",
                workspace,
                30,
            )
        except Exception:  # noqa: BLE001 - git is advisory evidence only
            return 1, ""
        return evidence.returncode, (evidence.stdout or "").strip()

    def current_revision(self, workspace: Path) -> str:
        code, revision = self._git(workspace, "rev-parse", "HEAD")
        return revision if code == 0 else ""

    def changed_files(self, workspace: Path) -> tuple[str, ...]:
        code, porcelain = self._git(
            workspace, "status", "--porcelain", "--untracked-files=all"
        )
        if code != 0:
            return ()
        names = []
        for line in porcelain.splitlines():
            entry = line[3:].strip() if len(line) > 3 else ""
            if not entry:
                continue
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            names.append(entry.strip('"'))
        return tuple(dict.fromkeys(names))


class SubscriptionRouter:
    """Compile the slot table, invoke adapters, and fail over durably."""

    def __init__(
        self,
        *,
        adapters: Optional[dict] = None,
        store: Optional[SubscriptionStateStore] = None,
        db_path: Optional[Path | str] = None,
        routing_source: Optional[dict] = None,
        git: Optional[GitInspector] = None,
        health_ttl_seconds: int = 300,
    ) -> None:
        self.adapters = adapters if adapters is not None else build_adapter_registry()
        self.routing_source = (
            routing_source if routing_source is not None else load_routing_source()
        )
        self.git = git if git is not None else GitInspector()
        if store is None:
            self.store = SubscriptionStateStore(
                db_path if db_path is not None else default_db_path()
            )
        else:
            self.store = store
        self.health_ttl_seconds = health_ttl_seconds

    # -- execution --------------------------------------------------------

    def run(
        self, request: InvocationRequest, routes: Iterable[RouteSpec]
    ) -> InvocationResult:
        """Execute one stage assignment across the slot chain."""
        recorded = self.store.latest_success(request.workflow_id, request.stage_id)
        if recorded is not None:
            return _result_from_attempt(recorded)

        request = self._absorb_workspace_state(request)
        last_failure: Optional[InvocationResult] = None
        attempted_any = False

        for route in routes:
            if self._in_cooldown(route.adapter):
                continue
            adapter = self.adapters.get(route.adapter)
            if adapter is None:
                continue
            attempt_no = self.store.next_attempt(
                request.workflow_id, request.stage_id, route.slot
            )
            availability = self._availability(route)
            if not availability.ready:
                failure = availability.failure or FailureKind.TASK_FAILURE
                skipped = InvocationResult(
                    success=False,
                    text=f"{route.adapter} invocation failed ({failure.value})",
                    adapter=route.adapter,
                    model=route.model,
                    billing_mode=route.billing_mode,
                    failure=failure,
                )
                self.store.record_attempt(
                    workflow_id=request.workflow_id,
                    stage_id=request.stage_id,
                    slot=route.slot,
                    attempt=attempt_no,
                    adapter=route.adapter,
                    model=route.model,
                    billing_mode=route.billing_mode,
                    mode=request.mode,
                    success=False,
                    failure=failure.value,
                    session_id="",
                    start_revision=request.start_revision,
                    end_revision=self.git.current_revision(request.workspace),
                    changed_files=list(self.git.changed_files(request.workspace)),
                    detail=skipped.text,
                )
                attempted_any = True
                last_failure = skipped
                continue

            attempted_any = True
            result = self._invoke_route(request, route)
            changed = self.git.changed_files(request.workspace)
            revision = self.git.current_revision(request.workspace)
            self.store.record_attempt(
                workflow_id=request.workflow_id,
                stage_id=request.stage_id,
                slot=route.slot,
                attempt=attempt_no,
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                mode=request.mode,
                success=result.success,
                failure=result.failure.value if result.failure else None,
                session_id=result.session_id,
                start_revision=request.start_revision,
                end_revision=revision,
                changed_files=list(changed),
                detail=result.text[:2000],
            )
            self._record_health(route.adapter, result)
            if result.success:
                return result
            last_failure = result
            mutated = bool(changed)
            if result.failure not in _ADVANCING_FAILURES:
                # Task/invalid failures never auto-advance; the stage blocks
                # with evidence and the next run continues from recorded state.
                return result
            if result.failure is FailureKind.TRANSPORT and mutated:
                return result
            if result.failure is FailureKind.QUOTA:
                self.store.set_cooldown(
                    route.adapter,
                    until=datetime.now(timezone.utc)
                    + timedelta(minutes=_QUOTA_COOLDOWN_MINUTES),
                )
            if mutated:
                request = replace(
                    request,
                    mode="continue",
                    current_revision=revision or request.current_revision,
                    changed_files=changed,
                )

        if not attempted_any:
            return InvocationResult(
                success=False,
                text="all routes for this stage are cooling down; retry later",
                adapter="router",
                model="",
                billing_mode="subscription",
                failure=FailureKind.QUOTA,
            )
        return InvocationResult(
            success=False,
            text=(
                f"{last_failure.text} — all eight slots Primary..F7 are "
                "exhausted for this stage; owner review is required"
            ),
            adapter=last_failure.adapter,
            model=last_failure.model,
            billing_mode=last_failure.billing_mode,
            failure=last_failure.failure,
        )

    def _invoke_route(
        self, request: InvocationRequest, route: RouteSpec
    ) -> InvocationResult:
        return self.adapters[route.adapter].invoke(request, route)

    def _absorb_workspace_state(self, request: InvocationRequest) -> InvocationRequest:
        """A recorded mutation turns a fresh run into an explicit continuation."""
        changed = self.git.changed_files(request.workspace)
        revision = self.git.current_revision(request.workspace)
        if not changed:
            return request
        return replace(
            request,
            mode="continue",
            current_revision=revision or request.current_revision,
            changed_files=changed,
        )

    def _in_cooldown(self, adapter: str) -> bool:
        until = self.store.cooldown_until(adapter)
        if until is None:
            return False
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
        return until > datetime.now(timezone.utc)

    def _availability(self, route: RouteSpec) -> AdapterHealth:
        """Use bounded health evidence, probing and normalizing it when stale."""
        cached = self.store.fresh_health(route.adapter, self.health_ttl_seconds)
        if cached is not None:
            failure = cached.get("failure")
            return AdapterHealth(
                ready=bool(cached["ready"]),
                account_class=str(cached["account_class"]),
                failure=FailureKind(failure) if failure else None,
                detail=str(cached.get("detail") or ""),
            )

        health = replace(self.adapters[route.adapter].probe(route), detail="")
        self.store.save_health(
            route.adapter,
            ready=health.ready,
            account_class=health.account_class,
            failure=health.failure.value if health.failure else None,
            detail=health.detail,
        )
        return health

    def _record_health(self, adapter: str, result: InvocationResult) -> None:
        if result.success:
            self.store.save_health(
                adapter,
                ready=True,
                account_class=adapter,
                detail="live attempt succeeded",
            )
            return
        failure = result.failure
        self.store.save_health(
            adapter,
            ready=failure not in _ADVANCING_FAILURES,
            account_class=adapter,
            failure=failure.value if failure else None,
            detail="live attempt failed",
        )

    # -- status -----------------------------------------------------------

    def status(
        self,
        profile: str,
        *,
        workflow_id: Optional[str] = None,
        stage_id: Optional[str] = None,
    ) -> dict:
        """Bounded, non-generative status surface for one profile."""
        routes = compile_profile_routes(profile, self.routing_source)
        payload = {
            "profile": profile,
            "routes": [
                {
                    "slot": route.slot,
                    "label": route.label,
                    "adapter": route.adapter,
                    "model": route.model,
                    "billing_mode": route.billing_mode,
                    "health": self.store.health(route.adapter),
                }
                for route in routes
            ],
        }
        if workflow_id and stage_id:
            attempts = self.store.stage_attempts(workflow_id, stage_id)
            payload["stage"] = {
                "workflow_id": workflow_id,
                "stage_id": stage_id,
                "attempts": attempts,
            }
        return payload


def _result_from_attempt(attempt: dict) -> InvocationResult:
    return InvocationResult(
        success=bool(attempt["success"]),
        text=attempt.get("detail") or "",
        adapter=attempt["adapter"],
        model=attempt["model"],
        billing_mode=attempt["billing_mode"],
        session_id=attempt.get("session_id") or "",
        failure=None if not attempt.get("failure") else FailureKind(attempt["failure"]),
    )
