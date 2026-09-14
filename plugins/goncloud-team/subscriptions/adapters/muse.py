"""Muse CLI adapter reusing the existing Windows-to-WSL bridge."""

from __future__ import annotations

from pathlib import Path

from ..process import SubscriptionProcessError, run_cli
from ..types import AdapterHealth, InvocationRequest, InvocationResult, RouteSpec
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure

try:  # Loaded as a subpackage of the Hermes plugin.
    from ...muse_bridge import MuseBridgeError, delegate_to_muse
except ImportError:  # Loaded with the plugin directory itself on sys.path.
    from muse_bridge import MuseBridgeError, delegate_to_muse  # type: ignore

_MUSE = "/home/goncloud/.local/bin/muse"


class MuseAdapter(SubscriptionAdapter):
    """Delegate one bounded stage to the owner's Muse subscription in WSL."""

    max_steps = 80

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
            return AdapterHealth(
                ready=False,
                account_class="muse_cli",
                detail=str(error),
            )
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

    def invoke(
        self, request: InvocationRequest, route: RouteSpec
    ) -> InvocationResult:
        try:
            outcome = delegate_to_muse(
                prompt=build_assignment_prompt(request),
                workspace=request.workspace,
                actor_role=request.role,
                max_steps=self.max_steps,
                timeout=self.timeout_seconds,
                runner=self.runner,
            )
        except MuseBridgeError as error:
            failure = classify_failure(1, "", str(error))
            return self._failed_result(route, failure)

        if outcome.get("terminal") != "completed":
            failure = classify_failure(
                1,
                str(outcome.get("text") or ""),
                str(outcome.get("terminal") or ""),
            )
            return self._failed_result(route, failure)
        return InvocationResult(
            success=True,
            text=str(outcome.get("text") or ""),
            adapter=route.adapter,
            model=route.model,
            billing_mode=route.billing_mode,
            session_id=str(outcome.get("session_id") or ""),
        )
