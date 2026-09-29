# Director on Hermes-native providers, chain compiled from the table

## Problem

Since 2026-09-27 13:00 the director (`claw`)
no longer finishes tasks. On 2026-09-28 it told the owner "lo verifico ahorita"
for seven hours without running a single tool.

Root cause is our architecture, not the model or OpenCode Go:

- Until 2026-09-27 11:00 every director turn ran on Hermes' native
  `opencode-go` provider (agent.log: `provider=opencode-go`). Tool calls were
  structured API tool calls, and the director worked.
- `6a5bb5f` + `30ff6c9` + `1b028ed` moved the director onto our
  `goncloud-subscriptions` model provider. That provider serves the turn by
  running a CLI with its own tools disabled (`OPENCODE_CONFIG_CONTENT` →
  `tools: {"*": false}`). The model then has to write each call as text,
  `<tool_call>{json}</tool_call>`, and Hermes rebuilds it with
  `extract_tool_calls_from_text`.
- That text channel is lossy. When the model writes a shell command with
  nested quotes, the JSON breaks. Hermes then drops the block, or rejects the
  arguments and retries without saying so, and the turn ends on the sentence
  that preceded the call. Replayed from `subscription-router.db` for the
  2026-09-28 conversation: 8 of the 13 emitted calls were never executed
  (attempts 384, 390, 396, 400, 402, 404, 406, 408).
- `b91b152`, `090e2b5`, `095b7fe`, `7f5d165`, `f9b4da8` and the conversation
  half of `aa84d85` are patches on that text channel. None of them removes the
  cause.

The move was made for a real reason (spec
`2026-09-27-exact-subscription-routing-design.md`): the director had a
hand-written native chain (Go → Kimi → Z.AI) that contradicted the owner's
table. That goal stays. The mechanism chosen for it is what this spec
replaces.

## Decision

The director converses only through Hermes-native providers, with native tool
calls. Its `model` + `fallback_providers` are **compiled from the `main` row of
`model-routing.source.json`** by `configure_team.py`, so no hand-written chain
exists to contradict the table. Workers keep the subscription router and their
CLIs unchanged; `entrega` inherits the `main` row as a worker stage and stays
on the router. The owner decided this on 2026-09-28, and confirmed that every
position of the table has a native subscription or API credential, Muse
included.

## Native binding of the `main` row

Each label compiles to one Hermes provider. Native model ids are what the
provider's catalog lists; the live probe in the rollout confirms them before
deploy.

| Slot | Table label | Hermes provider | Credential (owner's own) |
|---|---|---|---|
| 0 | `opencode-go-resp/muse-spark-1.3-contributor` | `opencode-go` | OpenCode Go account 1 |
| 1 | same label (second account) | `opencode-go` | OpenCode Go account 2 in the same provider's credential pool; Hermes rotates on 429/exhaustion |
| 2 | `anthropic/claude-opus-5 (xhigh)` | `anthropic` | Claude Code OAuth (Claude Max) |
| 3 | `meta/muse-spark-1.3 (max)` | `meta-ai` | Muse API key |
| 4 | `xai/grok-4.6` | `xai-oauth` | xAI OAuth |
| 5 | `zai/glm-5.3` | `zai` on `api/coding/paas/v4` | Z.AI Coding Plan key |
| 6 | `openai/gpt-5.6-sol (xhigh)` | `openai-codex` | ChatGPT OAuth |
| 7 | `kimi/k3` | `kimi-coding` on `/coding` | Kimi For Coding key |

- Slots 0 and 1 are one provider entry backed by a two-credential pool, not
  two fallback entries. That is how Hermes models two accounts of one
  provider.
- Effort markers compile to `agent.reasoning_overrides: {model: effort}`.
  Hermes re-resolves them when a fallback activates (`resolve_reasoning_config`),
  and `xhigh` and `max` are valid efforts there.
- A label whose family has no native binding makes `configure_team.py` fail
  loudly. It never skips a position.

## Changes

1. `scripts/configure_team.py`: add a compiler, a new function next to
   `catalog.compile_profile_routes`, that reuses its label and effort parsing
   and produces the director's `model`, `fallback_providers` and
   `agent.reasoning_overrides`. `team.json` gets no chain back.
2. Remove the conversation path end to end: the
   `plugins/model-providers/goncloud-subscriptions` provider,
   `subscriptions/conversation.py`, `InvocationRequest.tool_bridge`, and every
   adapter branch that exists only for it (`_TOOL_BRIDGE_ENV`,
   `_REISSUE_PROMPT`, `_as_text_tool_calls`, Claude invoke-markup parsing, the
   conversation-only Muse skip). The deploy also deletes the deployed copy of
   the provider from `claw` and `entrega`.
3. Worker stages are untouched. Their suite must pass unchanged, except for
   tests that only covered the removed conversation path, which are deleted
   along with it.

## Tests (must fail on today's code)

- The director config compiled from the real table has exactly the providers,
  models, order and efforts of the table above.
- A table edit (reorder two positions) changes the compiled chain accordingly.
  This is what makes a hand-written chain impossible to reintroduce silently.
- An unknown family raises, and the error names the label.
- No profile config references `goncloud-subscriptions` after `configure`.

## Rollout

1. Owner logs in each native credential that is missing. Status is checked
   with Hermes' own auth listing, never by reading credential files.
2. Live probe per provider: a single turn that must execute one tool call
   natively.
3. Deploy with `configure_team.py` from this branch, once the director is
   idle; restart the gateway; owner sends `/new`.
4. Rollback: redeploy `a97a1bf`.

## Out of scope

Workflow stage routing, and the owner's AppFlowy task from the 2026-09-28
conversation, which is handled separately after deploy.
