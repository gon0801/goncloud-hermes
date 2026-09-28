# Director native chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The director (`claw`) converses on Hermes-native providers with native tool calls, and its chain is compiled from the `main` row of `model-routing.source.json`. The text tool-call bridge is removed.

**Architecture:** `scripts/configure_team.py` compiles the `main` row, through the existing `subscriptions.catalog.compile_profile_routes`, into the director's `model`, `fallback_providers` and `agent.reasoning_overrides`, using a fixed adapter→native-provider map. Managed config keys and managed files that a deploy stops shipping are retired. The `goncloud-subscriptions` model provider, `subscriptions/conversation.py` and every `tool_bridge` branch are deleted. Worker stages keep the subscription router unchanged.

**Tech Stack:** Python 3.11 (Hermes runtime venv), `unittest`, PyYAML, Hermes 0.21.2 native providers and credential pool.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-09-28-director-native-chain-design.md`.
- Test command (full): `& "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" -m unittest discover -s tests`. From Git Bash use `/c/Users/ehven/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe`. There is no pytest.
- Pre-commit runs on every commit (8 hooks). Never `--no-verify`.
- Every test added for a bug must fail on the code before the fix. Prove it (red, then green) before each commit.
- Only `claw` gets the native chain. `entrega` inherits the `main` row **as a worker stage** and stays on the router. This corrects the spec's "claw and entrega".
- Never read credential files. Credential state comes only from `hermes -p claw auth list` / `auth status`.
- Never touch the running gateway or a live profile outside Task 6, and never while the director is mid-turn.
- Work only in `.worktrees/director-native-chain` (branch `fix/director-native-chain`). Files use LF endings.

## File map

| File | Responsibility | Tasks |
|---|---|---|
| `scripts/configure_team.py` | director native chain compiler; config key retirement; managed file retirement; stop shipping the provider | 1, 2, 3 |
| `tests/test_configuration.py` | compiler, director desired config, retirement tests; rewrite the bridge-era director tests | 1, 2, 3 |
| `tests/test_subscription_configuration.py` | deployment test no longer expects the provider copy | 3 |
| `plugins/model-providers/goncloud-subscriptions/` | **deleted** | 4 |
| `plugins/goncloud-team/subscriptions/conversation.py` | **deleted** | 4 |
| `plugins/goncloud-team/subscriptions/{types,router}.py`, `adapters/{opencode,claude,codex,grok,kimi,muse}.py` | drop `tool_bridge` and its branches | 4 |
| `tests/test_subscription_{model_provider,conversation}.py` | **deleted** | 4 |
| `tests/test_subscription_{opencode,router,vendor_adapters}.py` | drop bridge-only tests | 4 |
| `docs/status.md`, `docs/model-routing.md`, the spec | record the change | 5 |

---

### Task 1: Compile the director's native chain from the table

**Files:**
- Modify: `scripts/configure_team.py` (new constants + function after `_DIRECTOR_TOOLSETS`)
- Test: `tests/test_configuration.py` (new class `DirectorNativeChainTests`)

**Interfaces:**
- Consumes: `subscriptions.catalog.compile_profile_routes(profile: str, source: Mapping) -> tuple[RouteSpec, ...]`. `RouteSpec` has `.slot`, `.label`, `.adapter`, `.model`, `.reasoning_effort`.
- Produces: `director_native_config(source: dict) -> dict`, returning exactly `{"model": {...}, "fallback_providers": [...], "agent": {"reasoning_overrides": {...}}}`. Also `ZAI_CODING_URL: str`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_configuration.py`)

