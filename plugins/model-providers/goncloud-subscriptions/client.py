"""OpenAI-compatible facade over the profile-local subscription router."""

from __future__ import annotations

import hashlib
import importlib
import itertools
import json
import re
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

from agent.acp_openai_bridge import (
    completion_to_stream_chunks,
    extract_tool_calls_from_text,
    render_tool_bridge_sections,
)
from hermes_constants import get_hermes_home

_PROVIDER = "goncloud-subscriptions"


def _as_text_contract(message: dict[str, Any]) -> dict[str, Any]:
    """Show a past assistant tool call the way the contract asks for new ones.

    A history of native ``tool_calls`` taught the model to call natively, which
    the tool-less CLI rejects (live 2026-09-27).
    """
    calls = message.get("tool_calls")
    if message.get("role") != "assistant" or not calls:
        return message
    blocks = [str(message.get("content") or "").strip()]
    blocks += [
        f"<tool_call>{json.dumps(call, ensure_ascii=False)}</tool_call>"
        for call in calls
    ]
    rendered = {key: value for key, value in message.items() if key != "tool_calls"}
    rendered["content"] = "\n".join(block for block in blocks if block)
    return rendered


_CALLS_WRAPPER = re.compile(r"</?(?:\w+:)?function_calls>")
_INVOKE = re.compile(
    r'<(?:\w+:)?invoke name="([^"]+)">(.*?)</(?:\w+:)?invoke>', re.DOTALL
)
_PARAMETER = re.compile(
    r'<(?:\w+:)?parameter name="([^"]+)">(.*?)</(?:\w+:)?parameter>', re.DOTALL
)


def _parameter_types(tools: list[dict[str, Any]] | None) -> dict[str, dict]:
    types: dict[str, dict] = {}
    for tool in tools or []:
        function = tool.get("function") or {}
        properties = (function.get("parameters") or {}).get("properties") or {}
        types[str(function.get("name"))] = {
            name: (spec or {}).get("type") for name, spec in properties.items()
        }
    return types


