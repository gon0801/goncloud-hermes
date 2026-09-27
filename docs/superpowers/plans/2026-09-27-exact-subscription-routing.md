# Exact Subscription Routing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route Hermes `main` and every workflow profile through the owner's exact seven-position, subscription-backed table without a competing native fallback chain.

**Architecture:** Preserve `model-routing.source.json` as the only routing authority. Extend the existing subscription router for conversational requests, then expose it through Hermes's supported user model-provider plugin interface so `claw` retains normal Telegram tool calls while every failure advances exactly one table position.

**Tech Stack:** Python 3.11, `unittest`, Hermes `ProviderProfile`, Hermes OpenAI-compatible completion bridge, existing Goncloud subscription adapters, JSON/YAML configuration, pre-commit.

**Spec:** `docs/superpowers/specs/2026-09-27-exact-subscription-routing-design.md`

## Global Constraints

- `model-routing.source.json` is the single source of truth for all 56 cells.
- `claw` and `entrega` resolve to the complete `main` row.
- `main` F1 is `anthropic/claude-opus-5 (xhigh)`; `xhigh` is an execution setting, not the label "Claude Max".
- Every route uses the owner's subscription-backed adapter except the two explicit `deepseek/deepseek-flash` API cells.
- No upstream Hermes source modification and no new API-key substitution.
- Preserve all pre-existing uncommitted user changes; edit overlapping files additively and review their diff before each commit.
- Never bypass pre-commit hooks.

## File Structure

- Modify `plugins/goncloud-team/subscriptions/types.py`: typed reasoning marker on a compiled route.
- Modify `plugins/goncloud-team/subscriptions/catalog.py`: parse annotations without losing their literal labels.
- Modify vendor adapter files under `plugins/goncloud-team/subscriptions/adapters/`: enforce supported reasoning markers on CLI invocations.
- Create `plugins/goncloud-team/subscriptions/conversation.py`: conversation-to-router boundary independent of Hermes provider objects.
- Create `plugins/model-providers/goncloud-subscriptions/`: user-scoped Hermes provider registration and OpenAI-compatible client facade.
- Modify `scripts/configure_team.py` and `team.json`: deploy/select the provider and remove the competing director fallback list.
- Modify routing, adapter, configuration, and deployment tests in `tests/`.
- Update `README.md`, `docs/model-routing.md`, and `docs/status.md` after verified behavior is known.

## Review Focus

- A streaming completion must yield the same terminal model, text, and tool calls as a non-streaming completion (Task 3 test).
- A model-produced tool request followed by a Hermes tool result must survive the next completion request without losing message order (Task 2 test).
- Provider discovery under a named profile must load only that profile's deployed Goncloud package and routing source (Task 4 test).
- `TASK_FAILURE`, invalid requests, and malformed tool output must stop rather than spend later subscriptions (Task 2 tests).
- An adapter cooldown may skip its own position only with sanitized evidence naming the skipped slot and reason; it must not silently jump to Kimi (Task 2 test).

---

### Task 1: Preserve and enforce routing annotations

**Files:**
- Modify: `plugins/goncloud-team/subscriptions/types.py`
- Modify: `plugins/goncloud-team/subscriptions/catalog.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/claude.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/codex.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/grok.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/kimi.py`
- Modify: `plugins/goncloud-team/subscriptions/adapters/muse.py`
- Test: `tests/test_subscription_catalog.py`
- Test: `tests/test_subscription_premium_clis.py`
- Test: `tests/test_subscription_vendor_adapters.py`

**Interfaces:**
- Consumes: literal labels from `model-routing.source.json`.
- Produces: `RouteSpec.reasoning_effort: Literal["high", "xhigh", "max"] | None`; `compile_profile_routes(profile, source) -> tuple[RouteSpec, ...]` with label, model, adapter, effort, slot, and billing preserved.

- [ ] **Step 1: Write failing catalog tests for the literal annotations**

