"""Vendor adapter contracts and shared failure classification."""

from __future__ import annotations

import re
import subprocess

from ..redaction import redact_text
from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from ._http import failure_for_status

_AUTH_PATTERN = re.compile(
    r"unauthorized|not authenticated|authentication|credential|"
    r"api[ _-]?key|not logged in|login required|sign in required|"
    r"forbidden|\bexpired\b|signed out|invalid token|"
    r"token (is )?(invalid|expired|missing)",
    re.IGNORECASE,
)
#: The plan's window is spent ("The usage limit has been reached", kimi's
#: weekly limit). It outranks throttle wording when a message carries both.
_QUOTA_PATTERN = re.compile(
    r"\bquota\b|\bbalance\b|insufficient|usage[ _-]?limit|out of credit|"
    r"\b(?:daily|weekly|monthly)\b[^.\n]{0,24}\blimit",
    re.IGNORECASE,
)
#: A throttle that names the plan or a reset hours away ("Rate limit exceeded
#: for your plan. Resets at 3:00 PM.") is the window spent in throttle words.
#: It only re-reads a throttle: "resets at midnight" alone is the model's prose,
#: and a reset seconds away is the throttle itself.
_SPENT_WINDOW_PATTERN = re.compile(
    r"\bresets?\s+at\b|"
    r"\bresets?\s+in\b(?!\s+\d)|"
    r"billing (?:period|cycle)|"
    r"for (?:your|this|the) (?:current )?plan|plan[ _-]?limit",
    re.IGNORECASE,
)
#: "resets in N <unit>": short enough for the router to wait is still a throttle.
_RESETS_IN_PATTERN = re.compile(
    r"\bresets?\s+in\s+(\d+(?:\.\d+)?)\s*([a-z]*)", re.IGNORECASE
)
#: The longest wait the router treats as a throttle; the router's
#: _RATE_LIMIT_MAX_WAIT_SECONDS must match (a test keeps them equal).
SHORT_THROTTLE_SECONDS = 120
#: A short throttle: OpenCode Go answers "429 Output token rate limit
#: exceeded. Please retry after a brief wait." The same route works again in
#: seconds, so the router retries it instead of benching the account.
_RATE_LIMIT_PATTERN = re.compile(
    r"rate[ _-]?limit|too many requests|retry[ _-]?after|throttl|"
    r"try again (?:later|in a (?:moment|bit|few))|slow down|overloaded",
    re.IGNORECASE,
)
#: The model's own text (stdout) says "slow down" or "retry-after" about the
#: task; only the vendor's error phrasing counts there, as a bare 429 counts
#: there only in an HTTP context (2026-09-15).
_STDOUT_RATE_LIMIT_PATTERN = re.compile(
    r"rate[ _-]?limit(?:s)?[ _-](?:exceeded|reached)|rate_limit_error|"
    r"too many requests",
    re.IGNORECASE,
)
#: Older wording the router always advanced as quota; kept as a last resort.
_EXCEEDED_PATTERN = re.compile(r"\bexceeded\b", re.IGNORECASE)
_RETRY_AFTER_PATTERN = re.compile(
    r"(?:retry[ _-]?after|try again in)[:\s]+(\d+(?:\.\d+)?)\s*([a-z]*)",
    re.IGNORECASE,
)
#: Seconds per unit the vendor may name; a bare number is seconds, as in the
#: HTTP Retry-After header. Any other word is not a wait we can read.
_RETRY_AFTER_UNITS = {
    **dict.fromkeys(("", "s", "sec", "secs", "second", "seconds"), 1.0),
    **dict.fromkeys(("ms", "millisecond", "milliseconds"), 0.001),
    **dict.fromkeys(("m", "min", "mins", "minute", "minutes"), 60.0),
    **dict.fromkeys(("h", "hr", "hrs", "hour", "hours"), 3600.0),
}
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

#: Health probes keep a hard ceiling: they are reachability/auth checks with a
#: bounded job, and 300 s still covers a cold WSL launch. A stage invocation is
#: NOT a probe — the vendor process runs without any deadline at all (owner,
#: 2026-10-19: no model finishes a large design or implementation inside a
#: fixed window, and Kimi already ran unbounded in the owner's terminal).
_MAX_PROBE_TIMEOUT_SECONDS = 300

#: Per-channel cap on the failure evidence an attempt detail keeps: the LAST
#: characters of stderr and of the stream, where a CLI's closing error lives.
_DETAIL_TAIL_CHARS = 1500

