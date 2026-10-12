# Phase 0A: Unblock the hermes installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/configure_team.py` deploy again from one branch by adopting the hand edits made on the PC on 2026-10-03, with zero effective change to the running profiles.

**Architecture:** The installer refuses to write when a managed value on disk equals neither what the repo wants nor what it last applied. The repo is changed until what it wants equals what is live, so the conflict disappears without touching the PC. A new `--check` flag proves that equality against the live profiles before anything is merged or written.

**Tech Stack:** Python 3 (`unittest`, the Hermes runtime's venv on the PC), PowerShell over SSH, `gh`, `pre-commit`.

**Spec:** `docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md` (phase 0A).

## Global Constraints

- No model is renamed, added or removed. `model-routing.source.json` is not touched.
- No model inference, no probe that spends tokens. Never run `configure_subscriptions.py --live-probe-all`.
- Edit only in the Mac clone `/Users/dn/dev/goncloud-hermes`, on branch `fix/adopt-live-state`. It already exists on `origin`: it is `origin/feat/bot-mode-messaging` (`ab65f66`, the revision deployed on the PC) plus one commit that adds the spec and this plan.
- Tests need the Hermes runtime, which exists only on the PC (`ehven@10.13.13.4`). Run them in the PC worktree `C:\dev\goncloud-hermes-fase0a`. Do not edit, switch or pull `C:\dev\goncloud-hermes` (the deploy checkout) before Task 5.
- Before Task 5 Step 5, nothing under `%LOCALAPPDATA%\hermes` is written. `configure_team.py` runs against the live home only with `--check`.
- Never pass `--allow-regression` or `--preserve-local-soul`. If a check reports a conflict, stop and report the path.
- Never use `--no-verify`. Run `pre-commit run --files <changed files>` before each commit.
- Do not read or print `.env`, `auth.json`, keys or tokens. Do not read `~/.ssh`. No `sudo`.
- Merge with `gh pr merge --merge` only. A squash or rebase drops `ab65f66` from history and `refuse_regression` then rejects the deploy.
- Review is the assigned reviewer plus the PR bots. No cross-model second review.
- Task 5 Step 5 writes to production profiles. It needs the owner's go for this run.

Shell helpers used by every task (paste once per shell on the Mac):

```bash
pcrun() {  # run one PowerShell command on the PC
  local enc
  enc=$(printf '%s' "$1" | iconv -t UTF-16LE | base64 | tr -d '\n')
  ssh -o BatchMode=yes -o ConnectTimeout=10 ehven@10.13.13.4 \
    "powershell -NoProfile -EncodedCommand $enc" 2>&1 | grep -v -E '^#< CLIXML|<Objs'
}
pctest() {  # $1 = test file pattern, $2 = optional -k filter
  local k=""
  [ -n "$2" ] && k="-k $2"
  pcrun "Set-Location C:\\dev\\goncloud-hermes-fase0a; git fetch -q origin; git checkout -q --detach origin/fix/adopt-live-state; & \"\$env:LOCALAPPDATA\\hermes\\hermes-agent\\venv\\Scripts\\python.exe\" -m unittest discover -s tests -p \"$1\" $k 2>&1 | ForEach-Object { \"\$_\" } | Select-Object -Last 30"
}
mkdir -p "${TMPDIR:-/tmp}/fase0a"
```

`pctest` always tests what is pushed, so push before each run.

---

### Task 1: Adopt the eleven specialist roles

On 2026-10-03 eleven roles were appended by hand to `TEAM_ROLES` in all 20 profile copies of `workflow.py`. The installer compares file content exactly, so the repo must carry the same eleven lines in the same order.

**Files:**
- Modify: `plugins/goncloud-team/workflow.py:40-50`
- Test: `tests/test_workflow.py` (new class before the final `if __name__` block)

**Interfaces:**
- Consumes: nothing.
- Produces: `TEAM_ROLES` (tuple of 20 role names); the PC worktree `C:\dev\goncloud-hermes-fase0a`; `${TMPDIR:-/tmp}/fase0a/baseline.txt`.

- [ ] **Step 1: Check out the branch, create the PC worktree and the baseline**

```bash
cd /Users/dn/dev/goncloud-hermes
git fetch origin
git switch fix/adopt-live-state
git pull --ff-only
pcrun 'git -C C:\dev\goncloud-hermes fetch -q origin; git -C C:\dev\goncloud-hermes worktree add --detach C:\dev\goncloud-hermes-fase0a origin/fix/adopt-live-state; git -C C:\dev\goncloud-hermes status -sb'
pctest 'test_*.py' | tee "${TMPDIR:-/tmp}/fase0a/baseline.txt"
```

Expected: the `status -sb` line still reads `## feat/bot-mode-messaging...origin/feat/bot-mode-messaging` with no modified files (the deploy checkout is untouched), and the last line of the test run starts with `OK`. If the baseline does not end in `OK`, stop and report the failing test names; they are not this plan's to fix.

- [ ] **Step 2: Write the failing test**

Insert in `tests/test_workflow.py` immediately above the final `if __name__ == "__main__":` line:

```python
class SpecialistRolesTests(unittest.TestCase):
    def test_the_eleven_specialist_profiles_are_team_roles(self):
        # 2026-10-03: the owner created eleven specialist profiles on the PC
        # and extended TEAM_ROLES by hand in every profile copy; adopted
        # 2026-10-12. The order is the live one: a redeploy compares the file
        # content exactly, so a reordered tuple reads as a local conflict.
        from workflow import TEAM_ROLES

        self.assertEqual(
            TEAM_ROLES[9:],
            (
                "amazon",
                "meli",
                "mail",
                "limpia",
                "odoo",
                "pers",
                "arras",
                "gonserver",
                "skillverif",
                "tinkabot",
                "eggbot",
            ),
        )


```

- [ ] **Step 3: Run the test to verify it fails**

```bash
pre-commit run --files tests/test_workflow.py
git add tests/test_workflow.py && git commit -m "test: specialist profiles are team roles" && git push
pctest 'test_workflow.py' specialist
```

Expected: `FAIL: test_the_eleven_specialist_profiles_are_team_roles` with `AssertionError: () != ('amazon', 'meli', ...`.

- [ ] **Step 4: Add the eleven roles**

In `plugins/goncloud-team/workflow.py`, replace

```python
    "reviewer",
    "entrega",
)
INITIAL_STAGES = (
```

with

```python
    "reviewer",
    "entrega",
    "amazon",
    "meli",
    "mail",
    "limpia",
    "odoo",
    "pers",
    "arras",
    "gonserver",
    "skillverif",
    "tinkabot",
    "eggbot",
)
INITIAL_STAGES = (
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
pre-commit run --files plugins/goncloud-team/workflow.py
git add plugins/goncloud-team/workflow.py && git commit -m "fix: adopt the eleven specialist roles added on the PC" && git push
pctest 'test_workflow.py'
```

Expected: last line starts with `OK`.

- [ ] **Step 6: Prove the file now equals the live copies**

```bash
pcrun '$want=(Get-Content C:\dev\goncloud-hermes-fase0a\plugins\goncloud-team\workflow.py -Raw) -replace "`r",""; Get-ChildItem "$env:LOCALAPPDATA\hermes\profiles\*\plugins\goncloud-team\workflow.py" | ForEach-Object { $live=(Get-Content $_.FullName -Raw) -replace "`r",""; "{0} {1}" -f ($live -ceq $want), $_.Directory.Parent.Parent.Name }'
```

Expected: 20 lines, every one starting with `True`. A `False` means that profile's copy differs from the repo; stop and report which.

---

### Task 2: Adopt the director's kanban toolset and gateway dispatch

The live `claw` profile carries the `kanban` toolset and `kanban.dispatch_in_gateway: true`. Workers keep dispatch off. This reverses two choices recorded on 2026-09-15 and 2026-09-27; the spec lists the risks. This task records the live state; it does not judge it.

**Files:**
- Modify: `team.json`
- Modify: `scripts/configure_team.py:22-46` and `:328-347`
- Modify: `README.md:58`
- Test: `tests/test_configuration.py:170-177` and `:263-268`

**Interfaces:**
- Consumes: nothing.
- Produces: `team["director_kanban"]` (dict merged over `team["kanban"]` for the director only); `desired_profile_config(role, team, runtime_home)` returns `kanban.dispatch_in_gateway` `True` for the director and `False` for every other role.

- [ ] **Step 1: Rewrite the two tests**

In `tests/test_configuration.py`, replace the whole method `test_gateway_never_dispatches_goncloud_stage_cards_natively` with:

```python
    def test_only_the_director_gateway_dispatches_cards(self):
        # Until 2026-10-03 dispatch was off everywhere: the gateway's Kanban
        # dispatcher claims any `ready` card with an assignee, and stage cards
        # carry their role as assignee, so it could race the local
        # orchestrator. On 2026-10-03 the owner turned it on for the director
        # so cards for the specialist profiles run; adopted 2026-10-12. Workers
        # keep it off. Whether the director's dispatcher can claim a stage
        # card is an open risk in
        # docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md.
        team = json.loads((ROOT / "team.json").read_text(encoding="utf-8"))

        self.assertIs(team["kanban"].get("dispatch_in_gateway"), False)
        worker = module.desired_profile_config("scout", team, Path("C:/home"))
        director = module.desired_profile_config(
            team["director"], team, Path("C:/home")
        )
        self.assertIs(worker["kanban"]["dispatch_in_gateway"], False)
        self.assertIs(director["kanban"]["dispatch_in_gateway"], True)

        def without_dispatch(kanban):
            return {k: v for k, v in kanban.items() if k != "dispatch_in_gateway"}

        self.assertEqual(
            without_dispatch(director["kanban"]), without_dispatch(worker["kanban"])
        )
```

In `test_director_gets_the_full_toolset_and_workers_the_baseline`, replace

```python
        # The director may not create or dispatch cards (goncloud_local_run
        # does), yet the 14 kanban schemas cost 23.5 KB of every call
        # (hermes prompt-size, 2026-09-27). Workers keep them.
        self.assertNotIn("kanban", director["platform_toolsets"]["telegram"])
        self.assertIn("goncloud_team", director["platform_toolsets"]["telegram"])
        self.assertIn("kanban", worker["platform_toolsets"]["telegram"])
```

with

```python
        # 2026-10-03: the owner gave the director the kanban toolset so it can
        # hand work to a specialist profile with a card; adopted 2026-10-12.
        # Its 14 schemas cost 23.5 KB of every director call (hermes
        # prompt-size, 2026-09-27). The order is the live one: a redeploy
        # compares the whole list.
        self.assertEqual(
            director["platform_toolsets"]["telegram"],
            [
                "browser",
                "clarify",
                "code_execution",
                "coding",
                "computer_use",
                "connections",
                "delegation",
                "file",
                "goncloud_team",
                "kanban",
                "memory",
                "session_search",
                "skills",
                "terminal",
                "todo",
                "vision",
                "web",
            ],
        )
        self.assertIn("kanban", worker["platform_toolsets"]["telegram"])
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pre-commit run --files tests/test_configuration.py
git add tests/test_configuration.py && git commit -m "test: director dispatches cards and carries kanban" && git push
pctest 'test_configuration.py' director
```

Expected: two failures. `test_only_the_director_gateway_dispatches_cards` fails with `AssertionError: False is not True`, and `test_director_gets_the_full_toolset_and_workers_the_baseline` fails on a list that lacks `'kanban'`.

- [ ] **Step 3: Add the director override to `team.json`**

Replace the end of `team.json`

```json
    "failure_limit": 2,
    "dispatch_in_gateway": false
  }
}
```

with

```json
    "failure_limit": 2,
    "dispatch_in_gateway": false
  },
  "director_kanban": {
    "dispatch_in_gateway": true
  }
}
```

- [ ] **Step 4: Apply it in `scripts/configure_team.py`**

Replace the comment and list that start at line 22

```python
#: The director drives Odoo and Edge over Telegram (owner decision 2026-09-15),
#: so it carries the full toolset — browser and computer_use included. Baking it
#: in stops the director from re-editing its own config every session. Its role
#: text, not a narrow toolset, keeps it out of logs and plugin code. No kanban:
#: the director may not create or dispatch cards (goncloud_local_run does and
#: goncloud_local_status reads them), and its 14 schemas cost 23.5 KB of every
#: call (hermes prompt-size, 2026-09-27).
_DIRECTOR_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "coding",
    "computer_use",
    "connections",
    "delegation",
    "file",
    "goncloud_team",
    "memory",
```

with

```python
#: The director drives Odoo and Edge over Telegram (owner decision 2026-09-15),
#: so it carries the full toolset — browser and computer_use included. Baking it
#: in stops the director from re-editing its own config every session. Its role
#: text, not a narrow toolset, keeps it out of logs and plugin code. kanban is
#: in since 2026-10-03 (owner; adopted 2026-10-12) so the director can hand
#: work to a specialist profile with a card; its 14 schemas cost 23.5 KB of
#: every call (hermes prompt-size, 2026-09-27). The order is the live one.
_DIRECTOR_TOOLSETS = [
    "browser",
    "clarify",
    "code_execution",
    "coding",
    "computer_use",
    "connections",
    "delegation",
    "file",
    "goncloud_team",
    "kanban",
    "memory",
```

In `desired_profile_config`, replace

```python
        desired["platform_toolsets"] = {
            "cli": list(_DIRECTOR_TOOLSETS),
            "telegram": list(_DIRECTOR_TOOLSETS),
        }
        desired["auxiliary"] = copy.deepcopy(_DIRECTOR_VISION_AUXILIARY)
```

with

```python
        desired["platform_toolsets"] = {
            "cli": list(_DIRECTOR_TOOLSETS),
            "telegram": list(_DIRECTOR_TOOLSETS),
        }
        # Only the director's gateway dispatches cards (owner, 2026-10-03):
        # that is what runs a card assigned to a specialist profile.
        desired["kanban"] = {**team["kanban"], **team.get("director_kanban", {})}
        desired["auxiliary"] = copy.deepcopy(_DIRECTOR_VISION_AUXILIARY)
```

- [ ] **Step 5: Correct the README sentence**

In `README.md` line 58, replace

```
El despachador nativo del gateway esta apagado (`kanban.dispatch_in_gateway: false`) para que ninguna etapa corra fuera del router.
```

with

```
El despachador nativo del gateway esta apagado en los trabajadores (`kanban.dispatch_in_gateway: false`) para que ninguna etapa corra fuera del router, y encendido en el director desde 2026-10-03 (`director_kanban` en `team.json`) para que corran las tarjetas de los especialistas; si puede tomar una tarjeta de etapa es un riesgo abierto en `docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md`.
```

- [ ] **Step 6: Run the tests to verify they pass**

```bash
pre-commit run --files team.json scripts/configure_team.py README.md
git add team.json scripts/configure_team.py README.md && git commit -m "fix: adopt the director's kanban toolset and gateway dispatch" && git push
pctest 'test_configuration.py'
```

Expected: last line starts with `OK`. `test_bot_mode_messages_never_carry_engineering_work` still passes here; Task 3 changes it.

---

### Task 3: Adopt the director's live role text

`SOUL.md` is composed as `shared/CONTRACT.md`, `shared/BUSINESS.md` and `roles/<role>.md` joined by blank lines. The live `claw` file differs only inside the role part: one paragraph was replaced and a section `# Especialistas` was appended. The role file is derived from the live file so the composed text is equal by construction.

**Files:**
- Modify: `roles/claw.md`
- Test: `tests/test_configuration.py` (`test_bot_mode_messages_never_carry_engineering_work`, plus one new test after it)

**Interfaces:**
- Consumes: nothing.
- Produces: `roles/claw.md` such that CONTRACT + BUSINESS + role equals the live `profiles\claw\SOUL.md` after newline normalization.

- [ ] **Step 1: Rewrite the assertion and add the new test**

In `test_bot_mode_messages_never_carry_engineering_work`, replace

```python
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertIn("el trabajo tecnico entra solo por goncloud_local_run", claw)
```

with

```python
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertIn(
            "El codigo sigue por goncloud_local_run, no por estas tarjetas", claw
        )

    def test_the_director_hands_specialist_work_to_a_card(self):
        # 2026-10-03 (adopted 2026-10-12): work that belongs to a specialist
        # profile goes to it through a kanban card; from Telegram there is no
        # message_agent. Code still goes through the chain.
        claw = (ROOT / "roles/claw.md").read_text(encoding="utf-8")
        self.assertIn("hermes kanban create --assignee", claw)
        self.assertIn("# Especialistas", claw)
        for specialist in ("amazon", "meli", "odoo", "gonserver", "eggbot"):
            with self.subTest(specialist=specialist):
                self.assertIn(f"- {specialist}:", claw)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
pre-commit run --files tests/test_configuration.py
git add tests/test_configuration.py && git commit -m "test: director hands specialist work to a card" && git push
pctest 'test_configuration.py' "bot_mode or specialist_work"
```

Expected: both tests fail with `AssertionError: ... not found in`.

- [ ] **Step 3: Derive `roles/claw.md` from the live file**

```bash
cd /Users/dn/dev/goncloud-hermes
pcrun '[Convert]::ToBase64String([IO.File]::ReadAllBytes("$env:LOCALAPPDATA\hermes\profiles\claw\SOUL.md"))' \
  | tr -d '\r\n' | base64 -d > "${TMPDIR:-/tmp}/fase0a/claw-SOUL.live.md"
python3 - <<'EOF'
import os
import pathlib

live_path = pathlib.Path(os.environ.get("TMPDIR", "/tmp"), "fase0a/claw-SOUL.live.md")
live = live_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
prefix = (
    "\n\n".join(
        pathlib.Path(name).read_text(encoding="utf-8")
        for name in ("shared/CONTRACT.md", "shared/BUSINESS.md")
    )
    + "\n\n"
)
assert live.startswith(prefix), "live SOUL no longer starts with CONTRACT + BUSINESS"
pathlib.Path("roles/claw.md").write_text(live[len(prefix):], encoding="utf-8")
EOF
git diff --stat roles/claw.md
```

Expected: `1 file changed, 21 insertions(+), 1 deletion(-)`. The deleted line is the paragraph that began "No conviertas a David en mensajero. Los especialistas se consultan entre si"; the inserted lines are its replacement (beginning "No conviertas a David en mensajero. El trabajo de un especialista no lo haces tu.") and the section `# Especialistas` with twelve bullets. If the assertion fires or the stat differs, stop: the live file changed after 2026-10-12 and the owner must see the new diff first.

- [ ] **Step 4: Run the tests to verify they pass**

```bash
pre-commit run --files roles/claw.md
git add roles/claw.md && git commit -m "fix: adopt the director role text edited on the PC" && git push
pctest 'test_configuration.py'
```

Expected: `pre-commit` reports `Passed` for every hook and never prints `files were modified by this hook` (a hook that rewrites the file would break equality with the live one; if that happens, stop and report), and the test run's last line starts with `OK`.

---

### Task 4: Add `configure_team.py --check`

Validation already runs for every profile before the first write. `--check` stops there, so the same code that would refuse a deploy can be run against the live profiles safely.

**Files:**
- Modify: `scripts/configure_team.py:351-358` (signature), `:384-385` (profile creation), `:458` (early return), `:516-548` (argument)
- Modify: `README.md:44-48`
- Test: `tests/test_configuration.py` (new method in `ConfigurationTests`, after `test_real_profiles_apply_twice_without_touching_credentials`)

**Interfaces:**
- Consumes: nothing.
- Produces: `configure(..., check_only=False)`; CLI flag `--check`, which exits 0 and prints `Check passed: <n> profiles would deploy <revision>` when every profile validates, exits non-zero with the existing conflict message otherwise, and writes nothing in both cases.

- [ ] **Step 1: Write the failing test**

```python
    def test_check_validates_without_writing(self):
        upstream = Path(
            os.environ.get(
                "HERMES_INSTALL_DIR",
                Path(os.environ.get("LOCALAPPDATA", "")) / "hermes/hermes-agent",
            )
        )
        self.assertTrue(
            (upstream / "hermes_cli/profiles.py").is_file(),
            "Run tests with the installed Hermes runtime",
        )
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "hermes-test"
            command = [
                sys.executable,
                str(ROOT / "scripts/configure_team.py"),
                "--home",
                str(runtime),
                "--install-dir",
                str(upstream),
            ]
            env = {**os.environ, "PYTHONUTF8": "1"}

            fresh = subprocess.run(
                command + ["--check"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertEqual(fresh.returncode, 0, fresh.stderr)
            self.assertIn("Check passed: 9 profiles", fresh.stdout)
            self.assertFalse((runtime / "profiles" / "claw").exists())

            applied = subprocess.run(
                command, capture_output=True, text=True, env=env, timeout=120
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            soul = runtime / "profiles/scout/SOUL.md"
            soul.write_text("user edit", encoding="utf-8")
            state = runtime / "profiles/claw/goncloud-managed.json"
            recorded = state.read_bytes()

            conflict = subprocess.run(
                command + ["--check"],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn("Local managed-file conflict", conflict.stderr)
            self.assertEqual(soul.read_text(encoding="utf-8"), "user edit")
            self.assertEqual(state.read_bytes(), recorded)
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
pre-commit run --files tests/test_configuration.py
git add tests/test_configuration.py && git commit -m "test: --check validates without writing" && git push
pctest 'test_configuration.py' check_validates
```

Expected: `FAIL` on the first `assertEqual(fresh.returncode, 0, ...)`, with `unrecognized arguments: --check` in the message.

- [ ] **Step 3: Implement the flag**

In `scripts/configure_team.py`, replace the signature

```python
    allow_regression=False,
    preserve_local_soul=False,
):
    import yaml
```

with

```python
    allow_regression=False,
    preserve_local_soul=False,
    check_only=False,
):
    import yaml
```

Replace

```python
        if fresh:
            create_profile(role, no_alias=True, description=description)
```

with

```python
        if fresh and not check_only:
            create_profile(role, no_alias=True, description=description)
```

Replace

```python
    # Validate every existing profile before changing any managed content.
    for (
```

with

```python
    # Every profile validated above; nothing has been written yet.
    if check_only:
        print(f"Check passed: {len(prepared)} profiles would deploy {revision}")
        return
    for (
```

In `main()`, replace

```python
    args = parser.parse_args()
    configure(
        args.home,
        args.install_dir,
        args.apply_model_routing,
        allow_regression=args.allow_regression,
        preserve_local_soul=args.preserve_local_soul,
    )
```

with

```python
    parser.add_argument(
        "--check",
        action="store_true",
        help="Validate every profile against this checkout and exit without writing",
    )
    args = parser.parse_args()
    configure(
        args.home,
        args.install_dir,
        args.apply_model_routing,
        allow_regression=args.allow_regression,
        preserve_local_soul=args.preserve_local_soul,
        check_only=args.check,
    )
```

- [ ] **Step 4: Document it**

In `README.md`, replace the paragraph start

```
Reaplicar es idempotente. El configurador retira
```

with

```
Antes de cada despliegue corre el mismo comando con `--check`: valida los nueve perfiles contra este checkout y sale sin escribir nada; si reporta un conflicto, no hay despliegue hasta reconciliarlo.

Reaplicar es idempotente. El configurador retira
```

- [ ] **Step 5: Run the test to verify it passes**

```bash
pre-commit run --files scripts/configure_team.py README.md
git add scripts/configure_team.py README.md && git commit -m "feat: configure_team --check validates without writing" && git push
pctest 'test_configuration.py'
```

Expected: last line starts with `OK`.

---

### Task 5: Prove it against the live profiles, converge on one branch, redeploy

**Files:**
- Modify: `docs/status.md` (new section at the top)
- No code changes.

**Interfaces:**
- Consumes: `--check` (Task 4); a branch whose desired state equals the live state (Tasks 1 to 3).
- Produces: default branch `feat/hermes-team` containing `ab65f66` and this work; the PC deploy checkout on `feat/hermes-team`; nine profiles whose `goncloud-managed.json` records the new revision.

- [ ] **Step 1: Run the full battery and the hooks**

```bash
cd /Users/dn/dev/goncloud-hermes
pctest 'test_*.py' | tee "${TMPDIR:-/tmp}/fase0a/final.txt"
pre-commit run --all-files
git diff --check origin/feat/bot-mode-messaging...HEAD
```

Expected: the test run's last line starts with `OK`; the count of tests is the baseline's plus 3; every hook `Passed`; `git diff --check` prints nothing.

- [ ] **Step 2: Check the branch against the live profiles (read-only)**

```bash
pcrun 'Set-Location C:\dev\goncloud-hermes-fase0a; git fetch -q origin; git checkout -q --detach origin/fix/adopt-live-state; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
```

Expected: exactly `Check passed: 9 profiles would deploy <40-hex sha>`. This is the gate for the whole plan. `Local configuration conflict: <key>` or `Local managed-file conflict: <path>` means the repo does not yet equal the live state: stop, report the key or path, and do not continue to Step 3.

- [ ] **Step 3: Record the result in `docs/status.md`**

Insert at the very top of `docs/status.md`, above the current first line:

```markdown
# Estado — 2026-10-12

## El instalador vuelve a desplegar

Causa: el 2026-10-03 se editaron a mano en la PC el perfil `claw` (toolset
`kanban`, `kanban.dispatch_in_gateway: true`, `SOUL.md` con la seccion
"Especialistas") y `workflow.py` en las 20 copias (11 roles nuevos). Ninguna
edicion estaba en el repo, asi que `configure_team.py` abortaba con
`Local configuration conflict` y `Local managed-file conflict`.

Arreglo: el repo adopta el estado vivo por decision del propietario
(2026-10-12) y `configure_team.py --check` valida contra los perfiles sin
escribir. El despliegue no cambia el comportamiento de ningun perfil. Los 11
perfiles especialistas siguen fuera de `team.json`: su `config.yaml` y su
`SOUL.md` no son administrados. Riesgos que quedan abiertos (carrera del
despachador con las tarjetas de etapa, costo de los esquemas kanban, texto
contradictorio en el rol del director):
`docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md`.

```

```bash
pre-commit run --files docs/status.md
git add docs/status.md && git commit -m "docs: record the adopted live state" && git push
```

- [ ] **Step 4: Merge to one branch**

```bash
gh pr create --base feat/bot-mode-messaging --head fix/adopt-live-state \
  --title "fix: adopt the live state so the installer deploys again" \
  --body "Adopts the 2026-10-03 hand edits (11 roles, director kanban + gateway dispatch, director role text) and adds configure_team.py --check. No model changes. Spec: docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md. Verified: configure_team.py --check passes against the live profiles from this branch."
gh pr merge fix/adopt-live-state --merge
gh pr create --base feat/hermes-team --head feat/bot-mode-messaging \
  --title "chore: bring the default branch up to the deployed revision" \
  --body "feat/hermes-team was two commits behind what the PC runs. Merge commit only: the deployed revision ab65f66 must stay an ancestor or refuse_regression rejects the deploy."
gh pr merge feat/bot-mode-messaging --merge
git fetch origin && git merge-base --is-ancestor ab65f66 origin/feat/hermes-team && echo "ab65f66 is an ancestor"
```

Expected: both PRs merged after their checks and review pass; the last command prints `ab65f66 is an ancestor`.

- [ ] **Step 5: Redeploy on the PC (needs the owner's go)**

Back up, move the deploy checkout to the default branch, check, apply:

```bash
pcrun '$h="$env:LOCALAPPDATA\hermes"; $b="$h\backups\pre-fase0a-" + (Get-Date -Format yyyyMMdd-HHmmss); foreach($p in "claw","operaciones","ingenieria","scout","implementer","verifier","adversary","reviewer","entrega"){ New-Item -ItemType Directory -Force "$b\$p" | Out-Null; Copy-Item "$h\profiles\$p\config.yaml","$h\profiles\$p\SOUL.md","$h\profiles\$p\goncloud-managed.json" "$b\$p" }; $b; (Get-ChildItem $b -Recurse -File).Count; Get-Content "$h\gateway.pid"'
pcrun 'git -C C:\dev\goncloud-hermes fetch -q origin; git -C C:\dev\goncloud-hermes switch feat/hermes-team; git -C C:\dev\goncloud-hermes pull --ff-only; git -C C:\dev\goncloud-hermes status -sb'
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
```

Expected: the backup path, then `27` (three files for each of nine profiles), then the gateway PID; `## feat/hermes-team...origin/feat/hermes-team` with no modified files; `Check passed: 9 profiles would deploy <sha>`. Only if the check passed:

```bash
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py 2>&1 | ForEach-Object { "$_" }'
```

Expected: nine lines `Configured <role>: C:\Users\ehven\AppData\Local\hermes\profiles\<role>`.

- [ ] **Step 6: Verify the deploy changed nothing that runs**

```bash
pcrun '$h="$env:LOCALAPPDATA\hermes"; $head=(git -C C:\dev\goncloud-hermes rev-parse HEAD); foreach($p in "claw","operaciones","ingenieria","scout","implementer","verifier","adversary","reviewer","entrega"){ $s=(Get-Content "$h\profiles\$p\goncloud-managed.json" -Raw | ConvertFrom-Json).source_revision; "{0} {1}" -f ($s -eq $head), $p }; "pid " + (Get-Content "$h\gateway.pid")'
pcrun '$h="$env:LOCALAPPDATA\hermes"; $b=(Get-ChildItem "$h\backups" -Directory -Filter pre-fase0a-* | Sort-Object Name | Select-Object -Last 1).FullName; $a=(Get-Content "$b\claw\SOUL.md" -Raw) -replace "`r",""; $n=(Get-Content "$h\profiles\claw\SOUL.md" -Raw) -replace "`r",""; "soul unchanged: " + ($a -ceq $n); $want=(Get-Content "$h\profiles\claw\plugins\goncloud-team\workflow.py" -Raw) -replace "`r",""; "workflow copies equal: " + (@(Get-ChildItem "$h\profiles\*\plugins\goncloud-team\workflow.py" | Where-Object { ((Get-Content $_.FullName -Raw) -replace "`r","") -cne $want }).Count -eq 0)'
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
```

Expected: nine lines starting with `True`; the same gateway PID as in Step 5 (no restart is needed because no plugin `.py` changed); `soul unchanged: True`; `workflow copies equal: True`; and `Check passed: 9 profiles would deploy <sha>` again.

Rollback, if any line differs: copy the three files of each profile back from the `pre-fase0a-*` directory printed in Step 5, then report. Do not use `--allow-regression`.

- [ ] **Step 7: Remove the PC worktree and close**

```bash
pcrun 'git -C C:\dev\goncloud-hermes worktree remove C:\dev\goncloud-hermes-fase0a; git -C C:\dev\goncloud-hermes worktree list'
git -C /Users/dn/dev/goncloud-hermes switch feat/hermes-team && git -C /Users/dn/dev/goncloud-hermes pull --ff-only
```

Expected: `worktree list` shows only `C:/dev/goncloud-hermes`. Report to the owner in three lines: the installer deploys again from `feat/hermes-team`; nothing that runs changed; the three open risks in the spec remain for phase 0B.
