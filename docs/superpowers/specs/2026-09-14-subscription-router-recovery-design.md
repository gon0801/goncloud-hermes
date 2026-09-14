# Subscription Router Recovery Design

Date: 2026-09-14
Status: approved in conversation; awaiting written-spec review
Supersedes: the coordinator and cutover sections of
`2026-09-13-subscription-router-design.md`

## Goal

Recover the existing implementation without changing the user's authoritative
eight-position table. Every role always attempts Primary through F7 in the
recorded order. Primary is OpenCode Go, F1 is OpenCode Zen, F2 through F7 bind
to the subscription for the named model family, and DeepSeek remains the only
API-billed exception.

The recovery must prove the orchestration locally before Telegram is allowed
to start it. Passing unit tests or finding a credential file is not acceptance.

## Immutable routing contract

`model-routing.source.json` is the single source of truth. Recovery code must
not reorder, replace, deduplicate, or reinterpret its slots.

- OpenCode Go is always slot 0.
- OpenCode Zen is always slot 1.
- Slots 2 through 7 use the model-family adapter selected by the table.
- `entrega` inherits `main` exactly.
- A `dup` marker preserves a distinct attempt but is not sent as part of a
  vendor model identifier.
- No emergency coordinator or native Hermes fallback may silently replace a
  slot with Z.AI, Copilot, Anthropic API, or another provider.

## Architecture

The subscription router becomes the sole authority for role execution. Hermes
profiles no longer translate the table into native `model` and
`fallback_providers` entries because that translation binds later slots to the
wrong account class and creates a competing fallback system.

Development and acceptance use a local orchestration entry point:

```text
local command -> durable workflow -> role stage -> subscription router
                                                |-> slot 0 OpenCode Go
                                                |-> slot 1 OpenCode Zen
                                                `-> slots 2..7 by model family
```

The entry point accepts a bounded assignment and repository, creates or resumes
the durable workflow, and reports every attempted slot, adapter, model, billing
class, and terminal result. It does not require Telegram or a separate Hermes
model to choose a worker.

Telegram configuration and its allowlist remain stored, but the gateway stays
stopped during recovery. After local acceptance, Telegram becomes a transport
that submits the same bounded request to the proven orchestration entry point;
it does not implement a second routing policy.

The Hermes model picker is not a routing authority. Providers outside the
authoritative table, including automatically discovered Copilot credentials,
must be excluded from the active Goncloud profiles and must never be selected
by this workflow.

## Availability and failure handling

Before invocation, the router applies a non-generative probe when supported and
persists the result with a bounded lifetime. A route that is unauthenticated,
in quota cooldown, unavailable, or explicitly quarantined is recorded and
skipped without starting its generative command.

Transport failures before useful output advance to the next slot. Invalid
assignments and genuine task failures block the stage. Exit code zero without
a valid terminal result is a task failure, never success.

Every vendor process has a deadline and process-tree termination. A timeout
must return control to the router within a bounded grace period; orphaned CLI
children are a failed acceptance condition.

## Durable execution

The router reserves a workflow-stage attempt atomically before invoking a
vendor. Concurrent callers may observe or resume that reservation but may not
execute the same attempt twice.

Continuation detection compares both the starting revision and workspace
status. A changed commit with a clean worktree is still a mutation. After any
mutation, later slots receive an explicit continuation containing the recorded
revision and changed-file evidence.

All workflow identity, assignee, repository, revision, status, and step-limit
checks occur before model execution. `max_steps` is propagated to the adapter
and enforced rather than merely validated.

## Security

- OAuth and subscription credentials remain in each vendor's native store.
- DeepSeek and Z.AI secrets remain in the private local environment store.
- Secrets, token prefixes, account identifiers, prompts, and raw credential
  errors are redacted before logs or durable records are written.
- Credential migration may remove a launcher secret only after a real
  replacement-store probe succeeds; presence checks are insufficient.
- No login, purchase, trial, deployment, publication, or external message is
  authorized implicitly by failover.

## Recovery sequence

1. Freeze Telegram by stopping the gateway and capture reversible configuration
   backups.
2. Separate native Hermes model configuration from the immutable worker table.
3. Make probe state and quarantine enforceable by the router.
4. Correct terminal-result validation, transport classification, deadlines,
   and process-tree cleanup.
5. Add atomic attempt ownership and revision-aware continuation.
6. Move every authorization check before execution and enforce step limits.
7. Verify each adapter independently in slot order, without changing the table.
8. Run one local role, then the complete engineering-to-delivery workflow.
9. Re-enable Telegram only after the same workflow passes through the local
   entry point and all quality gates succeed.

Each implementation step starts with a regression test that fails for the
observed defect, then makes the smallest production change needed to pass.

## Acceptance criteria

- The compiled route sequence for every role exactly matches
  `model-routing.source.json` and contains eight observable slots.
- No Goncloud profile contains a competing native copy of Primary through F7.
- A quarantined Zen or Kimi route is recorded and skipped without invocation.
- A restored Zen or Kimi route is used only after a real minimal inference
  succeeds.
- Empty or malformed vendor output cannot complete a stage.
- Two concurrent requests for one stage cause at most one vendor invocation.
- A committed mutation causes the next slot to run in continuation mode.
- Every pre-execution identity and repository mismatch is rejected before a
  vendor command starts.
- The local end-to-end workflow reaches `entrega` and stores route evidence for
  every stage.
- The complete test battery, repository hooks, plugin doctor, and runtime
  health checks pass with fresh evidence.
- Telegram remains stopped until all previous criteria pass; its final check is
  one harmless, allowlisted, bidirectional workflow.

## Non-goals

- Changing the user's model order to improve current availability.
- Treating Z.AI or another provider as a hidden emergency primary.
- Making the native Hermes model picker represent the workflow table.
- Reinstalling Hermes or rewriting adapters that already satisfy their
  contracts.
- Claiming Zen, Kimi, or any account ready based only on stored credentials.