Add literal `EXPECTED_EFFORTS` values for every profile and assert the complete compiled tuple for all 56 cells. Explicitly assert `main[1] == ("claude", "claude-opus-5", "xhigh")`, `main[2] == ("muse", "muse-spark-1.3", "max")`, and `main[6] == ("kimi", "k3", None)`.

- [ ] **Step 2: Run the catalog tests and verify RED**

Run: `python -m unittest tests.test_subscription_catalog -v`

Expected: FAIL because `RouteSpec` has no `reasoning_effort`.

- [ ] **Step 3: Implement annotation parsing**

Add `reasoning_effort` to `RouteSpec` and replace the annotation-stripping helper with `_runtime_model_and_effort(raw_model: str) -> tuple[str, str | None]`. Reject unknown parenthetical markers rather than silently discarding them.

- [ ] **Step 4: Run the catalog tests and verify GREEN**

Run: `python -m unittest tests.test_subscription_catalog -v`

Expected: all catalog tests PASS.

- [ ] **Step 5: Write failing adapter command tests**

Assert Claude receives `--effort xhigh`, Muse receives `--reasoning-effort max`, Grok receives `--reasoning-effort <value>`, and Codex receives `-c model_reasoning_effort="<value>"`. For Kimi `(high)`, point the adapter at a temporary `config.toml`, assert it accepts `effort = "high"`, and assert it fails closed before invocation when the effective configured effort is not `high`; the adapter must never rewrite that file.

- [ ] **Step 6: Run the focused adapter tests and verify RED**

Run: `python -m unittest tests.test_subscription_premium_clis tests.test_subscription_vendor_adapters -v`

Expected: new effort assertions FAIL.

- [ ] **Step 7: Add the minimal vendor-specific effort arguments**

Map only non-null `RouteSpec.reasoning_effort` values using the exact flags above. For Kimi, read and validate its effective `config.toml` effort because the installed CLI exposes no per-invocation effort flag. Keep model IDs annotation-free and preserve the literal label for evidence.

- [ ] **Step 8: Run focused tests and commit**

Run: `python -m unittest tests.test_subscription_catalog tests.test_subscription_premium_clis tests.test_subscription_vendor_adapters -v`

Expected: PASS.

Commit: `feat: preserve routing reasoning levels`

### Task 2: Add a conversational boundary to the subscription router

**Files:**
- Create: `plugins/goncloud-team/subscriptions/conversation.py`
- Modify: `plugins/goncloud-team/subscriptions/router.py`
- Modify: `plugins/goncloud-team/subscriptions/types.py`
- Test: `tests/test_subscription_conversation.py`
- Test: `tests/test_subscription_router.py`

**Interfaces:**
- Consumes: `compile_profile_routes(profile, source)`, `SubscriptionRouter.run(request, routes)`, and Hermes-style `messages`, `tools`, and `tool_choice` values.
- Produces: `render_conversation_prompt(messages: list[dict], tools: list[dict] | None, tool_choice: object) -> str`; `ConversationRouter.complete(profile: str, messages: list[dict], tools: list[dict] | None, tool_choice: object, workspace: Path) -> InvocationResult`; optional `InvocationResult.slot`, `route_label`, `reasoning_effort`, and `skipped_routes: tuple[SkippedRoute, ...]`, where `SkippedRoute` contains only slot, adapter, and normalized failure.

- [ ] **Step 1: Write the failing main regression test**

With Primary returning `MODEL_UNAVAILABLE` and F1 succeeding, assert adapter calls are exactly `["opencode_go", "claude"]`, the terminal model is `claude-opus-5`, and the selected route carries `xhigh`. Assert Kimi receives no request.

- [ ] **Step 2: Add failing boundary tests for tools, terminal failures, cooldowns, and message order**

Test a tool schema and subsequent tool result in chronological order; `TASK_FAILURE` and `INVALID_REQUEST` stop at the current slot; a cooldown records a sanitized skipped-slot event before the immediately following position; malformed tool JSON is terminal.

- [ ] **Step 3: Run the new test module and verify RED**

