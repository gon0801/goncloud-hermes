# Subscription Router Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover the existing Hermes team so every role executes the user's immutable Primary-to-F7 table through the correct subscriptions, proves the complete workflow locally, and keeps Telegram stopped until final acceptance.

**Architecture:** `model-routing.source.json` remains the only routing authority. Native Hermes model fallbacks are removed from Goncloud-managed profile configuration; a plugin-local router owns health gating, durable attempt ownership, continuation, and vendor invocation, while a local orchestration entry point drives the existing Kanban workflow without Telegram.

**Tech Stack:** Python 3.11, Hermes Agent v0.21.x plugin and Kanban APIs, SQLite, vendor CLIs, PowerShell, `unittest`, pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-14-subscription-router-recovery-design.md`

## Global Constraints

- Never modify the order or contents of `model-routing.source.json`.
- Slot 0 is always OpenCode Go and slot 1 is always OpenCode Zen.
- Slots 2 through 7 bind by model family to the owner's subscription; only DeepSeek is API-billed.
- `entrega` inherits `main`; `dup` remains a distinct attempt and is stripped only from the runtime model ID.
- No native Hermes fallback, emergency coordinator, or Copilot route may replace a table slot.
- Telegram stays stopped until local end-to-end acceptance and every final gate pass.
- Every production change begins with a regression test observed failing for the expected reason.
- Never put credentials, token prefixes, prompts, account identifiers, or raw vendor errors in Git, logs, Kanban, or command arguments.
- Do not run multiple live inference probes in parallel; verify accounts one at a time in table order.
- Preserve all unrelated staged and unstaged work already present in the repository.

---

### Task 1: Freeze Telegram and remove competing native routing

**Files:**
- Modify: `scripts/configure_team.py:13-59,151-232`
- Modify: `tests/test_configuration.py:20-120`
- Modify: `docs/status.md`

**Interfaces:**
- Consumes: existing `goncloud-managed.json`, profile `config.yaml`, and `model-routing.source.json`.
- Produces: `remove_owned_native_routing(existing: dict, previous: dict) -> dict`; deployment installs the routing source but never writes the eight slots to native `model` or `fallback_providers`.

- [x] **Step 1: Stop the Telegram gateway and record the reversible state**

Run:

```powershell
$hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe'
& $hermes -p claw gateway stop
& $hermes -p claw gateway status --deep --full
```

Expected: the process is stopped; the profile, token, allowlist, service script, and startup entry remain on disk.

- [x] **Step 2: Write failing configuration regressions**

Replace tests that expect `translate_model_chain()` to populate native fallbacks with these behaviors:

```python
def test_routing_source_is_installed_without_native_fallback_chain(self):
    existing = {
        "model": {"provider": "opencode-go", "default": "muse"},
        "fallback_providers": [{"provider": "opencode-zen", "model": "muse-free"}],
        "plugins": {"enabled": ["goncloud-team"]},
    }
    previous = {
        "model": existing["model"],
        "fallback_providers": existing["fallback_providers"],
    }
    cleaned = module.remove_owned_native_routing(existing, previous)
    self.assertNotIn("model", cleaned)
    self.assertNotIn("fallback_providers", cleaned)
    self.assertIn("plugins", cleaned)


def test_cleanup_refuses_to_delete_user_changed_model(self):
    previous = {"model": {"provider": "opencode-go", "default": "muse"}}
    existing = {"model": {"provider": "custom", "default": "user-choice"}}
    with self.assertRaisesRegex(ValueError, "user-managed model conflict"):
        module.remove_owned_native_routing(existing, previous)
```

- [x] **Step 3: Run the focused tests and confirm RED**

Run:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_configuration.ConfigurationTests.test_routing_source_is_installed_without_native_fallback_chain tests.test_configuration.ConfigurationTests.test_cleanup_refuses_to_delete_user_changed_model -v
```

Expected: both fail because `remove_owned_native_routing` does not exist and deployment still writes native fallbacks.

- [x] **Step 4: Implement owned-field cleanup and Copilot exclusion**

Add the exact ownership rule:

```python
def remove_owned_native_routing(existing, previous):
    cleaned = copy.deepcopy(existing)
    for key in ("model", "fallback_providers"):
        if key not in previous:
            continue
        if cleaned.get(key, MISSING) != previous[key]:
            raise ValueError(f"user-managed model conflict: {key}")
        cleaned.pop(key, None)
    return cleaned
```

