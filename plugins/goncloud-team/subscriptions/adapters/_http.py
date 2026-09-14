"""Minimal JSON HTTP client for the two direct-API adapters."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Mapping

from ..types import FailureKind

MAX_BODY_CHARS = 20_000

_STATUS_FAILURES = {
    400: FailureKind.INVALID_REQUEST,
    401: FailureKind.AUTH,
    402: FailureKind.QUOTA,
    403: FailureKind.AUTH,
    404: FailureKind.MODEL_UNAVAILABLE,
    429: FailureKind.QUOTA,
}


class SubscriptionHttpError(RuntimeError):
    """A vendor endpoint could not be reached or did not answer in time."""

    def __init__(self, message: str, *, timeout: bool = False) -> None:
        super().__init__(message)
        self.timeout = timeout


def failure_for_status(status: int) -> FailureKind | None:
    return _STATUS_FAILURES.get(status)


def request_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    payload: Any = None,
    timeout: float = 120,
    opener=None,
) -> tuple[int, str]:
    """One JSON request; returns (status, capped body) or raises."""
    data = (
        json.dumps(payload).encode("utf-8") if payload is not None else None
    )
    request = urllib.request.Request(
        url, data=data, headers=dict(headers), method=method
    )
    call = opener or urllib.request.urlopen
    try:
        with call(request, timeout=timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            body = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            body = error.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 - body is diagnostic only
            body = ""
    except urllib.error.URLError as error:
        reason = str(getattr(error, "reason", error))
        raise SubscriptionHttpError(
            "endpoint unreachable: " + reason, timeout="timed out" in reason.lower()
        ) from error
    except TimeoutError as error:
        raise SubscriptionHttpError(
            "endpoint timed out", timeout=True
        ) from error
    return int(status), body[:MAX_BODY_CHARS]
