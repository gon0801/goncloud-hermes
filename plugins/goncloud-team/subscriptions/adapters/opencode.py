"""OpenCode Go and OpenCode Zen subscription adapters.

Both slots consume the owner's OpenCode subscriptions through the native CLI
credential store. ``auth.json`` is never read or copied by this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..process import SubscriptionProcessError, run_cli
from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure

#: Slot identity, not the historical label prefix, selects the subscription.
_PROVIDER_PREFIXES = {"opencode_go": "opencode-go", "opencode_zen": "opencode"}
_CREDENTIAL_NAMES = {"opencode_go": "opencode go", "opencode_zen": "opencode zen"}


def parse_opencode_jsonl(output: str) -> dict:
    """Accumulate text parts and session identity from raw JSON events."""
    text_parts: list[str] = []
    session_id = None
    terminal_valid = False
    saw_error = False
    malformed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        event_type = str(event.get("type") or "").lower()
        if "error" in event_type or event.get("error"):
            saw_error = True
        session_id = event.get("sessionID") or event.get("session_id") or session_id
        info = event.get("info")
        if isinstance(info, dict):
            session_id = info.get("id") or session_id
        part = event.get("part")
        if isinstance(part, dict):
            part_type = str(part.get("type") or "").lower()
            reason = str(part.get("reason") or "").lower()
            if (
                "error" in part_type
                or part.get("error")
                or reason in {"error", "failed"}
            ):
                saw_error = True
            if part_type == "text":
                text_parts.append(str(part.get("text") or ""))
            if (
                event_type == "step_finish"
                and part_type == "step-finish"
                and reason == "stop"
            ):
                terminal_valid = True
    return {
        "session_id": session_id,
        "text": "".join(text_parts),
        "terminal_valid": terminal_valid and not saw_error and not malformed,
    }


class OpenCodeAdapter(SubscriptionAdapter):
    """Invoke ``opencode run`` against the Go or Zen subscription of a slot."""

    def _provider(self, route: RouteSpec) -> str:
        provider = _PROVIDER_PREFIXES.get(route.adapter)
        if provider is None:
            raise ValueError(f"unsupported OpenCode slot adapter: {route.adapter}")
        return provider

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Non-generative check that the slot's credential class is installed."""
        expected = _CREDENTIAL_NAMES.get(route.adapter, route.adapter)
        try:
            evidence = run_cli(
                ["opencode", "auth", "list"],
                "",
                Path.cwd(),
                self.probe_timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=FailureKind.TASK_FAILURE,
                detail=str(error),
            )
        if evidence.returncode != 0:
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=classify_failure(
                    evidence.returncode, evidence.stdout, evidence.stderr
                ),
                detail="opencode auth list failed",
            )
        installed = evidence.stdout.lower()
        if expected in installed:
            return AdapterHealth(ready=True, account_class=route.adapter)
        return AdapterHealth(
            ready=False,
            account_class=route.adapter,
            failure=FailureKind.AUTH,
            detail=f"credential class not installed: {expected}",
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        command = [
            "opencode",
            "run",
            "--format",
            "json",
            "--model",
            f"{self._provider(route)}/{route.model}",
            "--dir",
            str(request.workspace),
        ]
        try:
            evidence = run_cli(
                command,
                build_assignment_prompt(request),
                request.workspace,
                self.timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            if "timed out" in str(error):
                return self._failed_result(route, FailureKind.TIMEOUT)
            return self._failed_result(route, FailureKind.TASK_FAILURE, str(error))

        if evidence.returncode != 0:
            failure = classify_failure(
                evidence.returncode, evidence.stdout, evidence.stderr
            )
            return self._failed_result(route, failure)

        parsed = parse_opencode_jsonl(evidence.stdout)
        if (
            evidence.returncode == 0
            and parsed["terminal_valid"]
            and parsed["text"].strip()
        ):
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        return self._failed_result(
            route,
            classify_failure(evidence.returncode, evidence.stdout, evidence.stderr),
        )