In `configure()`, call this before `merge_profile_config`, stop calling `model_config_for_profile`, and always copy `model-routing.source.json` next to the plugin. Add the managed inventory exclusion:

```python
"model_catalog": {"excluded_providers": ["copilot", "copilot-acp"]},
```

Keep `--apply-model-routing` as a deprecated compatibility flag that installs the authoritative source but never creates native model fallbacks.

- [x] **Step 5: Verify configuration behavior in a temporary Hermes home**

Run:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_configuration -v
```

Expected: all configuration tests pass; generated profiles contain the plugin routing source, no generated `fallback_providers`, and explicit Copilot exclusions.

- [x] **Step 6: Document the frozen state and commit**

Add a short recovery-in-progress entry to `docs/status.md`, then run:

```powershell
git add scripts/configure_team.py tests/test_configuration.py docs/status.md
git commit -m "fix: separate Hermes profiles from worker routing"
```

---

### Task 2: Enforce health probes and quarantine before invocation

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/state.py:25-262`
- Modify: `plugins/goncloud-team/subscriptions/router.py:79-267`
- Modify: `tests/test_subscription_router.py`

**Interfaces:**
- Consumes: `SubscriptionAdapter.probe(route) -> AdapterHealth` and stored health rows.
- Produces: `SubscriptionStateStore.fresh_health(adapter: str, max_age_seconds: int) -> dict | None` and `SubscriptionRouter._availability(route: RouteSpec) -> AdapterHealth`; unavailable routes are recorded and skipped before `invoke`.

- [x] **Step 1: Write failing quarantine regressions**

```python
def test_quarantined_route_is_recorded_without_invocation(self):
    go = self.adapters["opencode_go"]
    go.health = AdapterHealth(False, "opencode_go", FailureKind.AUTH, "login required")
    self.adapters["opencode_zen"].result = completed("zen")

    result = self.router.run(request(), routes_for_main())

    self.assertEqual(result.adapter, "opencode_zen")
    self.assertEqual(go.calls, [])
    self.assertEqual(self.store.stage_attempts("wf", "stage")[0]["failure"], "auth")


def test_fresh_unready_health_skips_probe_and_invoke(self):
    self.store.save_health("opencode_go", ready=False, account_class="opencode_go", failure="quota", detail="quarantined")
    result = self.router.run(request(), routes_for_main())
    self.assertNotIn("opencode_go", self.router.calls)
    self.assertNotEqual(result.adapter, "opencode_go")
```

- [x] **Step 2: Run both tests and confirm RED**

Run the two named tests with the installed Hermes Python. Expected: OpenCode Go is invoked because `SubscriptionRouter.run` checks only cooldown.

- [x] **Step 3: Add freshness-aware health lookup**

Implement:

```python
def fresh_health(self, adapter: str, max_age_seconds: int) -> Optional[dict[str, Any]]:
    row = self.health(adapter)
    updated = _parse_iso(row.get("updated_at")) if row else None
    if updated is None or (_utcnow() - updated).total_seconds() > max_age_seconds:
        return None
    return row
```

Make `_attempt_view`/health serialization retain `updated_at` without exposing raw provider output.

- [x] **Step 4: Gate every route before invoking it**

Add a router `health_ttl_seconds=300` constructor argument. `_availability` uses fresh stored health or calls `adapter.probe(route)`, persists the normalized result, and returns it. In `run`, reserve a skipped attempt with the health failure and continue; never call `invoke` when `ready` is false.

- [x] **Step 5: Verify health and router tests**

Run:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_router -v
```

Expected: quarantine tests and existing slot/cooldown/status tests pass.

- [x] **Step 6: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/state.py plugins/goncloud-team/subscriptions/router.py tests/test_subscription_router.py
git commit -m "fix: enforce subscription route quarantine"
```

---

