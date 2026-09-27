"""Hermes conversation boundary backed by the exact subscription table."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from .catalog import compile_profile_routes
from .router import SubscriptionRouter
from .types import FailureKind, InvocationRequest, InvocationResult

_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validated_message(message: object) -> dict:
    if not isinstance(message, dict):
        raise ValueError("each conversation message must be an object")
    role = message.get("role")
    if role not in _ROLES:
        raise ValueError("conversation message has an invalid role")
    normalized = json.loads(_json(message))
    tool_calls = normalized.get("tool_calls", [])
    if not isinstance(tool_calls, list):
        raise ValueError("assistant tool_calls must be a list")
    for call in tool_calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ValueError("tool call must contain a function object")
        function = call["function"]
        if not isinstance(function.get("name"), str):
            raise ValueError("tool call function must have a name")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ValueError("tool call arguments must be JSON text")
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError as error:
            raise ValueError("tool call arguments are malformed JSON") from error
        if not isinstance(decoded, dict):
            raise ValueError("tool call arguments must decode to an object")
        function["arguments"] = _json(decoded)
    return normalized


def render_conversation_prompt(
    messages: list[dict], tools: list[dict] | None, tool_choice: object
) -> str:
    """Render OpenAI-style conversation state without changing its order."""
    if not isinstance(messages, list) or not messages:
        raise ValueError("conversation messages must be a non-empty list")
    normalized_messages = [_validated_message(message) for message in messages]
    if tools is not None and (
        not isinstance(tools, list) or any(not isinstance(tool, dict) for tool in tools)
    ):
        raise ValueError("tools must be a list of objects")
    normalized_tools = None if tools is None else json.loads(_json(tools))
    normalized_choice = json.loads(_json(tool_choice))
    lines = [
        "Continue this Hermes conversation. Preserve chronological order.",
        f"TOOLS={_json(normalized_tools)}",
        f"TOOL_CHOICE={_json(normalized_choice)}",
        "MESSAGES_BEGIN",
    ]
    lines.extend(_json(message) for message in normalized_messages)
    lines.extend(
        [
            "MESSAGES_END",
            "Return the next assistant turn. Use only the declared tools and schema.",
        ]
    )
    return "\n".join(lines)


class ConversationRouter:
    """Turn one Hermes completion into a fresh subscription-router stage."""

    def __init__(self, subscription_router: SubscriptionRouter | None = None) -> None:
        self.subscription_router = subscription_router or SubscriptionRouter()

    @staticmethod
    def _invalid_request() -> InvocationResult:
        return InvocationResult(
            success=False,
            text="conversation request is invalid",
            adapter="router",
            model="",
            billing_mode="subscription",
            failure=FailureKind.INVALID_REQUEST,
        )

    def complete(
        self,
        profile: str,
        messages: list[dict],
        tools: list[dict] | None,
        tool_choice: object,
        workspace: Path,
    ) -> InvocationResult:
        try:
            prompt = render_conversation_prompt(messages, tools, tool_choice)
            workspace = Path(workspace).resolve()
            if not workspace.is_dir():
                raise ValueError("conversation workspace must be a directory")
            routes = compile_profile_routes(
                profile, self.subscription_router.routing_source
            )
        except (OSError, TypeError, ValueError):
            return self._invalid_request()

        identity = uuid4().hex
        revision = self.subscription_router.git.current_revision(workspace)
        request = InvocationRequest(
            workflow_id=f"conversation-{identity}",
            stage_id=f"turn-{identity}",
            role=profile,
            prompt=prompt,
            workspace=workspace,
            start_revision=revision,
            current_revision=revision,
        )
        return self.subscription_router.run(request, routes)
