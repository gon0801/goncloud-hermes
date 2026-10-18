"""Compile the owner's slot table into typed subscription routes."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from .types import RouteSpec

#: Every position binds by model family (v6 table, 2026-10-17: four positions
#: of primary + 3 fallbacks, Anthropic on the owner's Claude subscription).
#: Each family is a pattern matched at the start of the id: Kimi's own releases
#: are ``k<digits>`` (k3, k4, k3-256k), so a literal "k3" would drop k4.
#: DeepSeek Flash rides the owner's OpenCode Go subscription; the pay-per-token
#: ``deepseek`` adapter is deliberately unbound (owner, 2026-10-17).
_FAMILY_ADAPTERS: tuple[tuple[str, str], ...] = (
    ("muse-", "muse"),
    ("claude-", "claude"),
    ("gpt-", "codex"),
    (r"k\d", "kimi"),
    ("kimi-", "kimi"),
    # Served through the OpenCode CLI so GLM gets workspace tools; the plain
    # HTTP zai adapter has none. Same Coding Plan subscription either way.
    ("glm-", "opencode_zai"),
    ("grok-", "grok"),
    ("deepseek-", "opencode_go"),
)

SLOT_COUNT = 4
SLOT_RANGE_LABEL = "Primary..F3"

#: Effort markers on the source label, never part of the vendor model id.
#: ``(off)`` means the position runs with no reasoning parameter at all.
_TRAILING_ANNOTATION = re.compile(r"\s+\(([^)]+)\)\s*$")
_REASONING_EFFORTS = frozenset({"high", "xhigh", "max", "medium", "low", "off"})


def _family_adapter(model: str) -> str:
    for family, adapter in _FAMILY_ADAPTERS:
        if re.match(family, model):
            return adapter
    raise ValueError(f"unknown model family: {model}")


def _runtime_model_and_effort(raw_model: str) -> tuple[str, str | None]:
    model = raw_model.removesuffix(" dup")
    annotation = _TRAILING_ANNOTATION.search(model)
    if annotation is None:
        return model, None
    effort = annotation.group(1)
    if effort not in _REASONING_EFFORTS:
        raise ValueError(f"unknown reasoning effort: {effort}")
    return model[: annotation.start()], None if effort == "off" else effort


def _compile_route(slot: int, label: str) -> RouteSpec:
    try:
        _prefix, raw_model = label.split("/", 1)
    except ValueError as error:
        raise ValueError(f"route label must be provider/model: {label}") from error
    # ``dup`` is provenance; effort is both provenance and runtime policy.
    model, reasoning_effort = _runtime_model_and_effort(raw_model)
    adapter = _family_adapter(model)
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
        reasoning_effort=reasoning_effort,
    )


def _source_role(profile: str, source: Mapping[str, Any]) -> str:
    """The chain row a profile runs: its lane, named by ``profile_inheritance``."""
    entry = source.get("profile_inheritance", {}).get(profile)
    if entry:
        return entry
    if profile in source.get("chains", {}):
        return profile
    owned = next(
        (row for row, who in source.get("profile_names", {}).items() if who == profile),
        None,
    )
    if owned:
        return owned
    raise ValueError(f"No model chain configured for profile: {profile}")


def compile_profile_routes(
    profile: str, source: Mapping[str, Any]
) -> tuple[RouteSpec, ...]:
    """Compile one agent's exact four-position chain from the source table."""
    chain = source.get("chains", {}).get(_source_role(profile, source))
    if chain is None:
        raise ValueError(f"No model chain configured for profile: {profile}")
    if len(chain) != SLOT_COUNT:
        raise ValueError(
            f"profile {profile} must define exactly {SLOT_COUNT} slots "
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
