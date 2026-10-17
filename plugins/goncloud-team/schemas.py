"""Model-visible tool contracts for the Goncloud team plugin."""

from __future__ import annotations

from .workflow import INITIAL_STAGES, TEAM_ROLES


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

LOCAL_RUN = {
    "name": "goncloud_local_run",
    "description": (
        "Start technical work that arrived over a messaging transport. Only the claw "
        "profile may call this. Creates the durable Goncloud chain and leaves it running "
        "in the background through the subscription router; returns at once with the "
        "workflow_id. Never selects models or workers. Reuse request_key on retries."
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
                "description": "Exact revision every stage must inspect; defaults to HEAD.",
            },
            "instruction": {
                "type": "string",
                "description": "The owner-approved bounded assignment.",
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
            "max_steps": {
                "type": "integer",
                "description": "Autonomous step bound per stage (1-200, default 20).",
            },
            "stop_after": {
                "type": "string",
                "enum": list(INITIAL_STAGES),
                "description": (
                    'Stop after this stage. Use "scout" for read-only questions and '
                    "inspections; omit only for an owner-authorized change that needs "
                    "the full chain."
                ),
            },
            "affects_business_rules": {"type": "boolean"},
        },
        "required": ["request_key", "repository", "instruction", "sources"],
    },
}

LOCAL_STATUS = {
    "name": "goncloud_local_status",
    "description": (
        "Read the bounded durable status of a workflow started by goncloud_local_run, "
        "including per-stage attempts and, once the background driver finishes, its outcome."
    ),
    "parameters": {
        "type": "object",
        "properties": {"workflow_id": {"type": "string"}},
        "required": ["workflow_id"],
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

SUBSCRIPTION_RUN = {
    "name": "goncloud_subscription_run",
    "description": (
        "Execute the current native stage through the owner's subscription router "
        "(Primary OpenCode Go, F1-F6 by model family; DeepSeek is "
        "the only API-billed exception). The calling profile is bound from the "
        "Hermes context and can never be supplied as an argument. The handoff "
        "records the adapter, slot, model, and billing mode actually used."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "Goncloud engineering workflow root id.",
            },
            "task_id": {
                "type": "string",
                "description": "The active native Kanban stage task id.",
            },
            "instruction": {
                "type": "string",
                "description": "Complete self-contained stage assignment.",
            },
            "repository": {
                "type": "string",
                "description": (
                    "Absolute repository path; must match the stage workspace."
                ),
            },
            "revision": {
                "type": "string",
                "description": "Exact owner-approved baseline revision.",
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
                "description": "Dated provenance records supporting the stage.",
            },
            "max_steps": {
                "type": "integer",
                "minimum": 1,
                "maximum": 200,
                "description": "Bounded step ceiling for the subscription worker.",
            },
        },
        "required": [
            "workflow_id",
            "task_id",
            "instruction",
            "repository",
            "revision",
            "sources",
            "max_steps",
        ],
    },
}

SUBSCRIPTION_STATUS = {
    "name": "goncloud_subscription_status",
    "description": (
        "Read the non-generative subscription route matrix for the calling "
        "profile: slot, adapter, model, billing mode, and cached adapter health. "
        "Optionally include the durable attempt evidence of one workflow stage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "Optional workflow root id for stage evidence.",
            },
            "stage_id": {
                "type": "string",
                "description": "Optional stage task id for attempt evidence.",
            },
        },
        "required": [],
    },
}

ROOT_SUBMIT = {
    "name": "goncloud_root_submit",
    "description": (
        "Open a durable managed root from a sistema-agentes.meta.v1 plan "
        "document. Only the claw profile may call this. Retries with the same "
        "rootId and planDigest return the original receipt; a different plan "
        "under the same rootId is refused."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "object",
                "description": (
                    "Complete sistema-agentes.meta.v1 document; validated "
                    "against the fixed copy of the contract."
                ),
            },
        },
        "required": ["plan"],
    },
}

ROOT_READ = {
    "name": "goncloud_root_read",
    "description": (
        "Read the snapshot of one managed root: meta.v1, units (unidad.v1), "
        "open exceptions and control state, with the contract field names. "
        "Only the claw profile may call this."
    ),
    "parameters": {
        "type": "object",
        "properties": {"rootId": {"type": "string", "description": "Root id."}},
        "required": ["rootId"],
    },
}

ROOT_CONTROL = {
    "name": "goncloud_root_control",
    "description": (
        "Send a sistema-agentes.control.v1 command (pause, resume, cancel) to "
        "one managed root and get its receipt; an exact retry returns the "
        "original receipt with replayed=true. Only the claw profile may call this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rootId": {"type": "string", "description": "Root id."},
            "command": {
                "type": "string",
                "enum": ["pause", "resume", "cancel"],
                "description": "Control command.",
            },
            "commandId": {
                "type": "string",
                "description": "Stable id of this command; reuse it on retries.",
            },
            "expectedControlEpoch": {
                "type": "integer",
                "minimum": 1,
                "description": "Epoch the caller believes is current.",
            },
            "reason": {
                "type": "string",
                "description": "Optional owner-facing reason.",
            },
        },
        "required": ["rootId", "command", "commandId", "expectedControlEpoch"],
    },
}

ROOT_RESOLVE = {
    "name": "goncloud_root_resolve",
    "description": (
        "Answer a raised excepcion.v1 with the owner's chosen optionId; the "
        "answer is stored once and a second answer is refused. Only the claw "
        "profile may call this."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "rootId": {
                "type": "string",
                "description": "Root the exception belongs to.",
            },
            "exceptionId": {"type": "string", "description": "Exception id."},
            "optionId": {
                "type": "string",
                "description": "One of the options the exception offered.",
            },
            "by": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": (
                    'Principal that answers, for example ["telegram", "owner-01"].'
                ),
            },
        },
        "required": ["rootId", "exceptionId", "optionId", "by"],
    },
}