_STATUS_CODES = {
    "401": FailureKind.AUTH,
    "403": FailureKind.AUTH,
    "429": FailureKind.RATE_LIMIT,
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
    number and any throttle wording (the vendor's stderr); otherwise a number
    counts only in an HTTP context and a throttle only in the vendor's error
    phrasing (the model's stdout, where a count or "slow down" is prose)."""
    # A disabled/unenabled model is a model-level answer; it outranks the
    # status code so its 401 is not misread as an account auth failure.
    if _MODEL_LEVEL_PATTERN.search(haystack):
        return FailureKind.MODEL_UNAVAILABLE
    # A plan usage limit wrapped as HTTP 403 / provider.auth_error is quota,
    # not a dead credential. kimi 0.34 reports weekly limits that way.
    if _QUOTA_PATTERN.search(haystack):
        return FailureKind.QUOTA
    throttle = _RATE_LIMIT_PATTERN if bare_codes else _STDOUT_RATE_LIMIT_PATTERN
    if throttle.search(haystack):
        return FailureKind.RATE_LIMIT
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
    if _EXCEEDED_PATTERN.search(haystack):
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
            return _spent_or(kind, channel)
    return FailureKind.TASK_FAILURE


def _spent_or(failure: FailureKind, text: str) -> FailureKind:
    """A throttle whose words name the plan or its reset is quota."""
    if failure is not FailureKind.RATE_LIMIT:
        return failure
    if _QUOTA_PATTERN.search(text) or _SPENT_WINDOW_PATTERN.search(text):
        return FailureKind.QUOTA
    reset = _RESETS_IN_PATTERN.search(text)
    if reset:
        scale = _RETRY_AFTER_UNITS.get(reset.group(2).lower())
        if scale is None or float(reset.group(1)) * scale > SHORT_THROTTLE_SECONDS:
            return FailureKind.QUOTA
    return failure


def classify_http_failure(status: int, body: str) -> FailureKind:
    """An HTTP status read with its body: a 429 that names the plan is quota."""
    failure = failure_for_status(status)
    return _spent_or(failure, body) if failure else classify_failure(1, body, "")


def retry_after_hint(*evidence: str) -> float | None:
    """The vendor's own "retry after N s", in seconds, when it named one."""
    for text in evidence:
        match = _RETRY_AFTER_PATTERN.search(text)
        if match:
            scale = _RETRY_AFTER_UNITS.get(match.group(2).lower())
            return None if scale is None else float(match.group(1)) * scale
    return None


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
    # A real stage is unbounded: the model process runs until it ends, and the
    # router's lease heartbeat keeps stage ownership alive while it does. An
    # adapter may still opt into its own deadline by declaring a number here;
    # None — the default — is a stage without a time limit.
    timeout_seconds = None
    # Probes stay bounded: the default window and default ceiling are 30 s,
    # and an adapter may raise its own ceiling up to the probe limit when its
    # launch path genuinely needs longer (a WSL cold start outruns the default
    # probe window); clamping a declared window to the default instead reports
    # a healthy account as a failed one.
    max_probe_timeout_seconds = 30
    probe_timeout_seconds = 30
    probe_is_generative = False

    def __init__(self, runner=None) -> None:
        self.runner = runner if runner is not None else subprocess.run
        # Class-declared probe values bypass __setattr__, so force them through
        # it; timeout_seconds needs no forcing — a stage is never clamped.
        self.max_probe_timeout_seconds = self.max_probe_timeout_seconds
        self.probe_timeout_seconds = self.probe_timeout_seconds

    def __setattr__(self, name, value) -> None:
        if name == "max_probe_timeout_seconds":
            value = min(value, _MAX_PROBE_TIMEOUT_SECONDS)
        elif name == "probe_timeout_seconds":
            value = min(value, self.max_probe_timeout_seconds)
        super().__setattr__(name, value)

    def probe(self, route: RouteSpec) -> AdapterHealth:
        raise NotImplementedError

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        raise NotImplementedError

    def _failed_result(
        self,
        route: RouteSpec,
        failure: FailureKind,
        detail: str = "",
        *,
        request: InvocationRequest,
        evidence: tuple[str, ...] = (),
        returncode: int | None = None,
        channel_labels: tuple[str, ...] = ("stderr", "stream"),
    ) -> InvocationResult:
        text = f"{route.adapter} invocation failed ({failure.value})"
        # The durable attempt detail keeps the vendor's own answer: the class,
        # the exit code and the redacted tails of stderr and the stream, so a
        # failed chain records WHY it failed. Diagnostics are not a public
        # evidence channel; only the attempt record and a recovered attempt
        # read them, and every boundary re-redacts. Redaction runs on the FULL
        # channel — the process boundary never holds the assignment prompt, so
        # a CLI echoing a prompt longer than the tail window would otherwise
        # leave a prompt suffix no later boundary can recognize (2026-10-19).
        parts = [text]
        if returncode is not None:
            parts.append(f"exit {returncode}")
        if detail:
            parts.append(detail)
        parts.extend(
            f"{label} tail: {redact_text(channel or '', (request.prompt,))[-_DETAIL_TAIL_CHARS:]}"
            for label, channel in zip(channel_labels, evidence)
            if channel
        )
        # Diagnostics are not a public evidence channel, even after redaction;
        # only a throttle's wait is read out of them.
        return InvocationResult(
            success=False,
            text=text,
            detail="\n".join(parts) if len(parts) > 1 else "",
            adapter=route.adapter,
            model=route.model,
            billing_mode=route.billing_mode,
            failure=failure,
            retry_after_seconds=retry_after_hint(*evidence)
            if failure is FailureKind.RATE_LIMIT
            else None,
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
    "classify_http_failure",
    "health_for_process_error",
    "retry_after_hint",
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
        "opencode_go_2": opencode,
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
