"""Typed contracts shared by the subscription router and its adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal


class FailureKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    TRANSPORT = "transport"
    INVALID_REQUEST = "invalid_request"
    TASK_FAILURE = "task_failure"


@dataclass(frozen=True)
class RouteSpec:
    """One compiled Primary-to-F7 table position."""

    slot: int
    label: str
    adapter: str
    model: str
    billing_mode: Literal["subscription", "api"]


@dataclass(frozen=True)
class InvocationRequest:
    """A bounded, mutation-aware assignment for one route attempt."""

    workflow_id: str
    stage_id: str
    role: str
    prompt: str
    workspace: Path
    start_revision: str
    current_revision: str
    mode: Literal["start", "continue"] = "start"
    changed_files: tuple[str, ...] = ()
    max_steps: int = 40
    # In-process authorization capability, never included in evidence or repr.
    # Raise to revoke; legacy direct router callers need no workflow callback.
    pre_invoke: Callable[[], None] | None = field(
        default=None, repr=False, compare=False
    )
    result_transform: Callable[[InvocationResult], InvocationResult] | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 200:
            raise ValueError("max_steps must be an integer between 1 and 200")


@dataclass(frozen=True)
class InvocationResult:
    """Normalized vendor evidence; never carries credentials."""

    success: bool
    text: str
    adapter: str
    model: str
    billing_mode: Literal["subscription", "api"]
    session_id: str = ""
    failure: FailureKind | None = None


@dataclass(frozen=True)
class AdapterHealth:
    """Non-generative readiness evidence for one adapter."""

    ready: bool
    account_class: str
    failure: FailureKind | None = None
    detail: str = ""
