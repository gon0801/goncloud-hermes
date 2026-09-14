"""Plugin-local subscription router for the owner's per-slot vendor bindings."""

from .catalog import compile_profile_routes
from .types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)

__all__ = [
    "AdapterHealth",
    "FailureKind",
    "InvocationRequest",
    "InvocationResult",
    "RouteSpec",
    "compile_profile_routes",
]
