"""ChatGPT Codex adapter running inside the Codex workspace sandbox."""

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


def parse_codex_jsonl(output: str) -> dict:
    """Extract agent text and thread identity from ``codex exec --json``."""
    texts: list[str] = []
    thread_id = None
    completed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        thread_id = event.get("thread_id") or event.get("session_id") or thread_id
        event_type = str(event.get("type") or "")
        if "turn.completed" in event_type or event_type == "task_complete":
            completed = True
        item = event.get("item")
        if isinstance(item, dict):
            kind = item.get("type") or item.get("item_type")
            if kind in {"agent_message", "message"}:
                texts.append(str(item.get("text") or item.get("message") or ""))
        message = event.get("msg")
        if isinstance(message, dict) and message.get("type") == "agent_message":
            texts.append(str(message.get("message") or ""))
    return {
        "text": "\n".join(text for text in texts if text),
        "session_id": thread_id,
        "completed": completed,
    }


class CodexAdapter(SubscriptionAdapter):
    """Run one stage on the owner's ChatGPT subscription (never API keys)."""

    account_class = "chatgpt"

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Ready only when ``codex login status`` reports the ChatGPT plan."""
        try:
            evidence = run_cli(
                ["codex", "login", "status"],
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
        # The npm shim prints this status on stderr while stdout stays empty.
        haystack = f"{evidence.stdout or ''}\n{evidence.stderr or ''}".lower()
        if evidence.returncode == 0 and "chatgpt" in haystack:
            return AdapterHealth(
                ready=True,
                account_class=self.account_class,
            )
        return AdapterHealth(
            ready=False,
            account_class=self.account_class,
            failure=FailureKind.AUTH,
            detail="no ChatGPT subscription login",
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        command = [
            "codex",
            "exec",
            "-",
            "--json",
            "--sandbox",
            "workspace-write",
            # Stage workspaces are validated absolute directories already;
            # skipping the git-repo convenience check keeps non-git
            # workspaces usable without weakening the sandbox.
            "--skip-git-repo-check",
            "--cd",
            str(request.workspace),
            "--model",
            route.model,
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

        parsed = parse_codex_jsonl(evidence.stdout)
        if evidence.returncode == 0 and (parsed["completed"] or parsed["text"]):
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
