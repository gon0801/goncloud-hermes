# Telegram Transport Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner start the already proven local orchestration from Telegram, as a transport only, without reintroducing native Hermes model selection or a second routing policy.

**Architecture:** The Telegram gateway submits the same bounded request that `scripts/run_local_workflow.py` submits today and reports the durable status the orchestrator returns. `model-routing.source.json` remains the only routing source and the subscription router remains the only authority for role execution.

**Tech Stack:** Python 3.11, Hermes Agent v0.21.x gateway and Kanban APIs, the existing `local_orchestrator` boundary, SQLite, PowerShell, `unittest`, pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-14-telegram-transport-cutover-design.md`

**Precondition:** Task 9 of `2026-09-14-subscription-router-recovery.md` passed, including the complete local engineering chain with reviewer approval. Do not start this plan from a NO-GO.

## Global Constraints

- Telegram never selects a model, a worker, or a fallback. It carries a request and returns a status.
- No Goncloud profile regains a native `model` or `fallback_providers` chain, and Copilot stays excluded.
- The owner allowlist is the authorization boundary; a message from any other id is ignored and recorded, never queued.
- The same bounds apply as locally: `max_steps` within range and the existing instruction size limit. An oversized or unparseable message is refused, not silently reshaped.
- No new durable store. A Telegram message starts or resumes a workflow in the existing Kanban board and `subscription-router.db`.
- Never put a token, chat id, account identifier, prompt, or raw vendor error in Git, logs, Kanban, or a command argument.
- Every production change begins with a regression test observed failing for the expected reason.
- The acceptance test runs against a disposable Git repository, never a real project.

---

### Task 1: Bind the gateway command to the local orchestration boundary

**Files:**
- Modify: `plugins/goncloud-team/workflow.py`
- Modify: `plugins/goncloud-team/local_orchestrator.py`
- Modify: `tests/test_local_orchestrator.py`

**Interfaces:**
- Consumes: the bounded request shape already accepted by `LocalOrchestrator.start` / `.resume` / `.status`.
- Produces: one gateway-facing entry point that validates and forwards that exact shape, and refuses anything else.

- [x] **Step 1: Write failing tests for request translation and refusal**

Cover: a well-formed allowlisted message becomes exactly the local request; an out-of-range `max_steps` is refused; an oversized instruction is refused; a message from a non-allowlisted id produces no workflow at all.

- [x] **Step 2: Confirm RED**

- [x] **Step 3: Implement the translation and refusal path**

Reuse the existing validation rather than restating it, so the transport cannot accept a request the local entry point would reject.

- [x] **Step 4: Verify and commit**

Done as `goncloud_local_run` / `goncloud_local_status`: `LocalOrchestrator.create` registers the chain with the CLI's own bounds and runs no stage; `launch_detached` drives it in a detached process with only identifiers in argv and the bounded result in a private file; only the `claw` profile may call it. The allowlist itself stays the gateway's native one.

---

### Task 2: Prove no routing authority moved to the transport

**Files:**
- Modify: `tests/test_configuration.py`
- Modify: `tests/test_subscription_configuration.py`

**Interfaces:**
- Consumes: deployed profile `config.yaml` and `goncloud-managed.json`.
- Produces: a regression that fails if a native chain, a model picker, or a Copilot route reappears once the gateway is enabled.

- [x] **Step 1: Write the failing guard**

Assert that enabling the gateway changes no routing-bearing key in any of the nine profiles.

- [x] **Step 2: Confirm RED, implement, verify, commit**

Two guards: `kanban.dispatch_in_gateway` is `false` in every profile, so the gateway's own dispatcher never claims a stage card (it claims any `ready` card with an assignee and would run it with a native agent outside the router); and only the director carries one owner-chosen native model (`team.json` `director_model`), owned whole and never deep-merged, while workers carry none and no profile carries a native fallback chain. The director needed that model to converse at all: the global Hermes config routes everything to OpenRouter without a key.

---

### Task 3: One harmless bidirectional acceptance test, then the decision

**Files:**
- Modify: `docs/status.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: the preserved `claw` gateway profile, token, allowlist, service script, and Startup entry.
- Produces: a go/no-go record for Telegram, with the gateway either running and proven or returned to stopped.

- [x] **Step 1: Run the focused suites and the deployment verification**

Expected: focused suites pass, nine profiles verified, `git diff --check` clean.

Observed: 170 focused tests and the full battery (354, 3 platform skips) pass; nine profiles verified byte-identical with the director model and dispatch off; Plugin Doctor 9 tools, 1 hook; the director answered `GONCLOUD_DIRECTOR_OK` with its profile model and no flags.

- [x] **Step 2: Start the gateway and confirm deep status**

Expected: `hermes -p claw gateway status --deep --full` reports a running, healthy gateway. Record the state it reports.

Observed: started from a shell carrying `NODE_EXTRA_CA_CERTS`, `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE`; PID 42880, `gateway_state.json` `running`, six deep probes PASS.

- [x] **Step 3: Send one allowlisted message against a disposable repository**

Create a throwaway Git repository, send a bounded read-only inspection assignment with `--stop-after scout` semantics from the allowlisted owner id, and read the durable status back in the same chat.

Expected: exactly one workflow created, the scout stage executed through the router with slot/adapter/model/billing recorded, the disposable repository unchanged, and the status returned to the chat. Record the workflow id.

Observed (fourth owner message, after `/new`): workflow `t_4fe237e6` on `C:\dev\prueba-telegram`; one router attempt, slot 0 `opencode_go` / `deepseek-v4.1-flash` (subscription), completed, no failure, zero leases left; driver outcome `paused` with scout `done` and every later stage untouched; repository clean at `248aad1`; the director used only `goncloud_local_run` and `goncloud_local_status` and reported the two files back in the chat. Three earlier messages failed for reasons fixed on the way (`306bb8a`, `f0ea38e`, `e03fadd`; see `docs/status.md`).

- [ ] **Step 4: Confirm a non-allowlisted id creates nothing**

Expected: no workflow, no queued work, and a recorded refusal.

Not exercised: the owner has a single Telegram identity available, so no message from a second id could be sent. The allowlist is the gateway's native one and was not changed by this plan; this step stays open until a second id is available.

- [x] **Step 5: Run the final gates**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest discover -s tests
python -m pre_commit run --all-files
git diff --check
$hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe'
& $hermes -p claw plugins doctor goncloud-team --ci
& $hermes -p claw gateway status --deep --full
```

Observed: 361 tests (358 passed, 3 platform skips), pre-commit 8/8, `git diff --check` clean, Plugin Doctor 9 tools and 1 hook, gateway `running` with six deep probes PASS.

- [x] **Step 6: Record the decision**

On success, replace `Telegram: stopped pending separate transport cutover` with the proven state, the workflow id, and the exact acceptance evidence. On failure, stop the gateway, leave that line as it is, and record what failed without attributing a cause you did not observe.

Decision: GO. The gateway stays running; `docs/status.md` and `README.md` carry the proven state and `t_4fe237e6`. Open item: step 4 (second Telegram id).