```python
class DirectorNativeChainTests(unittest.TestCase):
    def table(self):
        return json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )

    def test_director_chain_is_the_main_row_on_native_providers(self):
        config = module.director_native_config(self.table())

        # Slots 0 and 1 are the owner's two OpenCode Go accounts: one native
        # provider whose credential pool rotates between them.
        self.assertEqual(
            config["model"],
            {"provider": "opencode-go", "default": "muse-spark-1.3-contributor"},
        )
        self.assertEqual(
            config["fallback_providers"],
            [
                {"provider": "anthropic", "model": "claude-opus-5"},
                {"provider": "meta-ai", "model": "muse-spark-1.3"},
                {"provider": "xai-oauth", "model": "grok-4.6"},
                {
                    "provider": "zai",
                    "model": "glm-5.3",
                    "base_url": "https://api.z.ai/api/coding/paas/v4",
                },
                {"provider": "openai-codex", "model": "gpt-5.6-sol"},
                {"provider": "kimi-coding", "model": "k3"},
            ],
        )
        self.assertEqual(
            config["agent"],
            {
                "reasoning_overrides": {
                    "claude-opus-5": "xhigh",
                    "muse-spark-1.3": "max",
                    "gpt-5.6-sol": "xhigh",
                }
            },
        )

    def test_reordering_the_table_reorders_the_chain(self):
        table = self.table()
        main = table["chains"]["main"]
        main[3], main[4] = main[4], main[3]

        chain = module.director_native_config(table)["fallback_providers"]

        self.assertEqual(
            [entry["provider"] for entry in chain[:3]],
            ["anthropic", "xai-oauth", "meta-ai"],
        )

    def test_a_position_without_native_binding_fails_naming_the_label(self):
        table = self.table()
        table["chains"]["main"][5] = "deepseek/deepseek-flash"

        with self.assertRaisesRegex(ValueError, "deepseek/deepseek-flash"):
            module.director_native_config(table)

    def test_one_model_with_two_efforts_is_refused(self):
        table = self.table()
        table["chains"]["main"][6] = "anthropic/claude-opus-5 (high)"

        with self.assertRaisesRegex(ValueError, "claude-opus-5"):
            module.director_native_config(table)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `$PY -m unittest tests.test_configuration.DirectorNativeChainTests -v`
Expected: 4 errors, `AttributeError: module 'configure_team' has no attribute 'director_native_config'`.

- [ ] **Step 3: Implement** (in `scripts/configure_team.py`, after `_DIRECTOR_TOOLSETS`)

```python
ZAI_CODING_URL = "https://api.z.ai/api/coding/paas/v4"

#: How Hermes reaches each table adapter natively. Native tool calls are the
#: point: through a CLI with its tools off, the director had to write calls as
#: text and 8 of 13 were dropped on mis-escaped JSON (2026-09-28).
_NATIVE_PROVIDERS = {
    "opencode_go": {"provider": "opencode-go"},
    # The owner's second Go account is a second credential of the same
    # provider; Hermes' credential pool rotates to it on 429/exhaustion.
    "opencode_go_2": {"provider": "opencode-go"},
    "claude": {"provider": "anthropic"},
    "muse": {"provider": "meta-ai"},
    "grok": {"provider": "xai-oauth"},
    "opencode_zai": {"provider": "zai", "base_url": ZAI_CODING_URL},
    "codex": {"provider": "openai-codex"},
    "kimi": {"provider": "kimi-coding"},
}


def director_native_config(source):
    """The director's native chain, compiled from the table's ``main`` row."""
    plugin_dir = str(ROOT / "plugins" / "goncloud-team")
    if plugin_dir not in sys.path:
        sys.path.insert(0, plugin_dir)
    from subscriptions.catalog import compile_profile_routes

    entries, overrides = [], {}
    for route in compile_profile_routes("claw", source):
        binding = _NATIVE_PROVIDERS.get(route.adapter)
        if binding is None:
            raise ValueError(
                f"no native provider for table position {route.slot}: {route.label}"
            )
        entry = {**binding, "model": route.model}
        if not entries or entries[-1] != entry:
            entries.append(entry)
        if route.reasoning_effort:
            chosen = overrides.setdefault(route.model, route.reasoning_effort)
            if chosen != route.reasoning_effort:
                raise ValueError(f"two reasoning efforts for {route.model}")
    primary, *fallbacks = entries
    model = {"provider": primary["provider"], "default": primary["model"]}
    if "base_url" in primary:
        model["base_url"] = primary["base_url"]
    return {
        "model": model,
        "fallback_providers": fallbacks,
        "agent": {"reasoning_overrides": overrides},
    }
