"""Compile the owner's slot table into typed subscription routes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .types import RouteSpec

#: Only the primary position is bound by slot. Later positions bind by family.
_SLOT_FORCED_ADAPTERS = {0: "opencode_go"}

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

SLOT_COUNT = 7
SLOT_RANGE_LABEL = "Primary..F6"

#: Effort or plan markers on the source label, never part of the vendor model id.
_TRAILING_ANNOTATION = re.compile(r"\s+\([^)]+\)\s*$")


def _family_adapter(model: str) -> str:
    for prefix, adapter in _FAMILY_ADAPTERS:
        if model.startswith(prefix):
            return adapter
    raise ValueError(f"unknown model family: {model}")


def _runtime_model(raw_model: str) -> str:
    model = raw_model.removesuffix(" dup")
    return _TRAILING_ANNOTATION.sub("", model)


def _compile_route(slot: int, label: str) -> RouteSpec:
    try:
        _prefix, raw_model = label.split("/", 1)
    except ValueError as error:
        raise ValueError(f"route label must be provider/model: {label}") from error
    # ``dup`` and trailing ``(max)`` / ``(xhigh)`` are provenance on the label.
    model = _runtime_model(raw_model)
    adapter = _SLOT_FORCED_ADAPTERS.get(slot) or _family_adapter(model)
    if adapter == "muse":
        # ``-free`` names OpenCode Zen's free tier, where it is a real model id.
        # The Muse subscription serves the same family without it and refuses
        # the suffixed id, so on this binding the suffix is provenance on the
        # label, like ``dup``, not part of the model id.
        model = model.removesuffix("-free")
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
    """Compile one agent's exact seven-position chain from the source table."""
    source_role = "main" if profile in {"claw", "entrega"} else profile
    chain = source.get("chains", {}).get(source_role)
    if chain is None:
        raise ValueError(f"No model chain configured for profile: {profile}")
    if len(chain) != SLOT_COUNT:
        raise ValueError(
            f"profile {profile} must define exactly seven slots "
            f"({SLOT_RANGE_LABEL}); found {len(chain)}"
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
