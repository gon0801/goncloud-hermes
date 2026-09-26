"""Vendor adapter contracts and shared failure classification."""

from __future__ import annotations

import re
import subprocess

from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)

_AUTH_PATTERN = re.compile(
    r"unauthorized|not authenticated|authentication|credential|"
    r"api[ _-]?key|not logged in|login required|sign in required|"
    r"forbidden|\bexpired\b|signed out|invalid token|"
    r"token (is )?(invalid|expired|missing)",
    re.IGNORECASE,
)
_QUOTA_PATTERN = re.compile(
    r"\bquota\b|\bbalance\b|insufficient|rate[ _-]?limit|usage[ _-]?limit|"
    r"\bexceeded\b|out of credit",
    re.IGNORECASE,
)
_MODEL_PATTERN = re.compile(
    r"model (is )?not|unknown model|no such model|not available|"
    r"does not exist|deprecat",
    re.IGNORECASE,
)
#: A model the plan does not enable is the vendor answering about the model,
#: not the account. opencode reports it as {"message":"Model is disabled",
#: "statusCode":401}; the 401 must not be read as an account auth failure.
_MODEL_LEVEL_PATTERN = re.compile(
    r"model (is )?disabled|model (is )?not enabled|"
    r"model (is )?not allowed|model not permitted",
    re.IGNORECASE,
)
_INVALID_PATTERN = re.compile(
    r"invalid request|bad request|malformed|schema", re.IGNORECASE
)
_TRANSPORT_PATTERN = re.compile(
    r"connection[ _-]?error|connection reset|reset by peer|"
    r"disconnected[ _-]+before[ _-]+response|"
    r"dns(?: lookup| resolution)? (?:failure|failed|error)|name or service not known|"
    r"getaddrinfo|connection refused|econnreset|econnrefused|enotfound|"
    r"network is unreachable|socket hang up",
    re.IGNORECASE,
)

#: Every vendor process shares this hard ceiling, probes included.
_ABSOLUTE_DEADLINE_SECONDS = 300

_STATUS_CODES = {
    "401": FailureKind.AUTH,
    "403": FailureKind.AUTH,
    "429": FailureKind.QUOTA,
    "404": FailureKind.MODEL_UNAVAILABLE,
    "400": FailureKind.INVALID_REQUEST,
}

#: A bare HTTP status number in model output — a CSS width, an item count, a
#: "404 page" — is not a vendor failure. Trust a status code only next to an
#: HTTP cue ("status code 401", "http 429", "returned 404") or its canonical
#: reason phrase ("401 unauthorized"). Matching bare numbers anywhere let one
#: normal sentence poison the router health cache (2026-09-15).
_STATUS_CUE = (
    r"(?:https?|status(?:[ _-]?code)?|code|error|response|responded|"
    r"returned|failed(?:\s+with)?)"
)
_STATUS_REASON = (
    r"(?:unauthorized|forbidden|payment required|too many requests|"
    r"not found|bad request)"
)
_STATUS_CODE_PATTERN = re.compile(
    rf"{_STATUS_CUE}[^0-9a-z\n]{{0,16}}(40[0134]|429)\b"
    rf"|\b(40[0134]|429)\b[^0-9a-z\n]{{0,4}}{_STATUS_REASON}",
    re.IGNORECASE,
)


def _classify_channel(haystack: str, *, bare_codes: bool):
    """Classify one output channel, or None. ``bare_codes`` trusts a status
    number anywhere (the vendor's stderr); otherwise a number counts only in an
    HTTP context (the model's stdout, where a width or a count is not a code)."""
    # A disabled/unenabled model is a model-level answer; it outranks the
    # status code so its 401 is not misread as an account auth failure.
    if _MODEL_LEVEL_PATTERN.search(haystack):
        return FailureKind.MODEL_UNAVAILABLE
    # A plan usage limit wrapped as HTTP 403 / provider.auth_error is quota,
    # not a dead credential. kimi 0.34 reports weekly limits that way.
    if _QUOTA_PATTERN.search(haystack):
        return FailureKind.QUOTA
    if bare_codes:
        for code, failure in _STATUS_CODES.items():
            if re.search(rf"\b{code}\b", haystack):
                return failure
    else:
        status = _STATUS_CODE_PATTERN.search(haystack)
        if status:
            return _STATUS_CODES[status.group(1) or status.group(2)]
    if _TRANSPORT_PATTERN.search(haystack):
        return FailureKind.TRANSPORT
    if _AUTH_PATTERN.search(haystack):
        return FailureKind.AUTH
    if _QUOTA_PATTERN.search(haystack):
        return FailureKind.QUOTA
    if _MODEL_PATTERN.search(haystack):
        return FailureKind.MODEL_UNAVAILABLE
    if _INVALID_PATTERN.search(haystack):
        return FailureKind.INVALID_REQUEST
    return None


def classify_failure(returncode: int, stdout: str, stderr: str) -> FailureKind:
    """Best-effort vendor failure classification from redacted evidence.

    stderr is the vendor's own error channel, so a bare status number there is
    a real signal; stdout carries the model's generated text, where a bare
    number is a width, a count or a "404 page", never a code. Reading stderr
    first, then stdout under the stricter rule, stops one normal sentence from
    faking an auth/quota/model failure (2026-09-15)."""
    for channel, bare_codes in ((stderr, True), (stdout, False)):
        kind = _classify_channel(channel.lower(), bare_codes=bare_codes)
        if kind is not None:
            return kind
    return FailureKind.TASK_FAILURE


