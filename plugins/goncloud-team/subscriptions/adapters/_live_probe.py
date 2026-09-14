"""Bounded, text-only credential proof shared by the direct HTTP adapters."""

from dataclasses import replace

from ..types import FailureKind, InvocationResult
from ._http import SubscriptionHttpError, failure_for_status, request_json

PROBE_MARKER = "GONCLOUD_PROBE_OK"
PROBE_PROMPT = (
    "Reply with exactly GONCLOUD_PROBE_OK and nothing else. "
    "Do not use tools, modify files, or perform any other action."
)
#: A reasoning model spends its allowance thinking before it emits any text.
#: A budget that cannot outlast that preamble reports a healthy account as a
#: task failure; the marker itself stays one short line either way.
PROBE_MAX_TOKENS = 1024


def minimal_http_probe(route, *, billing_mode, url, headers, payload, parse, timeout):
    """Require an exact generated marker; never return raw provider evidence."""
    route = replace(route, billing_mode=billing_mode)
    failure = None
    try:
        status, body = request_json(
            "POST", url, headers=headers, payload=payload, timeout=timeout
        )
        if status == 200:
            parsed = parse(body)
            if parsed["text"] != PROBE_MARKER:
                # Our own output budget cutting the answer short is evidence
                # about the budget, not about the account, so it advances the
                # slot instead of blocking the stage on a task failure.
                failure = (
                    FailureKind.TRANSPORT
                    if parsed.get("truncated")
                    else FailureKind.TASK_FAILURE
                )
        else:
            failure = failure_for_status(status) or (
                FailureKind.TRANSPORT if status >= 500 else FailureKind.TASK_FAILURE
            )
    except SubscriptionHttpError as error:
        failure = FailureKind.TIMEOUT if error.timeout else FailureKind.TRANSPORT
    except (ValueError, KeyError, TypeError, AttributeError):
        failure = FailureKind.TASK_FAILURE
    return InvocationResult(
        success=failure is None,
        text=PROBE_MARKER if failure is None else "",
        adapter=route.adapter,
        model=route.model,
        billing_mode=route.billing_mode,
        failure=failure,
    )