```

- [ ] **Step 4: Run to verify they pass**

Run: `$PY -m unittest tests.test_configuration.DirectorNativeChainTests -v`
Expected: 4 OK.

- [ ] **Step 5: Commit**

```bash
git add scripts/configure_team.py tests/test_configuration.py
git commit -m "feat: compile the director's native chain from the owner table"
```

---

### Task 2: The director deploys the native chain; retired config keys are removed

**Files:**
- Modify: `scripts/configure_team.py`: `desired_profile_config` (director branch) and `merge_owned` (key retirement)
- Test: `tests/test_configuration.py`: rewrite `test_only_the_director_converses_natively_and_workers_carry_nothing` and `test_the_director_provider_compiles_the_exact_main_row`; adapt the bootstrap migration test (the one asserting `{"provider": "goncloud-subscriptions", "default": "main"}` around line 130 and line 467); add retirement tests

**Interfaces:**
- Consumes: `director_native_config(source) -> dict` (Task 1).
- Produces: `desired_profile_config(role, team, runtime_home)`, whose director dict now carries `model`, `fallback_providers` and `agent.reasoning_overrides` from the table. `merge_owned(existing, desired, previous, fresh=False, prefix="")` now also retires keys.

- [ ] **Step 1: Write the failing tests**

Replace the body of `test_only_the_director_converses_natively_and_workers_carry_nothing` and `test_the_director_provider_compiles_the_exact_main_row` with:

```python
    def test_only_the_director_converses_natively_and_workers_carry_nothing(self):
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        table = json.loads(
            (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
        )
        self.assertNotIn("director_model", team)
        self.assertNotIn("director_fallbacks", team)
        native = module.director_native_config(table)

        for role in team["profiles"]:
            with self.subTest(role=role):
                desired = module.desired_profile_config(
                    role, team, Path("C:/runtime-home")
                )
                if role == team["director"]:
                    self.assertEqual(desired["model"], native["model"])
                    self.assertEqual(
                        desired["fallback_providers"], native["fallback_providers"]
                    )
                    self.assertEqual(
                        desired["agent"]["reasoning_overrides"],
                        native["agent"]["reasoning_overrides"],
                    )
                else:
                    self.assertNotIn("model", desired)
                    self.assertNotIn("fallback_providers", desired)
                    self.assertNotIn("agent", desired)

    def test_no_profile_is_configured_through_the_text_bridge(self):
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))
        for role in team["profiles"]:
            desired = module.desired_profile_config(
                role, team, Path("C:/runtime-home")
            )
            self.assertNotIn("goncloud-subscriptions", json.dumps(desired))
```

Add to the class that tests `merge_owned` (or to `DirectorNativeChainTests`):

```python
    def test_an_owned_key_no_longer_desired_is_retired(self):
        previous = {"agent": {"reasoning_overrides": {"a": "high", "b": "max"}}}
        existing = {"agent": {"reasoning_overrides": {"a": "high", "b": "max"},
                              "keep": 1}}
        desired = {"agent": {"reasoning_overrides": {"a": "high"}}}

        merged = module.merge_owned(existing, desired, previous)

        self.assertEqual(
            merged, {"agent": {"reasoning_overrides": {"a": "high"}, "keep": 1}}
        )

    def test_a_retired_key_the_user_changed_is_a_conflict(self):
        previous = {"agent": {"reasoning_overrides": {"b": "max"}}}
        existing = {"agent": {"reasoning_overrides": {"b": "low"}}}

        with self.assertRaisesRegex(ValueError, "agent.reasoning_overrides.b"):
            module.merge_owned(existing, {"agent": {"reasoning_overrides": {}}}, previous)