### Task 3: Reject false successes and advance recoverable transport failures

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/types.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/__init__.py:15-117`
- Modify: `plugins/goncloud-team/subscriptions/adapters/opencode.py:27-132`
- Modify: `plugins/goncloud-team/subscriptions/adapters/kimi.py:21-124`
- Modify: `plugins/goncloud-team/subscriptions/router.py:15-189`
- Modify: `tests/test_subscription_opencode.py`
- Modify: `tests/test_subscription_vendor_adapters.py`
- Modify: `tests/test_subscription_router.py`

**Interfaces:**
- Consumes: redacted `ProcessEvidence` and vendor JSONL terminal events.
- Produces: `FailureKind.TRANSPORT`; parsers return a `terminal_valid` flag; exit code zero without terminal evidence fails; pre-output transport errors advance to the next slot.

- [x] **Step 1: Write failing OpenCode and Kimi regressions**

```python
def test_exit_zero_without_terminal_event_is_failure(self):
    self.runner.result = ProcessEvidence(0, "", "", 0.1)
    result = self.adapter.invoke(request(), route(0, "opencode_go", "muse"))
    self.assertFalse(result.success)
    self.assertEqual(result.failure, FailureKind.TASK_FAILURE)


def test_kimi_connection_error_is_transport_failure(self):
    self.runner.result = ProcessEvidence(1, "", "provider.connection_error", 0.1)
    result = KimiAdapter(runner=self.runner).invoke(request(), route_for("kimi", "k3"))
    self.assertEqual(result.failure, FailureKind.TRANSPORT)
```

- [x] **Step 2: Confirm RED**

Run the named tests. Expected: empty OpenCode output is accepted and the Kimi error is classified as `task_failure`.

- [x] **Step 3: Implement strict terminal validation and transport classification**

Add `TRANSPORT = "transport"` to `FailureKind`; detect `connection_error`, connection reset, disconnected-before-response, DNS failure, and refused connection in `classify_failure`. Extend `_ADVANCING_FAILURES` with `TRANSPORT` only for attempts with no mutation.

Make `parse_opencode_jsonl` and `parse_kimi_jsonl` return `terminal_valid`. Success requires all three conditions:

```python
if evidence.returncode == 0 and parsed["terminal_valid"] and parsed["text"].strip():
    return InvocationResult(...)
```

- [x] **Step 4: Verify adapter and failover suites**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_opencode tests.test_subscription_vendor_adapters tests.test_subscription_router -v
```

Expected: malformed output blocks; Kimi transport failure advances; task failures still block.

- [x] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/types.py plugins/goncloud-team/subscriptions/adapters plugins/goncloud-team/subscriptions/router.py tests/test_subscription_opencode.py tests/test_subscription_vendor_adapters.py tests/test_subscription_router.py
git commit -m "fix: require terminal evidence from subscription CLIs"
```

---

### Task 4: Bound vendor processes and terminate their process trees

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/process.py:19-136`
- Modify: `plugins/goncloud-team/subscriptions/adapters/__init__.py:86-117`
- Modify: `scripts/configure_subscriptions.py:270-337`
- Modify: `tests/test_subscription_process.py`

**Interfaces:**
- Consumes: argv, stdin/private prompt file, workspace, environment overlay, and an integer deadline.
- Produces: `run_cli(...) -> ProcessEvidence` using a controlled `Popen` boundary; timeout kills the full child tree and returns within a five-second cleanup grace period.

- [x] **Step 1: Write a failing process-tree timeout regression**

Create a small test helper that starts a child process which starts a grandchild and waits. Assert:

```python
started = time.monotonic()
with self.assertRaisesRegex(SubscriptionProcessError, "timed out after 1s"):
    run_cli(command, "", self.root, timeout=1)
self.assertLess(time.monotonic() - started, 6)
self.assertFalse(grandchild_is_alive())
```

On Windows, use the test process PID recorded in a temporary file; never search or kill by executable name.

- [x] **Step 2: Confirm RED**

Run the named timeout test. Expected: the descendant survives or cleanup exceeds the five-second grace period.

- [x] **Step 3: Replace the real subprocess boundary**

Keep injected test runners compatible, but make the production path use `subprocess.Popen`. Start a new process group. On timeout, terminate only that PID's tree:

```python
if os.name == "nt":
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
        shell=False,
    )
else:
    os.killpg(process.pid, signal.SIGKILL)
```

Always delete the private prompt file in `finally`. Set the normal adapter maximum to 300 seconds and the probe maximum to 30 seconds; a caller may lower but not raise them.

- [x] **Step 4: Run the safe-process suite**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_process -v
```

Expected: process tree is gone, deadline is bounded, prompt files are deleted, and secrets stay out of command/result representations.

- [x] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/process.py plugins/goncloud-team/subscriptions/adapters/__init__.py scripts/configure_subscriptions.py tests/test_subscription_process.py
git commit -m "fix: bound subscription CLI process trees"
```

