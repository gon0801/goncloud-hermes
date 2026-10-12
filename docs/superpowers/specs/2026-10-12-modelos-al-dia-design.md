# Keeping every agent's models current, in hermes and in claw

## Problem

The owner wants a newly released model (2026-10-12: Muse Wonder replacing
Muse Spark 1.3) to reach every agent position without a week of manual edits,
and wants to know what breaks when a model ID changes.

Today the same routing table exists twice and runs through three paths:

- hermes workers: `model-routing.source.json` (8 rows x 8 positions), read by
  the `goncloud-team` plugin on every stage, which launches a vendor CLI.
- hermes director (`claw`): `scripts/configure_team.py` compiles the `main`
  row into `profiles\claw\config.yaml` and Hermes calls native providers.
- claw (OpenClaw 2026.9.7 on the Mac Mini): the chains are hand-written in
  `/Users/gon/.openclaw/openclaw.json` for 19 agents; no repo generates them.

Measured on 2026-10-12 (read-only survey, four explorers plus a synthesis):

1. A model ID is not one name. Muse Wonder is `muse-wonder-1.0` on Meta's
   API, four different IDs in the Muse CLI catalog, `meta/muse-wonder` on
   OpenRouter, and absent from OpenCode Go. No rule predicts the next name:
   the family changed from `spark` to `wonder` and the version restarted.
2. Each path keys behaviour on the exact ID or on its prefix: adapter choice
   (`subscriptions/catalog.py`), HTTP protocol for `opencode-go`
   (`hermes_cli/models.py`, upstream), context window, reasoning effort
   (`agent.reasoning_overrides` in hermes, `params.thinking` in OpenClaw),
   runtime (`agentRuntime` in OpenClaw). A renamed model silently loses them.
3. An ID the provider does not know rarely fails loudly. Workers advance one
   position per stage; the director falls back to F1 on every message; claw
   answers `Unknown model` locally and moves on. `--live-probe-all` is the
   exception: a failed probe quarantines the whole adapter for one hour.
4. Several things are already broken before any model change:
   - `configure_team.py` cannot deploy. The `claw` profile was edited by hand
     on 2026-10-03 (`kanban` toolset, `kanban.dispatch_in_gateway: true`, a
     new `SOUL.md`), and `plugins/goncloud-team/workflow.py` gained 11 roles
     by hand in all 20 profile copies. Neither edit is in any branch.
   - The PC is deployed from `feat/bot-mode-messaging` (`ab65f66`), two
     commits ahead of the default branch `feat/hermes-team`.
   - 11 profiles (amazon, meli, mail, limpia, odoo, pers, arras, gonserver,
     skillverif, tinkabot, eggbot) are not in `team.json` and have no row in
     the table.
   - In claw, `meta/muse-spark-1.3` returns 401, `openai/gpt-5.6-sol` returns
     404, the OpenAI profile is in cooldown until about 2026-11-07, and the
     Muse-through-Go link returns 400 on isolated completions.
   - The worker Muse route last ran on 2026-09-28 and failed four times with
     `model_unavailable`. The Muse CLI in WSL is 1.4.0 while the stable
     channel is 1.4.6; its auto-update runs and does not land.
   - There is no CI and pre-commit runs no tests.

## Owner decisions (2026-10-12)

1. The hand edits of 2026-10-03 are adopted into the repo, not reverted.
2. The 11 extra profiles get a chain in hermes, the same one each already has
   in claw.
3. The watcher notifies; it never switches a model by itself. The owner
   approves each switch.

## Non-goals

- No automatic model switching.
- No change to which model sits in which position, other than what the owner
  approves in phase 3.
- No migration of the 11 extra profiles into `team.json` management in phase
  0; their `config.yaml` and `SOUL.md` stay hand-owned until phase 1 decides.

## Design

The table the owner already edits, `model-routing.source.json`, stays the only
place a human changes a model. The first draft of this design added a registry
of nicknames above it. Measuring the two systems on 2026-10-12 showed it is not
needed: every one of claw's 19 chains equals a row of the hermes table under
two rules, with a single difference in 19 agents (one missing thinking value).

