"""Kimi Coding Plan adapter using the kimi CLI prompt mode."""

from __future__ import annotations

import json
from pathlib import Path

from ..process import SubscriptionProcessError, run_cli
from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from . import SubscriptionAdapter, build_assignment_prompt, classify_failure

_SUCCESS_SUBTYPES = {"success", "success_streamcomplete", "streamcomplete"}


def parse_kimi_jsonl(output: str) -> dict:
    """Extract text and session identity from kimi stream-json events."""
    texts: list[str] = []
    session_id = None
    result_text = None
    subtype = None
    saw_error = False
    malformed = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        raw_event_type = event.get("type")
        if not isinstance(raw_event_type, str):
            malformed = True
            continue
        event_type = raw_event_type.lower()
        if "error" in event_type or event.get("error"):
            saw_error = True
        raw_session_id = event.get("session_id")
        if raw_session_id is not None:
            if not isinstance(raw_session_id, str):
                malformed = True
            else:
                session_id = raw_session_id
        if event_type == "assistant":
            message = event.get("message")
            if not isinstance(message, dict):
                malformed = True
                continue
            content = message.get("content")
            if not isinstance(content, list):
                malformed = True
                continue
            for block in content:
                if not isinstance(block, dict):
                    malformed = True
                    continue
                block_type = block.get("type")
                if not isinstance(block_type, str):
                    malformed = True
                    continue
                if block_type == "text":
                    block_text = block.get("text")
                    if not isinstance(block_text, str):
                        malformed = True
                        continue
                    texts.append(block_text)
        if event_type == "result":
            raw_subtype = event.get("subtype")
            raw_result = event.get("result")
            if not isinstance(raw_subtype, str) or not isinstance(raw_result, str):
                malformed = True
                continue
            result_text = raw_result
            subtype = raw_subtype
    text = result_text if result_text is not None else "".join(texts)
    return {
        "text": str(text or ""),
        "session_id": session_id,
        "subtype": subtype,
        "terminal_valid": bool(
            subtype in _SUCCESS_SUBTYPES and not saw_error and not malformed
        ),
    }


class KimiAdapter(SubscriptionAdapter):
    """Run one stage on the Kimi Coding Plan through prompt mode.

    kimi 0.34.0 rejects ``--auto`` combined with ``-p``; prompt mode is
    already the autonomous non-interactive mode, so autonomy is preserved
    and the prompt still travels by stdin (``-p -``), never by argument.
    """

    account_class = "kimi_coding"

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """``kimi doctor`` is non-generative; auth needs live inference."""
        try:
            evidence = run_cli(
                ["kimi", "doctor"],
                "",
                Path.cwd(),
                self.probe_timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return AdapterHealth(
                ready=False, account_class=self.account_class, detail=str(error)
            )
        if evidence.returncode != 0:
            return AdapterHealth(
                ready=False,
                account_class=self.account_class,
                detail="kimi doctor reported configuration problems",
            )
        return AdapterHealth(
            ready=True,
            account_class=self.account_class,
            detail="auth is proven only by live inference",
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        # kimi resolves models through its configured aliases (config.toml
        # defines them as kimi-code/<name>), so qualify bare table models.
        model = route.model if "/" in route.model else f"kimi-code/{route.model}"
        command = [
            "kimi",
            "-p",
            "-",
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        try:
            evidence = run_cli(
                command,
                build_assignment_prompt(request),
                request.workspace,
                self.timeout_seconds,
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            if "timed out" in str(error):
                return self._failed_result(route, FailureKind.TIMEOUT)
            return self._failed_result(route, FailureKind.TASK_FAILURE, str(error))

        parsed = parse_kimi_jsonl(evidence.stdout)
        if (
            evidence.returncode == 0
            and parsed["terminal_valid"]
            and parsed["text"].strip()
        ):
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        failure = classify_failure(
            evidence.returncode, evidence.stdout, evidence.stderr
        )
        return self._failed_result(route, failure)
