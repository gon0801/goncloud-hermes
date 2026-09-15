"""Mutation-aware subscription failover router."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional
from uuid import uuid4

from .adapters import build_adapter_registry
from .catalog import compile_profile_routes, load_routing_source
from .process import run_cli
from .redaction import sanitize_result
from .state import SubscriptionBusy, SubscriptionStateStore, parse_iso
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
# Quota, connectivity and even credentials come back on their own, so a
# quarantine is a verdict with a lifetime, not a permanent ban. After this
# horizon the router may try the account again; only a successful real
# inference lifts the quarantine, and a repeated failure re-arms it.
_QUARANTINE_RETRY_SECONDS = 3600
# Renew before every invoke; this exceeds the 300s vendor deadline, its 5s
# cleanup grace, and the bounded Git evidence reads after invocation.
_STAGE_LEASE_SECONDS = 600


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
        return evidence.returncode, evidence.stdout or ""

    def current_revision(self, workspace: Path) -> str:
        return self.resolve_revision(workspace, "HEAD")

    def resolve_revision(self, workspace: Path, revision: str) -> str:
        code, resolved = self._git(
            workspace,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{revision}^{{commit}}",
        )
        return resolved.strip() if code == 0 else ""

    def changed_files(self, workspace: Path) -> tuple[str, ...]:
        code, porcelain = self._git(
            workspace, "status", "--porcelain", "-z", "--untracked-files=all"
        )
        if code != 0:
            return ()
        names = []
        entries = iter(porcelain.split("\0"))
        for entry in entries:
            if len(entry) <= 3:
                continue
            names.append(entry[3:])
            if "R" in entry[:2] or "C" in entry[:2]:
                # In -z format the destination precedes the original path.
                next(entries, None)
        return tuple(dict.fromkeys(names))

    def mutation_since(
        self, workspace: Path, revision: str
    ) -> tuple[str, tuple[str, ...], bool]:
        current = self.current_revision(workspace)
        committed = ()
        baseline = self.resolve_revision(workspace, revision) if revision else ""
        if baseline and current and baseline != current:
            code, names = self._git(
                workspace,
                "diff",
                "--name-only",
                "-z",
                "--end-of-options",
                f"{baseline}..{current}",
                "--",
            )
            if code == 0:
                committed = tuple(name for name in names.split("\0") if name)
        files = tuple(sorted(set(committed) | set(self.changed_files(workspace))))
        return current, files, bool(files or current != (baseline or revision))


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
        owner = uuid4().hex
        if not self.store.claim_stage(
            request.workflow_id, request.stage_id, owner, _STAGE_LEASE_SECONDS
        ):
            return self._stage_busy()
        try:
            result = self._run_owned(request, routes, owner)
            if request.result_transform is not None and not (
                result.adapter == "router"
                and result.failure is FailureKind.TASK_FAILURE
            ):
                if not self.store.mark_stage_terminal(
                    request.workflow_id, request.stage_id, owner
                ):
                    return self._stage_busy()
            return sanitize_result(result, (request.prompt,))
        finally:
            self.store.release_stage(request.workflow_id, request.stage_id, owner)

    @staticmethod
    def _stage_busy() -> InvocationResult:
        return InvocationResult(
            success=False,
            text="stage already running or ownership expired; retry later",
            adapter="router",
            model="",
            billing_mode="subscription",
            failure=FailureKind.TASK_FAILURE,
        )

    def _run_owned(
        self, request: InvocationRequest, routes: Iterable[RouteSpec], owner: str
    ) -> InvocationResult:
        if request.result_transform is None:
            recorded = self.store.latest_success(request.workflow_id, request.stage_id)
            if recorded is not None:
                return self._recover_result(request, recorded, owner)
            attempts = self.store.stage_attempts(request.workflow_id, request.stage_id)
            if (
                attempts
                and attempts[-1]["status"] == "completed"
                and attempts[-1]["disposition"] == "skipped"
            ):
                routes = tuple(
                    route for route in routes if route.slot > attempts[-1]["slot"]
                )
        else:
            routes = tuple(routes)
            attempts = self.store.stage_attempts(request.workflow_id, request.stage_id)
            if attempts:
                latest = attempts[-1]
                if latest["status"] == "completed":
                    failure = (
                        FailureKind(latest["failure"]) if latest["failure"] else None
                    )
                    mutated = bool(
                        latest["changed_files"]
                        or latest["end_revision"] != latest["start_revision"]
                    )
                    if (
                        latest["success"]
                        or latest["terminal"]
                        or (
                            latest["disposition"] != "skipped"
                            and (
                                failure not in _ADVANCING_FAILURES
                                or (failure is FailureKind.TRANSPORT and mutated)
                            )
                        )
                        or latest["slot"]
                        == max((route.slot for route in routes), default=-1)
                    ):
                        return self._recover_result(request, latest, owner)
                    # A crash between slots resumes the next distinct attempt.
                    routes = tuple(
                        route for route in routes if route.slot > latest["slot"]
                    )
                elif latest["status"] == "interrupted":
                    routes = tuple(
                        route for route in routes if route.slot >= latest["slot"]
                    )

        request = self._absorb_workspace_state(request)
        last_failure: Optional[InvocationResult] = None
        attempted_any = False

        for route in routes:
            if not self.store.renew_stage(
                request.workflow_id, request.stage_id, owner, _STAGE_LEASE_SECONDS
            ):
                return self._stage_busy()
            stored_health = self.store.health(route.adapter)
            cooling = self._in_cooldown(route.adapter) and not (
                self._quarantine_active(stored_health)
            )
            adapter = self.adapters.get(route.adapter)
            if adapter is None:
                continue
            attempt_no = self.store.next_attempt(
                request.workflow_id, request.stage_id, route.slot
            )
            denied = False
            if cooling:
                availability = AdapterHealth(False, route.adapter, FailureKind.QUOTA)
            elif getattr(adapter, "probe_is_generative", False):
                # Authorization runs outside OS locks: it may itself publish
                # health. Recheck that publication under exclusion before any
                # generative probe, and renew the lease after the callback.
                try:
                    if request.pre_invoke is not None:
                        request.pre_invoke()
                except Exception:
                    denied = True
                if denied:
                    availability = AdapterHealth(
                        False, route.adapter, FailureKind.INVALID_REQUEST
                    )
                else:
                    if not self.store.renew_stage(
                        request.workflow_id,
                        request.stage_id,
                        owner,
                        _STAGE_LEASE_SECONDS,
                    ):
                        return self._stage_busy()
                    try:
                        with self.store.dispatch_lock(route.adapter):
                            availability = self._availability(route)
                    except SubscriptionBusy:
                        return self._stage_busy()
            else:
                availability = self._availability(route)
            if not availability.ready:
                failure = availability.failure or FailureKind.TASK_FAILURE
                skipped = InvocationResult(
                    success=False,
                    text="subscription assignment authorization changed; request refused"
                    if denied
                    else f"{route.adapter} invocation failed ({failure.value})",
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
                    owner=owner,
                    disposition="denied" if denied else "skipped",
                )
                if denied:
                    return skipped
                attempted_any = True
                last_failure = skipped
                continue

            attempted_any = True
            if not self.store.renew_stage(
                request.workflow_id, request.stage_id, owner, _STAGE_LEASE_SECONDS
            ):
                return self._stage_busy()
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
                failure=None,
                session_id="",
                start_revision=request.start_revision,
                end_revision=request.current_revision,
                changed_files=list(request.changed_files),
                detail="attempt running",
                status="running",
                owner=owner,
            )
            # Probes and durable attempt reservation can outlive the initial
            # workflow validation. Recheck at the invocation boundary, not
            # before a potentially slow probe. Never publish callback errors.
            try:
                if request.pre_invoke is not None:
                    request.pre_invoke()
            except Exception:
                denied = InvocationResult(
                    success=False,
                    text="subscription assignment authorization changed; request refused",
                    adapter=route.adapter,
                    model=route.model,
                    billing_mode=route.billing_mode,
                    failure=FailureKind.INVALID_REQUEST,
                )
                recorded = self.store.finish_attempt(
                    workflow_id=request.workflow_id,
                    stage_id=request.stage_id,
                    slot=route.slot,
                    attempt=attempt_no,
                    owner=owner,
                    success=False,
                    failure=denied.failure.value,
                    session_id="",
                    end_revision=request.current_revision,
                    changed_files=list(request.changed_files),
                    detail=denied.text,
                    disposition="denied",
                )
                # Authorization says nothing about vendor health or quota.
                return denied if recorded else self._stage_busy()
            # Authorization may block longer than the lease. Fence a stale
            # owner and restore the full invocation/cleanup budget at dispatch.
            if not self.store.renew_stage(
                request.workflow_id, request.stage_id, owner, _STAGE_LEASE_SECONDS
            ):
                return self._stage_busy()
            disposition = "executed"
            try:
                # Publication and dispatch share an OS lock. A quarantine
                # committed during the probe/authorization wins before invoke;
                # publication cannot race the final check and vendor start.
                with self.store.dispatch_lock(route.adapter):
                    health = self.store.health(route.adapter)
                    if self._quarantine_active(health):
                        disposition = "skipped"
                        failure = FailureKind(health["failure"] or "task_failure")
                        result = InvocationResult(
                            success=False,
                            text=f"{route.adapter} invocation failed ({failure.value})",
                            adapter=route.adapter,
                            model=route.model,
                            billing_mode=route.billing_mode,
                            failure=failure,
                        )
                    else:
                        result = self._invoke_route(request, route)
            except SubscriptionBusy:
                return self._stage_busy()
            revision, changed, mutated = self.git.mutation_since(
                request.workspace, request.start_revision
            )
            changed = tuple(sorted(set(changed) | set(request.changed_files)))
            mutated = mutated or request.mode == "continue"
            recorded = self.store.finish_attempt(
                workflow_id=request.workflow_id,
                stage_id=request.stage_id,
                slot=route.slot,
                attempt=attempt_no,
                owner=owner,
                success=result.success,
                failure=result.failure.value if result.failure else None,
                session_id=result.session_id,
                end_revision=revision,
                changed_files=list(changed),
                detail=result.text[:2000],
                disposition=disposition,
            )
            if not recorded:
                return self._stage_busy()
            if disposition == "skipped":
                last_failure = result
                continue
            self._record_health(route.adapter, result)
            if result.success:
                return result
            last_failure = result
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
        try:
            result = self.adapters[route.adapter].invoke(request, route)
            if request.result_transform is not None:
                result = request.result_transform(result)
            return sanitize_result(result, (request.prompt,))
        except Exception:
            return InvocationResult(
                success=False,
                text="local terminal validation failed"
                if request.result_transform is not None
                else "subscription invocation failed",
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                failure=FailureKind.TASK_FAILURE,
            )

    def _recover_result(self, request, recorded, owner):
        result = _result_from_attempt(recorded)
        if request.pre_invoke is not None:
            request.pre_invoke()
        try:
            if request.result_transform is not None:
                result = request.result_transform(result)
            result = sanitize_result(result, (request.prompt,))
        except Exception:
            result = replace(
                result,
                success=False,
                text="local terminal validation failed",
                session_id="",
                failure=FailureKind.TASK_FAILURE,
            )
        if not self.store.normalize_completed_attempt(
            workflow_id=request.workflow_id,
            stage_id=request.stage_id,
            attempt_id=recorded["id"],
            owner=owner,
            success=result.success,
            failure=result.failure.value if result.failure else None,
            detail=result.text,
            session_id=result.session_id,
        ):
            return self._stage_busy()
        return result

    def _absorb_workspace_state(self, request: InvocationRequest) -> InvocationRequest:
        """A recorded mutation turns a fresh run into an explicit continuation."""
        evidence = self.store.stage_mutation(request.workflow_id, request.stage_id)
        attempts = self.store.stage_attempts(request.workflow_id, request.stage_id)
        # An interrupted attempt may have committed without recording its end
        # revision. Keep the durable baseline even if the caller supplies HEAD.
        baseline = next(
            (row["start_revision"] for row in attempts if row["start_revision"]),
            request.start_revision,
        )
        baseline = baseline or self.git.current_revision(request.workspace)
        baseline = self.git.resolve_revision(request.workspace, baseline) or baseline
        revision, changed, mutated = self.git.mutation_since(
            request.workspace, baseline
        )
        changed = tuple(
            sorted(
                set(changed)
                | set(request.changed_files)
                | set(evidence["changed_files"] if evidence else ())
            )
        )
        return replace(
            request,
            mode="continue"
            if mutated or evidence or request.mode == "continue"
            else "start",
            start_revision=baseline,
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

    @staticmethod
    def _quarantine_active(stored) -> bool:
        """A quarantine binds only within its retry horizon."""
        if not (stored and stored["quarantined"]):
            return False
        published = parse_iso(stored.get("quarantined_at") or stored.get("updated_at"))
        if published is None:
            return True
        age = (datetime.now(timezone.utc) - published).total_seconds()
        return age <= _QUARANTINE_RETRY_SECONDS

    def _cached_health(self, route: RouteSpec):
        cached = self.store.health(route.adapter)
        if not self._quarantine_active(cached):
            cached = self.store.fresh_health(route.adapter, self.health_ttl_seconds)
            if cached is not None and cached["quarantined"]:
                # An expired quarantine is not a verdict any more; re-probe.
                cached = None
        return cached

    def _availability(self, route: RouteSpec) -> AdapterHealth:
        """Read/probe health; generative callers own the adapter exclusion lock."""
        cached = self._cached_health(route)
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
        # Ordinary probe publication preserves a concurrent live quarantine,
        # but an expired one no longer outranks the probe just taken.
        stored = self.store.health(route.adapter)
        if not self._quarantine_active(stored):
            return health
        return AdapterHealth(
            ready=stored["ready"],
            account_class=stored["account_class"],
            failure=FailureKind(stored["failure"]) if stored["failure"] else None,
            detail=stored["detail"],
        )

    def _record_health(self, adapter: str, result: InvocationResult) -> None:
        if result.success:
            self.store.save_health(
                adapter,
                ready=True,
                account_class=adapter,
                detail="live attempt succeeded",
                lift_quarantine=True,
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