```

In the bootstrap migration test (native chain seeded, then `--apply-model-routing`), replace the `goncloud-subscriptions` assertions with:

```python
            native = module.director_native_config(
                json.loads(routing_source.decode("utf-8"))
            )
            self.assertEqual(migrated["model"], native["model"])
            self.assertEqual(
                migrated["fallback_providers"], native["fallback_providers"]
            )
            self.assertEqual(managed["config"]["model"], migrated["model"])
```

Apply the same replacement at the other `{"provider": "goncloud-subscriptions", "default": "main"}` assertion in this file (about line 467). Find it with `grep -n goncloud-subscriptions tests/test_configuration.py`.

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m unittest tests.test_configuration -v 2>&1 | tail -30`
Expected: the rewritten director tests FAIL (desired model is `goncloud-subscriptions`); the retirement test FAILS (`b` still present); the conflict test FAILS (no error raised).

- [ ] **Step 3: Implement**

In `desired_profile_config`, replace the director `desired["model"] = {...goncloud-subscriptions...}` block and its comment with:

```python
        # The director converses on native providers (native tool calls),
        # compiled from the authoritative main row so no hand-written chain
        # can contradict the table.
        desired.update(
            director_native_config(
                json.loads(
                    (ROOT / "model-routing.source.json").read_text(encoding="utf-8")
                )
            )
        )
```

In `merge_owned`, before `return result`, add:

```python
    # A key this script owned last time and no longer ships is retired, unless
    # the user changed it, which is a conflict like any other.
    for key, before in previous.items():
        if key in desired or key not in existing:
            continue
        if existing[key] != before:
            location = f"{prefix}.{key}".strip(".")
            raise ValueError(
                f"Local configuration conflict: {location}; preserve and reconcile manually"
            )
        result.pop(key)
```

- [ ] **Step 4: Run the full suite**

Run: `$PY -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK (skipped=3)`. If a worker-profile test fails because retirement removed a key it expected to survive, stop and report it: that is a key an earlier deploy owned and this one no longer ships. Decide it with the lead; do not weaken the retirement.

- [ ] **Step 5: Commit**

```bash
git add scripts/configure_team.py tests/test_configuration.py
git commit -m "feat: deploy the director on its native chain and retire dropped keys"
```

---

### Task 3: Stop shipping the provider and retire managed files a deploy drops

**Files:**
- Modify: `scripts/configure_team.py` `configure()`: remove `provider_root`/`provider_files`; add file retirement
- Test: `tests/test_configuration.py` (new test); `tests/test_subscription_configuration.py` (`test_configure_team_copies_subscriptions_and_routing_source`, about line 2070: drop its provider expectations)

**Interfaces:**
- Consumes: `check_file(target, desired, previous=None)`, `contained(path, parent)`, `write_changed(path, content)`.
- Produces: after `configure`, every path in the old `goncloud-managed.json` `files` that is no longer shipped is deleted from the profile and from the record. Directories left empty are pruned, ignoring `__pycache__`.

- [ ] **Step 1: Write the failing test** (new class in `tests/test_configuration.py`; the helper runs the script exactly like `test_compatibility_apply_migrates_owned_native_routing_idempotently` does)

```python
class ManagedFileRetirementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.temporary = Path(self.tmp.name)

    def deploy(self, runtime=None):
        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        runtime = runtime or self.temporary / "runtime"
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/configure_team.py"),
             "--home", str(runtime), "--install-dir", str(upstream)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "HOME": str(self.temporary / "home"),
                 "LOCALAPPDATA": str(self.temporary / "local-app-data"),
                 "PYTHONUTF8": "1"},
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)
        return runtime

    def test_a_managed_file_no_longer_shipped_is_removed(self):
        # Deploy once, then plant a managed file as if an earlier revision had
        # shipped it (the goncloud-subscriptions provider did).
        runtime = self.deploy()
        profile = runtime / "profiles/claw"
        stale = "plugins/model-providers/goncloud-subscriptions/client.py"
        (profile / stale).parent.mkdir(parents=True, exist_ok=True)
        (profile / stale).write_text("old\n", encoding="utf-8")
        state_path = profile / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][stale] = "old\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        self.deploy(runtime)

        self.assertFalse((profile / stale).exists())
        self.assertFalse((profile / "plugins/model-providers").exists())
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertNotIn(stale, state["files"])

    def test_a_dropped_managed_file_the_user_edited_is_a_conflict(self):
        runtime = self.deploy()
        profile = runtime / "profiles/claw"
        stale = "plugins/model-providers/goncloud-subscriptions/client.py"
        (profile / stale).parent.mkdir(parents=True, exist_ok=True)
        (profile / stale).write_text("edited\n", encoding="utf-8")
        state_path = profile / "goncloud-managed.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["files"][stale] = "old\n"
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(AssertionError, "managed-file conflict"):
            self.deploy(runtime)
        self.assertEqual((profile / stale).read_text(encoding="utf-8"), "edited\n")
```

