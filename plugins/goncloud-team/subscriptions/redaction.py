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
# Credential shapes worth catching without knowing the value. Only real
# credentials: hex runs would erase git revisions, and email addresses are
# working data (a redacted address in a tool call broke the Odoo login,
# 2026-09-27). Every open class stays bounded: an unbounded run backtracked
# from every word boundary of a minified blob, minutes per 100 KB.
SENSITIVE_TEXT = re.compile(
    r"\b(?:sk-[\w-]+|xai-[\w-]+|gh[pousr]_[\w]+|github_pat_[\w]+|glpat-[\w-]+|"
    r"xox[abopr]-[\w-]+|AIza[\w-]{20,}|(?-i:AKIA[0-9A-Z]{16})|"
    r"Bearer\s+[\w.\-]+|(?-i:Basic)\s+[A-Za-z0-9+/]{16,}={0,2}|"
    r"eyJ[\w-]+\.[\w-]+\.[\w-]+)"
    r"|(?<=[?&])(?:access_token|token|api_key|apikey|secret|password|key)="
    r"[^&\s\"']+",
    re.IGNORECASE,
)
_MARKER = "[redacted]"
_MIN_SECRET_CHARS = 6


def usable_secret(secret) -> bool:
    # Under six characters it is a switch or a placeholder, not a credential,
    # and replacing it would rewrite JSON keys and break the terminal envelope.
    # A piece of the marker would feed the next redaction pass forever.
    return (
        isinstance(secret, str)
        and len(secret) >= _MIN_SECRET_CHARS
        and secret not in _MARKER
    )


def sensitive_values(environment: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Select credential values by key; ordinary inherited settings stay usable."""
    values = []
    for key, value in (os.environ if environment is None else environment).items():
        if isinstance(value, str) and _SENSITIVE_KEY.search(key):
            candidates = [value]
            if value.lower().startswith("bearer "):
                candidates.append(value[7:])
            values.extend(
                candidate for candidate in candidates if usable_secret(candidate)
            )
    return tuple(values)


def _replacement(original: str) -> str:
    # Redaction precedes terminal validation. Never shrink original character
    # or UTF-8 byte counts: that could turn an oversized input/field into a
    # valid approval. Padding carries only a size bound, never provider text.
    width = len(original.encode("utf-8", errors="replace"))
    return _MARKER + " " * max(0, width - len(_MARKER))


def redact_text(text: str, secrets: Sequence[str] = ()) -> str:
    """Redact before truncation, including JSON-escaped copies of credentials."""
    result = text or ""
    variants = set()
    for secret in (*sensitive_values(), *secrets):
        if usable_secret(secret):
            variants.add(secret)
            variants.add(json.dumps(secret, ensure_ascii=True)[1:-1])
            variants.add(json.dumps(secret, ensure_ascii=False)[1:-1])
    for secret in sorted(variants, key=len, reverse=True):
        result = result.replace(secret, _replacement(secret))
    return SENSITIVE_TEXT.sub(lambda match: _replacement(match.group()), result)


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
