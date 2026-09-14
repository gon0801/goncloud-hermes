"""DeepSeek API adapter — the single pay-per-use exception in the table."""

from __future__ import annotations

import json
import os

from ..types import AdapterHealth, FailureKind, InvocationRequest, InvocationResult, RouteSpec
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure
from ._http import SubscriptionHttpError, failure_for_status, request_json

_DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekAdapter(SubscriptionAdapter):
    """Billed per token through the owner's DeepSeek API key (``api`` mode)."""

    billing_mode = "api"
    account_class = "deepseek_api"

    def __init__(
        self,
        runner=None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        super().__init__(runner=runner)
        self.api_key = (
            api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        )
        configured_base = base_url or os.environ.get("DEEPSEEK_BASE_URL", "")
        self.base_url = (configured_base or _DEFAULT_BASE_URL).rstrip("/")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """GET /models is non-generative and validates the key."""
        if not self.api_key:
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                failure=FailureKind.AUTH,
                detail="DeepSeek API key is not configured",
            )
        try:
            status, _body = request_json(
                "GET",
                f"{self.base_url}/models",
                headers=self._headers(),
                timeout=self.probe_timeout_seconds,
            )
        except SubscriptionHttpError as error:
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                failure=FailureKind.TIMEOUT if error.timeout else FailureKind.TASK_FAILURE,
                detail=str(error),
            )
        if status == 200:
            return AdapterHealth(ready=True, account_class=self.account_class)
        failure = failure_for_status(status) or FailureKind.AUTH
        return AdapterHealth(
            ready=False, account_class=self.account_class, failure=failure
        )

    def invoke(
        self, request: InvocationRequest, route: RouteSpec
    ) -> InvocationResult:
        if not self.api_key:
            return self._failed_result(
                route, FailureKind.AUTH, "DeepSeek API key is not configured"
            )
        try:
            status, body = request_json(
                "POST",
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                payload={
                    "model": route.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": build_assignment_prompt(request),
                        }
                    ],
                    "max_tokens": 4000,
                    "stream": False,
                },
                timeout=self.timeout_seconds,
            )
        except SubscriptionHttpError as error:
            failure = FailureKind.TIMEOUT if error.timeout else FailureKind.TASK_FAILURE
            return self._failed_result(route, failure, str(error))

        if status == 200:
            parsed = _parse_completion_body(body)
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


def _parse_completion_body(body: str) -> dict:
    try:
        document = json.loads(body)
    except json.JSONDecodeError:
        return {"text": "", "id": ""}
    if not isinstance(document, dict):
        return {"text": "", "id": ""}
    choices = document.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        message = choices[0].get("message") or {}
        text = str(message.get("content") or "")
    return {"text": text, "id": str(document.get("id") or "")}
