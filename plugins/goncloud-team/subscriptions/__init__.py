"""Plugin-local subscription router for the owner's per-slot vendor bindings."""

import os
from pathlib import Path

from .catalog import compile_profile_routes
from .types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)


def _load_private_env() -> None:
    """Seed the process with the private store; never override real env."""
    candidates = []
    configured = os.environ.get("GONCLOUD_SUBSCRIPTION_ENV")
    if configured:
        candidates.append(Path(configured))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "hermes" / "subscription-router.env")
    for candidate in candidates:
        if not candidate.is_file():
            continue
        for raw in candidate.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value:
                os.environ.setdefault(key, value)
        break


_load_private_env()

__all__ = [
    "AdapterHealth",
    "FailureKind",
    "InvocationRequest",
    "InvocationResult",
    "RouteSpec",
    "compile_profile_routes",
]
