# Goncloud team plugin

This native Hermes plugin registers these tools in the `goncloud_team` toolset:

- `goncloud_engineering_start` creates an idempotent director root and the
  `scout -> ingenieria -> implementer -> verifier -> adversary -> reviewer -> entrega`
  dependency graph.
- `goncloud_engineering_status` reads the root, initial stages, and optional
  corrective stages.
- `goncloud_consult` creates an idempotent peer task and adds it as a native
  dependency of the source task.
- `goncloud_engineering_review` approves, blocks, or creates the sole permitted
  corrective review chain.
- `goncloud_muse_delegate` sends a complete specialist assignment to the owner's
  authenticated Muse CLI through WSL, using standard input and Muse's own sandbox.


The adapter uses `hermes_cli.kanban_db_connect.connect_closing`, public native
Kanban operations, and the native atomic decomposition builder. The local runner
below drives dependency-ready stages through the subscription router. Kanban
owns dependency promotion and review transitions; the router owns vendor
attempts, leases, deadlines, and failover.

Durably enforced by the database graph:

- later stages cannot complete before their parents;
- every initial and corrective stage uses the exact same `dir:<repository>`
  workspace;
- retries of start and consultation keys do not create duplicate tasks;
- an atomic native write transaction rejects a different active workflow for
  the same canonical repository, preventing cross-request stage overlap in the
  shared directory within the current Kanban board/database;
- entrega remains gated after a rejection;
- only a first high/critical rejection can add round two, and no code path adds
  round three.
- a fail-closed `pre_tool_call` hook always blocks reviewer cards from
  substituting native `kanban_complete` for the required durable review tool,
  and blocks a reviewer completion whose task id it cannot resolve at all;
  review comments are audit records, never completion authorization.
- a `pre_llm_call` hook consults the JEV skill router automatically on every
  owner message the claw director receives over telegram or cli (never cron,
  bot-chat, kanban or subagents): the suggested skill rides the turn context
  as a `JEV skill_select: ...` line — the old rule "consult before loading a
  skill" becomes "the platform already consulted: use the context's
  suggestion" — with fit >= `UMBRAL_SKILL_FIT` (0.6) the line says
  `(cárgala)` and the footer is `JEV: X`; below that (or with no fit) the
  line says `posible X (fit …): decide tú` and the footer marks it
  `JEV: X? (dudosa)` — and a `transform_llm_output` hook appends a `JEV: ...` footer
  to that same turn's reply, once per turn and switchable off with the
  plugin config key `jev_auto_visible` or the `GONCLOUD_JEV_AUTO_VISIBLE`
  variable (on by default, same `false`/`0`/`no`/`off` parsing for both).
  The request text leaves the machine redacted through the router's own
  `subscriptions/redaction.redact_text` filter and capped at 4,000
  characters (work data may reach TypeSafe; credentials never do). The
  router is called the way its own `mcp.json`
  does (its `suggest` function from the pinned, unpatched installed plugin,
  in a subprocess via `uv run --project`, request text over stdin, 8 s cap,
  fail-open with a bounded reason), `JEV_TYPESAFE_API_KEY` resolves through
  `agent.secret_scope.get_secret` and only ever travels to the child
  environment, and each consult lands in `<claw home>/jev/consultas.jsonl`
  with no message text (just its length and a short sha256), no key, and
  the router's `gate`/`fit` fields.
- the same hook also routes the message (claw direct, one of the specialists,
  or the engineering chain) in parallel with `skill_select` and adds
  `Jev sugiere: X (conf)`; Goncloud stage cards and specialist cards that
  finished since the previous turn add `Jev cierre: X (conf)`: a completed
  card only when it left a report (and it can only come out
  quedo/corregir), a blocked card as corregir/bloqueado (cursor in
  `<claw home>/jev/cierre-cursor.json`). Confidence >= 0.85 is followed, below
  it claw decides. Every JEV call has a hard 2 s cap, no retries, and fails
  open; each decision is one line in `<claw home>/jev/decisiones.jsonl`
  (`input` capped at 200 redacted chars and `rawLen`, output, confidence,
  latency, error).

