# Subscription Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route every role's Primary-to-F7 table through the owner's matching subscriptions, with DeepSeek as the only API-billed exception.

**Architecture:** Hermes remains the Telegram director, durable Kanban dispatcher, and workflow authority. A plugin-local router compiles routes by slot, invokes vendor adapters, records attempts durably, and continues rather than blindly replaying after workspace mutation.

**Tech Stack:** Python 3.11, Hermes plugin API, SQLite, safe subprocess execution, vendor CLIs, unittest/pytest, pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-13-subscription-router-design.md`

## Global Constraints

- Primary is OpenCode Go; F1 is OpenCode Zen; F2-F7 bind by model family regardless of historical prefix.
- All routes consume subscriptions except DeepSeek, explicitly billed through its API.
- `entrega` inherits `main`; repeated `dup` slots remain distinct attempts.
- OAuth credentials remain in native stores; no secret enters Git, prompts, logs, Kanban, or command arguments.
- Remove plaintext GLM/DeepSeek launcher credentials only after replacement storage and live verification.
- After workspace mutation, failover continues from recorded state and never blindly replays.
- Preserve Telegram allowlisting, gateway supervision, review limits, and external-action authority.
- Run focused tests while building and the complete local battery exactly once at final closure.

---

### Task 1: Typed slot-aware route catalog

**Files:**
- Create: `plugins/goncloud-team/subscriptions/__init__.py`
- Create: `plugins/goncloud-team/subscriptions/types.py`
- Create: `plugins/goncloud-team/subscriptions/catalog.py`
- Create: `tests/test_subscription_catalog.py`

**Interfaces:**
- Consumes: `model-routing.source.json`.
- Produces: `RouteSpec`, `InvocationRequest`, `InvocationResult`, `FailureKind`, and `compile_profile_routes(profile: str, source: dict) -> tuple[RouteSpec, ...]`.

- [ ] **Step 1: Write failing slot-semantics tests**

```python
def test_slots_override_historical_prefix_after_f1():
    routes = compile_profile_routes("claw", load_source())
    assert [route.adapter for route in routes] == [
        "opencode_go", "opencode_zen", "claude", "kimi",
        "muse", "zai", "grok", "codex",
    ]


def test_duplicate_slot_and_entrega_inheritance():
    source = load_source()
    assert compile_profile_routes("entrega", source) == compile_profile_routes("claw", source)
    verifier = compile_profile_routes("verifier", source)
    assert len(verifier) == 8
    assert verifier[3].label.endswith(" dup")
    assert verifier[3].model == "muse-spark-1.3-contributor-free"
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_catalog.py -q`

Expected: import failure because `subscriptions.catalog` does not exist.

- [ ] **Step 3: Implement contracts and compiler**

```python
class FailureKind(StrEnum):
    AUTH = "auth"
    QUOTA = "quota"
    MODEL_UNAVAILABLE = "model_unavailable"
    TIMEOUT = "timeout"
    INVALID_REQUEST = "invalid_request"
    TASK_FAILURE = "task_failure"


@dataclass(frozen=True)
class RouteSpec:
    slot: int
    label: str
    adapter: str
    model: str
    billing_mode: Literal["subscription", "api"]


@dataclass(frozen=True)
class InvocationRequest:
    workflow_id: str
    stage_id: str
    role: str
    prompt: str
    workspace: Path
    start_revision: str
    current_revision: str
    mode: Literal["start", "continue"] = "start"
    changed_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvocationResult:
    success: bool
    text: str
    adapter: str
    model: str
    billing_mode: Literal["subscription", "api"]
    session_id: str = ""
    failure: FailureKind | None = None


@dataclass(frozen=True)
class AdapterHealth:
    ready: bool
    account_class: str
    failure: FailureKind | None = None
    detail: str = ""


def compile_profile_routes(profile: str, source: dict) -> tuple[RouteSpec, ...]:
    source_role = "main" if profile in {"claw", "entrega"} else profile
    return tuple(
        _compile_route(slot, label)
        for slot, label in enumerate(source["chains"][source_role])
    )
