"""Model-visible tool contracts for the Goncloud team plugin."""

from __future__ import annotations

from .workflow import TEAM_ROLES


ENGINEERING_START = {
    "name": "goncloud_engineering_start",
    "description": (
        "Start the durable Goncloud engineering chain. Only the claw profile may call this. "
        "Creates scout -> ingenieria -> implementer -> verifier -> adversary -> reviewer -> "
        "entrega in one exact repository workspace. Reuse request_key on retries."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "request_key": {
                "type": "string",
                "description": "Stable owner-request idempotency key.",
            },
            "repository": {
                "type": "string",
                "description": "Absolute existing repository path.",
            },
            "revision": {
                "type": "string",
                "description": "Exact revision every stage must inspect.",
            },
            "instruction": {
                "type": "string",
                "description": "The owner-approved engineering outcome.",
            },
            "sources": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "reference": {"type": "string"},
                        "observed_at": {"type": "string"},
                    },
                    "required": ["reference", "observed_at"],
                },
                "description": "Dated provenance records supporting the request.",
            },
            "affects_business_rules": {
                "type": "boolean",
                "description": "Makes verifier include business acceptance when true.",
            },
        },
        "required": ["request_key", "repository", "revision", "instruction", "sources"],
    },
}

ENGINEERING_STATUS = {
    "name": "goncloud_engineering_status",
    "description": "Read the durable native-Kanban status of one Goncloud engineering workflow.",
    "parameters": {
        "type": "object",
        "properties": {"workflow_id": {"type": "string"}},
        "required": ["workflow_id"],
    },
}

CONSULT = {
    "name": "goncloud_consult",
    "description": (
        "Ask another Goncloud role for a durable peer consultation tied to the current native "
        "Kanban task. The source task is dependency-gated until the consultation completes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Source native Kanban task id.",
            },
            "target_role": {"type": "string", "enum": list(TEAM_ROLES)},
            "question": {"type": "string"},
            "request_key": {
                "type": "string",
                "description": "Stable per-source consultation key.",
            },
        },
        "required": ["task_id", "target_role", "question", "request_key"],
    },
}

ENGINEERING_REVIEW = {
    "name": "goncloud_engineering_review",
    "description": (
        "Record the reviewer decision. Approval releases entrega through native dependencies. "
        "Only a first-round high/critical rejection creates one corrective round; a second "
        "rejection or lower-severity unresolved finding blocks delivery. No third round exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_id": {"type": "string"},
            "task_id": {
                "type": "string",
                "description": "The active reviewer-stage task id.",
            },
            "decision": {"type": "string", "enum": ["approve", "reject"]},
            "summary": {
                "type": "string",
                "description": "Evidence-backed adjudication.",
            },
            "severity": {
                "type": "string",
                "enum": ["low", "medium", "high", "critical"],
                "description": "Required when decision is reject.",
            },
        },
        "required": ["workflow_id", "task_id", "decision", "summary"],
    },
}

MUSE_DELEGATE = {
    "name": "goncloud_muse_delegate",
    "description": (
        "Delegate a bounded specialist task to the owner's authenticated Muse CLI. "
        "Use this to obtain implementation, analysis, or a second technical opinion "
        "without asking the owner to relay messages between agents."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Complete self-contained assignment for Muse.",
            },
            "workspace": {
                "type": "string",
                "description": "Absolute existing Windows workspace path.",
            },
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 500,
                "default": 80,
            },
        },
        "required": ["prompt", "workspace"],
    },
}