---

### Task 5: Make stage execution single-owner and continuation revision-aware

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/state.py:14-262`
- Modify: `plugins/goncloud-team/subscriptions/router.py:42-233`
- Modify: `tests/test_subscription_router.py`

**Interfaces:**
- Consumes: workflow/stage identity, workspace HEAD, dirty-file state, and recorded attempts.
- Produces: `claim_stage(workflow_id, stage_id, owner, lease_seconds) -> bool`, `release_stage(...)`, and `GitInspector.mutation_since(workspace, revision) -> tuple[str, tuple[str, ...], bool]`.

- [x] **Step 1: Write failing concurrency and clean-commit regressions**

```python
def test_concurrent_stage_runs_invoke_vendor_once(self):
    barrier = threading.Barrier(2)
    adapter = BlockingAdapter(barrier, completed("done"))
    router = self.router_with(adapter)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: router.run(request(), routes_for_main()), range(2)))
    self.assertEqual(adapter.invocation_count, 1)
    self.assertEqual(len(self.store.stage_attempts("wf", "stage")), 1)


def test_clean_commit_causes_continuation(self):
    start = self.git_head()
    self.commit_file("changed.py", "new")
    request_after = self.router._absorb_workspace_state(request(start_revision=start, current_revision=start))
    self.assertEqual(request_after.mode, "continue")
    self.assertIn("changed.py", request_after.changed_files)
```

- [x] **Step 2: Confirm RED**

Run both tests. Expected: two adapter calls are observed and a committed clean change remains `mode="start"`.

- [x] **Step 3: Add an atomic stage lease**

Add `subscription_stage_leases` with `(workflow_id, stage_id)` as the primary key, an opaque owner, and UTC expiry. `claim_stage` uses `BEGIN IMMEDIATE`, removes only expired leases, and inserts the caller atomically. A second live owner receives a normalized “stage already running” result without invoking an adapter. Always release in `finally`; expired leases support crash recovery.

- [x] **Step 4: Detect dirty and committed mutations**

Implement `mutation_since` using argument arrays and `shell=False`:

```python
current = self.current_revision(workspace)
committed = self._git(workspace, "diff", "--name-only", f"{start_revision}..{current}")
dirty = self.changed_files(workspace)
files = tuple(sorted(set(committed) | set(dirty)))
return current, files, bool(files or current != start_revision)
```

Persist the new revision even when the worktree is clean. Base continuation on the boolean mutation result, not only `changed_files`.

- [x] **Step 5: Verify concurrency, restart, and mutation tests**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_router -v
```

Expected: one invocation, one durable attempt, clean commits continue, and existing dirty-worktree/restart cases pass.

- [x] **Step 6: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/state.py plugins/goncloud-team/subscriptions/router.py tests/test_subscription_router.py
git commit -m "fix: serialize durable subscription stages"
```

---

### Task 6: Validate workflow identity before execution and enforce step limits

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/types.py:25-55`
- Modify: `plugins/goncloud-team/subscriptions/adapters/__init__.py:62-117`
- Modify: `plugins/goncloud-team/workflow.py:414-567`
- Modify: `tests/test_subscription_tools.py`

**Interfaces:**
- Consumes: workflow root, stage task, actor role, repository, revision, sources, and `max_steps`.
- Produces: `WorkflowService.validate_subscription_assignment(...) -> Path`; `InvocationRequest.max_steps: int`; all checks finish before `SubscriptionRouter.run`.

- [x] **Step 1: Write failing pre-execution validation tests**

Add a router whose `run` increments a counter. Assert that a stage from another workflow and a repository mismatch both raise `WorkflowError` while the counter remains zero:

```python
with self.assertRaisesRegex(WorkflowError, "not a stage of workflow"):
    service.subscription_run(**mismatched_payload)
self.assertEqual(router.run_count, 0)
```

Add a successful case asserting `request.max_steps == 17`.

- [x] **Step 2: Confirm RED**

Expected: the mismatched stage reaches `router.run`, and `InvocationRequest` has no `max_steps` field.

- [x] **Step 3: Extract and call validation before constructing the request**

Move workflow membership checks currently performed by `attach_subscription_result` into:

