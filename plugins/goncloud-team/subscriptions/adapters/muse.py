"""Muse CLI adapter reusing the existing Windows-to-WSL bridge."""

from __future__ import annotations

from pathlib import Path

from ..process import SubscriptionProcessError, run_cli
from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from . import (
    SubscriptionAdapter,
    build_assignment_prompt,
    classify_failure,
    health_for_process_error,
)

try:  # Loaded as a subpackage of the Hermes plugin.
    from ...muse_bridge import MuseBridgeError, delegate_to_muse
except ImportError:  # Loaded with the plugin directory itself on sys.path.
    from muse_bridge import MuseBridgeError, delegate_to_muse  # type: ignore

_MUSE = "/home/goncloud/.local/bin/muse"


class MuseAdapter(SubscriptionAdapter):
    """Delegate one bounded stage to the owner's Muse subscription in WSL."""

    # WSL cold starts can exceed the default probe window by a wide margin.
    max_probe_timeout_seconds = 120
    probe_timeout_seconds = 120
    #: La config JEV resuelta del router; el router la inyecta antes de
    #: invoke. Este adaptador no clasifica con JEV: dentro del router
    #: clasifica solo el router (F6b).
    jev_config = None

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Muse exposes no non-generative auth probe; report install state."""
        try:
            evidence = run_cli(
                ["wsl.exe", "-d", "Ubuntu", "--exec", _MUSE, "--version"],
                "",
                Path.cwd(),
                self.probe_timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return health_for_process_error("muse_cli", error)
        if evidence.returncode != 0:
            return AdapterHealth(
                ready=False,
                account_class="muse_cli",
                detail="muse CLI did not answer --version",
            )
        return AdapterHealth(
            ready=True,
            account_class="muse_cli",
            detail="auth is proven only by live inference",
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        try:
            outcome = delegate_to_muse(
                prompt=build_assignment_prompt(request),
                workspace=request.workspace,
                actor_role=request.role,
                max_steps=request.max_steps,
                timeout=self.timeout_seconds,
                runner=self.runner,
                model=route.model,
                reasoning_effort=route.reasoning_effort,
                jev_config=self.jev_config,
                clasificar_con_jev=False,
            )
        except MuseBridgeError as error:
            return self._failed_result(route, error.failure)

        if outcome.get("model") != route.model:
            return self._failed_result(route, FailureKind.MODEL_UNAVAILABLE)
        if not isinstance(outcome.get("text"), str) or not outcome["text"].strip():
            return self._failed_result(route, FailureKind.TASK_FAILURE)
        if outcome.get("terminal") != "completed":
            detail = (
                str(outcome.get("terminal") or ""),
                str(outcome.get("text") or ""),
            )
            failure = classify_failure(1, detail[1], detail[0])
            return self._failed_result(route, failure, evidence=detail)
        return InvocationResult(
            success=True,
            text=str(outcome.get("text") or ""),
            adapter=route.adapter,
            model=route.model,
            billing_mode=route.billing_mode,
            session_id=str(outcome.get("session_id") or ""),
        )
