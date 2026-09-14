# Telegram Transport Cutover Design

Date: 2026-09-14
Status: written after local acceptance passed; awaiting owner approval
Depends on: `2026-09-14-subscription-router-recovery-design.md`

## Goal

Let the owner start the already proven local orchestration from Telegram,
without Telegram becoming a second routing policy. The gateway has been stopped
since Task 1 of the recovery; its profile, token, allowlist, service script and
Startup entry were preserved, so this is a cutover, not a reinstall.

## What Telegram is allowed to be

A transport. It accepts a bounded assignment from an allowlisted owner and
submits exactly the request `scripts/run_local_workflow.py` already submits:
repository, optional revision, instruction, bounded `max_steps`, optional
`stop_after`. It reports the durable status the orchestrator returns.

Telegram never selects a model, never picks a worker, and never sees a vendor
credential. The subscription router stays the only authority for role
execution, and `model-routing.source.json` stays the only routing source.

## What it must not reintroduce

- No native Hermes `model` or `fallback_providers` chain in any Goncloud
  profile. The recovery removed those precisely because they bound later slots
  to the wrong account class and created a competing fallback system.
- No emergency coordinator, no Copilot route, no model picker.
- No second durable store. The cutover reuses the existing Kanban workflow and
  `subscription-router.db`; a Telegram message resumes a workflow id, it does
  not fork a parallel one.
- No unbounded assignment. The same `max_steps` bound and the same instruction
  size limit apply; an oversized or unparseable message is refused, not
  truncated into a different request.

## Authorization

The gateway already restricts traffic to the owner's private allowlist. That
allowlist is the authorization boundary for starting work: a message from any
other id is ignored and recorded, never queued. The allowlist lives outside
Git and is not printed by this plan's commands.

## Acceptance

One harmless bidirectional test, from an allowlisted id, on a disposable Git
repository: a message in, a bounded read-only inspection assignment
(`--stop-after scout`), and the durable status back out in the same chat. The
test proves transport, authorization and durable identity. It does not re-prove
the chain: local acceptance already did that, and re-running seven live stages
through a new surface would spend real subscription quota to test a message
bus.

Failure of that single test blocks the cutover; the gateway returns to stopped
and the Startup entry stays as it is.

## Rollback

Stopping the gateway restores the current, accepted state exactly: the local
entry point keeps working, the nine profiles are untouched, and no durable
record is deleted. Rollback is stopping a process, not undoing a migration.
