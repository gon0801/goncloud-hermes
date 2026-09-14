"""Compile the owner's slot table into typed subscription routes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from .types import RouteSpec

#: Position, not the historical provider prefix, binds the first two slots.
_SLOT_FORCED_ADAPTERS = {0: "opencode_go", 1: "opencode_zen"}

#: Later slots bind by model family regardless of the recorded prefix.
_FAMILY_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("muse-", "muse"),
    ("claude-", "claude"),
    ("gpt-", "codex"),
    ("k3", "kimi"),
    ("glm-", "zai"),
    ("grok-", "grok"),
    ("deepseek-", "deepseek"),
)

_SLOT_COUNT = 8


def _family_adapter(model: str) -> str:
    for prefix, adapter in _FAMILY_ADAPTERS:
        if model.startswith(prefix):
            return adapter
    raise ValueError(f"unknown model family: {model}")


def _compile_route(slot: int, label: str) -> RouteSpec:
    try:
        _prefix, raw_model = label.split("/", 1)
    except ValueError as error:
        raise ValueError(f"route label must be provider/model: {label}") from error
    # ``dup`` is provenance metadata on the label, never part of the model id.
    model = raw_model.removesuffix(" dup")
    adapter = _SLOT_FORCED_ADAPTERS.get(slot) or _family_adapter(model)
    billing_mode = "api" if adapter == "deepseek" else "subscription"
    return RouteSpec(
        slot=slot,
        label=label,
        adapter=adapter,
        model=model,
        billing_mode=billing_mode,
    )


def compile_profile_routes(
    profile: str, source: Mapping[str, Any]
) -> tuple[RouteSpec, ...]:
    """Compile one agent's exact eight-position chain from the source table."""
    source_role = "main" if profile in {"claw", "entrega"} else profile
    chain = source.get("chains", {}).get(source_role)
    if chain is None:
        raise ValueError(f"No model chain configured for profile: {profile}")
    if len(chain) != _SLOT_COUNT:
        raise ValueError(
            f"profile {profile} must define exactly eight slots "
            f"(Primary..F7); found {len(chain)}"
        )
    return tuple(_compile_route(slot, label) for slot, label in enumerate(chain))


def load_routing_source() -> dict:
    """Locate the routing table: env override, plugin copy, then repo root."""
    here = Path(__file__).resolve()
    candidates = []
    configured = os.environ.get("GONCLOUD_ROUTING_SOURCE")
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            here.parent.parent / "model-routing.source.json",
            here.parents[3] / "model-routing.source.json",
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(
        "model-routing.source.json not found; set GONCLOUD_ROUTING_SOURCE"
    )
