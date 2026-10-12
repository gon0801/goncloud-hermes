# Phase 0A: Unblock the hermes installer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `scripts/configure_team.py` deploy again from one branch by adopting the hand edits made on the PC on 2026-10-03, and correct the parts of them that were wrong.

**Architecture:** The installer refuses to write when a managed value on disk equals neither what the repo wants nor what it last applied. The repo is changed until what it wants equals what is live, so the conflict disappears. Corrections to hand-edited text need two revisions: one that adopts the text verbatim, so the installer records it, and one that changes it. A new `--check` flag proves the repo against the live profiles without writing.

**Tech Stack:** Python 3 (`unittest`, the Hermes runtime's venv on the PC), PowerShell over SSH, `git bundle`, `gh`, `pre-commit`.

**Spec:** `docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md` (phase 0A and "Corrections to the 2026-10-03 hand edits").

## Global Constraints

- No model is renamed, added or removed. `model-routing.source.json` is not touched.
- No model inference, no probe that spends tokens. Never run `configure_subscriptions.py --live-probe-all`.
- Edit only in the Mac clone `/Users/dn/dev/goncloud-hermes`, on branch `fix/adopt-live-state` (`origin/feat/bot-mode-messaging` at `ab65f66`, the revision deployed on the PC, plus this work).
- Tests need the Hermes runtime, which exists only on the PC (`ehven@10.13.13.4`). They run in the PC worktree `C:\dev\goncloud-hermes-fase0a`, never in `C:\dev\goncloud-hermes` (the deploy checkout).
- The PC cannot `git fetch` from GitHub in an SSH session (its credential store needs an interactive logon). Commits travel as a `git bundle` over `scp`.
- Before Task 6 Step 3, nothing under `%LOCALAPPDATA%\hermes` is written. `configure_team.py` runs against the live home only with `--check`.
- Never pass `--allow-regression` or `--preserve-local-soul`. If a check reports a conflict, stop and report the path.
- Never use `--no-verify`. Run `pre-commit run --files <changed files>` before each commit.
- Do not read or print `.env`, `auth.json`, keys or tokens. Do not read `~/.ssh`. No `sudo`.
- Merge with `gh pr merge --merge` only. A squash or rebase drops `ab65f66` from history and `refuse_regression` then rejects the deploy.
- Review is the assigned reviewer plus the PR bots. No cross-model second review.
- Task 6 Steps 2 to 4 merge to the default branch and write to production profiles. They need the owner's go.

Shell helpers (paste once per shell on the Mac):

```bash
B="${TMPDIR:-/tmp}/fase0a.bundle"
pcrun() {  # run one PowerShell command on the PC
  local enc
  enc=$(printf '%s' "$1" | iconv -t UTF-16LE | base64 | tr -d '\n')
  ssh -o BatchMode=yes -o ConnectTimeout=10 ehven@10.13.13.4 \
    "powershell -NoProfile -EncodedCommand $enc" 2>&1 | grep -a -v -E '^#< CLIXML|<Objs'
}
pcsync() {  # ship the local branch to the PC test worktree
  git -C /Users/dn/dev/goncloud-hermes bundle create -q "$B" origin/feat/bot-mode-messaging..fix/adopt-live-state || return 1
  scp -q -o BatchMode=yes "$B" 'ehven@10.13.13.4:C:/dev/fase0a.bundle' || return 1
  pcrun 'git -C C:\dev\goncloud-hermes fetch -q C:\dev\fase0a.bundle "+fix/adopt-live-state:refs/remotes/bundle/fix/adopt-live-state" 2>&1 | ForEach-Object { "$_" }; if (-not (Test-Path C:\dev\goncloud-hermes-fase0a)) { git -C C:\dev\goncloud-hermes worktree add -q --detach C:\dev\goncloud-hermes-fase0a bundle/fix/adopt-live-state 2>&1 | ForEach-Object { "$_" } } else { git -C C:\dev\goncloud-hermes-fase0a checkout -q --detach bundle/fix/adopt-live-state 2>&1 | ForEach-Object { "$_" } }; "pc worktree at " + (git -C C:\dev\goncloud-hermes-fase0a log --oneline -1)'
}
pctest() {  # $1 = test file pattern, $2 = optional -k filter, $3 = tail lines
  local k=""
  [ -n "$2" ] && k="-k \"$2\""
  pcrun "Set-Location C:\\dev\\goncloud-hermes-fase0a; & \"\$env:LOCALAPPDATA\\hermes\\hermes-agent\\venv\\Scripts\\python.exe\" -m unittest discover -s tests -p \"$1\" $k 2>&1 | ForEach-Object { \"\$_\" } | Select-Object -Last ${3:-12}"
}
```

Baseline on `ab65f66`, run over SSH on 2026-10-12: 444 tests, 2 failures, 2 skips. Both failures are one cause that predates this plan and is not fixed here: `test_subscription_configuration.MigrationTests.test_private_temp_and_replaced_store_have_protected_owner_only_dacl` ("ACE must bind to the file owner"), and `test_discovery.DiscoveryTests.test_discovered_modules_execute_with_local_fixture_imports`, which re-runs the battery and sees that failure.

---

### Task 1: Adopt the eleven specialist roles — done in `eb84452`

**Files:** `plugins/goncloud-team/workflow.py` (`TEAM_ROLES`), `tests/test_workflow.py` (`SpecialistRolesTests`).

- [x] The eleven names sit between `reviewer` and `entrega`, the position they have on the PC. The test pins that order because the installer compares file content exactly.
- [x] Verified: `plugins/goncloud-team/workflow.py` equals the PC's live copy after newline normalization; `pctest 'test_workflow.py' specialist` passes.

### Task 2: Adopt the director's role text verbatim — done in `bff329f`

**Files:** `roles/claw.md`.

- [x] Derived from the live `profiles\claw\SOUL.md` by removing the `shared/CONTRACT.md` + `shared/BUSINESS.md` prefix, so the composed text equals the live file. This revision exists only so the next deploy records the text as applied; Task 5 corrects it.

### Task 3: Guard the real dispatcher switch — done in `db55f73`

**Files:** `team.json` (`kanban.dispatch_profiles`, `director_kanban`), `scripts/configure_team.py` (`_DIRECTOR_TOOLSETS`, `root_kanban_policy`, `apply_root_kanban`, the director's `kanban` override), `tests/test_configuration.py` (`RootKanbanPolicyTests`, `test_the_gateway_may_dispatch_specialist_cards_and_no_stage_card`, `test_only_the_director_profile_says_its_cards_are_dispatched`), `README.md`.

**Interfaces produced:**
- `root_kanban_policy(team) -> dict`: `{"auto_decompose": False, "dispatch_profiles": [...]}` taken from `team["kanban"]`.
- `apply_root_kanban(text, policy) -> str`: sets those keys inside the top-level `kanban:` block of the root `config.yaml` text, in place; keeps every other line and the newline style; appends the block when absent; raises `ValueError` on an inline `kanban: {...}`. Idempotent.

- [x] The gateway dispatcher reads only the root config, where dispatch is on by default. `configure()` now writes the allowlist there before it touches any profile. No chain role is in the allowlist, so a `ready` stage card can no longer be claimed and run natively.
- [x] The director's `kanban` toolset and its `dispatch_in_gateway: true` are adopted as edited on the PC (see the spec for why each stays).

### Task 4: `configure_team.py --check` — done in `db55f73`

**Files:** `scripts/configure_team.py` (`configure(..., check_only=False)`, `--check`), `tests/test_configuration.py` (`test_check_validates_without_writing`), `README.md`.

- [x] `--check` runs the same validation as a deploy, prints `Check passed: <n> profiles would deploy <revision>` and `Root dispatch allowlist: up to date|would be written`, and writes nothing: no profile is created, no root config, no managed file.
- [x] Gate observed on 2026-10-12 from the PC worktree against the live profiles at `db55f73`:
  `Check passed: 9 profiles would deploy db55f73c5e8836c054dc147b630c016fb7222410` and `Root dispatch allowlist: would be written`.

### Task 5: Correct the director's role text — done in `6adb0c0`

**Files:** `roles/claw.md`, `tests/test_configuration.py` (`test_the_director_hands_specialist_work_to_a_card`).

- [x] Three paragraphs and the `# Especialistas` section now say one thing: specialist work goes out with the `kanban_create` tool (it subscribes the chat, so the gateway reports back), only to the eleven allowlisted specialists; `operaciones` and the chain roles never get a director card; chain cards come only from `goncloud_local_run`. The text no longer says the dispatcher is off or that the director has no kanban tools.
- [x] The test ties the section to `team.json`: every allowlisted profile is listed, no chain role is.

---

### Task 6: Full battery, one branch, deploy in two applies — done 2026-10-12, deployed at `acd792e`

**Files:**
- Modify: `docs/status.md` (new section at the top)

**Interfaces:**
- Consumes: revision `db55f73` (adoption; desired state equals live state) and the branch head (corrections).
- Produces: default branch `feat/hermes-team` containing `ab65f66` and this work; nine profiles recorded at the branch head; the allowlist in the root config.

- [x] **Step 1: Full battery and hooks at the branch head**

```bash
cd /Users/dn/dev/goncloud-hermes
pcsync && pctest 'test_*.py' '' 40 | grep -a -E '^(FAIL|ERROR):|^Ran |^OK|^FAILED'
pre-commit run --all-files
git diff --check origin/feat/bot-mode-messaging...HEAD
```

Expected: `Ran 452 tests`, and the only `FAIL:` lines are the two baseline tests named above; every hook `Passed`; `git diff --check` prints nothing. Any other failing test stops the plan.

- [x] **Step 2: Record the result and merge to one branch (needs the owner's go)**

Insert at the very top of `docs/status.md`:

```markdown
# Estado — 2026-10-12

## El instalador vuelve a desplegar y el despachador ya no toca la cadena

Causa: el 2026-10-03 se editaron a mano en la PC el perfil `claw` (toolset
`kanban`, `kanban.dispatch_in_gateway: true`, `SOUL.md` con la seccion
"Especialistas") y `workflow.py` en las 20 copias (11 roles nuevos). Nada de
eso estaba en el repo, asi que `configure_team.py` abortaba con
`Local configuration conflict` y `Local managed-file conflict`.

Al revisarlo aparecio un defecto anterior: el gateway corre un solo
despachador Kanban para todos los perfiles y lee el `config.yaml` raiz, donde
esta encendido por defecto desde 2026-10-02. El `dispatch_in_gateway: false`
de cada perfil nunca le llego, asi que cualquier tarjeta de etapa en `ready`
podia correr con un agente nativo, fuera del router. No ocurrio porque ningun
workflow corrio desde 2026-09-28.

Arreglo: el repo adopta el estado vivo (decision del propietario, 2026-10-12)
y corrige lo que estaba mal. `configure_team.py` escribe en el `config.yaml`
raiz la lista blanca `kanban.dispatch_profiles` con los once especialistas y
`auto_decompose: false`; el rol del director usa `kanban_create` solo para
ellos; `--check` valida contra los perfiles sin escribir. Pendiente para la
fase 1: los especialistas no tienen `model:` propio.
`docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md`.

```

```bash
pre-commit run --files docs/status.md
git add docs/status.md && git commit -m "docs: record the adopted and corrected live state" && git push
gh pr create --base feat/bot-mode-messaging --head fix/adopt-live-state \
  --title "fix: adopt the live state, guard the dispatcher, add --check" \
  --body "Adopts the 2026-10-03 hand edits and corrects them: root dispatch allowlist so no stage card is dispatched natively, director role text, configure_team.py --check. No model changes. Spec: docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md. Verified: --check passes against the live profiles at db55f73; battery equals the baseline plus 8 new tests."
gh pr merge fix/adopt-live-state --merge
gh pr create --base feat/hermes-team --head feat/bot-mode-messaging \
  --title "chore: bring the default branch up to the deployed revision" \
  --body "feat/hermes-team was behind what the PC runs. Merge commit only: the deployed revision must stay an ancestor or refuse_regression rejects the deploy."
gh pr merge feat/bot-mode-messaging --merge
git fetch origin && git merge-base --is-ancestor ab65f66 origin/feat/hermes-team && echo "ab65f66 is an ancestor"
```

Expected: both PRs merged after their checks and review pass; the last command prints `ab65f66 is an ancestor`.

- [x] **Step 3: Back up, then apply the adoption revision (needs the owner's go)**

```bash
cd /Users/dn/dev/goncloud-hermes && git fetch origin
git bundle create -q "$B" ab65f66..origin/feat/hermes-team
scp -q -o BatchMode=yes "$B" 'ehven@10.13.13.4:C:/dev/fase0a.bundle'
pcrun '$h="$env:LOCALAPPDATA\hermes"; $b="$h\backups\pre-fase0a-" + (Get-Date -Format yyyyMMdd-HHmmss); New-Item -ItemType Directory -Force $b | Out-Null; Copy-Item "$h\config.yaml" $b; foreach($p in "claw","operaciones","ingenieria","scout","implementer","verifier","adversary","reviewer","entrega"){ New-Item -ItemType Directory -Force "$b\$p" | Out-Null; Copy-Item "$h\profiles\$p\config.yaml","$h\profiles\$p\SOUL.md","$h\profiles\$p\goncloud-managed.json" "$b\$p" }; $b; (Get-ChildItem $b -Recurse -File).Count; Get-Content "$h\gateway.pid"'
pcrun 'git -C C:\dev\goncloud-hermes fetch -q C:\dev\fase0a.bundle "+feat/hermes-team:refs/remotes/origin/feat/hermes-team" 2>&1 | ForEach-Object { "$_" }; git -C C:\dev\goncloud-hermes checkout -q --detach db55f73 2>&1 | ForEach-Object { "$_" }; git -C C:\dev\goncloud-hermes status -sb'
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
```

Expected: the backup path, `28` (the root config plus three files for each of nine profiles) and the gateway PID; `## HEAD (no branch)` with no modified files; `Check passed: 9 profiles would deploy db55f73...` and `Root dispatch allowlist: would be written`. Only then:

```bash
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py 2>&1 | ForEach-Object { "$_" }'
```

Expected: nine lines `Configured <role>: ...`.

- [x] **Step 4: Apply the corrections and verify**

```bash
pcrun 'git -C C:\dev\goncloud-hermes switch -q feat/hermes-team 2>&1 | ForEach-Object { "$_" }; git -C C:\dev\goncloud-hermes merge -q --ff-only origin/feat/hermes-team 2>&1 | ForEach-Object { "$_" }; git -C C:\dev\goncloud-hermes status -sb; Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
pcrun 'Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py 2>&1 | ForEach-Object { "$_" }'
pcrun '$h="$env:LOCALAPPDATA\hermes"; $head=(git -C C:\dev\goncloud-hermes rev-parse HEAD); foreach($p in "claw","operaciones","ingenieria","scout","implementer","verifier","adversary","reviewer","entrega"){ $s=(Get-Content "$h\profiles\$p\goncloud-managed.json" -Raw | ConvertFrom-Json).source_revision; "{0} {1}" -f ($s -eq $head), $p }; "pid " + (Get-Content "$h\gateway.pid"); Select-String -Path "$h\config.yaml" -Pattern "^  (dispatch_profiles|auto_decompose):" | ForEach-Object { $_.Line }; "soul corrected: " + ((Get-Content "$h\profiles\claw\SOUL.md" -Raw) -match "La via es una tarjeta con kanban_create"); Set-Location C:\dev\goncloud-hermes; & "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\python.exe" scripts\configure_team.py --check 2>&1 | ForEach-Object { "$_" }'
```

Expected: the first check passes with `Root dispatch allowlist: up to date`; nine `Configured` lines; then nine lines starting with `True`, the same gateway PID as in Step 3 (no plugin `.py` changed, so no restart), the two root lines `auto_decompose: false` and `dispatch_profiles: ["amazon", ...]`, `soul corrected: True`, and a final `Check passed` with `up to date`.

Rollback, if any line differs: copy the files back from the `pre-fase0a-*` directory printed in Step 3 (root `config.yaml` and the three files of each profile), then report. Do not use `--allow-regression`.

- [x] **Step 5: Clean up and tell the owner**

```bash
pcrun 'git -C C:\dev\goncloud-hermes worktree remove C:\dev\goncloud-hermes-fase0a; git -C C:\dev\goncloud-hermes update-ref -d refs/remotes/bundle/fix/adopt-live-state; Remove-Item C:\dev\fase0a.bundle; git -C C:\dev\goncloud-hermes worktree list'
git -C /Users/dn/dev/goncloud-hermes switch feat/hermes-team && git -C /Users/dn/dev/goncloud-hermes pull --ff-only
```

Expected: `worktree list` shows only `C:/dev/goncloud-hermes`. The director's open Telegram session keeps the old role text until the owner sends `/new`. Report in three lines: the installer deploys again from `feat/hermes-team`; the gateway can only dispatch cards for the eleven specialists; specialists still have no pinned model (phase 1).