```

`_compile_route` forces slots 0/1 to OpenCode Go/Zen. Later slots select Muse, Claude, Codex, Kimi, Z.AI, Grok, or DeepSeek from the model family, reject unknown families, strip ` dup` only from `model`, and mark only DeepSeek as `api`.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_subscription_catalog.py -q`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions tests/test_subscription_catalog.py
git commit -m "feat: compile subscription routes by table slot"
```

---

### Task 2: Safe process boundary

**Files:**
- Create: `plugins/goncloud-team/subscriptions/process.py`
- Create: `tests/test_subscription_process.py`

**Interfaces:**
- Consumes: command arrays, prompt, workspace, timeout, optional child environment, and stdin/temp-file transport.
- Produces: `ProcessEvidence` and `run_cli(command: list[str], prompt: str, workspace: Path, timeout: int, env_overlay: Mapping[str, str] | None = None, prompt_transport: Literal["stdin", "tempfile"] = "stdin") -> ProcessEvidence`.

- [ ] **Step 1: Write failing safety tests**

```python
def test_prompt_and_secret_never_enter_command_or_result(fake_runner):
    evidence = run_cli(
        ["vendor", "exec", "-"], 'literal $(unsafe) `text`', Path(r"C:\dev\repo"),
        timeout=30, env_overlay={"VENDOR_TOKEN": "super-secret"}, runner=fake_runner,
    )
    assert evidence.returncode == 0
    assert "super-secret" not in repr(evidence)
    assert "super-secret" not in " ".join(fake_runner.command)
    assert fake_runner.shell is False


def test_temp_prompt_is_deleted_after_timeout(fake_timeout_runner):
    with pytest.raises(SubscriptionProcessError):
        run_cli(["vendor", "--prompt-file", "{prompt_file}"], "task", Path(r"C:\dev\repo"), timeout=1, prompt_transport="tempfile", runner=fake_timeout_runner)
    assert not fake_timeout_runner.prompt_path.exists()
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_process.py -q`

- [ ] **Step 3: Implement safe execution**

```python
@dataclass(frozen=True)
class ProcessEvidence:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float


def run_cli(command, prompt, workspace, timeout, env_overlay=None,
            prompt_transport="stdin", runner=subprocess.run) -> ProcessEvidence:
    workspace = Path(workspace).resolve()
    if not workspace.is_absolute() or not workspace.is_dir():
        raise SubscriptionProcessError("workspace must be an existing absolute directory")
    child_env = {**os.environ, **dict(env_overlay or {})}
    secrets = tuple(value for value in (env_overlay or {}).values() if value)
    prompt_path = None
    started = time.monotonic()
    try:
        if prompt_transport == "tempfile":
            prompt_path = write_private_prompt(prompt)
            command = [str(prompt_path) if value == "{prompt_file}" else value for value in command]
            stdin_text = None
        else:
            stdin_text = prompt
        completed = runner(
            command, cwd=workspace, env=child_env, input=stdin_text,
            capture_output=True, text=True, timeout=timeout, check=False, shell=False,
        )
    except subprocess.TimeoutExpired as error:
        raise SubscriptionProcessError(f"vendor process timed out after {timeout}s") from error
    finally:
        if prompt_path is not None:
            prompt_path.unlink(missing_ok=True)
    return ProcessEvidence(
        completed.returncode,
        redact_and_cap(completed.stdout, secrets),
        redact_and_cap(completed.stderr, secrets),
        time.monotonic() - started,
    )
```

Resolve an existing absolute workspace; use `shell=False`; pass secrets only in the child environment; cap stdout/stderr; redact every overlay value; delete temporary prompt files in `finally`.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_subscription_process.py -q`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/process.py tests/test_subscription_process.py
git commit -m "feat: add safe subscription process boundary"
```

---

### Task 3: OpenCode Go and Zen adapters

**Files:**
- Create: `plugins/goncloud-team/subscriptions/adapters/__init__.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/opencode.py`
- Create: `tests/test_subscription_opencode.py`

**Interfaces:**
- Consumes: OpenCode native credential store and Go/Zen `RouteSpec`.
- Produces: `OpenCodeAdapter.probe(route) -> AdapterHealth` and `invoke(request, route) -> InvocationResult`.

- [ ] **Step 1: Write failing command tests**

```python
def test_go_and_zen_use_exact_subscription_models(runner):
    adapter = OpenCodeAdapter(runner=runner)
    adapter.invoke(request(), route(0, "opencode_go", "muse-spark-1.3-contributor"))
    assert runner.command[:4] == ["opencode", "run", "--format", "json"]
    assert runner.command[4:6] == ["--model", "opencode-go/muse-spark-1.3-contributor"]
    assert not any("TOKEN" in value or "KEY" in value for value in runner.command)
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_opencode.py -q`

- [ ] **Step 3: Implement invocation and JSONL parsing**

Build `opencode run --format json --model <provider/model> --dir <workspace>` and send the prompt through stdin. Classify authentication, balance/rate limit, unknown model, timeout, and task errors. Never read or copy OpenCode `auth.json`.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_subscription_opencode.py -q`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/adapters tests/test_subscription_opencode.py
git commit -m "feat: route primary slots through OpenCode subscriptions"
```

---

### Task 4: Muse, Claude Max, and ChatGPT Codex adapters

**Files:**
- Modify: `plugins/goncloud-team/muse_bridge.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/muse.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/claude.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/codex.py`
- Create: `tests/test_subscription_premium_clis.py`

**Interfaces:**
- Consumes: native Muse, Claude, and Codex sessions.
- Produces: `MuseAdapter`, `ClaudeAdapter`, and `CodexAdapter`, each with `probe` and `invoke`.

- [ ] **Step 1: Write failing CLI tests**

```python
def test_codex_uses_chatgpt_session_and_sandbox(runner):
    CodexAdapter(runner=runner).invoke(request(), route_for("codex", "gpt-6-astra"))
    assert runner.command[:2] == ["codex", "exec"]
    assert "--json" in runner.command
    assert ["--sandbox", "workspace-write"] == runner.command[runner.command.index("--sandbox"):runner.command.index("--sandbox") + 2]
    assert "--dangerously-bypass-approvals-and-sandbox" not in runner.command