- [ ] **Step 2: Run to verify they fail**

Run: `$PY -m unittest tests.test_configuration -k managed_file -v`
Expected: first FAILS (file still exists), second FAILS (no conflict).

- [ ] **Step 3: Implement** in `configure()`

Delete the `provider_root = ...` line, the `if not (provider_root / "plugin.yaml").is_file(): raise ...` block, the `provider_files = [...]` comprehension, and the `for source in provider_files:` loop.

After `files` is fully built and **before** the `if preserve_local_soul ...: files.pop("SOUL.md")` line, add `shipped = set(files)`. After the existing `check_file` loop, add:

```python
        retired = [rel for rel in state.get("files", {}) if rel not in shipped]
        for relative in retired:
            # Deleting is safe only for the exact content this script wrote.
            check_file(
                contained(profile / relative, runtime_home),
                state["files"][relative],
            )
```

and carry `retired` and `shipped` in the `prepared` tuple. In the write loop, build the record from shipped paths only, then remove the retired files:

```python
        recorded_files = {
            **{
                rel: content
                for rel, content in state.get("files", {}).items()
                if rel in shipped
            },
            **{
                relative: (
                    content.decode("utf-8") if isinstance(content, bytes) else content
                )
                for relative, content in files.items()
            },
        }
        ...
        for relative in retired:
            target = profile / relative
            if target.exists():
                target.unlink()
            _prune_empty_dirs(target.parent, profile)
```

Add the helper at module level:

```python
def _prune_empty_dirs(directory, stop):
    """Remove directories a retired file left empty (``__pycache__`` aside)."""
    import shutil

    while directory != stop and directory.is_dir():
        if any(child.name != "__pycache__" for child in directory.iterdir()):
            return
        shutil.rmtree(directory)
        directory = directory.parent
```

In `tests/test_subscription_configuration.py`, `test_configure_team_copies_subscriptions_and_routing_source`: delete the `provider_root` variable, its loop, and the assertions about `plugins/model-providers/goncloud-subscriptions` and `get_provider_profile('goncloud-subscriptions')`. Replace the `claw_config` model assertion with `self.assertEqual(claw_config["model"]["provider"], "opencode-go")`. Add `self.assertFalse((profile / "plugins/model-providers").exists())`.

- [ ] **Step 4: Run the full suite**

Run: `$PY -m unittest discover -s tests 2>&1 | tail -3`
Expected: `OK (skipped=3)`.

- [ ] **Step 5: Commit**

```bash
git add scripts/configure_team.py tests/test_configuration.py tests/test_subscription_configuration.py
git commit -m "fix: retire managed files a deploy stops shipping"
```

---

### Task 4: Delete the text tool-call bridge

**Files:**
- Delete: `plugins/model-providers/goncloud-subscriptions/` (whole dir), `plugins/goncloud-team/subscriptions/conversation.py`, `tests/test_subscription_model_provider.py`, `tests/test_subscription_conversation.py`
- Modify: `plugins/goncloud-team/subscriptions/types.py`, `router.py`, `adapters/opencode.py`, `adapters/claude.py`, `adapters/codex.py`, `adapters/grok.py`, `adapters/kimi.py`, `adapters/muse.py`
- Modify tests: `tests/test_subscription_opencode.py`, `tests/test_subscription_router.py`, `tests/test_subscription_vendor_adapters.py`

