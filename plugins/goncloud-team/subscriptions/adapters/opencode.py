"""OpenCode Go and OpenCode Zen subscription adapters.

Both slots consume the owner's OpenCode subscriptions through the native CLI
credential store. ``auth.json`` is never read or copied by this module.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from ..process import SubscriptionProcessError, probe_workspace, run_cli
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

#: Slot identity, not the historical label prefix, selects the subscription.
#: Provider prefixes exactly as ``opencode models`` lists them. The Z.AI entry
#: is what gives GLM the same read/grep/shell toolset as every other CLI-backed
#: slot: reached over its plain HTTP API instead, the model has no way to touch
#: the workspace and can only report that it could not look (real scout stage,
#: 2026-09-26). Verified live: ``zai-coding-plan/glm-5.3-flash`` read a file
#: through the ``read`` tool, closed with ``reason: stop``, and cost 0, so the
#: Coding Plan subscription serves it rather than a metered API.
_PROVIDER_PREFIXES = {
    "opencode_go": "opencode-go",
    "opencode_go_2": "opencode-go",
    "opencode_zen": "opencode",
    "opencode_zai": "zai-coding-plan",
}
#: The owner's second OpenCode Go account (2026-09-27). The saved CLI login is
#: the first account, so the second key reaches opencode as an env reference in
#: inline config: the value never enters argv, and redaction keys on the name.
_ACCOUNT_KEY_ENVS = {"opencode_go_2": "OPENCODE_GO_2_API_KEY"}

_CREDENTIAL_NAMES = {
    "opencode_go": "opencode go",
    "opencode_zen": "opencode zen",
    "opencode_zai": "z.ai coding plan",
}

#: The PC runs OpenCode 1.19.3; the Mac moved to 2.x (owner decision,
#: 2026-10-19). One ``--version`` per adapter picks the invocation. Real
#: measured outputs: Mac/Mini 2.x print "opencode v2.0.29", the PC 1.x
#: prints "1.19.3" bare. The line must start as a version line (optional
#: "opencode" prefix, then the version; a suffix such as "-rc.1" or
#: "(darwin arm64)" may follow) so a canned test stream never parses as
#: a version. This pattern only ever runs on ``--version`` output. 2.x: --dir is gone (the workspace is the
#: process cwd), the model travels by --model/-m the same way, every
#: attempt needs --standalone to get a private server instead of the
#: shared background one, and the JSON stream closes with the final
#: text part — optionally followed by a step-finish reason "stop"; a
#: step-finish reason "tool-calls" only appears mid-stream.
_VERSION_LINE = re.compile(r"(?m)^\s*(?:opencode\s+)?v?(\d+)\.\d+(?:\.\d+)?")


def parse_opencode_jsonl(output: str, *, major: int = 1) -> dict:
    """Accumulate text parts and session identity from raw JSON events."""
    text_parts: list[str] = []
    session_id = None
    terminal_valid = False
    saw_error = False
    saw_text = False
    malformed = False
    last_text_index = -1
    last_progress_index = -1
    saw_bad_step_finish_reason = False
    for index, line in enumerate(output.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if line.strip():
                malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        event_type = str(event.get("type") or "").lower()
        session_id = event.get("sessionID") or event.get("session_id") or session_id
        if event_type in {"tool_use", "step_start"}:
            last_progress_index = index
        if event_type == "tool_use":
            continue
        if "error" in event_type or event.get("error"):
            saw_error = True
        info = event.get("info")
        if isinstance(info, dict):
            session_id = info.get("id") or session_id
        part = event.get("part")
        if isinstance(part, dict):
            part_type = str(part.get("type") or "").lower()
            reason = str(part.get("reason") or "").lower()
            if (
                "error" in part_type
                or part.get("error")
                or reason in {"error", "failed"}
            ):
                saw_error = True
            if part_type == "step-start":
                last_progress_index = index
            if part_type == "text":
                text_parts.append(str(part.get("text") or ""))
                saw_text = True
                last_text_index = index
            if event_type == "step_finish" and part_type == "step-finish":
                if reason == "tool-calls":
                    last_progress_index = index
                if reason not in {"stop", "tool-calls"}:
                    saw_bad_step_finish_reason = True
                if reason == "stop":
                    terminal_valid = True
    if major >= 2:
        # 2.x closes with the final text part, optionally followed by a
        # step-finish reason "stop". Text that only precedes the last
        # progress event (tool_use, step_start or a "tool-calls"
        # step-finish) is a mid-stream fragment, and a step-finish
        # reason outside stop/tool-calls (e.g. length) means the
        # stream was cut.
        terminal_valid = (
            saw_text
            and last_text_index > last_progress_index
            and not saw_bad_step_finish_reason
            and not saw_error
            and not malformed
        )
    return {
        "session_id": session_id,
        "text": "".join(text_parts),
        "terminal_valid": terminal_valid and not saw_error and not malformed,
    }


class OpenCodeAdapter(SubscriptionAdapter):
    """Invoke ``opencode run`` against the Go or Zen subscription of a slot."""

    def _provider(self, route: RouteSpec) -> str:
        provider = _PROVIDER_PREFIXES.get(route.adapter)
        if provider is None:
            raise ValueError(f"unsupported OpenCode slot adapter: {route.adapter}")
        return provider

    def _env_overlay(self, route: RouteSpec):
        config: dict = {}
        key_env = _ACCOUNT_KEY_ENVS.get(route.adapter)
        if key_env:
            config["provider"] = {
                self._provider(route): {"options": {"apiKey": f"{{env:{key_env}}}"}}
            }
        return {"OPENCODE_CONFIG_CONTENT": json.dumps(config)} if config else None

    def probe(self, route: RouteSpec) -> AdapterHealth:
        """Non-generative check that the slot's credential class is installed."""
        key_env = _ACCOUNT_KEY_ENVS.get(route.adapter)
        if key_env:
            if os.environ.get(key_env, "").strip():
                return AdapterHealth(ready=True, account_class=route.adapter)
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=FailureKind.AUTH,
                detail=f"{key_env} is not set",
            )
        expected = _CREDENTIAL_NAMES.get(route.adapter, route.adapter)
        try:
            with probe_workspace() as probe_dir:
                evidence = run_cli(
                    ["opencode", "auth", "list"],
                    "",
                    Path(probe_dir),
                    self.probe_timeout_seconds,
                    runner=self.runner,
                )
        except SubscriptionProcessError as error:
            return health_for_process_error(route.adapter, error)
        if evidence.returncode != 0:
            return AdapterHealth(
                ready=False,
                account_class=route.adapter,
                failure=classify_failure(
                    evidence.returncode, evidence.stream, evidence.stderr
                ),
                detail="opencode auth list failed",
            )
        installed = evidence.stream.lower()
        if expected in installed:
            return AdapterHealth(ready=True, account_class=route.adapter)
        return AdapterHealth(
            ready=False,
            account_class=route.adapter,
            failure=FailureKind.AUTH,
            detail=f"credential class not installed: {expected}",
        )

    def _major(self) -> int:
        """``opencode --version`` una vez por adaptador: >=2, camino nuevo.

        Cualquier duda (CLI ausente, salida que no es una línea de versión)
        se resuelve en 1: la PC corre 1.19.3 y no debe romperse aunque el
        sondeo falle.
        """
        cached = getattr(self, "_major_version", None)
        if cached is not None:
            return cached
        major = 1
        try:
            with probe_workspace() as probe_dir:
                evidence = run_cli(
                    ["opencode", "--version"],
                    "",
                    Path(probe_dir),
                    self.probe_timeout_seconds,
                    runner=self.runner,
                )
            found = _VERSION_LINE.search(evidence.stdout)
            if found:
                major = max(1, int(found.group(1)))
        except SubscriptionProcessError:
            major = 1
        self._major_version = major
        return major

    def _run(self, request: InvocationRequest, route: RouteSpec, prompt: str):
        if self._major() >= 2:
            # OpenCode 2: no --dir (the workspace is the process cwd) and
            # --standalone so the attempt gets a private server instead of
            # joining the shared background one. The prompt still travels
            # by stdin, never as an argument.
            command = [
                "opencode",
                "run",
                "--standalone",
                "--format",
                "json",
                "--model",
                f"{self._provider(route)}/{route.model}",
            ]
        else:
            command = [
                "opencode",
                "run",
                "--format",
                "json",
                "--model",
                f"{self._provider(route)}/{route.model}",
                "--dir",
                str(request.workspace),
            ]
        return run_cli(
            command,
            prompt,
            request.workspace,
            self.timeout_seconds,
            env_overlay=self._env_overlay(route),
            runner=self.runner,
        )

    def invoke(self, request: InvocationRequest, route: RouteSpec) -> InvocationResult:
        try:
            evidence = self._run(request, route, build_assignment_prompt(request))
        except SubscriptionProcessError as error:
            return self._failed_result(
                route, error.failure, str(error), request=request
            )

        if evidence.returncode != 0:
            failure = classify_failure(
                evidence.returncode, evidence.stream, evidence.stderr
            )
            return self._failed_result(
                route,
                failure,
                request=request,
                evidence=(evidence.stderr, evidence.stream),
                returncode=evidence.returncode,
            )

        parsed = parse_opencode_jsonl(evidence.stream, major=self._major())
        if parsed["terminal_valid"] and parsed["text"].strip():
            return InvocationResult(
                success=True,
                text=parsed["text"],
                adapter=route.adapter,
                model=route.model,
                billing_mode=route.billing_mode,
                session_id=parsed["session_id"] or "",
            )
        # The CLI answered, it just did not answer in the terminal
        # contract: the vendor responded and delivered nothing usable.
        # Nothing here shows the task itself failed, which is exactly
        # invalid_terminal.
        return self._failed_result(
            route,
            FailureKind.INVALID_TERMINAL,
            request=request,
            evidence=(evidence.stderr, evidence.stream),
            returncode=evidence.returncode,
        )
