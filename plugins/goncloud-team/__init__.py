"""Hermes registration for the Goncloud durable team contract."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .muse_bridge import delegate_to_muse
from .schemas import (
    CONSULT,
    ENGINEERING_REVIEW,
    ENGINEERING_START,
    ENGINEERING_STATUS,
    LOCAL_RUN,
    LOCAL_STATUS,
    MUSE_DELEGATE,
    SUBSCRIPTION_RUN,
    SUBSCRIPTION_STATUS,
)
from .workflow import WorkflowService


def _build_service(ctx):
    """Bind the durable service to the calling profile; test seam."""
    return WorkflowService(actor_role=ctx.profile_name)


def _build_orchestrator(ctx):
    """The proven local entry point a messaging transport submits to; test seam."""
    del ctx
    from .local_orchestrator import LocalOrchestrator

    return LocalOrchestrator()


def _transport_handlers(ctx):
    """Bind the transport tools: create the chain, detach the driver, report."""

    def local_run(args: dict, **kwargs) -> str:
        del kwargs
        try:
            if ctx.profile_name != "claw":
                raise PermissionError("only the claw director may start local work")
            arguments = dict(args or {})
            orchestrator = _build_orchestrator(ctx)
            created = orchestrator.create(
                request_key=str(arguments.get("request_key", "")),
                repository=str(arguments.get("repository", "")),
                revision=arguments.get("revision"),
                instruction=str(arguments.get("instruction", "")),
                sources=arguments.get("sources"),
                max_steps=int(arguments.get("max_steps", 20)),
                affects_business_rules=bool(
                    arguments.get("affects_business_rules", False)
                ),
            )
            launched = orchestrator.launch_detached(
                created["workflow_id"], stop_after=arguments.get("stop_after") or None
            )
            return json.dumps(
                {**created, **launched}, ensure_ascii=False, sort_keys=True, default=str
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)

    def local_status(args: dict, **kwargs) -> str:
        del kwargs
        try:
            workflow_id = str((args or {}).get("workflow_id", ""))
            orchestrator = _build_orchestrator(ctx)
            result = dict(orchestrator.status(workflow_id))
            driver = getattr(orchestrator, "driver_result", None)
            if driver is not None:
                result["driver"] = driver(workflow_id)
            return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)

    return local_run, local_status


def _handler(ctx, method_name: str):
    def handle(args: dict, **kwargs) -> str:
        del kwargs
        try:
            service = _build_service(ctx)
            result = getattr(service, method_name)(**dict(args or {}))
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)

    return handle


def register(ctx) -> None:
    """Register the Goncloud model tools under the ``goncloud_team`` toolset."""
    for name, schema, method, emoji in (
        ("goncloud_engineering_start", ENGINEERING_START, "start", "🚀"),
        ("goncloud_engineering_status", ENGINEERING_STATUS, "status", "📋"),
        ("goncloud_consult", CONSULT, "consult", "🤝"),
        ("goncloud_engineering_review", ENGINEERING_REVIEW, "review", "🔎"),
        (
            "goncloud_subscription_run",
            SUBSCRIPTION_RUN,
            "subscription_run",
            "🧭",
        ),
        (
            "goncloud_subscription_status",
            SUBSCRIPTION_STATUS,
            "subscription_status",
            "📡",
        ),
    ):
        ctx.register_tool(
            name=name,
            toolset="goncloud_team",
            schema=schema,
            handler=_handler(ctx, method),
            emoji=emoji,
        )

    def handle_muse(args: dict, **kwargs) -> str:
        del kwargs
        try:
            arguments = dict(args or {})
            result = delegate_to_muse(
                prompt=str(arguments.get("prompt", "")),
                workspace=Path(str(arguments.get("workspace", ""))),
                actor_role=ctx.profile_name,
                max_steps=int(arguments.get("max_steps", 80)),
            )
            return json.dumps(result, ensure_ascii=False, sort_keys=True)
        except Exception as exc:
            return json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True)

    ctx.register_tool(
        name="goncloud_muse_delegate",
        toolset="goncloud_team",
        schema=MUSE_DELEGATE,
        handler=handle_muse,
        emoji="🧠",
    )

    local_run, local_status = _transport_handlers(ctx)
    ctx.register_tool(
        name="goncloud_local_run",
        toolset="goncloud_team",
        schema=LOCAL_RUN,
        handler=local_run,
        emoji="📨",
    )
    ctx.register_tool(
        name="goncloud_local_status",
        toolset="goncloud_team",
        schema=LOCAL_STATUS,
        handler=local_status,
        emoji="📈",
    )

    def guard_reviewer_completion(tool_name, args, task_id=None, **kwargs):
        del kwargs
        if tool_name != "kanban_complete":
            return None
        native_task_id = str(
            (args or {}).get("task_id")
            or os.environ.get("HERMES_KANBAN_TASK")
            or task_id
            or ""
        ).strip()
        if not native_task_id:
            return None
        try:
            return WorkflowService(actor_role=ctx.profile_name).guard_native_completion(
                native_task_id
            )
        except Exception as exc:
            if ctx.profile_name != "reviewer":
                return None
            return {
                "action": "block",
                "message": f"Goncloud review completion guard unavailable: {exc}",
            }

    ctx.register_hook("pre_tool_call", guard_reviewer_completion)