**Interfaces:**
- Produces: `InvocationRequest` without `tool_bridge`. `kimi_agent_definition(prompt: str) -> str`. `OpenCodeAdapter._run` without `env_overlay`. Worker behaviour byte-identical to `tool_bridge=False` today.

This task removes code, so its "failing test" is a guard that fails while any bridge remains:

- [ ] **Step 1: Write the guard test** (append to `tests/test_configuration.py`)

```python
class NoTextBridgeTests(unittest.TestCase):
    def test_no_text_tool_call_bridge_remains(self):
        self.assertFalse((ROOT / "plugins/model-providers").exists())
        self.assertFalse(
            (ROOT / "plugins/goncloud-team/subscriptions/conversation.py").exists()
        )
        for path in (ROOT / "plugins").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in ("tool_bridge", "_REISSUE_PROMPT", "_TOOL_BRIDGE",
                           "serves_conversations", "<tool_call>"):
                self.assertNotIn(marker, text, f"{marker} in {path}")
```

- [ ] **Step 2: Run to verify it fails**

Run: `$PY -m unittest tests.test_configuration.NoTextBridgeTests -v`
Expected: FAIL (`plugins/model-providers` exists).

- [ ] **Step 3: Delete and simplify**

```bash
git rm -r plugins/model-providers/goncloud-subscriptions plugins/goncloud-team/subscriptions/conversation.py tests/test_subscription_model_provider.py tests/test_subscription_conversation.py
```

Then edit, keeping the `tool_bridge=False` path of every branch:
- `types.py`: delete the `tool_bridge: bool = False` field and its comment.
- `router.py`: delete `_CONVERSATION_TIMEOUT_COOLDOWN_MINUTES` and its comment; in the timeout cooldown use `_TIMEOUT_COOLDOWN_MINUTES` directly; delete the `if request.tool_bridge and not getattr(adapter, "serves_conversations", True):` branch (turn the following `elif cooling:` into `if cooling:`). **Keep** the "Name the real reason" cooldown code from `fc3a8be`.
- `adapters/opencode.py`: delete `_TOOL_BRIDGE_ENV` and its comment, `_REISSUE_PROMPT`, `_as_text_tool_calls`, `_has_tool_call`, the `rejected_native_call` variable, its dict key and the `tool_use` state check (keep `continue` for `tool_use` events), the `env_overlay=` argument in `_run`, and the reissue block and `_as_text_tool_calls` call in `invoke`.
- `adapters/claude.py`: delete the `*(["--tools", "", "--strict-mcp-config"] if request.tool_bridge else [])` element and the `if request.tool_bridge` branch at line ~122, keeping its else-value.
- `adapters/codex.py`: delete `_TOOL_BRIDGE_ARGS`; the sandbox argument becomes `"workspace-write"`; drop `*(_TOOL_BRIDGE_ARGS if ...)`.
- `adapters/grok.py`: delete `*(["--tools", "none"] if request.tool_bridge else [])`.
- `adapters/kimi.py`: `kimi_agent_definition(prompt: str) -> str` without the parameter and its `if tool_bridge:` block; update both call sites (~258, ~277) to the non-bridge form.
- `adapters/muse.py`: delete `serves_conversations = False`, its comment, and the `if request.tool_bridge:` block.

Remove the bridge-only tests. List them with:

```bash
grep -n "tool_bridge\|serves_conversations\|_REISSUE\|as_text_tool_calls\|rejected_native_call\|conversation-" tests/test_subscription_opencode.py tests/test_subscription_router.py tests/test_subscription_vendor_adapters.py
```

Delete each test method whose request or assertion uses one of those names. Where a test only passes `tool_bridge=False` explicitly, drop the argument and keep the test.

- [ ] **Step 4: Run the guard and the full suite**

Run: `$PY -m unittest tests.test_configuration.NoTextBridgeTests -v` → OK.
Run: `$PY -m unittest discover -s tests 2>&1 | tail -3` → `OK (skipped=3)`. The count drops only by the deleted bridge tests. Report the before and after counts.

