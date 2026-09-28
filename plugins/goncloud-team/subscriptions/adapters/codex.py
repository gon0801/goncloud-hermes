"""ChatGPT Codex adapter running inside the Codex workspace sandbox."""

from __future__ import annotations

import json
import re
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

#: Codex reports this as item.type=error, then completes the turn. It is a
#: context-budget warning, not a failed terminal.
_SKILLS_BUDGET_WARNING = re.compile(
    r"skill descriptions were shortened|skills context budget",
    re.IGNORECASE,
)


def parse_codex_jsonl(output: str) -> dict:
    """Extract agent text and thread identity from ``codex exec --json``."""
    texts: list[str] = []
    thread_id = None
    completed = False
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
            for key in ("type", "thread_id", "session_id")
        ):
            invalid = True
            continue
        thread_id = event.get("thread_id") or event.get("session_id") or thread_id
        event_type = str(event.get("type") or "")
        if event_type in {"error", "turn.failed", "task_failed"} or event.get("error"):
            invalid = True
        if event_type == "turn.completed":
            invalid = invalid or completed
            completed = True
        item = event.get("item")
        if isinstance(item, dict):
            kind = item.get("type") or item.get("item_type")
            if not isinstance(kind, str):
                invalid = True
                continue
            if kind == "error" or item.get("error"):
                message = (
                    item.get("message") if isinstance(item.get("message"), str) else ""
                )
                if not _SKILLS_BUDGET_WARNING.search(message):
                    invalid = True
            if kind in {"agent_message", "message"}:
                text = item.get("text") or item.get("message") or ""
                if not isinstance(text, str) or completed:
                    invalid = True
                else:
                    texts.append(text)
        message = event.get("msg")
        if isinstance(message, dict) and not isinstance(message.get("type"), str):
            invalid = True
            continue
        if isinstance(message, dict) and message.get("type") == "agent_message":
            text = message.get("message") or ""
            if not isinstance(text, str) or completed:
                invalid = True
            else:
                texts.append(text)
        if isinstance(message, dict) and message.get("type") in {
            "error",
            "turn.failed",
        }:
            invalid = True
    return {
        "text": "\n".join(text for text in texts if text),
        "session_id": thread_id,
        "completed": completed,
        "terminal_valid": completed and not invalid,
    }


#: Conversation turns: Hermes owns the tools (see claude.py). Codex has no
#: "no tools" switch; without the instruction it answers that a listed Hermes
#: tool "is not enabled" instead of emitting <tool_call> (measured, 0.157.1).
_TOOL_BRIDGE_ARGS = (
    "--ignore-user-config",
    *(
        arg
        for feature in (
            "shell_tool",
            "unified_exec",
            "apps",
            "browser_use",
            "browser_use_external",
            "computer_use",
            "plugins",
            "multi_agent",
            "image_generation",
            "skill_search",
            "tool_suggest",
            "goals",
            "sleep_tool",
            "view_image",
            "in_app_browser",
            "hooks",
        )
        for arg in ("--disable", feature)
    ),
    "-c",
    "web_search='disabled'",
    "-c",
    # npm's codex is a .cmd shim run through cmd.exe: no < > or double quotes
    # here, or cmd reads "<tool_call" as an input redirect and the launch dies.
    "developer_instructions='You are the text-only model behind a tool bridge. "
    "You have NO native tools, skills or MCP servers of your own. The ONLY tools "
    "that exist are the ones listed in the user prompt as an OpenAI function "
    "schema; to call one, reply with ONLY the tool_call block exactly as the "
    "prompt specifies, and the host will run it and send back the result. "
    "Never claim a listed tool is unavailable.'",
)


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
            return health_for_process_error(self.account_class, error)
        # The npm shim prints this status on stderr while stdout stays empty.
        haystack = f"{evidence.stream or ''}\n{evidence.stderr or ''}".lower()
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
            "read-only" if request.tool_bridge else "workspace-write",
            *(_TOOL_BRIDGE_ARGS if request.tool_bridge else ()),
            # Stage workspaces are validated absolute directories already;
            # skipping the git-repo convenience check keeps non-git
            # workspaces usable without weakening the sandbox.
            "--skip-git-repo-check",
            "--cd",
            str(request.workspace),
            "--model",
            route.model,
            *(
                ["-c", f'model_reasoning_effort="{route.reasoning_effort}"']
                if route.reasoning_effort is not None
                else []
            ),
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
            return self._failed_result(route, error.failure, str(error))

        parsed = parse_codex_jsonl(evidence.stream)
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
            else classify_failure(evidence.returncode, evidence.stream, evidence.stderr)
        )
        return self._failed_result(route, failure)