def _invoke_markup_as_tool_calls(text: str, tools: list[dict[str, Any]] | None) -> str:
    """Rewrite Claude's ``<invoke name=...>`` markup into the text contract.

    Claude sometimes answers in its own tool markup; Hermes parsed no call and
    the turn stopped at a promise (live, 2026-09-27).
    """
    if "invoke name=" not in text:
        return text
    types = _parameter_types(tools)
    ordinal = itertools.count(1)

    def to_call(match: re.Match) -> str:
        name = match.group(1)
        arguments = {}
        for key, raw in _PARAMETER.findall(match.group(2)):
            value = raw.removeprefix("\n").removesuffix("\n")
            if types.get(name, {}).get(key) != "string":
                try:
                    value = json.loads(value)
                except json.JSONDecodeError:
                    pass
            arguments[key] = value
        call = {
            "id": f"invoke_{next(ordinal)}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }
        return f"<tool_call>{json.dumps(call, ensure_ascii=False)}</tool_call>"

    return _CALLS_WRAPPER.sub("", _INVOKE.sub(to_call, text)).strip()


def _contained(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(parent.resolve()):
        raise RuntimeError("Goncloud provider path escapes the active profile")
    return resolved


def _load_profile_runtime(home: Path | None = None):
    """Load only the active profile's sibling team package and routing bytes."""
    profile_home = Path(home or get_hermes_home()).resolve()
    plugins_root = _contained(profile_home / "plugins", profile_home)
    expected_provider_root = _contained(
        plugins_root / "model-providers" / "goncloud-subscriptions",
        plugins_root,
    )
    if Path(__file__).resolve().parent != expected_provider_root:
        raise RuntimeError(
            "Goncloud provider is not the active profile's deployed copy"
        )
    team_root = _contained(plugins_root / "goncloud-team", plugins_root)
    if not team_root.is_dir():
        raise RuntimeError("active profile has no Goncloud team plugin")
    routing_path = _contained(team_root / "model-routing.source.json", team_root)
    try:
        routing_bytes = routing_path.read_bytes()
        routing_source = json.loads(routing_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError(
            "active profile has no valid byte-identical routing source"
        ) from error
    if not isinstance(routing_source, dict):
        raise RuntimeError("active profile routing source must be an object")

    identity = hashlib.sha256(str(profile_home).casefold().encode()).hexdigest()[:16]
    namespace = f"_goncloud_profile_{identity}"
    root_module = sys.modules.get(namespace)
    if root_module is None:
        root_module = ModuleType(namespace)
        root_module.__package__ = namespace
        root_module.__path__ = [str(team_root)]
        sys.modules[namespace] = root_module
    else:
        roots = [Path(item).resolve() for item in getattr(root_module, "__path__", ())]
        if roots != [team_root]:
            raise RuntimeError(
                "profile package namespace points outside active profile"
            )

    conversation_module = importlib.import_module(
        f"{namespace}.subscriptions.conversation"
    )
    router_module = importlib.import_module(f"{namespace}.subscriptions.router")
    catalog_module = importlib.import_module(f"{namespace}.subscriptions.catalog")
    loaded_source = _contained(
        Path(catalog_module.__file__).resolve().parents[1]
        / "model-routing.source.json",
        team_root,
    )
    if loaded_source != routing_path or loaded_source.read_bytes() != routing_bytes:
        raise RuntimeError("loaded routing source is not the active profile copy")

    subscription_router = router_module.SubscriptionRouter(
        routing_source=routing_source
    )
    return conversation_module.ConversationRouter(subscription_router)


def _profile_workspace(home: Path | None = None) -> Path:
    profile_home = Path(home or get_hermes_home()).resolve()
    try:
        import yaml

        configuration = yaml.safe_load(
            (profile_home / "config.yaml").read_text(encoding="utf-8")
        )
        cwd = configuration["terminal"]["cwd"]
    except (OSError, TypeError, KeyError, yaml.YAMLError) as error:
        raise RuntimeError("active profile has no terminal workspace") from error
    if not isinstance(cwd, str):
        raise RuntimeError("active profile terminal workspace must be text")
    workspace = Path(cwd).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError("active profile terminal workspace does not exist")
    return workspace


class GoncloudSubscriptionClient:
    """Minimal OpenAI client facade that never spawns the resolver command."""

    HERMES_SKIP_TRANSPORT_WRAP = True
    HERMES_SKIP_ASYNC_WRAP = True

    def __init__(
        self,
        *,
        conversation_router=None,
        workspace: Path | str | None = None,
        **_: Any,
    ) -> None:
        self._conversation_router = conversation_router or _load_profile_runtime()
        self._workspace = (
            Path(workspace).resolve() if workspace is not None else _profile_workspace()
        )
        if not self._workspace.is_dir():
            raise RuntimeError("Goncloud conversation workspace does not exist")
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_chat_completion)
        )
        self.is_closed = False

    def close(self) -> None:
        self.is_closed = True

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        **_: Any,
    ) -> Any:
        requested_model = model or "main"
        if requested_model != "main":
            raise ValueError("goncloud-subscriptions exposes only the main row")
        bridged_messages = [_as_text_contract(message) for message in messages or []]
        bridge_sections = render_tool_bridge_sections(tools, tool_choice)
        if bridge_sections:
            bridged_messages.insert(
                0,
                {"role": "developer", "content": "\n\n".join(bridge_sections)},
            )
        # The developer section already carries the schemas and the choice;
        # passing them on as well rendered all of them a second time (TOOLS=).
        result = self._conversation_router.complete(
            "main",
            bridged_messages,
            None,
            None,
            self._workspace,
        )
        if not result.success:
            raise RuntimeError(result.text)

        tool_calls, cleaned_text = extract_tool_calls_from_text(
            _invoke_markup_as_tool_calls(result.text, tools)
        )
        message = SimpleNamespace(
            role="assistant",
            content=cleaned_text,
            tool_calls=tool_calls,
            reasoning=None,
            reasoning_content=None,
            reasoning_details=None,
        )
        completion = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    index=0,
                    message=message,
                    finish_reason="tool_calls" if tool_calls else "stop",
                )
            ],
            usage=SimpleNamespace(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                prompt_tokens_details=SimpleNamespace(cached_tokens=0),
            ),
            model=result.model or requested_model,
            hermes_provider=_PROVIDER,
            hermes_requested_model=requested_model,
            hermes_route_slot=result.slot,
            hermes_route_label=result.route_label,
            hermes_reasoning_effort=result.reasoning_effort,
            hermes_billing_mode=result.billing_mode,
            hermes_session_id=result.session_id,
            hermes_skipped_routes=[
                {
                    "slot": skipped.slot,
                    "adapter": skipped.adapter,
                    "failure": skipped.failure.value,
                }
                for skipped in result.skipped_routes
            ],
        )
        return completion_to_stream_chunks(completion) if stream else completion


__all__ = ["GoncloudSubscriptionClient"]