def health_for_process_error(account_class: str, error: Exception) -> AdapterHealth:
    """Report a probe the vendor never answered with the error's own kind.

    A probe that hit its deadline is recoverable and must advance the slot;
    reporting it as a task failure blocks the stage on a slow launch instead.
    """
    return AdapterHealth(
        ready=False,
        account_class=account_class,
        failure=getattr(error, "failure", None) or FailureKind.TASK_FAILURE,
        detail=str(error),
    )


def build_assignment_prompt(request: InvocationRequest) -> str:
    """Compose the bounded assignment, including continuation state."""
    lines = [
        f"Goncloud stage {request.stage_id} of workflow {request.workflow_id}.",
        f"Role: {request.role}.",
        f"Maximum autonomous steps: {request.max_steps}",
        f"Workspace: {request.workspace} "
        f"(owner-approved baseline revision {request.start_revision}).",
    ]
    if request.mode == "continue":
        lines.append(
            "CONTINUATION: a previous subscription worker already mutated this "
            "workspace before failing. Continue from the recorded state; do not "
            "redo completed work and do not replay the original task blindly."
        )
        lines.append(f"Current recorded revision: {request.current_revision}.")
        if request.changed_files:
            lines.append("Files already changed by the previous attempt:")
            lines.extend(f"- {name}" for name in request.changed_files)
    lines.append("")
    lines.append(request.prompt)
    return "\n".join(lines)


class SubscriptionAdapter:
    """Common shape for one vendor binding; subclasses fill the CLI details."""

    name: str = ""
    billing_mode = "subscription"
    # An adapter may raise its own ceilings up to the absolute process deadline
    # when its launch path genuinely needs longer (a WSL cold start outruns the
    # default probe window); clamping a declared window to the default instead
    # reports a healthy account as a failed one.
    max_timeout_seconds = _ABSOLUTE_DEADLINE_SECONDS
    max_probe_timeout_seconds = 30
    timeout_seconds = 300
    probe_timeout_seconds = 30
    probe_is_generative = False

    def __init__(self, runner=None) -> None:
        self.runner = runner if runner is not None else subprocess.run
        # Class-declared values bypass __setattr__, so force them through it.
        self.max_timeout_seconds = self.max_timeout_seconds
        self.max_probe_timeout_seconds = self.max_probe_timeout_seconds
        self.timeout_seconds = self.timeout_seconds
        self.probe_timeout_seconds = self.probe_timeout_seconds

    def __setattr__(self, name, value) -> None:
        if name in {"max_timeout_seconds", "max_probe_timeout_seconds"}:
            value = min(value, _ABSOLUTE_DEADLINE_SECONDS)
        elif name == "timeout_seconds":
            value = min(value, self.max_timeout_seconds)
        elif name == "probe_timeout_seconds":
            value = min(value, self.max_probe_timeout_seconds)
        super().__setattr__(name, value)

    def probe(self, route: RouteSpec) -> AdapterHealth:
        raise NotImplementedError

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        raise NotImplementedError

    def _failed_result(
        self, route: RouteSpec, failure: FailureKind, detail: str = ""
    ) -> InvocationResult:
        text = f"{route.adapter} invocation failed ({failure.value})"
        # Diagnostics are not a public evidence channel, even after redaction.
        return InvocationResult(
            success=False,
            text=text,
            adapter=route.adapter,
            model=route.model,
            billing_mode=route.billing_mode,
            failure=failure,
        )


__all__ = [
    "SubscriptionAdapter",
    "AdapterHealth",
    "FailureKind",
    "InvocationRequest",
    "InvocationResult",
    "RouteSpec",
    "build_adapter_registry",
    "build_assignment_prompt",
    "classify_failure",
    "health_for_process_error",
]


def build_adapter_registry(
    *,
    runner=None,
    zai_auth_token: str | None = None,
    zai_base_url: str | None = None,
    deepseek_api_key: str | None = None,
    deepseek_base_url: str | None = None,
) -> dict:
    """One adapter instance per vendor binding, keyed by adapter id."""
    from .claude import ClaudeAdapter
    from .codex import CodexAdapter
    from .deepseek import DeepSeekAdapter
    from .grok import GrokAdapter
    from .kimi import KimiAdapter
    from .muse import MuseAdapter
    from .opencode import OpenCodeAdapter
    from .zai import ZAIAdapter

    opencode = OpenCodeAdapter(runner=runner)
    return {
        "opencode_go": opencode,
        "opencode_zai": opencode,
        "opencode_zen": opencode,
        "muse": MuseAdapter(runner=runner),
        "claude": ClaudeAdapter(runner=runner),
        "codex": CodexAdapter(runner=runner),
        "kimi": KimiAdapter(runner=runner),
        "zai": ZAIAdapter(
            runner=runner,
            auth_token=zai_auth_token,
            base_url=zai_base_url,
        ),
        "grok": GrokAdapter(runner=runner),
        "deepseek": DeepSeekAdapter(
            runner=runner,
            api_key=deepseek_api_key,
            base_url=deepseek_base_url,
        ),
    }
