"""Grok subscription adapter with sandbox and private prompt transport."""

from __future__ import annotations

import json
from pathlib import Path

from ..process import SubscriptionProcessError, run_cli
from ..types import AdapterHealth, FailureKind, InvocationRequest, InvocationResult, RouteSpec
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure


def parse_grok_ndjson(output: str) -> dict:
    """Accumulate agent text from ACP session-update stream lines."""
    texts: list[str] = []
    session_id = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        session_id = event.get("sessionId") or session_id
        update = event.get("sessionUpdate")
        if not isinstance(update, dict):
            continue
        session_id = update.get("sessionId") or session_id
        kind = update.get("update")
        content = update.get("content")
        if kind in {"agent_message", "agent_message_chunk"}:
            if isinstance(content, dict):
                texts.append(str(content.get("text") or ""))
            elif isinstance(content, str):
                texts.append(content)
    return {"text": "".join(texts), "session_id": session_id}


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
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                failure=FailureKind.TASK_FAILURE,
                detail=str(error),
            )
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

    def invoke(
        self, request: InvocationRequest, route: RouteSpec
    ) -> InvocationResult:
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
            if "timed out" in str(error):
                return self._failed_result(route, FailureKind.TIMEOUT)
            return self._failed_result(route, FailureKind.TASK_FAILURE, str(error))

        parsed = parse_grok_ndjson(evidence.stdout)
        if evidence.returncode == 0 and parsed["text"]:
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        failure = classify_failure(
            evidence.returncode, evidence.stdout, evidence.stderr
        )
        return self._failed_result(route, failure)
