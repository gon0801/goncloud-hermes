"""Z.AI Coding Plan adapter over the documented Anthropic-compatible API."""

from __future__ import annotations

import json
import os

from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure
from ._http import SubscriptionHttpError, failure_for_status, request_json
from ._live_probe import PROBE_PROMPT, minimal_http_probe

_ANTHROPIC_VERSION = "2023-06-01"
#: Z.AI's documented Anthropic-compatible endpoint for the Coding Plan.
_DEFAULT_BASE_URL = "https://api.z.ai/api/anthropic"


class ZAIAdapter(SubscriptionAdapter):
    """Consume the Coding Plan subscription; the token stays in the environment."""

    billing_mode = "subscription"
    account_class = "zai_coding_plan"

    def __init__(
        self,
        runner=None,
        *,
        auth_token: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(runner=runner)
        if auth_token is None:
            auth_token = os.environ.get("GLM_API_KEY") or os.environ.get(
                "ZAI_AUTH_TOKEN", ""
            )
        self.auth_token = auth_token
        configured_base = (
            base_url
            or os.environ.get("GLM_BASE_URL")
            or os.environ.get("ZAI_BASE_URL")
            or _DEFAULT_BASE_URL
        )
        self.base_url = configured_base.rstrip("/")

    def _configured(self) -> bool:
        return bool(self.auth_token and self.base_url)

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """No non-generative endpoint proves this plan; require real inference."""
        result = self.live_probe(route)
        return AdapterHealth(
            ready=result.success,
            account_class=self.account_class,
            failure=result.failure,
        )

    def live_probe(self, route: RouteSpec) -> InvocationResult:
        """One minimal inference through the bounded HTTP process boundary."""
        if not self._configured():
            return InvocationResult(
                success=False,
                text="",
                adapter=route.adapter,
                model=route.model,
                billing_mode=self.billing_mode,
                failure=FailureKind.AUTH,
            )
        return minimal_http_probe(
            route,
            billing_mode=self.billing_mode,
            url=f"{self.base_url}/v1/messages",
            headers={
                "Authorization": f"Bearer {self.auth_token}",
                "anthropic-version": _ANTHROPIC_VERSION,
                "Content-Type": "application/json",
            },
            payload={
                "model": route.model,
                "max_tokens": 32,
                "messages": [{"role": "user", "content": PROBE_PROMPT}],
            },
            parse=_parse_message_body,
            timeout=self.probe_timeout_seconds,
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        if not self._configured():
            return self._failed_result(
                route, FailureKind.AUTH, "Z.AI Coding Plan is not configured"
            )
        try:
            status, body = request_json(
                "POST",
                f"{self.base_url}/v1/messages",
                headers={
                    "Authorization": f"Bearer {self.auth_token}",
                    "anthropic-version": _ANTHROPIC_VERSION,
                    "Content-Type": "application/json",
                },
                payload={
                    "model": route.model,
                    "max_tokens": 4000,
                    "messages": [
                        {
                            "role": "user",
                            "content": build_assignment_prompt(request),
                        }
                    ],
                },
                timeout=self.timeout_seconds,
            )
        except SubscriptionHttpError as error:
            failure = FailureKind.TIMEOUT if error.timeout else FailureKind.TASK_FAILURE
            return self._failed_result(route, failure, str(error))

        if status == 200:
            parsed = _parse_message_body(body)
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["id"],
            )
        failure = failure_for_status(status) or classify_failure(1, body, "")
        return self._failed_result(route, failure)


def _parse_message_body(body: str) -> dict:
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return {"text": "", "id": ""}
    if not isinstance(document, dict):
        return {"text": "", "id": ""}
    texts = [
        str(block.get("text") or "")
        for block in document.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    return {"text": "".join(texts), "id": str(document.get("id") or "")}
