# Phase 1A: Claw follows the table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Derive what claw's 19 model chains should be from `model-routing.source.json` and report where the live ones differ, without writing to claw.

**Architecture:** One pure module turns a table row into a claw agent entry under two rules (position 1 moves to the second OpenCode Go account; an effort marker becomes `params.thinking`). A small mapping file says which row each claw agent runs. A `check` command compares the result with a dated snapshot of the live config.

**Tech Stack:** Python 3 standard library only (`json`, `re`, `argparse`, `unittest`), so the tests run on the Mac and on the PC.

**Spec:** `docs/superpowers/specs/2026-10-12-modelos-al-dia-design.md` (Design, phase 1A).

## Global Constraints

- No model is renamed, added or removed. `model-routing.source.json` is not touched.
- Nothing is written to claw: no `openclaw config patch`, no gateway call. The live side is read from a snapshot file.
- No model inference.
- The new code imports nothing outside the standard library and nothing from the Hermes runtime.
- Nothing here is deployed to the PC profiles: `scripts/`, `tests/` and `claw-routing.json` are not managed files.

---

### Task 1: Chain derivation — done

**Files:** `scripts/claw_models.py`, `claw-routing.json`, `tests/test_claw_models.py` (`ChainTests`, `RealFilesTests`).

**Interfaces produced:**
- `split_label(label) -> (model_id, effort_or_None)`
- `desired_agent(labels, mapping, *, thinking) -> {"model": {"primary", "fallbacks"}, "models"?: {id: {"params": {"thinking": effort}}}}`
- `desired_claw(source, mapping) -> {"agents": {"entries": {agent: ...}}}`; raises `ValueError` naming the agent and the row when the row is not in the table.
- `claw-routing.json`: `second_account_suffix`, `id_overrides` (`{table id: claw id}`), `agents` (`{agent: {"row": str, "thinking": bool}}`).

- [x] Tests written first and seen failing (the module did not exist), then passing: position 1 on the second account, position 1 left alone when it is another model, efforts to thinking, an agent without thinking, an override that renames the ID for claw only, an unknown row.
- [x] `RealFilesTests` ties the real files together: every mapped agent yields a full chain, and every profile in `team.json`'s `kanban.dispatch_profiles` has a claw agent.

### Task 2: Drift report — done

**Files:** `scripts/claw_models.py` (`drift`, `load_live`, `main`), `tests/test_claw_models.py` (`DriftTests`), `README.md`.

**Interfaces produced:**
- `drift(desired, live, mapping) -> list[str]`: one line per differing position (`<agent> position <n>: claw has A, the table wants B`), per differing thinking value, and per agent present on only one side. Keys the generator does not own (`agentRuntime`, `heartbeat`, ...) are ignored; thinking is not compared for agents mapped with `"thinking": false`.
- `python3 scripts/claw_models.py desired` prints the desired entries; `check --live FILE` prints the differences and exits 1 when there are any.

- [x] Tests: no drift when equal (with foreign keys present), a differing model with its position, missing/stale/different thinking, thinking ignored for chain-only agents, agents on one side only.
- [x] Measured against `modelos-vivos-2026-10-12.json5`:
  `verifier thinking kimi/kimi-for-coding: claw has nothing, the table wants high` and `1 difference(s) in 19 agents`.

### Task 3: What is left for the owner and for 1B

- [x] Owner, 2026-10-12: claw's `verifier` gets `thinking: high` for `kimi/kimi-for-coding`. Applied; `check` reports `0 difference(s) in 19 agents`.
- [x] Owner, 2026-10-12: the eleven specialists keep carrying no efforts in claw (`"thinking": false`).
- [ ] 1B gets its own plan: native `model` + `fallback_providers` for the eleven specialist profiles in hermes.