```python
def validate_subscription_assignment(
    self, *, workflow_id: str, task_id: str, repository: str, revision: str
) -> Path:
    ...
```

Call it before compiling routes or invoking the router. Keep `attach_subscription_result` defensive and idempotent.

- [x] **Step 4: Propagate the bounded step limit**

Add `max_steps: int` to `InvocationRequest`. `build_assignment_prompt` includes an explicit line `Maximum autonomous steps: N`; reject values outside `1..200`. Adapters that support a native step flag pass the value as a separate argv element; all others retain it in the bounded assignment contract.

- [x] **Step 5: Verify plugin integration tests**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_tools tests.test_workflow -v
```

Expected: every identity mismatch is rejected before router invocation and existing review gating still passes.

- [x] **Step 6: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/types.py plugins/goncloud-team/subscriptions/adapters/__init__.py plugins/goncloud-team/workflow.py tests/test_subscription_tools.py
git commit -m "fix: validate subscription stages before execution"
```

---

### Task 7: Add a durable local orchestration entry point

**Files:**
- Create: `plugins/goncloud-team/local_orchestrator.py`
- Create: `scripts/run_local_workflow.py`
- Create: `tests/test_local_orchestrator.py`
- Modify: `plugins/goncloud-team/README.md`

**Interfaces:**
- Consumes: repository, revision, instruction, sources, max steps, existing `WorkflowService`, and the subscription router.
- Produces: `LocalOrchestrator.start(...) -> dict`, `LocalOrchestrator.resume(workflow_id: str) -> dict`, and a JSON CLI with `start`, `resume`, and `status` subcommands. `start --stop-after <role>` provides a bounded single-role acceptance checkpoint.

- [x] **Step 1: Write a failing local-orchestrator contract test**

Use fake role runners and a temporary Kanban database:

```python
def test_local_chain_runs_exact_stage_order_and_reaches_delivery(self):
    orchestrator = LocalOrchestrator(service_factory=self.services, stage_runner=self.runner)
    result = orchestrator.start(
        request_key="acceptance-1",
        repository=self.repo,
        revision=self.revision,
        instruction="Inspect this repository without external actions.",
        sources=SOURCES,
        max_steps=20,
    )
    self.assertEqual(
        self.runner.roles,
        ["scout", "ingenieria", "implementer", "verifier", "adversary", "reviewer", "entrega"],
    )
    self.assertEqual(result["outcome"], "delivered")
```

Add cases where a blocked stage stops the loop and where a high-severity reviewer result creates exactly one corrective round.

- [x] **Step 2: Confirm RED**

Expected: import failure because `local_orchestrator.py` does not exist.

- [x] **Step 3: Implement the orchestration boundary**

`LocalOrchestrator` starts the existing native workflow, queries durable status, selects only dependency-ready stages, and calls `WorkflowService.subscription_run` under the stage's bound role. It never reorders `INITIAL_STAGES` and never invokes `entrega` before reviewer approval.

Require each stage's terminal text to contain a final JSON envelope:

```json
{"status":"completed","summary":"bounded result","evidence":["command or file evidence"],"review":null}
```

Reviewer envelopes use:

```json
{"status":"completed","summary":"review result","evidence":[],"review":{"decision":"approve","severity":null}}
```

Parse only the final fenced JSON object, cap its size, and pass reviewer decisions through `WorkflowService.review`; do not infer approval from exit code.

- [x] **Step 4: Implement the local CLI wrapper**

`scripts/run_local_workflow.py` loads the plugin directory without copying credentials and supports:

```powershell
python scripts/run_local_workflow.py start --repository C:\dev\target --instruction-file C:\path\instruction.txt --max-steps 20 --json
python scripts/run_local_workflow.py start --repository C:\dev\target --instruction-file C:\path\instruction.txt --max-steps 20 --stop-after scout --json
python scripts/run_local_workflow.py resume --workflow-id <id> --json
python scripts/run_local_workflow.py status --workflow-id <id> --json
```

The instruction is read from a file or stdin, never placed in argv output or logs.