```
model-routing.source.json  (8 rows x 8 positions, the owner's table)
      |
      +--> hermes workers (read on every stage) and director (configure_team.py)
      +--> claw: scripts/claw_models.py + claw-routing.json
             desired chains per agent, and a drift report against live
```

- `claw-routing.json` says which row each claw agent runs (the eleven
  specialists run `operaciones`) and whether it carries per-model thinking.
- Rule 1: position 1 repeats position 0 in the table and means "second
  OpenCode Go account"; claw names that account as a provider with suffix `-2`.
- Rule 2: an effort marker `(max)` becomes `models.<id>.params.thinking` in
  claw and leaves the ID.
- `id_overrides` in `claw-routing.json` covers the case the nicknames were
  meant for: the same model with one ID on the route hermes uses and another
  on the route claw uses. It is empty today. If hermes itself needs two
  different IDs for one cell (Muse CLI for workers, Meta API for the director),
  the table cannot express it and that is a plugin change, not a config one.

Replacing a model everywhere is then a search and replace of one label in one
file, which phase 3's `promote` does after probing.

### Phases and exit criteria

Each phase gets its own implementation plan when it starts. A phase is done
when its exit criterion is observed, not when its code merges.

**Phase 0 - make the current state deployable and known.** No model changes.

- 0A (this repo): adopt the 2026-10-03 edits and correct them (see below), add
  `configure_team.py --check`, converge on one deploy branch, redeploy.
  Exit: `configure_team.py --check` passes on the PC from the default branch
  and a real apply reports nine profiles with the new revision.
  Plan: `docs/superpowers/plans/2026-10-12-modelos-fase0a-destrabar-instalador.md`.
- 0B (claw + health): commit a redacted snapshot of the live claw chains and
  catalog rows to `goncloud-openclaw` (`docs/patches/modelos-vivos-<date>.json5`,
  same convention as the 2026-09-15 snapshot, extended with
  `agents.*.models` and `models.providers` without keys or headers); run one
  real probe per distinct link in both systems and record alive or dead with
  the cause. Exit: a table of every link with a verdict, and the owner has
  decided what replaces each dead link.
  Measured 2026-10-12: `docs/salud-eslabones-2026-10-12.md`; snapshot in
  `goncloud-openclaw` PR #274. The dispatcher race was settled in 0A. Open:
  the owner's decisions on the dead links.
- 0C (CLIs): bring the PC's agent CLIs to current versions one at a time,
  probing after each, and find why the Muse auto-update does not land in WSL.
  Exit: versions on the PC match the Mac's and the Muse worker route passes
  one real stage.
  Done 2026-10-12 for muse and opencode; kimi, claude, grok and codex are left
  as they were, with the reason for each in `docs/status.md`.

**Phase 1 - one source for both systems.**

- 1A (done 2026-10-12): `scripts/claw_models.py desired|check`, with
  `claw-routing.json` and `tests/test_claw_models.py`. `check` against the
  live snapshot of 2026-10-12 reports one difference in 19 agents: `verifier`
  lacks `thinking: high` for `kimi/kimi-for-coding`. It never writes to claw.
- 1B: pin the eleven specialists in hermes to the `operaciones` row as native
  `model` + `fallback_providers`, so a dispatched card and the specialist's own
  bot chat stop running on whatever provider its environment selects. Needs
  first: which provider accounts each specialist profile can use, and one real
  specialist turn to prove it.
- Decided 2026-10-12: claw's `verifier` got the missing thinking value
  (applied, `check` now reports 0 differences); the specialists keep carrying
  no efforts in claw.

Exit: `check` reports no difference that the owner has not accepted, and a
card dispatched to a specialist runs on the pinned chain.