def test_claude_uses_max_session_without_api_budget(runner):
    ClaudeAdapter(runner=runner).invoke(request(), route_for("claude", "claude-sonnet-5"))
    assert runner.command[:2] == ["claude", "--print"]
    assert "--permission-mode" in runner.command
    assert "--max-budget-usd" not in runner.command
    assert "ANTHROPIC_API_KEY" not in runner.env_overlay
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_premium_clis.py -q`

- [ ] **Step 3: Implement adapters**

Reuse the Muse parser. Invoke Claude with `--print --output-format stream-json --permission-mode dontAsk --model <model>` and stdin. Invoke Codex with `exec - --json --sandbox workspace-write --cd <workspace> --model <model>`. Codex is ready only when `codex login status` reports ChatGPT; Claude is ready only when `claude auth status` reports `claude.ai` and `max`.

- [ ] **Step 4: Verify focused tests and read-only probes**

Run: `python -m pytest tests/test_muse_bridge.py tests/test_subscription_premium_clis.py -q`

Run: `codex login status`

Run: `claude auth status`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/muse_bridge.py plugins/goncloud-team/subscriptions/adapters tests/test_muse_bridge.py tests/test_subscription_premium_clis.py
git commit -m "feat: add premium subscription CLI adapters"
```

---

### Task 5: Kimi, Z.AI, Grok, and DeepSeek adapters

**Files:**
- Create: `plugins/goncloud-team/subscriptions/adapters/kimi.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/zai.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/grok.py`
- Create: `plugins/goncloud-team/subscriptions/adapters/deepseek.py`
- Create: `tests/test_subscription_vendor_adapters.py`

**Interfaces:**
- Consumes: Kimi session, Z.AI Coding Plan, Grok session, and DeepSeek secret.
- Produces: four adapters; only DeepSeek returns `billing_mode="api"`.

- [ ] **Step 1: Write failing billing and sandbox tests**

```python
def test_only_deepseek_is_api_billed(adapters):
    assert [adapters[name].billing_mode for name in ("kimi", "zai", "grok")] == ["subscription"] * 3
    assert adapters["deepseek"].billing_mode == "api"


def test_grok_runs_bypass_in_every_role_inside_the_sandbox(runner):
    GrokAdapter(runner=runner).invoke(request(), route_for("grok", "grok-4.6"))
    # Grok runs bypassPermissions in every role, always inside --sandbox
    # workspace: it has no per-tool allowlist and dontAsk denies a scout's
    # read-only commands (#57). Read-only roles are held by the read-only guard.
    assert "bypassPermissions" in runner.command and "--sandbox" in runner.command
    KimiAdapter(runner=runner).invoke(request(), route_for("kimi", "k3"))
    assert "--auto" in runner.command and "--output-format" in runner.command
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_vendor_adapters.py -q`

- [ ] **Step 3: Implement adapters**

Use Kimi stream JSON and auto mode with private prompt transport. Use Grok streaming JSON, `bypassPermissions` in every role (Grok has no per-tool allowlist and `dontAsk` denies a scout's read-only commands; read-only roles are held by `--sandbox workspace` and the orchestrator's read-only guard), a workspace sandbox, and `--prompt-file`. Pass Z.AI and DeepSeek secrets only through child environment variables. Normalize every result and label DeepSeek visibly as API-billed.

- [ ] **Step 4: Confirm GREEN**

