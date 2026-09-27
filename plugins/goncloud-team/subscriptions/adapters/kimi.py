"""Kimi Coding Plan adapter using the kimi CLI prompt mode."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from ..process import PROMPT_PLACEHOLDER, SubscriptionProcessError, run_cli
from ..types import (
    AdapterHealth,
    FailureKind,
    InvocationRequest,
    InvocationResult,
    RouteSpec,
)
from . import (
    SubscriptionAdapter,
    build_assignment_prompt,
    classify_failure,
    health_for_process_error,
)

_SUCCESS_SUBTYPES = {"success", "success_streamcomplete", "streamcomplete"}

#: The only prompt text that ever reaches argv. kimi 0.34 takes ``-p`` as an
#: argument and never reads stdin, so the assignment itself rides in a private
#: agent file and this fixed sentence just points the model at it.
KIMI_SENTINEL = "Carry out the assignment defined in your agent profile."


def kimi_agent_definition(prompt: str) -> str:
    """Wrap the assignment as the agent definition kimi loads from a private file.

    kimi refuses an agent file without frontmatter ("Missing frontmatter"); the
    extension is irrelevant, so run_cli's ``.txt`` prompt transport is fine.
    """
    return (
        "---\n"
        "name: goncloud-stage\n"
        "description: One bounded Goncloud stage assignment.\n"
        "---\n"
        f"{prompt}\n"
    )


def parse_kimi_jsonl(output: str) -> dict:
    """Extract text and session identity from kimi stream-json events.

    Two schemas are accepted: the ``type``/``subtype`` result stream, and the
    ``role``-based prompt-mode stream of kimi 0.34, where the
    ``session.resume_hint`` meta line is what closes a completed turn.
    """
    texts: list[str] = []
    session_id = None
    result_text = None
    subtype = None
    saw_error = False
    malformed = False
    resumed = False
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
        role = event.get("role")
        if role is not None:
            if role == "assistant":
                content = event.get("content")
                if content is None:
                    # A tool-call turn carries tool_calls instead of content;
                    # it is a valid step, not corruption, and adds no text.
                    continue
                if not isinstance(content, str):
                    malformed = True
                    continue
                texts.append(content)
            elif role == "meta":
                meta_type = event.get("type")
                if not isinstance(meta_type, str):
                    malformed = True
                    continue
                meta = meta_type.lower()
                if (
                    "error" in meta
                    or event.get("error")
                    or event.get("error_name")
                    or event.get("error_message")
                ):
                    saw_error = True
                if meta == "session.resume_hint":
                    resumed = True
                    raw_session_id = event.get("session_id")
                    if raw_session_id is not None:
                        if not isinstance(raw_session_id, str):
                            malformed = True
                        else:
                            session_id = raw_session_id
            else:
                # Unknown but well-formed roles (e.g. role:"tool" results)
                # are stream steps we don't consume, not corruption. Ignoring
                # them keeps a tool-using turn from being flagged malformed.
                pass
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
    completed = subtype in _SUCCESS_SUBTYPES or (
        subtype is None and resumed and bool(texts)
    )
    return {
        "text": str(text or ""),
        "session_id": session_id,
        "subtype": subtype,
        "terminal_valid": bool(completed and not saw_error and not malformed),
    }


class KimiAdapter(SubscriptionAdapter):
    """Run one stage on the Kimi Coding Plan through prompt mode.

    kimi 0.34.0 rejects ``--auto`` combined with ``-p``; prompt mode is
    already the autonomous non-interactive mode, so autonomy is preserved
    and the prompt still travels by stdin (``-p -``), never by argument.
    """

    account_class = "kimi_coding"

    def __init__(self, runner=None, config_path: Path | None = None) -> None:
        super().__init__(runner=runner)
        self.config_path = (
            config_path
            if config_path is not None
            else Path.home() / ".kimi-code" / "config.toml"
        )

    def _configured_effort(self) -> str | None:
        """Read Kimi's effective global effort without mutating owner config."""
        try:
            configuration = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return None
        effort = configuration.get("effort")
        return effort if isinstance(effort, str) else None

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
            return health_for_process_error(self.account_class, error)
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
        if (
            route.reasoning_effort is not None
            and self._configured_effort() != route.reasoning_effort
        ):
            return self._failed_result(route, FailureKind.INVALID_REQUEST)
        # kimi resolves models through its configured aliases (config.toml
        # defines them as kimi-code/<name>), so qualify bare table models.
        model = route.model if "/" in route.model else f"kimi-code/{route.model}"
        # The assignment never enters argv: run_cli writes it to a private file
        # that kimi loads as the agent definition; -p carries only the sentinel.
        command = [
            "kimi",
            "--agent-file",
            PROMPT_PLACEHOLDER,
            "-p",
            KIMI_SENTINEL,
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        try:
            evidence = run_cli(
                command,
                kimi_agent_definition(build_assignment_prompt(request)),
                request.workspace,
                self.timeout_seconds,
                prompt_transport="tempfile",
                runner=self.runner,
            )
        except SubscriptionProcessError as error:
            return self._failed_result(route, error.failure, str(error))

        parsed = parse_kimi_jsonl(evidence.stream)
        if evidence.returncode == 0:
            if parsed["terminal_valid"] and parsed["text"].strip():
                return InvocationResult(
                    success=True,
                    text=parsed["text"],
                    adapter=route.adapter,
                    model=route.model,
                    billing_mode=route.billing_mode,
                    session_id=parsed["session_id"] or "",
                )
            return self._failed_result(route, FailureKind.TASK_FAILURE)
        failure = classify_failure(
            evidence.returncode, evidence.stream, evidence.stderr
        )
        return self._failed_result(route, failure)