- [x] **Step 5: Verify local orchestration tests**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_local_orchestrator tests.test_workflow tests.test_subscription_tools -v
```

Expected: exact stage order, durable resume, reviewer gating, one corrective round maximum, and delivery evidence all pass.

- [x] **Step 6: Commit**

```powershell
git add plugins/goncloud-team/local_orchestrator.py scripts/run_local_workflow.py tests/test_local_orchestrator.py plugins/goncloud-team/README.md
git commit -m "feat: add local subscription workflow runner"
```

---

### Task 8: Require real credential verification and sanitize evidence

**Files:**
- Modify: `scripts/configure_subscriptions.py:114-337`
- Modify: `plugins/goncloud-team/subscriptions/adapters/zai.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/deepseek.py`
- Modify: `tests/test_subscription_configuration.py`
- Modify: `tests/test_subscription_vendor_adapters.py`

**Interfaces:**
- Consumes: private replacement store and explicit owner-requested live probe.
- Produces: migration refuses destructive cleanup without verified minimal inference; redacted result contains adapter, status, billing class, and failure category only.

- [x] **Step 1: Write failing migration and redaction tests**

```python
def test_migration_refuses_launcher_cleanup_without_verified_inference(self):
    with self.assertRaisesRegex(MigrationError, "verified live probe required"):
        migrate_one(self.launcher, self.private_env, probe=None, secret_name="GLM_API_KEY")
    self.assertIn("ANTHROPIC_AUTH_TOKEN", self.launcher.read_text())


def test_live_result_redacts_token_prefix_and_account_identity(self):
    result = sanitize_live_result("glm", "token sk-sensitive email owner@example.test")
    self.assertNotIn("sk-sensitive", repr(result))
    self.assertNotIn("owner@example.test", repr(result))
```

- [x] **Step 2: Confirm RED**

Expected: the migration accepts a non-verifying callback and raw diagnostic text survives sanitization.

- [x] **Step 3: Make verified inference mandatory for cleanup**

Replace presence-only Z.AI verification with a minimal non-mutating inference whose expected exact text is `GONCLOUD_PROBE_OK`. Require `--verify` whenever `--migrate-launchers` is used. If the response is missing, different, timed out, or classified as auth/quota/transport failure, keep the launcher unchanged and return a redacted failure.

- [x] **Step 4: Add centralized evidence sanitization**

Create `sanitize_live_result(adapter: str, evidence: ProcessEvidence | str) -> dict` that returns only:

```python
{"adapter": adapter, "status": "ready|quarantined|failed", "failure": None, "billing_mode": "subscription|api"}
```

Never retain raw stdout/stderr in the live matrix or gateway logs.

- [x] **Step 5: Verify migration and vendor tests**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_subscription_configuration tests.test_subscription_vendor_adapters -v
```

Expected: failed verification preserves recoverable launchers; successful verification removes plaintext; no token prefix or identity appears in results.

- [x] **Step 6: Commit**

```powershell
git add scripts/configure_subscriptions.py plugins/goncloud-team/subscriptions/adapters/zai.py plugins/goncloud-team/subscriptions/adapters/deepseek.py tests/test_subscription_configuration.py tests/test_subscription_vendor_adapters.py
git commit -m "fix: require live proof before credential cleanup"
```

---

### Task 9: Deploy locally, verify accounts sequentially, and prove the workflow

**Files:**
- Modify: `docs/model-routing.md`
- Modify: `docs/status.md`
- Modify: `README.md`

**Interfaces:**
- Consumes: Tasks 1-8, installed vendor sessions, private DeepSeek/Z.AI store, and a harmless local Git repository.
- Produces: fresh redacted readiness matrix, durable local workflow evidence, quality-gate output, and a go/no-go decision for Telegram.

- [x] **Step 1: Run focused non-live suites before deployment**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest tests.test_configuration tests.test_subscription_catalog tests.test_subscription_process tests.test_subscription_router tests.test_subscription_tools tests.test_local_orchestrator -v
git diff --check
```

Expected: zero failed focused tests and `git diff --check` exits zero. The complete battery and hooks run once at the final gate.

- [x] **Step 2: Back up and deploy the corrected profiles**

Copy each `config.yaml` and `goncloud-managed.json` to a timestamped directory under `%LOCALAPPDATA%\hermes\recovery-backups`, validate every target, then run:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python scripts/configure_team.py --apply-model-routing
```

Expected: nine profiles contain the plugin and authoritative routing source, no managed native fallback chain, and Copilot exclusions. The gateway remains stopped.

