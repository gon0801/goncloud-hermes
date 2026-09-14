# Goncloud Hermes Implementation Plan

**Goal:** Install and configure the approved autonomous business and engineering team.

**Architecture:** Native Windows Hermes profiles and durable Kanban, versioned configuration and role contracts, a small tested workflow plugin where needed. Keep upstream unmodified and runtime outside Git.

**Tech Stack:** Hermes, Python 3.11, PowerShell, SQLite (native Kanban), unittest.

**Spec:** ../specs/2026-09-13-goncloud-team-design.md

## Global Constraints

- Nine roles: claw, operaciones, ingenieria, scout, implementer, verifier, adversary, reviewer, entrega.
- One review round; second only if first found high severity; no third.
- User approved installation and implementation. Model/provider choice is pending, not authority to copy credentials.
- No modifications to OpenClaw, no credential migration, no live business crons copied.
- Preserve user state; configuration application must detect conflicting edits.

## Task 1: Install pinned upstream and profiles

Files: `team.json`, `roles/*.md`, `shared/*.md`, `scripts/configure_team.py`, `scripts/hermes.ps1`.

- [x] Install official Windows distribution pinned to Git SHA and check CLI help/version.
- [x] Define shared delegation, evidence and business source contracts plus role-specific responsibilities.
- [x] Create missing profiles with supported upstream API; apply managed configuration without replacing secrets.
- [x] Verify nine profiles, enabled toolsets, isolated memories, upstream config loading and idempotent reapplication.

## Task 2: Durable engineering pipeline and consultations

Files: `plugins/goncloud-team/`, `tests/test_workflow.py`.

- [x] Discover installed native Kanban APIs before integrating.
- [x] Add engineering workflow start/status/consultation/review tools; native dependency gating remains authoritative.
- [x] Test using a temporary native database: duplicate request, incomplete dependency, review rejection, high-severity-only second round, no third round, invalid role.
- [x] Keep source contracts explicit; report any behavioral limit instead of claiming a prompt guarantees enforcement.

## Task 3: Runtime verification and handoff

Files: `README.md`, `docs/status.md`, `.pre-commit-config.yaml`, `tests/test_configuration.py`.

- [x] Run focused tests and required commit hooks. Final full local battery: 20/20 tests; all hooks pass.
- [x] Verify plugin discovery with the installed Hermes runtime.
- [x] Connect supported native providers and Muse CLI; verify real fallback and director-to-Muse delegation.
- [x] Connect the new Telegram bot, start the installed Windows login gateway and verify one real message.
- [x] Record completed work and outstanding user-only credentials accurately. Local commit/publication requires explicit request.

Real inference, native fallback, director-to-Muse delegation and bidirectional Telegram messaging pass. Windows login supervision is installed and the gateway is running with an owner allowlist.

Final review: one independent round found three HIGH issues. Fixed native-completion rejection bypass, same-board repository overlap (atomic admission), and FAIL-report routing; regression coverage passes. No second review requested. Residual trust/runtime limits are recorded in docs/status.md and plugin README.

## Execution notes

The empty user workspace is the requested deliverable location; initialize a feature branch there. The upstream runtime is installed outside the repo. Use one independent implementation worker for the workflow adapter while the lead owns installation/configuration. One final independent review; any second review requires high severity, per the user's quality rule. No repeated skill-driven approval gate: user already approved this design and asked to execute.