Run: `python -m pytest tests/test_subscription_vendor_adapters.py -q`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/adapters tests/test_subscription_vendor_adapters.py
git commit -m "feat: add vendor subscription and DeepSeek adapters"
```

---

### Task 6: Durable mutation-aware router

**Files:**
- Create: `plugins/goncloud-team/subscriptions/state.py`
- Create: `plugins/goncloud-team/subscriptions/router.py`
- Create: `tests/test_subscription_router.py`

**Interfaces:**
- Consumes: compiled routes, adapter registry, invocation request, and Git evidence.
- Produces: `SubscriptionStateStore`, `SubscriptionRouter.run(request, routes) -> InvocationResult`, and `status(profile) -> dict`.

- [ ] **Step 1: Write failing failover tests**

```python
def test_auth_and_quota_advance_in_order(router):
    router.adapters["opencode_go"].result = failed(FailureKind.AUTH)
    router.adapters["opencode_zen"].result = failed(FailureKind.QUOTA)
    router.adapters["claude"].result = completed("ok")
    assert router.run(request(), routes_for_main()).text == "ok"
    assert router.calls == ["opencode_go", "opencode_zen", "claude"]


def test_mutation_turns_next_attempt_into_continuation(router, git_repo):
    router.adapters["opencode_go"].result = failed_after_edit("changed.py")
    router.adapters["opencode_zen"].result = completed("continued")
    router.run(request(workspace=git_repo), routes_for_test())
    second = router.adapters["opencode_zen"].requests[0]
    assert second.mode == "continue"
    assert second.changed_files == ("changed.py",)
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_router.py -q`

- [ ] **Step 3: Implement SQLite state and selection**

Create private `subscription_attempts` and `subscription_health` tables. Uniquely key workflow, stage, slot, and attempt. Record revisions, changed files, classified failure, cooldown, adapter, model, billing, and redacted session. Advance only on AUTH, QUOTA, MODEL_UNAVAILABLE, or timeout without mutation; otherwise block or continue explicitly.

- [ ] **Step 4: Confirm GREEN including concurrency/restart tests**

Run: `python -m pytest tests/test_subscription_router.py -q`

- [ ] **Step 5: Commit**

```powershell
git add plugins/goncloud-team/subscriptions/state.py plugins/goncloud-team/subscriptions/router.py tests/test_subscription_router.py
git commit -m "feat: add durable subscription failover"
```

---

### Task 7: Hermes tool and workflow integration

**Files:**
- Modify: `plugins/goncloud-team/schemas.py`
- Modify: `plugins/goncloud-team/__init__.py`
- Modify: `plugins/goncloud-team/plugin.yaml`
- Modify: `plugins/goncloud-team/workflow.py`
- Modify: `shared/CONTRACT.md`
- Modify: `roles/*.md`
- Create: `tests/test_subscription_tools.py`

**Interfaces:**
- Consumes: router `run` and `status`.
- Produces: `goncloud_subscription_run` and `goncloud_subscription_status`, bound to `PluginContext.profile_name` and native task identity.

- [ ] **Step 1: Write failing registration/authorization tests**

```python
def test_tool_binds_profile_from_context(fake_context):
    register(fake_context)
    schema = fake_context.tools["goncloud_subscription_run"].schema
    assert "profile" not in schema["parameters"]["properties"]
    assert fake_context.call("goncloud_subscription_run", payload())["profile"] == fake_context.profile_name


def test_result_from_other_stage_is_rejected(service):
    with pytest.raises(WorkflowError, match="stage identity"):
        service.attach_subscription_result(other_stage_result())
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_tools.py -q`

- [ ] **Step 3: Register and bind tools**

Require workflow id, task id, instruction, repository, revision, sources, and max steps. Bind profile from context. Reject mismatched assignee, workspace, root, or revision. Put actual adapter, slot, model, and billing evidence in the handoff.

- [ ] **Step 4: Update role contracts**

Require every stage to call the router, consult peers through `goncloud_consult`, and distinguish coordinator from subscription worker.

- [ ] **Step 5: Verify**

Run: `python -m pytest tests/test_workflow.py tests/test_subscription_tools.py -q`

Run: `& "$env:LOCALAPPDATA\hermes\bin\hermes.exe" -p claw plugins doctor goncloud-team`

- [ ] **Step 6: Commit**

```powershell
git add plugins/goncloud-team shared/CONTRACT.md roles tests/test_subscription_tools.py
git commit -m "feat: bind workflow stages to subscription router"
```

---

### Task 8: Secure credential migration and deployment

**Files:**
- Create: `scripts/configure_subscriptions.py`
- Modify: `scripts/configure_team.py`
- Modify: `.gitignore`
- Create: `tests/test_subscription_configuration.py`

**Interfaces:**
- Consumes: native login status and existing GLM/DeepSeek launchers.
- Produces: private profile configuration, two-phase launcher cleanup, redacted readiness report.

- [ ] **Step 1: Write failing migration tests**

```python
def test_migration_removes_plaintext_only_after_probe(tmp_path, capsys):
    glm, deepseek = seeded_launchers(tmp_path)
    result = migrate_launcher_secrets(glm, deepseek, private_env(tmp_path), passing_probe())
    assert result.names == ("DEEPSEEK_API_KEY", "GLM_API_KEY")
    assert "ANTHROPIC_AUTH_TOKEN=" not in glm.read_text()
    assert "ANTHROPIC_AUTH_TOKEN=" not in deepseek.read_text()
    assert seeded_secret() not in capsys.readouterr().out


def test_failed_probe_preserves_launcher(tmp_path):
    launcher = seeded_deepseek_launcher(tmp_path)
    before = launcher.read_bytes()
    with pytest.raises(MigrationError):
        migrate_one(launcher, private_env(tmp_path), failing_probe())
    assert launcher.read_bytes() == before
```

- [ ] **Step 2: Confirm RED**

Run: `python -m pytest tests/test_subscription_configuration.py -q`

- [ ] **Step 3: Implement two-phase migration**

Parse only known assignments, write mode-restricted private files atomically, probe replacements, then rewrite wrappers without values. Retain recoverable backups outside Git. Never print values or prefixes.

- [ ] **Step 4: Extend idempotent team deployment**

Copy the subscriptions package and route configuration through existing managed-state conflict rules; preserve secrets and unmanaged fields.

- [ ] **Step 5: Verify**

Run: `python -m pytest tests/test_configuration.py tests/test_subscription_configuration.py -q`

- [ ] **Step 6: Commit**

```powershell
git add scripts .gitignore tests/test_subscription_configuration.py
git commit -m "feat: configure subscriptions without copying OAuth tokens"
```

---

### Task 9: Live account proof and Telegram cutover

**Files:**
- Modify: `README.md`
- Modify: `docs/model-routing.md`
- Modify: `docs/status.md`

**Interfaces:**
- Consumes: installed router and vendor login flows.
- Produces: redacted readiness matrix and live Telegram workflow evidence.

- [ ] **Step 1: Stop gateway for cutover**

Run: `& "$env:LOCALAPPDATA\hermes\bin\hermes.exe" -p claw gateway stop`

- [ ] **Step 2: Complete only missing interactive logins**

Run `opencode auth login` for Go and Zen. Keep existing Muse, Claude Max, Codex ChatGPT, Kimi, and Z.AI sessions when their probes pass. Run `grok login` because its current probe is unauthenticated. Paste no token into chat.

- [ ] **Step 3: Migrate and verify GLM/DeepSeek**

Run: `python scripts/configure_subscriptions.py --migrate-launchers --verify`

Expected: Z.AI is subscription/Coding Plan; DeepSeek is API; launcher tokens are removed only after both probes succeed.

- [ ] **Step 4: Run one minimal inference per adapter**

Run: `python scripts/configure_subscriptions.py --live-probe-all`

Expected: redacted results for all nine adapters. A failed adapter remains quarantined; credential presence alone never counts as PASS.

- [ ] **Step 5: Deploy and restart**

Run: `python scripts/configure_team.py --apply-model-routing`

Run: `& "$env:LOCALAPPDATA\hermes\bin\hermes.exe" -p claw gateway start`

Run: `& "$env:LOCALAPPDATA\hermes\bin\hermes.exe" -p claw gateway status --deep --full`

- [ ] **Step 6: Prove fallback and mutation continuation**

In a temporary Git repository, force Go AUTH, Zen QUOTA, then F2 success. In a second task, mutate one file before failure and assert that the next adapter receives continuation metadata and the recorded diff.

- [ ] **Step 7: Verify a harmless Telegram engineering workflow**

Send an owner-authorized repository inspection. Confirm the chain reaches entrega, records slot/adapter/model/billing at every stage, and sends one final response.

- [ ] **Step 8: Run final gates once**

Run: `python -m pre_commit run --all-files`

Run: `& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests -v`

Run: `git diff --check`

- [ ] **Step 9: Document evidence and commit**

Record the readiness matrix, billing class, fallback evidence, test count, gateway health, and quarantined routes without account identifiers.

```powershell
git add README.md docs/model-routing.md docs/status.md
git commit -m "docs: record subscription router acceptance"
```