- [x] **Step 3: Run the first live probes sequentially**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python scripts/configure_subscriptions.py --live-probe-all --only opencode_go --json .claude/tmp/go.json
& $python scripts/configure_subscriptions.py --live-probe-all --only opencode_zen --json .claude/tmp/zen.json
```

Before any remaining live inference, inspect Go and Zen results. A failed adapter is quarantined and does not change position; do not wait longer than the configured five-minute inference deadline.

- [x] **Step 4: Verify remaining adapters one at a time**

Run separate `--only` invocations, in the first order in which each family occurs across the authoritative table: Muse, Claude, Codex, Kimi, Z.AI, Grok, DeepSeek. Review each redacted result before starting the next. Never run concurrent probes.

Expected: each usable account returns its exact marker and billing class. Failed accounts remain visible as quarantined and are not called by workflow execution.

- [x] **Step 5: Prove a single role locally**

Use a temporary Git repository and a harmless inspection assignment:

```powershell
$instruction = Join-Path $env:TEMP 'goncloud-inspection.txt'
Set-Content -LiteralPath $instruction -Value 'Inspect repository structure. Do not modify files or contact external systems.'
python scripts/run_local_workflow.py start --repository C:\dev\goncloud-hermes --instruction-file $instruction --max-steps 20 --stop-after scout --json
```

Expected: route attempts begin at Go then Zen and proceed only according to that role's exact F2-F7 chain; unavailable slots are recorded without invocation.

- [x] **Step 6: Prove the complete local engineering chain**

Run an owner-approved change in a disposable Git repository. Confirm durable stages in this exact order: scout, ingenieria, implementer, verifier, adversary, reviewer, entrega. Confirm reviewer approval metadata, no overlapping vendor invocation, and route/slot/model/billing evidence for every stage.

- [x] **Step 7: Run final quality and runtime gates**

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python -m unittest discover -s tests -v
python -m pre_commit run --all-files
git diff --check
$hermes = Join-Path $env:LOCALAPPDATA 'hermes\bin\hermes.exe'
& $hermes -p claw tools plugin doctor
& $hermes -p claw gateway status --deep --full
```

Expected: tests/hooks/doctor pass. Gateway status must still report stopped; do not start it in this task.

- [x] **Step 8: Record evidence and commit**

Update documentation with exact test counts, redacted adapter statuses, local workflow ID, durable evidence location, known quarantines, and the explicit statement `Telegram: stopped pending separate transport cutover`.

```powershell
git add README.md docs/model-routing.md docs/status.md
git commit -m "docs: record local subscription router acceptance"
```

---

## Telegram cutover gate

Telegram reactivation is deliberately excluded from implementation Tasks 1-9. After Task 9 passes, write a separate transport-cutover plan against the proven local entry point. That plan must not reintroduce native Hermes model selection and must include one harmless allowlisted bidirectional acceptance test.

Written: `docs/superpowers/plans/2026-09-14-telegram-transport-cutover.md` with
`docs/superpowers/specs/2026-09-14-telegram-transport-cutover-design.md`. It is
not executed; the gateway stays stopped until the owner approves that plan.

---

## Execution record

Provenance matters for reading the checkboxes above, so it is stated rather than implied.

- **Tasks 1-8** were implemented in a prior session, commits `b20a9f1..3797050`, plus `c535350`. Their boxes are checked because the deliverables exist, the per-task regressions ship in those commits, and the whole battery passes here. The "Confirm RED" steps are attested by those commits' own tests; they were not re-observed in this session.
- **Task 9** was re-run end to end in this session against a runtime head six commits newer than the one the earlier acceptance recorded. Steps 1-8 were executed and observed here.
- Closing Task 9 required six defect fixes, each with a regression observed failing first: `8e68f7f`, `5f24754`, `575eb9f`, `c259d26`, `2152603`, `0f206ca`. `docs/status.md` describes what each one was.
- After acceptance, the owner asked for every account to be usable. `d0cca29` sends the Muse subscription its own model id (the `-free` suffix names OpenCode Zen's tier, which Muse rejects); the source table is untouched. Kimi and Zen turned out to be blocked by Kaspersky's TLS interception, fixed outside the repository with `NODE_EXTRA_CA_CERTS` (an environment dependency recorded in `docs/status.md`), and the Kimi adapter now delivers the assignment through a private agent file because kimi 0.34 never reads stdin (`5c04159`). Codex is quota-limited until the provider's stated date.