### JEV tanda 2: todos los perfiles (David 2026-10-19)

The OpenClaw `jev-auto` improvements, by code and on Hermes hooks. The
5-minute watchdog stays OUT (the event notice replaces it): do not bring it
back. Which Hermes hook backs each point:

| Point | Hook | Where |
| --- | --- | --- |
| Classify tipo (codigo/diagnostico/operacion/comunicacion/investigacion/consulta) and modo (directo/investigar/aclarar), inject `Jev sugiere: tipo=.., modo=..` | `pre_llm_call` (every profile; claw keeps routing) | `jev_auto.py` |
| Evaluate quedo/seguir/bloqueado at the end and log it | `post_llm_call` (background thread, never holds the turn) | `jev_auto.py` |
| Loop detector: same tool as the previous call -> JEV asks bucle/avanza; >= 0.9 queues `Jev advierte:` for the NEXT turn, never blocks | `pre_tool_call` (always returns `None`; JEV runs in a daemon thread; max 3 queries per turn) | `jev_auto.py` |
| Learn from corrections: JEV suggested X, claw dispatched Y (`kanban_create` to a specialist, `goncloud_local_run` = ingenieria) or solved it alone -> `{texto,sugerido,correcto,confirmada:false}` in `<claw home>/jev/correcciones.jsonl`. Recorded corrections are NOT examples: only when David explicitly confirms ("eso era para X" / "no, eso va con X") is one stored `confirmada:true`, and only the last 15 confirmed become examples in routing questions | `post_tool_call` + `post_llm_call` + `pre_llm_call` (claw only) | `jev_auto.py`, `jev_decisiones.py` |
| Chain stages (scout, ingenieria, implementer, verifier, adversary, reviewer, entrega) | NO Hermes hook exists: stages run in subscription CLIs outside Hermes (no turn, no tool hooks). `Jev sugiere` is injected when the driver composes the stage prompt and the end is logged when the stage finishes; the loop detector does not apply inside stages. | `JevEtapa` |

Clean text: before any query `jev_texto.limpiar` keeps only the current user
message (no `[Replying to: ..]`, media/system notes, `[Sender]`, Telegram
headers, fenced metadata blocks, nor our own
`JEV skill_select:`/`Jev sugiere:` lines). `decisiones.jsonl` records
`input` (first 200 chars, redacted) and `rawLen` (length before cleaning).
Savings: texts under 15 characters skip JEV, and the same question is not
asked twice in one turn. JEV down, slow (2 s cap) or answering badly never
blocks a turn (all hooks are fail-open).

Non-claw profiles do not install the `jev` plugin: they read claw's
`client.py` read-only and need their own `TYPESAFE_API_KEY` (profile `.env` or
scope); without it JEV fails open and the turn goes on. The `jev` plugin
versions are NOT changed here.

Activating it: restart the gateway FROM OUTSIDE a turn (from a terminal or
the service manager), never from a turn or from a subagent: `plugins reload`
fails in "drain" while a turn is running, and a subagent that restarts the
gateway kills its own run.

The tool handlers bind authorization to `PluginContext.profile_name`, so callers
cannot supply a role argument. This is a trusted-single-user application guard,
not OS security isolation: a local process with direct write access to the
SQLite database or operator CLI can bypass plugin hooks. The same-repository
mutex follows the director root's lifecycle; an operator who manually archives
that root while its children still run also bypasses the mutex. Separate boards
pointed at the same directory do not share this mutex (the default deployment
uses one board). Prompts still
carry behavioral duties such as evidence quality and respecting external-action
authority; the plugin cannot prove that a model's prose is truthful.

## Local execution

The seven-step local driver, the roots runtime
(`raices/`) and the session notices were retired in motor-01 block G: Hermes
splits read-only work among its own agents and hands every change to the
motor through the `motor` skill (`skills/motor/SKILL.md`).
