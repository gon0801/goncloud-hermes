"""Grok subscription adapter with sandbox and private prompt transport."""

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
from . import (
    SubscriptionAdapter,
    build_assignment_prompt,
    classify_failure,
    health_for_process_error,
)


def parse_grok_ndjson(output: str) -> dict:
    """Accumulate agent text from grok streaming-json (and ACP) lines."""
    texts: list[str] = []
    session_id = None
    ended = False
    invalid = False
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            invalid = True
            continue
        if not isinstance(event, dict):
            invalid = True
            continue
        if any(
            key in event and not isinstance(event[key], str)
            for key in ("type", "sessionId", "stopReason")
        ):
            invalid = True
            continue
        session_id = event.get("sessionId") or session_id
        if event.get("type") in {"error", "turn.failed"} or event.get("error"):
            invalid = True
        if event.get("type") == "text" and "data" in event:
            text = event.get("data")
            if not isinstance(text, str) or ended:
                invalid = True
            else:
                texts.append(text)
        if event.get("type") == "end":
            invalid = invalid or ended or event.get("stopReason") != "end_turn"
            ended = True
        update = event.get("sessionUpdate")
        if not isinstance(update, dict):
            continue
        if any(
            key in update and not isinstance(update[key], str)
            for key in ("update", "sessionId")
        ):
            invalid = True
            continue
        session_id = update.get("sessionId") or session_id
        kind = update.get("update")
        if kind == "error" or update.get("error"):
            invalid = True
        content = update.get("content")
        if kind in {"agent_message", "agent_message_chunk"}:
            if isinstance(content, dict):
                content = content.get("text")
            if not isinstance(content, str) or ended:
                invalid = True
            elif isinstance(content, str):
                texts.append(content)
    return {
        "text": "".join(texts),
        "session_id": session_id,
        "ended": ended,
        "terminal_valid": ended and not invalid,
    }


class GrokAdapter(SubscriptionAdapter):
    """Run one stage on the grok.com subscription, sandboxed, prompt by file."""

    account_class = "grok_com"

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """``grok models`` is non-generative and reports the login state."""
        try:
            evidence = run_cli(
                ["grok", "models"],
                "",
                Path.cwd(),
                self.probe_timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return health_for_process_error(self.account_class, error)
        haystack = (evidence.stdout or "").lower()
        if evidence.returncode != 0 or "logged in" not in haystack:
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                failure=FailureKind.AUTH,
                detail="no grok.com subscription login",
            )
        if route.model.lower() not in haystack:
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                failure=FailureKind.MODEL_UNAVAILABLE,
                detail=f"model {route.model} is not offered by this login",
            )
        return AdapterHealth(ready=True, account_class=self.account_class)

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        command = [
            "grok",
            "--output-format",
            "streaming-json",
            "--permission-mode",
            "dontAsk",
            "--sandbox",
            "workspace",
            "--model",
            route.model,
            "--max-turns",
            str(request.max_steps),
            "--prompt-file",
            "{prompt_file}",
        ]
        try:
            evidence = run_cli(
                command,
                build_assignment_prompt(request),
                request.workspace,
                self.timeout_seconds,
                prompt_transport="tempfile",
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return self._failed_result(route, error.failure, str(error))

        parsed = parse_grok_ndjson(evidence.stdout)
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
        failure = (
            FailureKind.TASK_FAILURE
            if evidence.returncode == 0
            else classify_failure(evidence.returncode, evidence.stdout, evidence.stderr)
        )
        return self._failed_result(route, failure)