- [ ] **Step 5: Commit**

```bash
git add -A plugins tests
git commit -m "refactor: remove the text tool-call bridge the director no longer uses"
```

---

### Task 5: Record it

**Files:** `docs/status.md`, `docs/model-routing.md`, `docs/superpowers/specs/2026-09-28-director-native-chain-design.md`

- [ ] **Step 1:** In the spec, replace "The director (`claw`, and `entrega` through inheritance)" with "The director (`claw`)", and add under Decision: "`entrega` inherits the `main` row as a worker stage and stays on the router."
- [ ] **Step 2:** At the top of `docs/status.md`, add a section "Director en proveedores nativos". Cover: the cause (the text bridge, 8 of 13 calls dropped on 2026-09-28), the fix (chain compiled from `main`, native tool calls), what was removed, and the test count. Remove or mark superseded the section "Enrutamiento conversacional exacto, desplegado".
- [ ] **Step 3:** In `docs/model-routing.md`, state that the director's conversation uses native Hermes providers compiled from `main`, and workers use the subscription router.
- [ ] **Step 4:** `git add docs && git commit -m "docs: record the director's native chain"`

---

### Task 6: Rollout (lead only; needs the owner)

- [ ] **Step 1: Credentials (owner).** Run `hermes -p claw auth list` and `hermes -p claw auth status anthropic`. Missing today: a second `opencode-go` key and a `zai` Coding Plan key; `anthropic` is unconfirmed. Kimi's two keys show `permission_error (403)`. Ask the owner to run, in the Claude Code prompt:
  `! hermes -p claw auth add opencode-go`, `! hermes -p claw auth add zai`, and `! hermes -p claw auth add anthropic` if status shows no Claude Code OAuth. Check `hermes auth add --help` first for the exact flags.
- [ ] **Step 2: Dry-run the deploy on a copy.** Copy `%LOCALAPPDATA%\hermes\profiles` into the session scratchpad (`<scratch>\runtime\profiles`), then run `configure_team.py --home <scratch>\runtime --install-dir "$env:LOCALAPPDATA\hermes\hermes-agent"`. Diff every `config.yaml` and `goncloud-managed.json` against the live copies. The only changes allowed are: claw's `model`/`fallback_providers`/`agent.reasoning_overrides`, retired `plugins/model-providers/...` files, and the updated plugin files. Stop on anything else.
- [ ] **Step 3: Live probe per chain entry** (before restarting the gateway). For each entry of the compiled chain, run
  `hermes -p claw chat -Q --provider <provider> -m <model> -t terminal --max-turns 3 -q "Use the terminal tool to run: echo NATIVE_OK. Reply with its output only."`
  PASS requires `NATIVE_OK` in the output **and** a `tool terminal completed` line in `profiles/claw/logs/agent.log` after the probe started. For `zai`, pass the coding base URL if `chat --help` offers `--base-url`. Record the results in `docs/status.md`.
- [ ] **Step 4: Deploy.** Follow the memory `goncloud-hermes-telegram-session-reset`: refresh PATH and the CA vars from the registry, confirm the director is idle (no turn in the last minutes in `gateway.log`), run `configure_team.py` from this worktree, `hermes -p claw gateway stop`, start `Hermes_Gateway_claw.vbs`, check `gateway status`, then `plugins doctor goncloud-team --ci` (9 tools, 1 hook).
- [ ] **Step 5: Owner check.** The owner sends `/new`, then a message that needs a tool. Confirm `provider=opencode-go` in `agent.log` and a `tool ... completed` line.
- [ ] **Step 6: Merge and push.** Fast-forward `feat/hermes-team` to this branch, push, and remove the worktree. Rollback, if needed: redeploy `a97a1bf` with `--allow-regression`.
- [ ] **Step 7:** Update the memory `goncloud-hermes-telegram-session-reset`: set the deployed tree, and state that the director is native and the bridge is gone.
