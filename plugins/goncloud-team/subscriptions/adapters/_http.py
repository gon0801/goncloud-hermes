"""Minimal JSON HTTP client for the two direct-API adapters."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

from ..process import SubscriptionProcessError, _run_with_process_tree
from ..types import FailureKind
from ..redaction import redact_text, sensitive_values

MAX_BODY_CHARS = 20_000

# The isolated stdlib worker is contained by the same process boundary as CLIs.
# DNS, TLS, response headers, and the entire body read share one parent deadline;
# killing the worker closes its sockets before the stage lease can be reclaimed.
# Credentials and request bodies arrive only through the private stdin pipe.
_HTTP_WORKER = r"""
import json
import sys
import urllib.error
import urllib.request

config = json.loads(sys.stdin.read())
data = json.dumps(config['payload']).encode('utf-8') if config['payload'] is not None else None
request = urllib.request.Request(config['url'], data=data, headers=config['headers'], method=config['method'])
try:
    try:
        response = urllib.request.urlopen(request, timeout=config['timeout'])
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status = response.status or response.getcode()
        try:
            raw_body = response.read(config['max_body_chars'] * 4 + 1)
            if len(raw_body) > config['max_body_chars'] * 4:
                raise ValueError('response exceeds evidence boundary')
            body = raw_body.decode('utf-8', 'replace')
        except Exception as error:
            # A diagnostic body cannot erase a known HTTP failure. Timeouts
            # still propagate, and malformed success/unknown responses fail.
            if (isinstance(getattr(error, 'reason', error), TimeoutError)
                    or not isinstance(response, urllib.error.HTTPError)
                    or status not in config['known_failure_statuses']):
                raise
            body = ''
    # Return complete bounded text: the parent redacts before its evidence cap.
    # A partial body could split a credential and defeat exact-value redaction.
    result = {'status': status, 'body': body}
except (TimeoutError, urllib.error.URLError) as error:
    reason = getattr(error, 'reason', error)
    result = {'error': 'timeout' if isinstance(reason, TimeoutError) else 'unreachable'}
except Exception:
    result = {'error': 'failed'}
print(json.dumps(result))
"""

_STATUS_FAILURES = {
    400: FailureKind.INVALID_REQUEST,
    401: FailureKind.AUTH,
    402: FailureKind.QUOTA,
    403: FailureKind.AUTH,
    404: FailureKind.MODEL_UNAVAILABLE,
    429: FailureKind.RATE_LIMIT,
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
) -> tuple[int, str]:
    """One JSON request with a total deadline and bounded process-tree cleanup."""
    timeout = min(float(timeout), 300)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("HTTP timeout must be a positive finite number")
    config = json.dumps(
        {
            "method": method,
            "url": url,
            "headers": dict(headers),
            "payload": payload,
            "timeout": timeout,
            "max_body_chars": MAX_BODY_CHARS,
            "known_failure_statuses": list(_STATUS_FAILURES),
        }
    )
    try:
        returncode, stdout, _stderr = _run_with_process_tree(
            [sys.executable, "-I", "-S", "-c", _HTTP_WORKER],
            cwd=Path(sys.executable).resolve().parent,
            env=os.environ.copy(),
            stdin_text=config,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SubscriptionHttpError("endpoint timed out", timeout=True) from None
    except (OSError, SubscriptionProcessError):
        raise SubscriptionHttpError("endpoint request failed") from None
    if returncode:
        raise SubscriptionHttpError("endpoint request failed")
    try:
        result = json.loads(stdout)
        if "error" in result:
            timed_out = result["error"] == "timeout"
            raise SubscriptionHttpError(
                "endpoint timed out" if timed_out else "endpoint request failed",
                timeout=timed_out,
            )
        return int(result["status"]), redact_text(
            result["body"], sensitive_values(headers)
        )[:MAX_BODY_CHARS]
    except (ValueError, KeyError, TypeError):
        raise SubscriptionHttpError("endpoint response unavailable") from None