Run: `python -m unittest tests.test_subscription_conversation -v`

Expected: import failure because `subscriptions.conversation` does not exist.

- [ ] **Step 4: Implement prompt rendering and `ConversationRouter`**

Create a fresh UUID-backed workflow/stage identity per completion so tool-loop calls cannot replay a cached prior result. Use the active profile's terminal workspace, compile the requested profile row, and delegate all failover decisions to `SubscriptionRouter`.

- [ ] **Step 5: Add explicit sanitized skip evidence**

Populate `InvocationResult.slot`, `route_label`, `reasoning_effort`, and `skipped_routes` at the router boundary; never expose vendor stderr, credentials, or account identity.

- [ ] **Step 6: Run focused router tests and commit**

Run: `python -m unittest tests.test_subscription_conversation tests.test_subscription_router -v`

Expected: PASS.

Commit: `feat: route conversations through subscription chain`

### Task 3: Expose the router as a Hermes model-provider plugin

**Files:**
- Create: `plugins/model-providers/goncloud-subscriptions/__init__.py`
- Create: `plugins/model-providers/goncloud-subscriptions/client.py`
- Create: `plugins/model-providers/goncloud-subscriptions/plugin.yaml`
- Test: `tests/test_subscription_model_provider.py`

**Interfaces:**
- Consumes: `ConversationRouter.complete(...)`, Hermes `ProviderProfile`, `agent.acp_openai_bridge.render_tool_bridge_sections`, `extract_tool_calls_from_text`, and `completion_to_stream_chunks`.
- Produces: `GoncloudSubscriptionProfile.create_client(**kwargs) -> GoncloudSubscriptionClient`; `GoncloudSubscriptionClient.chat.completions.create(model, messages, tools=None, tool_choice=None, stream=False, **kwargs)`.

- [ ] **Step 1: Write failing provider-discovery and completion-shape tests**

Assert provider name `goncloud-subscriptions`, `auth_type="external_process"`, no API-key environment variables, model `main`, and an OpenAI-compatible completion with terminal model/provider metadata.

- [ ] **Step 2: Write failing tool and streaming equivalence tests**

Given a bridge response containing the supported tool-call envelope, assert non-streaming `message.tool_calls` and `finish_reason="tool_calls"`; collect streaming chunks and assert the same tool name, arguments, text, and terminal model.

- [ ] **Step 3: Run the provider tests and verify RED**

Run: `python -m unittest tests.test_subscription_model_provider -v`

Expected: provider module import failure.

- [ ] **Step 4: Implement the profile and client facade**

Use Hermes's existing ACP/OpenAI bridge helpers rather than inventing a second tool-call grammar. Register an `external_process` profile with local `acp://goncloud-subscriptions` identity and the installed `hermes` executable as the resolver's non-secret process command; `create_client` constructs the in-process client and does not spawn it. Resolve the active named profile from `get_hermes_home()`, load only its sibling `plugins/goncloud-team` package, and refuse path escape or a missing byte-identical routing source.

- [ ] **Step 5: Run provider tests and commit**

Run: `python -m unittest tests.test_subscription_model_provider -v`

Expected: PASS in streaming and non-streaming modes.

Commit: `feat: add goncloud subscription model provider`

### Task 4: Deploy one authoritative chain to all profiles

**Files:**
- Modify: `scripts/configure_team.py`
- Modify: `team.json`
- Modify: `tests/test_configuration.py`
- Modify: `tests/test_subscription_configuration.py`

**Interfaces:**
- Consumes: provider source at `plugins/model-providers/goncloud-subscriptions/` and the existing managed-file conflict rules.
- Produces: director config `model: {provider: goncloud-subscriptions, default: main}` with no `fallback_providers`; byte-identical provider and routing files under each named profile.

- [ ] **Step 1: Replace the old director-chain expectations with failing authoritative-provider tests**

Assert `director_model` and `director_fallbacks` are absent from `team.json`; only `claw` gets the conversational `main` provider; workers retain no native model chain; `entrega` still compiles `main` through workflows.

