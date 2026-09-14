"""Typed contracts shared by the subscription router and its adapters."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal


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