**Phase 2 - daily watcher, no tokens.** A script (no agent, no model call)
diffs models.dev (ETag), `opencode.ai/zen/go/v1/models`, OpenRouter, the Muse
release channel and the installed CLI versions against the registry, and
notifies over Telegram only when something changed: a new model in a family
in use, an ID in use that disappeared or was renamed, or a CLI behind its
channel. It runs from an existing script-type cron.
Exit: replaying the last three Muse releases from catalog history produces
the three expected notices and no others.
Done 2026-10-12: `scripts/model_watch.py` with `tests/test_model_watch.py`
(the replay is `test_replaying_the_last_three_muse_releases`). It watches
models.dev, the OpenCode Go list and the Muse channel; OpenRouter and the
other CLIs were left out until a notice is missed because of them.

**Phase 3 - promotion with a real probe.** `promote <old label> <new label>`
makes one isolated real call per affected route (never `--live-probe-all`,
which quarantines an adapter on failure), then replaces the label in the
table (and sets `id_overrides` when claw's ID differs), applies both sides,
records a before and after snapshot, and keeps the old OpenClaw
catalog row for one day because subagent sessions stay pinned to the old ID.
Exit: Muse Wonder is promoted on the direct routes, and a deliberate
promotion to a nonexistent ID is refused with nothing applied.

**Phase 4 - names pinned elsewhere.** Launchers, per-CLI agent files and the
PR reviewer either read a nickname or are listed in the watcher's report.
Exit: the watcher's "pinned elsewhere" list is empty or every entry is
acknowledged.

## Corrections to the 2026-10-03 hand edits

The owner delegated this on 2026-10-12 ("fix what was written by hand; remove
it if needed"). A read-only review of the live board, the gateway and the
upstream dispatcher settled each edit:

| Hand edit | Finding | Decision |
|---|---|---|
| 11 roles in `TEAM_ROLES` | Needed: without them the specialists' plugin tools answer "unknown Goncloud role". | Keep. |
| `kanban.dispatch_in_gateway: true` on `claw` | Inert for dispatch. One dispatcher serves every multiplexed profile and reads the root `config.yaml`, where dispatch has been on by default since 2026-10-02. In a profile the key only decides whether `hermes kanban create` warns. | Keep (it is truthful), and fix the real switch: the installer writes `kanban.dispatch_profiles` (the 11 specialists) and `auto_decompose: false` into the root config. |
| `kanban` toolset on `claw` | `kanban_create` subscribes the chat, so the gateway reports back when a card finishes; the CLI does not. Costs 23.9 KB per director call. | Keep. |
| Director role text | Told the director to use the CLI (nobody is notified), listed `operaciones` as a card target (never dispatched, the card would sit in `ready` forever, as on 2026-09-15), and still said the dispatcher was off. | Rewrite: `kanban_create` only, specialists only, no claim that the dispatcher is off. |

The race the repo's own test warned about was real and unguarded: a `ready`
stage card has its role as assignee, the orchestrator never claims it, and the
dispatcher ticks every 60 s. It has not happened only because no workflow has
run since 2026-09-28. The allowlist closes it: it is fail-closed and read on
every tick, and no chain role is in it.

Nothing had used the specialist flow yet: zero cards since 2026-10-03 and
zero sessions in the eleven specialist profiles.

The installer overwrites a managed file only if the file on disk equals what
it last recorded. So the role text is adopted verbatim in one revision and
corrected in the next, and the deploy applies both in order.

Still open after 0A, for phase 1: a specialist profile has no `model:` block,
so its card (and its own bot chat) runs on whatever provider its `.env`
selects and that provider's first catalog model. Phase 1 pins each one to the
chain it already has in claw, which is the `operaciones` row.

## Open questions

- Which ID does the WSL Muse CLI accept and echo for Wonder, and why did Spark
  fail on 2026-09-28? Needs one real call (0B).
- With which ID and protocol will OpenCode Go serve Wonder, and what does it
  answer to an unknown ID (404, 401 or 403)? Needs OpenCode Go to ship it.
- Do `gpt-5.6-sol` and `gpt-6-astra` still answer through Codex? Needs one
  real call (0B).
- Where do the 11 extra profiles get their conversational model today?
  Probably the root `config.yaml`; trace it in upstream before phase 1.
- Does the OpenClaw `config.patch` RPC validate model references, and does a
  hot reload kill in-flight runs? Read the gateway handler and measure once.
