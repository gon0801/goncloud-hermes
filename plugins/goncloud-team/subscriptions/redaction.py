"""Shared evidence redaction for process, adapter, durable and public boundaries."""

from __future__ import annotations

import json
import os
import re
from dataclasses import replace
from typing import Mapping, Sequence

_SENSITIVE_KEY = re.compile(
    r"(?:^|[_-])(?:API[_-]?KEY|(?:AUTH|ACCESS|REFRESH|SESSION)?[_-]?TOKEN|"
    r"SECRET(?:[_-]?KEY)?|PASSWORD|PASSWD|CREDENTIALS?|AUTHORIZATION|COOKIE|"
    r"PRIVATE[_-]?KEY|ACCESS[_-]?KEY)(?:[_-](?:VALUE|JSON))?$",
    re.IGNORECASE,
)
_SENSITIVE_TEXT = re.compile(
    r"\b(?:sk-[\w-]+|xai-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|"
    r"Bearer\s+[\w.\-]+|eyJ[\w-]+\.[\w-]+\.[\w-]+|[\w.+-]+@[\w.-]+\.[a-z]{2,})",
    re.IGNORECASE,
)


def sensitive_values(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Select credential values by key; ordinary inherited settings stay usable."""
    values = []
    for key, value in (os.environ if environment is None else environment).items():
        if isinstance(value, str) and value and _SENSITIVE_KEY.search(key):
            values.append(value)
            if value.lower().startswith("bearer "):
                values.append(value[7:])
    return tuple(values)


def _replacement(original: str) -> str:
    # Redaction precedes terminal validation. Never shrink original character
    # or UTF-8 byte counts: that could turn an oversized input/field into a
    # valid approval. Padding carries only a size bound, never provider text.
    marker = "[redacted]"
    width = len(original.encode("utf-8", errors="replace"))
    return marker + " " * max(0, width - len(marker))


def redact_text(text: str, secrets: Sequence[str] = ()) -> str:
    """Redact before truncation, including JSON-escaped copies of credentials."""
    result = text or ""
    variants = set()
    for secret in (*sensitive_values(), *secrets):
        if isinstance(secret, str) and secret:
            variants.add(secret)
            variants.add(json.dumps(secret, ensure_ascii=True)[1:-1])
            variants.add(json.dumps(secret, ensure_ascii=False)[1:-1])
    for secret in sorted(variants, key=len, reverse=True):
        result = result.replace(secret, _replacement(secret))
    return _SENSITIVE_TEXT.sub(lambda match: _replacement(match.group()), result)


def redact_data(value, secrets: Sequence[str] = ()):
    if isinstance(value, str):
        return redact_text(value, secrets)
    if isinstance(value, dict):
        return {key: redact_data(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_data(item, secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item, secrets) for item in value)
    return value


def sanitize_result(result, secrets: Sequence[str] = ()):
    return replace(
        result,
        **{
            field: redact_text(getattr(result, field), secrets)
            for field in ("text", "session_id", "adapter", "model")
        },
    )
