"""Durable Goncloud engineering workflow on Hermes' native Kanban kernel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli.kanban_db import (
    add_comment,
    block_task,
    complete_task,
    create_task,
    get_task,
    link_tasks,
    list_comments,
    list_events,
    list_runs,
    list_tasks,
    parent_ids,
)
from hermes_cli.kanban_db_connect import connect_closing, write_txn
from hermes_cli.kanban_db_graph import decompose_triage_task


TEAM_ROLES = (
    "claw",
    "operaciones",
    "ingenieria",
    "scout",
    "implementer",
    "verifier",
    "adversary",
    "reviewer",
    "entrega",
)
INITIAL_STAGES = (
    "scout",
    "ingenieria",
    "implementer",
    "verifier",
    "adversary",
    "reviewer",
    "entrega",
)
CORRECTIVE_STAGES = ("implementer", "verifier", "adversary", "reviewer")
HIGH_SEVERITIES = {"high", "critical"}

_WORKFLOW_KEY_PREFIX = "goncloud-workflow:"
_CONTRACT_MARKER = "GONCLOUD_ENGINEERING_V1\n"
_REVIEW_MARKER = "GONCLOUD_REVIEW_V1\n"


class WorkflowError(ValueError):
    """A rejected workflow operation safe to return through a model tool."""


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise WorkflowError(f"{field} is required")
    return text


def _contract_body(contract: dict[str, Any]) -> str:
    return _CONTRACT_MARKER + json.dumps(
        contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _task_view(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "assignee": task.assignee,
        "status": task.status,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "result": task.result,
    }


class WorkflowService:
    """Role-bound adapter over native Kanban APIs.

    ``actor_role`` is supplied by the Hermes ``PluginContext`` at registration
    time. It is an application authorization boundary for a trusted local
    Hermes profile, not OS-level isolation.
    """

    def __init__(self, *, actor_role: str, db_path: Optional[Path] = None) -> None:
        self.actor_role = str(actor_role or "").strip()
        self.db_path = Path(db_path) if db_path is not None else None

    def _connect(self):
        return connect_closing(self.db_path)

    def _require_known_actor(self) -> None:
        if self.actor_role not in TEAM_ROLES:
            raise WorkflowError(
                f"unknown Goncloud role: {self.actor_role or '<empty>'}"
            )

    def _require_actor(self, expected: str) -> None:
        self._require_known_actor()
        if self.actor_role != expected:
            raise WorkflowError(f"this operation requires role {expected}")

    def start(
        self,
        *,
        request_key: str,
        repository: str,
        revision: str,
        instruction: str,
        sources: Iterable[dict[str, Any]],
        affects_business_rules: bool = False,
    ) -> dict[str, Any]:
        """Create the initial chain atomically, or return its existing IDs."""
        self._require_actor("claw")
        request_key = _required_text(request_key, "request_key")
        revision = _required_text(revision, "revision")
        instruction = _required_text(instruction, "instruction")
        repo_input = _required_text(repository, "repository")
        repo = Path(repo_input).expanduser()
        if not repo.is_absolute():
            raise WorkflowError("repository must be an absolute path")
        try:
            repo = repo.resolve(strict=True)
        except OSError as exc:
            raise WorkflowError(f"repository is unavailable: {exc}") from exc
        if not repo.is_dir():
            raise WorkflowError("repository must be an existing directory")

        source_rows = self._validate_sources(sources)
        contract = {
            "request_key": request_key,
            "repository": str(repo),
            "revision": revision,
            "instruction": instruction,
            "sources": source_rows,
            "affects_business_rules": bool(affects_business_rules),
        }
        body = _contract_body(contract)
        idempotency_key = _WORKFLOW_KEY_PREFIX + request_key

        with self._connect() as conn:
            # BEGIN IMMEDIATE makes the same-repository check and root insert a
            # single native DB decision across concurrent gateway processes.
            # create_task explicitly supports nested composition via savepoint.
            with write_txn(conn):
                for active in list_tasks(conn, include_archived=True):
                    if not (active.body or "").startswith(_CONTRACT_MARKER):
                        continue
                    try:
                        active_repo = (
                            Path(active.workspace_path or "").expanduser().resolve()
                        )
                    except OSError:
                        continue
                    if active.workspace_kind != "dir" or active_repo != repo:
                        continue
                    if active.status in {"done", "archived"}:
                        continue
                    if active.idempotency_key != idempotency_key:
                        raise WorkflowError(
                            f"repository already has active Goncloud workflow {active.id}; "
                            "wait for its director root to finish"
                        )
                root_id = create_task(
                    conn,
                    title=f"Engineering: {instruction[:120]}",
                    body=body,
                    assignee="claw",
                    created_by=self.actor_role,
                    workspace_kind="dir",
                    workspace_path=str(repo),
                    triage=True,
                    idempotency_key=idempotency_key,
                )
            root = get_task(conn, root_id)
            if root is None:
                raise WorkflowError("native Kanban did not persist the workflow root")
            if root.body != body:
                raise WorkflowError(
                    "request_key already belongs to a different contract; use a new request_key"
                )

            child_ids = self._decomposed_child_ids(conn, root_id)
            if child_ids is None:
                child_ids = decompose_triage_task(
                    conn,
                    root_id,
                    root_assignee="claw",
                    children=self._initial_children(root_id, contract),
                    author=self.actor_role,
                )
                if child_ids is None:
                    child_ids = self._decomposed_child_ids(conn, root_id)
            if child_ids is None or len(child_ids) != len(INITIAL_STAGES):
                raise WorkflowError(
                    "workflow root exists without a complete native stage graph"
                )
            return self._start_result(root_id, child_ids)

    @staticmethod
    def _validate_sources(sources: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
        if isinstance(sources, (str, bytes, dict)):
            raise WorkflowError("sources must be a non-empty list of source records")
        try:
            raw_rows = list(sources)
        except TypeError as exc:
            raise WorkflowError(
                "sources must be a non-empty list of source records"
            ) from exc
        if not raw_rows:
            raise WorkflowError("sources must contain at least one source record")
        rows: list[dict[str, str]] = []
        for index, row in enumerate(raw_rows):
            if not isinstance(row, dict):
                raise WorkflowError(f"sources[{index}] must be an object")
            rows.append(
                {
                    "reference": _required_text(
                        row.get("reference"), f"sources[{index}].reference"
                    ),
                    "observed_at": _required_text(
                        row.get("observed_at"), f"sources[{index}].observed_at"
                    ),
                }
            )
        return rows

    @staticmethod
    def _initial_children(
        root_id: str, contract: dict[str, Any]
    ) -> list[dict[str, Any]]:
        common = (
            f"Goncloud engineering workflow {root_id}. Work only in the exact repository "
            f"{contract['repository']}. The owner-approved baseline revision is "
            f"{contract['revision']}. Preserve the shared workspace and record the exact current "
            "revision (or dirty-tree state) in every native Kanban handoff so downstream roles "
            "verify the artifact actually produced. "
        )
        stage_directions = {
            "scout": "Investigate the code and constraints; report evidence, do not implement.",
            "ingenieria": "Produce the technical plan and reconcile peer consultation findings.",
            "implementer": "Implement the approved change and its regression tests.",
            "verifier": (
                "Verify the requested behavior with fresh evidence. Also perform business-rule "
                "acceptance."
                if contract["affects_business_rules"]
                else "Verify the requested behavior with fresh evidence."
            ),
            "adversary": "Find risks and counterexamples; report findings and do not fix them.",
            "reviewer": (
                "Adjudicate findings with goncloud_engineering_review. A first high/critical "
                "rejection may open one corrective review; other unresolved material findings block."
            ),
            "entrega": (
                "Package the verified handoff. Do not deliver unless every dependency and review "
                "gate is complete; do not expand deployment or external-send authority."
            ),
        }
        children = []
        for index, stage in enumerate(INITIAL_STAGES):
            children.append(
                {
                    "title": f"[{stage}] {contract['instruction'][:120]}",
                    "body": common + stage_directions[stage],
                    "assignee": stage,
                    "parents": [] if index == 0 else [index - 1],
                }
            )
        return children

    @staticmethod
    def _decomposed_child_ids(conn: Any, root_id: str) -> Optional[list[str]]:
        for event in list_events(conn, root_id):
            if event.kind != "decomposed" or not isinstance(event.payload, dict):
                continue
            ids = event.payload.get("child_ids")
            if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                return ids
        return None

    @staticmethod
    def _start_result(root_id: str, child_ids: list[str]) -> dict[str, Any]:
        return {
            "workflow_id": root_id,
            "stages": dict(zip(INITIAL_STAGES, child_ids)),
        }

    def status(self, *, workflow_id: str) -> dict[str, Any]:
        """Return compact durable state without changing the board."""
        self._require_known_actor()
        workflow_id = _required_text(workflow_id, "workflow_id")
        with self._connect() as conn:
            root = self._require_workflow_root(conn, workflow_id)
            initial_ids = self._decomposed_child_ids(conn, workflow_id)
            if initial_ids is None or len(initial_ids) != len(INITIAL_STAGES):
                raise WorkflowError("workflow stage graph is incomplete")
            initial = {
                stage: _task_view(self._require_task(conn, task_id))
                for stage, task_id in zip(INITIAL_STAGES, initial_ids)
            }
            corrective_ids = self._correction_ids(conn, workflow_id)
            corrective = {
                stage: _task_view(self._require_task(conn, task_id))
                for stage, task_id in corrective_ids.items()
            }
            return {
                "workflow_id": workflow_id,
                "root": _task_view(root),
                "stages": initial,
                "corrective_stages": corrective,
                "delivery_blocked": initial["entrega"]["status"]
                not in {"ready", "running", "done"},
            }

    def consult(
        self, *, task_id: str, target_role: str, question: str, request_key: str
    ) -> dict[str, Any]:
        """Create one durable peer card and gate the source until it completes."""
        self._require_known_actor()
        task_id = _required_text(task_id, "task_id")
        target_role = _required_text(target_role, "target_role")
        question = _required_text(question, "question")
        request_key = _required_text(request_key, "request_key")
        if target_role not in TEAM_ROLES:
            raise WorkflowError(f"unknown consultation target role: {target_role}")
        if target_role == self.actor_role:
            raise WorkflowError("consultation target must be a different role")

        idempotency_key = f"goncloud-consult:{task_id}:{request_key}"
        body = (
            f"Peer consultation requested by {self.actor_role} for native task {task_id}.\n"
            f"Question: {question}\n"
            "Complete this consultation with a concise evidence-backed summary; the source task "
            "is dependency-gated on this card."
        )
        with self._connect() as conn:
            source = self._require_task(conn, task_id)
            if source.assignee != self.actor_role:
                raise WorkflowError(
                    "the bound role may consult only for a task assigned to it"
                )
            if source.status not in {"ready", "running", "todo"}:
                raise WorkflowError(
                    f"task {task_id} cannot consult from status {source.status}"
                )
            consultation_id = create_task(
                conn,
                title=f"Consult {target_role}: {question[:100]}",
                body=body,
                assignee=target_role,
                created_by=self.actor_role,
                workspace_kind=source.workspace_kind,
                workspace_path=source.workspace_path,
                tenant=source.tenant,
                idempotency_key=idempotency_key,
                creator_task_id=task_id,
            )
            consultation = self._require_task(conn, consultation_id)
            if consultation.body != body or consultation.assignee != target_role:
                raise WorkflowError(
                    "consultation request_key already belongs to a different consultation"
                )
            if consultation_id not in parent_ids(conn, task_id):
                link_tasks(conn, consultation_id, task_id)
            source = self._require_task(conn, task_id)
            if source.status == "running":
                block_task(
                    conn,
                    task_id,
                    reason=f"waiting for consultation {consultation_id}",
                    kind="dependency",
                )
            return {
                "source_task_id": task_id,
                "consultation_task_id": consultation_id,
                "target_role": target_role,
                "source_status": self._require_task(conn, task_id).status,
            }

    def guard_native_completion(self, task_id: str) -> Optional[dict[str, str]]:
        """Veto a reviewer card bypassing the durable adjudication tool.

        This is consumed by the plugin's ``pre_tool_call`` hook. Other roles
        and ordinary Kanban cards are deliberately outside this policy.
        """
        if self.actor_role != "reviewer":
            return None
        task_id = _required_text(task_id, "task_id")
        with self._connect() as conn:
            task = get_task(conn, task_id)
            if task is None or task.assignee != "reviewer":
                return None
            workflow_id = self._workflow_for_reviewer_task(conn, task_id)
            if workflow_id is None:
                return None
            return {
                "action": "block",
                "message": (
                    "Goncloud reviewer cards cannot use kanban_complete directly. "
                    "Adjudicate with goncloud_engineering_review."
                ),
            }

    def review(
        self,
        *,
        workflow_id: str,
        task_id: str,
        decision: str,
        summary: str,
        severity: Optional[str] = None,
    ) -> dict[str, Any]:
        """Adjudicate review and enforce the hard two-round ceiling."""
        self._require_actor("reviewer")
        workflow_id = _required_text(workflow_id, "workflow_id")
        task_id = _required_text(task_id, "task_id")
        decision = _required_text(decision, "decision").lower()
        summary = _required_text(summary, "summary")
        if decision not in {"approve", "reject"}:
            raise WorkflowError("decision must be approve or reject")
        severity_value = str(severity or "").strip().lower() or None
        if decision == "reject" and severity_value not in {
            "low",
            "medium",
            "high",
            "critical",
        }:
            raise WorkflowError(
                "a rejection requires severity low, medium, high, or critical"
            )

        with self._connect() as conn:
            root = self._require_workflow_root(conn, workflow_id)
            initial_ids = self._decomposed_child_ids(conn, workflow_id)
            if initial_ids is None or len(initial_ids) != len(INITIAL_STAGES):
                raise WorkflowError("workflow stage graph is incomplete")
            initial = dict(zip(INITIAL_STAGES, initial_ids))
            correction = self._correction_ids(conn, workflow_id)
            if task_id == initial["reviewer"]:
                round_number = 1
            elif correction.get("reviewer") == task_id:
                round_number = 2
            else:
                raise WorkflowError("task_id is not a reviewer stage in this workflow")
            task = self._require_task(conn, task_id)
            if task.assignee != "reviewer":
                raise WorkflowError("review task is not assigned to role reviewer")

            requested = {
                "decision": decision,
                "severity": severity_value,
                "summary": summary,
                "round": round_number,
            }
            existing = self._recorded_review_run(conn, task_id)
            if existing is not None:
                recorded_request = {key: existing.get(key) for key in requested}
                if recorded_request != requested:
                    raise WorkflowError(
                        "this review task already has a different durable decision"
                    )
                result = existing.get("result")
                if not isinstance(result, dict):
                    raise WorkflowError("native review run metadata is malformed")
                return result

            if task.status == "done":
                raise WorkflowError(
                    "review task completed without Goncloud review metadata; delivery must be audited"
                )
            if task.status == "blocked":
                if decision != "reject":
                    raise WorkflowError(
                        "a blocked rejected review cannot be changed to approval"
                    )
                return {
                    "outcome": "blocked",
                    "round": round_number,
                    "task_id": task_id,
                    "reason": "unresolved material review finding",
                }
            if task.status not in {"ready", "running"}:
                raise WorkflowError(
                    f"review task dependencies are incomplete or task is closed ({task.status})"
                )

            if (
                round_number == 1
                and correction
                and not (decision == "reject" and severity_value in HIGH_SEVERITIES)
            ):
                raise WorkflowError(
                    "corrective round already exists; the first high-severity rejection is durable"
                )

            if decision == "approve":
                result = {
                    "outcome": "approved",
                    "round": round_number,
                    "task_id": task_id,
                }
            elif round_number == 1 and severity_value in HIGH_SEVERITIES:
                correction = self._ensure_corrective_round(
                    conn, root, initial, finding=summary, severity=severity_value
                )
                result = {
                    "outcome": "corrective_round_created",
                    "round": 1,
                    "task_id": task_id,
                    "corrective_stages": correction,
                }
            else:
                result = {
                    "outcome": "blocked",
                    "round": round_number,
                    "task_id": task_id,
                    "reason": "unresolved material review finding",
                }

            record = {**requested, "task_id": task_id, "result": result}
            self._add_review_audit(conn, workflow_id, record)
            self._finish_recorded_review(conn, task_id, record)
            return result

    @staticmethod
    def _finish_recorded_review(
        conn: Any, task_id: str, record: dict[str, Any]
    ) -> None:
        task = get_task(conn, task_id)
        if task is None:
            raise WorkflowError(f"unknown task {task_id}")
        result = record["result"]
        summary = record["summary"]
        outcome = result.get("outcome")
        if outcome == "blocked":
            if task.status == "blocked":
                return
            if not block_task(conn, task_id, reason=summary, kind="needs_input"):
                raise WorkflowError(
                    "native Kanban refused to block the rejected review"
                )
            return
        if task.status == "done":
            return
        metadata = {"goncloud_review": record}
        if not complete_task(
            conn,
            task_id,
            result=(
                "review approved"
                if outcome == "approved"
                else "review rejected; correction opened"
            ),
            summary=summary,
            metadata=metadata,
            expected_run_id=task.current_run_id,
        ):
            raise WorkflowError("native Kanban refused the review transition")

    def _ensure_corrective_round(
        self,
        conn: Any,
        root: Any,
        initial: dict[str, str],
        *,
        finding: str,
        severity: str,
    ) -> dict[str, str]:
        ids: dict[str, str] = {}
        parent = initial["reviewer"]
        for stage in CORRECTIVE_STAGES:
            key = f"{_WORKFLOW_KEY_PREFIX}{root.id}:round2:{stage}"
            body = (
                f"Corrective review round 2 for workflow {root.id}. Severity: {severity}.\n"
                f"Finding: {finding}\n"
                f"Use the exact shared repository {root.workspace_path}, and record the exact current "
                "revision or dirty-tree state in the handoff. This is the final permitted review "
                "round; unresolved material findings must block entrega."
            )
            task_id = create_task(
                conn,
                title=f"[round 2 {stage}] {root.title[:100]}",
                body=body,
                assignee=stage,
                created_by="reviewer",
                workspace_kind=root.workspace_kind,
                workspace_path=root.workspace_path,
                tenant=root.tenant,
                parents=(parent,),
                idempotency_key=key,
                creator_task_id=root.id,
            )
            task = self._require_task(conn, task_id)
            if task.body != body or task.assignee != stage:
                raise WorkflowError(
                    "corrective stage idempotency key has conflicting content"
                )
            ids[stage] = task_id
            parent = task_id
        delivery_id = initial["entrega"]
        if ids["reviewer"] not in parent_ids(conn, delivery_id):
            link_tasks(conn, ids["reviewer"], delivery_id)
        return ids

    @staticmethod
    def _recorded_review_run(conn: Any, task_id: str) -> Optional[dict[str, Any]]:
        """Read plugin-produced native run metadata; comments are audit-only."""
        for run in reversed(list_runs(conn, task_id, include_active=False)):
            metadata = run.metadata
            if not isinstance(metadata, dict):
                continue
            record = metadata.get("goncloud_review")
            if isinstance(record, dict):
                return record
        return None

    @staticmethod
    def _add_review_audit(conn: Any, workflow_id: str, record: dict[str, Any]) -> None:
        body = _REVIEW_MARKER + json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if any(comment.body == body for comment in list_comments(conn, workflow_id)):
            return
        add_comment(conn, workflow_id, "reviewer", body)

    @staticmethod
    def _correction_ids(conn: Any, workflow_id: str) -> dict[str, str]:
        expected = {
            f"{_WORKFLOW_KEY_PREFIX}{workflow_id}:round2:{stage}": stage
            for stage in CORRECTIVE_STAGES
        }
        found: dict[str, str] = {}
        for task in list_tasks(conn, include_archived=True):
            stage = expected.get(task.idempotency_key or "")
            if stage:
                found[stage] = task.id
        return {stage: found[stage] for stage in CORRECTIVE_STAGES if stage in found}

    def _workflow_for_reviewer_task(
        self, conn: Any, reviewer_task_id: str
    ) -> Optional[str]:
        for candidate in list_tasks(conn, include_archived=True):
            if not (candidate.idempotency_key or "").startswith(_WORKFLOW_KEY_PREFIX):
                continue
            if not (candidate.body or "").startswith(_CONTRACT_MARKER):
                continue
            initial_ids = self._decomposed_child_ids(conn, candidate.id)
            if initial_ids and len(initial_ids) == len(INITIAL_STAGES):
                if (
                    dict(zip(INITIAL_STAGES, initial_ids))["reviewer"]
                    == reviewer_task_id
                ):
                    return candidate.id
            if (
                self._correction_ids(conn, candidate.id).get("reviewer")
                == reviewer_task_id
            ):
                return candidate.id
        return None

    @staticmethod
    def _require_task(conn: Any, task_id: str):
        task = get_task(conn, task_id)
        if task is None:
            raise WorkflowError(f"unknown task {task_id}")
        return task

    def _require_workflow_root(self, conn: Any, workflow_id: str):
        root = self._require_task(conn, workflow_id)
        if not (root.idempotency_key or "").startswith(_WORKFLOW_KEY_PREFIX):
            raise WorkflowError(
                f"task {workflow_id} is not a Goncloud engineering workflow"
            )
        if not (root.body or "").startswith(_CONTRACT_MARKER):
            raise WorkflowError(
                f"workflow {workflow_id} has no supported source contract"
            )
        return root