- [ ] **Step 2: Add failing deployment-isolation tests**

In a temporary Hermes home, configure all nine profiles and assert each deployed routing source matches the repo SHA-256, each provider copy matches the repo source, provider discovery under `claw` resolves `claw`'s sibling team plugin, and discovery under another profile cannot read `claw`'s copy.

- [ ] **Step 3: Run configuration tests and verify RED**

Run: `python -m unittest tests.test_configuration tests.test_subscription_configuration -v`

Expected: failures showing the old `Muse -> Kimi -> Z.AI` director chain and missing provider deployment.

- [ ] **Step 4: Implement managed configuration and deployment**

Remove the special director chain from `team.json`; have `desired_profile_config` emit only the local provider selection for `claw`; copy the provider plugin with the same conflict, backup, containment, revision, and idempotence rules already used for `goncloud-team`.

- [ ] **Step 5: Re-run configuration tests and commit**

Run: `python -m unittest tests.test_configuration tests.test_subscription_configuration -v`

Expected: PASS, including a second idempotent apply.

Commit: `fix: make main use the owner subscription table`

### Task 5: Verify, document, deploy, and prove the live fallback

**Files:**
- Modify: `README.md`
- Modify: `docs/model-routing.md`
- Modify: `docs/status.md`

**Interfaces:**
- Consumes: completed Tasks 1-4 and the installed Hermes runtime.
- Produces: passing repository gates, deployed profiles, restarted gateway, fresh Telegram session requirement, and sanitized proof that a controlled `main` Primary failure reaches F1 Claude Opus 5 at `xhigh`.

- [ ] **Step 1: Run the full automated suite**

Run: `python -m unittest discover -s tests -v`

Expected: PASS with only the repository's documented platform skips.

- [ ] **Step 2: Run repository quality gates**

Run: `python -m pre_commit run --all-files`

Expected: all hooks PASS; do not use `--no-verify`.

- [ ] **Step 3: Inspect the complete diff and preserve pre-existing work**

Run: `git status --short`, `git diff --check`, and focused diffs for every originally dirty file. Confirm no memory-system or unrelated subscription-probe change was reverted or absorbed accidentally.

- [ ] **Step 4: Update operator documentation with verified facts only**

Record the removal of the director exception, exact `xhigh` semantics, test counts, hook results, and rollback instructions. Merge additively with the user's existing uncommitted documentation changes.

- [ ] **Step 5: Commit documentation**

Commit: `docs: record exact subscription routing verification`

- [ ] **Step 6: Deploy idempotently and verify hashes/configuration**

Run the versioned `scripts/configure_team.py` with the installed Hermes Python, then compare all nine routing/provider hashes and inspect only the sanitized model fields. Confirm `claw` has provider `goncloud-subscriptions`, model `main`, and no fallback list.

- [ ] **Step 7: Restart the Hermes gateway and start a fresh Telegram session**

Use the existing Hermes gateway lifecycle command, verify the serving profile is `claw`, and require `/new` before acceptance so no cached agent retains the old chain.

- [ ] **Step 8: Run the controlled live fallback proof**

Use an isolated temporary routing copy and state database that changes only Primary to a known unavailable OpenCode Go model. Invoke one harmless marker prompt through the real conversation provider. Accept only sanitized evidence showing attempts `[Primary opencode_go, F1 claude]`, terminal `claude-opus-5`, effort `xhigh`, and subscription billing; Kimi must have no attempt.

- [ ] **Step 9: Re-run the exact-table audit and final gates**

Compile all nine profiles from their deployed sources and compare every slot, label, adapter, model, effort, and billing mode with the 56-cell literal fixture. Re-run the full suite and pre-commit after any documentation or evidence-command change.

- [ ] **Step 10: Final commit if deployment evidence changed tracked files**

Commit only tracked verification/documentation changes; never commit credentials, private state, runtime databases, or temporary live-proof files.
