# Exact subscription routing for every Hermes profile

## Goal

Hermes must execute the owner's routing table literally. A route is correct only
when its position, model, reasoning marker, and subscription-backed adapter all
match the table. An API-billed substitute is not equivalent to a subscription
route.

The current defect is isolated to the director's normal Hermes conversation:
`claw` uses a separately maintained native chain of OpenCode Go, Kimi, and Z.AI.
When OpenCode Go failed, Hermes therefore selected Kimi even though Kimi is the
last position in the `main` row. Worker stages already compile the authoritative
table through the subscription router, but the split architecture allows the
director to contradict it.

## Authoritative table

`model-routing.source.json` remains the single source of truth. The accepted
positions are:

| Profile | Primary | F1 | F2 | F3 | F4 | F5 | F6 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| main | `opencode-go-resp/muse-spark-1.3-contributor` | `anthropic/claude-opus-5 (xhigh)` | `meta/muse-spark-1.3 (max)` | `xai/grok-4.6` | `zai/glm-5.3` | `openai/gpt-5.6-sol (xhigh)` | `kimi/k3` |
| operaciones | `opencode-go/deepseek-v4.1-flash` | `zai/glm-5.3-flash` | `openai/gpt-5.6-terra (xhigh)` | `anthropic/claude-sonnet-5 (xhigh)` | `xai/grok-4.6` | `kimi/kimi-for-coding (high)` | `meta/muse-spark-1.3 (max)` |
| ingenieria | `opencode-go-resp/muse-spark-1.3-contributor` | `openai/gpt-5.6-sol (xhigh)` | `kimi/k3` | `zai/glm-5.3` | `meta/muse-spark-1.3 (max)` | `anthropic/claude-opus-5 (xhigh)` | `xai/grok-4.6` |
| implementer | `opencode-go-resp/muse-spark-1.3-contributor` | `meta/muse-spark-1.3 (max)` | `zai/glm-5.3-flash` | `xai/grok-4.6` | `anthropic/claude-sonnet-5 (xhigh)` | `openai/gpt-5.6-terra (xhigh)` | `kimi/kimi-for-coding (high)` |
| reviewer | `opencode-go/deepseek-v4.1-flash` | `anthropic/claude-fable-5-1` | `kimi/k3` | `openai/gpt-6-astra` | `meta/muse-spark-1.3 (max)` | `xai/grok-4.6` | `zai/glm-5.3` |
| adversary | `opencode-go/deepseek-v4.1-flash` | `xai/grok-4.6` | `kimi/k3` | `zai/glm-5.3` | `meta/muse-spark-1.3 (max)` | `anthropic/claude-opus-5 (xhigh)` | `openai/gpt-5.6-sol (xhigh)` |
| verifier | `opencode-go/deepseek-v4.1-flash` | `zai/glm-5.3-flash` | `meta/muse-spark-1.3 (max)` | `kimi/kimi-for-coding (high)` | `anthropic/claude-sonnet-5 (xhigh)` | `openai/gpt-5.6-terra (xhigh)` | `deepseek/deepseek-flash` |
| scout | `opencode-go/deepseek-v4.1-flash` | `zai/glm-5.3-flash` | `meta/muse-spark-1.3 (max)` | `kimi/kimi-for-coding` | `deepseek/deepseek-flash` | `anthropic/claude-sonnet-5 (xhigh)` | `openai/gpt-5.6-terra (xhigh)` |

The Hermes profile `claw` maps to `main`, and `entrega` inherits `main` exactly.
Parenthetical markers are execution settings and must not be discarded as mere
display text. In particular, the second `main` position is Claude Opus 5 with
`xhigh` reasoning. It must not be renamed or reduced to a generic "Claude Max"
entry.

## Architecture

Add a user-scoped Hermes model-provider plugin named for the Goncloud
subscription chain. Hermes already supports model-provider plugins under
`$HERMES_HOME/plugins/model-providers/`, so this does not require modifying the
installed upstream source.

The provider will adapt Hermes conversation requests to the existing
subscription router:

1. Resolve the active Hermes profile to its source row (`claw` and `entrega`
   resolve to `main`).
2. Compile the seven positions with the existing catalog compiler.
3. Invoke those routes in position order through the existing subscription
   adapters.
4. Translate the successful subscription result back into the response shape
   Hermes expects, including tool requests required by the Telegram director.
5. Preserve enough conversation and tool-result context for each subsequent
   turn to continue correctly even if a different position answers it.

The director will no longer carry `director_model` plus a separately written
`director_fallbacks` list. Its model configuration will select the local
subscription-chain provider and the `main` row. Workers will continue using the
same router for workflow stages. Both paths therefore compile the same source
instead of maintaining two chains.

## Failure and billing rules

The router may advance only for a classified authentication failure, exhausted
subscription quota, unavailable model, provider timeout, or another explicitly
classified advancing provider failure. It advances to the immediately following
position and never jumps over a healthy position.

A task failure, malformed tool request, local authorization refusal, or workspace
error is terminal for that attempt and must not silently consume later models.
Cooldown remains adapter-specific and durable. If a position is skipped because
its subscription is in a recorded cooldown, the evidence must name that position
and reason.

Every non-DeepSeek route uses the owner's corresponding subscription-backed CLI
or subscription endpoint. Only the two explicit `deepseek/deepseek-flash`
positions in `verifier` and `scout` retain their declared API billing. The bridge
must never translate Claude, Muse, Grok, Z.AI, Codex, Kimi, or OpenCode Go into a
different API-billed account class.

## Tests and acceptance evidence

Tests will be written before implementation and must demonstrate the regression:
with `main` Primary failing, the next invocation is
`anthropic/claude-opus-5 (xhigh)`, not Kimi.

The automated acceptance suite will also:

- compare all 56 source cells against the compiled adapter, model, reasoning
  marker, slot, and billing mode;
- verify `claw` and `entrega` both resolve to the complete `main` row;
- verify every failure at positions 0 through 5 advances to exactly the next
  position and position 6 is terminal;
- verify the director's deployed configuration has no independent fallback list;
- verify all nine deployed profiles contain a byte-identical routing source;
- exercise a tool-call round trip through the provider bridge so Telegram tools
  are not lost;
- preserve existing router, workflow, configuration, and adapter tests.

After local tests and all pre-commit hooks pass, deployment will update the user
model-provider plugin and all profile-owned files, restart the Hermes gateway,
start a fresh Telegram session, and run a controlled live fallback proof. The
proof is accepted only if a forced Primary failure selects Claude Opus 5 with
`xhigh` at F1 and records subscription billing. A final sanitized status dump
will show the exact compiled order for every profile without exposing credentials.

## Non-goals

- No upstream Hermes source modification.
- No replacement of subscriptions with vendor API keys.
- No reordering or deduplication of the owner table.
- No unrelated changes to workflow stages, role prompts, or business tooling.
