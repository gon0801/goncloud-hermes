# Goncloud Subscription Router Design

Date: 2026-09-13
Status: approved in conversation; awaiting written-spec review

## Goal

Run every entry in the user's eight-position per-agent model table against the
owner's existing subscription for that vendor. DeepSeek is the sole approved
pay-per-use API exception. Do not silently substitute a different account,
provider, or billable API.

## Authoritative interpretation

Position, not the historical provider prefix, determines how a route is bound:

- Primary uses the OpenCode Go subscription.
- F1 uses the OpenCode Zen subscription.
- F2 through F7 use the subscription belonging to the named model family.
- DeepSeek entries use the owner's DeepSeek API.
- `entrega` inherits `main` exactly.
- A `dup` suffix is provenance metadata, not part of a model identifier.

Consequently, labels such as `opencode/gpt-6-astra`,
`opencode/claude-fable-5-1`, or `opencode-go-resp/muse-*` do not authorize
routing those calls through OpenCode. They bind to Codex, Claude, or Muse
respectively when they occur at F2 or later.

## Architecture

Keep Hermes as the durable director, Telegram gateway, Kanban dispatcher, and
engineering workflow. Add a subscription router behind one plugin boundary.
The router owns provider selection; individual adapters own only one vendor
CLI or API.

```text
Telegram -> claw -> durable workflow stage -> subscription router
                                           |-> OpenCode Go adapter
                                           |-> OpenCode Zen adapter
                                           |-> Muse CLI adapter
                                           |-> Claude CLI adapter
                                           |-> Codex CLI adapter
                                           |-> Kimi CLI/plan adapter
                                           |-> Z.AI Coding Plan adapter
                                           |-> Grok CLI adapter
                                           `-> DeepSeek API adapter
```

Hermes still needs a working coordinator model before it can call tools. The
current Kimi Coding Plan and Z.AI Coding Plan bindings remain the coordinator
bootstrap pair. They must not be described as substitutes for the role's
selected subscription worker: the workflow record stores both coordinator and
worker identities.

## Adapter contract

Every adapter implements the same bounded operation:

- accept role, complete assignment, absolute workspace, immutable workflow
  and stage identifiers, revision, and step/time limits;
- pass prompts through stdin or a private temporary file, never shell string
  interpolation;
- run non-interactively using the vendor's supported autonomous permission
  mode while retaining its sandbox where available;
- return normalized status, text, model, account class, session identifier,
  quota/auth classification, changed-files metadata, and terminal evidence;
- leave OAuth/session tokens in the vendor's native credential store;
- redact credentials and account identifiers from logs and model-visible
  output.

DeepSeek is the exception: its key moves from command wrappers into the
private Hermes profile secret store and is labelled `billing_mode=api` in
routing evidence. GLM credentials currently embedded in a command wrapper
must also be removed from that wrapper; the Z.AI subscription binding is used
instead.

## Routing and failover

For each role, compile the exact Primary-to-F7 source table into eight route
records containing slot, model family, adapter, expected account class, and
model identifier. Do not deduplicate repeated slots: repeated routes remain
observable and may represent a later retry opportunity, although the router
may skip a route that is still inside a recorded cooldown.

Before assignment, the router performs a non-generative availability check
where supported. During execution it classifies failures as authentication,
quota/rate limit, unavailable model, timeout, invalid request, or task
failure. Authentication, quota, unavailable-model, and pre-output timeout
advance to the next slot automatically.

Failover is idempotent at the workflow-stage boundary:

- Before any workspace mutation, another adapter may receive the same
  assignment safely.
- Once an adapter reports or the router detects mutations, the durable stage
  records its revision and changed files. A later adapter receives a recovery
  or continuation assignment against that state, not a blind replay.
- External publication, deployment, purchase, deletion, or messaging never
  gains authorization merely because failover occurred.
- Exhausting all eight slots blocks the stage with evidence and one concise
  owner-facing explanation.

## Authentication rollout

Implementation verifies and connects one account at a time without pasting
secrets into chat:

1. OpenCode Go subscription and catalog.
2. OpenCode Zen subscription and catalog.
3. Muse CLI subscription.
4. Claude CLI with Claude Max.
5. Codex CLI with ChatGPT sign-in.
6. Kimi CLI/Coding Plan.
7. Z.AI Coding Plan.
8. Grok CLI subscription.
9. DeepSeek API key.

Each adapter receives a minimal real inference test. A route is not marked
ready merely because a credential file exists. Failed or unsupported account
bindings remain quarantined and do not stop verification of independent
adapters.

OpenAI's official authentication documentation confirms that Codex CLI can
use `Sign in with ChatGPT` for subscription access and that API-key login uses
separate API billing: https://learn.chatgpt.com/docs/auth

## Operations and security

- The Telegram allowlist and existing gateway supervision remain unchanged.
- Secrets stay outside Git and never enter Kanban comments, prompts, test
  fixtures, or command-line arguments.
- Plaintext GLM and DeepSeek launcher credentials are removed only after their
  replacement stores have been written and tested.
- Adapter health has a bounded cache so every Telegram message does not spend
  quota probing all providers.
- The status surface shows ready, authentication required, quota cooldown,
  unavailable, or API billing for every route and reports the adapter actually
  used for a stage.
- No adapter installation, login, or inference silently accepts a trial,
  purchase, plan upgrade, or new API billing agreement.

## Verification

Unit tests cover table compilation, slot semantics, duplicate preservation,
failure classification, cooldown selection, output normalization, prompt
transport, redaction, and mutation-aware continuation. Integration tests use
fake CLIs and a fake DeepSeek endpoint. Live acceptance then requires one
minimal inference per account plus these end-to-end cases:

1. A role uses its Primary OpenCode Go route.
2. A forced Go failure advances to OpenCode Zen.
3. Forced Go and Zen failures advance to the correct model-family subscription
   at F2, not to the historical prefix.
4. A later-slot Codex call is proven to use ChatGPT subscription login.
5. A DeepSeek call is visibly identified as API-billed.
6. A simulated failure after mutation produces continuation, never duplicate
   implementation.
7. Telegram starts a workflow, the engineering chain completes, and delivery
   reports every coordinator and worker route used.

The full local test battery, pre-commit hooks, plugin doctor, gateway deep
status, and a final bidirectional Telegram check are required before claiming
completion.

## Non-goals

- Turning consumer subscriptions into undocumented HTTP APIs.
- Sharing OAuth tokens between vendors or copying them into the repository.
- Guaranteeing availability after a vendor exhausts or changes a subscription.
- Changing the approved engineering review chain or its maximum review rounds.
- Connecting commercial systems, deploying code, or creating recurring work.
