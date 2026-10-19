"""Claude Max subscription adapter through the native Claude CLI login."""

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

#: Autonomous session mode that still keeps the CLI's sandbox intact. dontAsk
#: auto-denies every call that would otherwise prompt (Claude Code permission
#: modes), so a role's tools must be pre-approved with --allowedTools or no
#: stage could ever edit a file or run a command.
_PERMISSION_MODE = "dontAsk"

#: Roles that mutate the workspace: the implementer (initial and corrective
#: runs; the corrective loop in CORRECTIVE_STAGES, workflow.py, writes only
#: through its implementer turn) and the delivery stage. Every other role
#: reads and runs tests; local_orchestrator's _READ_ONLY_STAGES guard is the
#: second line against a read-only role that mutates the workspace anyway.
_WRITING_ROLES = frozenset({"implementer", "entrega"})

#: Bare tool names in Claude Code permission-rule syntax allow every use of
#: the tool. One comma-joined argv value: the flag also accepts space-
#: separated names, but as a variadic option a single argument cannot swallow
#: the flags that follow it.
_WRITING_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "Bash")
_READ_ONLY_TOOLS = ("Read", "Glob", "Grep", "Bash")


def allowed_tools_for(role: str) -> str:
    """The narrowest --allowedTools list that lets one stage role work."""
    tools = _WRITING_TOOLS if role in _WRITING_ROLES else _READ_ONLY_TOOLS
    return ",".join(tools)


_SUCCESS_SUBTYPES = {"success", "success_streamcomplete", "streamcomplete"}


def parse_claude_stream(output: str) -> dict:
    """Extract terminal text and session identity from stream-json events."""
    assistant_text: list[str] = []
    session_id = None
    result_text = None
    subtype = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        session_id = event.get("session_id") or session_id
        if event_type == "assistant":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if isinstance(block, dict) and block.get("type") == "text":
                    assistant_text.append(str(block.get("text") or ""))
        if event_type == "result":
            result_text = event.get("result")
            subtype = event.get("subtype")
    text = result_text if result_text is not None else "".join(assistant_text)
    return {"text": str(text or ""), "session_id": session_id, "subtype": subtype}


class ClaudeAdapter(SubscriptionAdapter):
    """Run one stage on the owner's Claude Max subscription (never API keys)."""

    account_class = "claude_max"

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Ready only for a claude.ai OAuth login, not an API-key session."""
        try:
            evidence = run_cli(
                ["claude", "auth", "status"],
                "",
                Path.cwd(),
                self.probe_timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return health_for_process_error(self.account_class, error)
        haystack = (evidence.stream or "").lower()
        if evidence.returncode == 0 and (
            ("claude.ai" in haystack or '"max"' in haystack)
            or self._oauth_subscription(evidence.stream)
        ):
            return AdapterHealth(
                ready=True,
                account_class=self.account_class,
                detail="subscription login present; tier proven by live inference",
            )
        return AdapterHealth(
            ready=False,
            account_class=self.account_class,
            failure=FailureKind.AUTH,
            detail="no claude.ai subscription login",
        )

    @staticmethod
    def _oauth_subscription(stdout: str) -> bool:
        try:
            status = json.loads(stdout)
        except json.JSONDecodeError:
            return False
        if not isinstance(status, dict) or not status.get("loggedIn"):
            return False
        return status.get("authMethod") == "oauth_token" and status.get(
            "apiProvider"
        ) in {"firstParty", "claude.ai"}

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        command = [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--permission-mode",
            _PERMISSION_MODE,
            "--allowedTools",
            allowed_tools_for(request.role),
            "--model",
            route.model,
            *(
                ["--effort", route.reasoning_effort]
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

        parsed = parse_claude_stream(evidence.stream)
        if evidence.returncode == 0 and parsed["subtype"] in _SUCCESS_SUBTYPES:
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        failure = classify_failure(
            evidence.returncode, evidence.stream, evidence.stderr
        )
        return self._failed_result(
            route, failure, evidence=(evidence.stderr, evidence.stream)
        )
