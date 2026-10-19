"""OpenCode Go and OpenCode Zen subscription adapters.

Both slots consume the owner's OpenCode subscriptions through the native CLI
credential store. ``auth.json`` is never read or copied by this module.
"""

from __future__ import annotations

import json
import os
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

#: Slot identity, not the historical label prefix, selects the subscription.
#: Provider prefixes exactly as ``opencode models`` lists them. The Z.AI entry
#: is what gives GLM the same read/grep/shell toolset as every other CLI-backed
#: slot: reached over its plain HTTP API instead, the model has no way to touch
#: the workspace and can only report that it could not look (real scout stage,
#: 2026-09-26). Verified live: ``zai-coding-plan/glm-5.3-flash`` read a file
#: through the ``read`` tool, closed with ``reason: stop``, and cost 0, so the
#: Coding Plan subscription serves it rather than a metered API.
_PROVIDER_PREFIXES = {
    "opencode_go": "opencode-go",
    "opencode_go_2": "opencode-go",
    "opencode_zen": "opencode",
    "opencode_zai": "zai-coding-plan",
}
#: The owner's second OpenCode Go account (2026-09-27). The saved CLI login is
#: the first account, so the second key reaches opencode as an env reference in
#: inline config: the value never enters argv, and redaction keys on the name.
_ACCOUNT_KEY_ENVS = {"opencode_go_2": "OPENCODE_GO_2_API_KEY"}

_CREDENTIAL_NAMES = {
    "opencode_go": "opencode go",
    "opencode_zen": "opencode zen",
    "opencode_zai": "z.ai coding plan",
}


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
        session_id = event.get("sessionID") or event.get("session_id") or session_id
        if event_type == "tool_use":
            continue
        if "error" in event_type or event.get("error"):
            saw_error = True
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

    def _env_overlay(self, route: RouteSpec):
        config: dict = {}
        key_env = _ACCOUNT_KEY_ENVS.get(route.adapter)
        if key_env:
            config["provider"] = {
                self._provider(route): {"options": {"apiKey": f"{{env:{key_env}}}"}}
            }
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(config)} if config else None

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Non-generative check that the slot's credential class is installed."""
        key_env = _ACCOUNT_KEY_ENVS.get(route.adapter)
        if key_env:
            if os.environ.get(key_env, "").strip():
                return AdapterHealth(ready=True, account_class=route.adapter)
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=FailureKind.AUTH,
                detail=f"{key_env} is not set",
            )
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
            return health_for_process_error(route.adapter, error)
        if evidence.returncode != 0:
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=classify_failure(
                    evidence.returncode, evidence.stream, evidence.stderr
                ),
                detail="opencode auth list failed",
            )
        installed = evidence.stream.lower()
        if expected in installed:
            return AdapterHealth(ready=True, account_class=route.adapter)
        return AdapterHealth(
            ready=False,
            account_class=route.adapter,
            failure=FailureKind.AUTH,
            detail=f"credential class not installed: {expected}",
        )

    def _run(self, request: InvocationRequest, route: RouteSpec, prompt: str):
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
        return run_cli(
            command,
            prompt,
            request.workspace,
            self.timeout_seconds,
            env_overlay=self._env_overlay(route),
            runner=self.runner,
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        try:
            evidence = self._run(request, route, build_assignment_prompt(request))
        except SubscriptionProcessError as error:
            return self._failed_result(route, error.failure, str(error))

        if evidence.returncode != 0:
            failure = classify_failure(
                evidence.returncode, evidence.stream, evidence.stderr
            )
            return self._failed_result(
                route,
                failure,
                evidence=(evidence.stderr, evidence.stream),
                returncode=evidence.returncode,
            )

        parsed = parse_opencode_jsonl(evidence.stream)
        if parsed["terminal_valid"] and parsed["text"].strip():
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        # The CLI answered, it just did not answer in the terminal
        # contract: the vendor responded and delivered nothing usable.
        # Nothing here shows the task itself failed, which is exactly
        # invalid_terminal.
        return self._failed_result(
            route,
            FailureKind.INVALID_TERMINAL,
            evidence=(evidence.stderr, evidence.stream),
            returncode=evidence.returncode,
        )
