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
- `goncloud_local_close` closes a local chain whose driver already stopped
  (archives the director root and the stages that are not done, keeps the
  done stages as evidence, and records the reason on the root) so the
  repository accepts new chains again. Claw-only, idempotent; it refuses a
  chain whose driver may still be running (requires one of the driver's own
  stopped outcomes — paused, blocked, waiting or error — and no live lease)
  and it also frees a root stuck in triage without a graph.

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
  suggestion" — and a `transform_llm_output` hook appends a `JEV: ...` footer
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
  with no message text (just its length and a short sha256) and no key.

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

## Raices del sistema de agentes (SUA-07)

Las herramientas `goncloud_root_submit`, `goncloud_root_read`,
`goncloud_root_control` y `goncloud_root_resolve` son solo para el perfil claw
y hablan con los nombres de campo del contrato `sistema-agentes.v1`: submit
abre la raiz con el documento `meta.v1` (reintentos con el mismo plan devuelven
el recibo original), read devuelve el snapshot de la raiz, control entrega el
recibo de `control.v1` (un reintento exacto devuelve el original con
`replayed: true`) y resolve guarda la respuesta del dueno en la `excepcion.v1`.

`goncloud_root_instruction_put` y `goncloud_root_reconcile` (SUA-07 H6a,
tambien solo claw) traen los hechos de asignacion que al ticker le faltaban:
instruction_put guarda el TEXTO del brief o de la evidencia en el store
content-addressed `<raiz>/raices/instrucciones/<sha256>` (escritura
atomica, idempotente por contenido: el mismo texto devuelve el mismo
`artifact:<hex>`; tope de 32,768 caracteres) y el texto NUNCA viaja dentro
de la asignacion, solo su `{ref, digest}` (fixture G). reconcile valida cada
assignment contra la copia fijada del contrato, exige que su `instructionRef`
resuelva en ese store con el digest verificado y persiste por clave estable
`(rootId, planRevision, unitId, relay)`: el reintento exacto es `replayed:
true`, otra asignacion con la fila viva es `operation-conflict` y una fila ya
consumida abre un ciclo nuevo de correccion o relevo. El recibo incluye las
necesidades vigentes de la raiz (el espejo de los `needs` de R), que
`goncloud_root_read` tambien expone. El consumo de cada asignacion ocurre en
la MISMA transaccion que liga el intento: una vuelta interrumpida nunca
duplica la tarjeta.

Desde H6b reconcile trae tambien los CLAIMS de verificacion, con la forma de
`managedRootFactsSchema` de R (evidenceClaim sin `verifier` ni `evidenceId`,
mas `role` en `verifier|ci`): el store pone `rootId` y `planRevision`, arma
el `verifier {principal: ["claw"], role}` y deriva el `evidenceId` del
contenido, con la misma vara con la que decidir siembra (contrato.validar
EVIDENCIA sobre el sembrado y `artifactRef` resoluble en el MISMO store de
instrucciones con su digest: la evidencia sube con instruction_put). Un claim
que decidir descartaria se rechaza tipado (`documento-invalido` /
`instruccion-invalida`), sin filas. Se persiste en tabla propia con clave
estable `(rootId, planRevision, unitId, attempt, criterionId)` — replay
exacto, `operation-conflict` con la fila viva, ciclo nuevo tras el consumo —
y el consumo ocurre en la MISMA transaccion que persiste la evidencia en
`aplicar`. `hechos_por_raiz` los transporta en `"claims"` con
`principal = ["claw"]` (quien los trajo: el unico perfil que llama a la
herramienta).

El trabajo lo corre el cron de Hermes, no el gateway: cada vuelta del ticker
concilia las tarjetas con sus intentos, decide y despacha por la bandeja de
salida. El instalador (H4b3) copia `raices/cron_ticker.py` a
`<home>/scripts/ticker-raices.py` y registra el trabajo sin agente en el perfil
default (la raíz del home; toda llamada lleva `-p default` porque el CLI usa el
perfil activo por encima de `HERMES_HOME`):

```powershell
hermes -p default cron create "every 1m" --name ticker-raices --script "ticker-raices.py" --no-agent --deliver local
```

`--script` se resuelve contra `<home del perfil>/scripts`
(hermes_cli/cron.py:604-618) y el argumento es el nombre pelado.

El despertar del dueño (H6b) es OTRO cron sin agente, `despertar-raices`
(`raices/despertar.py`), con entrega al chat del director:

```powershell
hermes -p default cron create "every 1m" --name despertar-raices --script "despertar-raices.py" --no-agent --deliver bot-chat:claw
```

Con `--deliver bot-chat:claw` el stdout de la corrida entra como un turno en
el chat "Bot Chat" de claw y stdout vacío es corrida silenciosa. En cada
vuelta, para cada raíz viva (control active, no terminal) con necesidades no
vacías, imprime un bloque si el `needs_digest` cambió desde el último aviso o
si venció `RENOTICE_MS`; el asiento (`avisos_de_necesidades`) ocurre en la
misma transacción del store que decide imprimir. El texto lleva el rootId,
las necesidades y la línea que le dice a claw qué contestar: nada de briefs
ni secretos. El instalador registra los DOS crons (estado versión 2 en
`raices/instalacion.json`, una fila por cron con su id) y `--revertir` retira
ambos con las mismas garantías que ticker-raices.

La convencion de salida es la de los scripts `no_agent`: nada si la vuelta salio
bien, una linea `ERROR ticker raices: ...` y salida 1 si fallo. Con la ruta de
raices cerrada (H4a: capacidad faltante o par incierto) el cron avisa
`CAPACIDAD_INCIERTA` una vez por
cambio de causa y sale 0; la vuelta sigue corriendo cerrada, con bloqueos y
conciliacion pero sin decidir ni despachar ni correr la pasada del emisor, y si
falta una capacidad ni siquiera abre el store. Una version sin certificar en
`raices/certificadas.json` ya no cierra (encargo H punto 3): la puerta abre con
aviso, el cron imprime `AVISO` una vez por cambio de causa y la vuelta corre
abierta. `goncloud_root_submit` responde
`ruta-cerrada` con la causa cuando la puerta cierra, y lleva el `aviso` en el
recibo cuando abre con uno; el par nuevo se acredita con `raices/certificar.py`
en la PC y entra por PR. El store vive en la raíz del home de Hermes
(`raices/hogar.py` la resuelve con `hermes_constants`: el home de perfil del
gateway y el del cron suben a la misma raíz); el kanban, `HERMES_KANBAN_DB` o
`<raíz>\kanban.db`, y solo se abre si hay algo que despachar. La
expresion `* * * * *` es el cada minuto estandar de cron; la referencia 0.21.5
copiada fuera del repo solo trae `kanban_db*`, no el planificador del cron, asi
que en H5 el coordinador confirma en la PC que el cron acepta cada minuto (si
no, se baja la frecuencia: la vuelta es idempotente y dos tickers a la vez no
duplican tarjetas).

## Local execution

Use the installed Hermes Python environment. Telegram and the native Hermes
model picker are not involved:

```powershell
$python = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
& $python scripts/run_local_workflow.py start --repository C:\dev\target --instruction-file C:\private\assignment.txt --max-steps 20 --stop-after scout --json
& $python scripts/run_local_workflow.py resume --workflow-id <id> --json
& $python scripts/run_local_workflow.py status --workflow-id <id> --json
```

Omit `--stop-after` to run through delivery. The exact initial sequence is
`scout -> ingenieria -> implementer -> verifier -> adversary -> reviewer -> entrega`.
`--instruction-file -` (the default) reads stdin. Instructions never become
command arguments or CLI output. They remain in the existing private Kanban
workflow contract for resume, so **do not put secrets in assignments or sources**.
The CLI limits instruction and source files to 32,768 characters.

On first creation, `--revision` defaults to the repository's current Git commit. Optional
`--sources-file` reads a JSON list of `reference`/`observed_at` records; otherwise
the runner records the local owner assignment and its observation time.
`--request-key` enables repeatable start requests; the same key must retain the
same contract and step limit. `--max-steps` accepts integers from 1 through 200
and persists with the workflow. `--affects-business-rules` enables the existing
business acceptance direction. Use the same Kanban database and subscription
database across commands (`HERMES_KANBAN_DB` and `GONCLOUD_SUBSCRIPTION_DB` can
select isolated local stores).

Repeated starts with the same request key reuse the original revision and
source observation time when those flags are omitted. Explicit revision/source
changes still fail the service's exact contract comparison; invalid source
files (including JSON `null`) are rejected.

Every worker must end its response with a fenced JSON object:

```json
{"status":"completed","summary":"bounded result","evidence":["command or file evidence"],"review":null}
```

Completed reviewers must instead supply exactly
`"review":{"decision":"approve","severity":null}` or
`"review":{"decision":"reject","severity":"high"}`. Rejection severities are
`low`, `medium`, `high`, or `critical`; blocked responses use `"review":null`.
Only the final JSON fence is parsed, with no trailing content, duplicate keys,
extra fields, nonfinite numbers, or coerced types. Limits: 24,000 UTF-8 input
bytes, 1,800 envelope bytes, a 400-character summary, and eight evidence strings
of up to 120 characters each.

An optional in-process result transformer in the service/router validates and
redacts that envelope before attempt persistence and the existing 2,000-character
text truncation. It persists no raw surrounding output or session identifier;
malformed results and exceptions become a fixed failure message. Its default is
unset, preserving existing subscription callers. A crash after inference can
reuse the persisted envelope and finish the native handoff without another
vendor invocation. Downstream roles receive completed stages' bounded evidence.

Recovery also normalizes previously cached results before exposing or handing
them off. Completed task failures and exhausted route chains remain terminal
across a crash; resume blocks them without another invocation. A crash between
recoverable slots resumes at the next slot, while an interrupted attempt may
resume its own slot with the router's existing continuation evidence.

Approval always passes through `WorkflowService.review` and its native run
metadata. A first high/critical rejection opens exactly one corrective
`implementer -> verifier -> adversary -> reviewer` round; any unresolved second
review blocks delivery. Other rejected or blocked stages stop the runner.
External dependencies return `waiting`; concurrent router ownership returns
`running`. A checkpoint returns `paused`; `resume` continues from durable state.
Every driver start — a detached launch and a CLI `resume` alike — first
overwrites that result file with a `running` mark (its own `run_id` and pid)
and writes its outcome under the same `run_id` — signed with the writer's
pid (review F12) — when it quits, so a stopped outcome is only evidence
while it is the file's last write: a relaunched chain refuses the close
until its own driver stops. A driver that dies without writing its outcome
leaves the mark in `running`; one more `resume` produces a fresh outcome. A
`running` mark is read through its pid (encargo H punto 4): the marked
process must predate its own mark, so a dead pid — or one recycled by a
process born after the mark — reads as a stopped driver, while a live pid
keeps the close refused and a relaunch queued. A `running` mark without a
usable pid has no driver behind it — every driver signs its launch and its
report — so it reads as driverless (review F12): the chain stays closable
and relaunchable instead of jamming the repository, and an unreadable
result file reads the same way (a foreign state: every write is atomic).
Relaunching is retrying (encargo H punto 1): a run whose request key lands
on a chain with blocked stages and a stopped driver unblocks those stages
with the native unblock and opens a new attempt generation for each, so the
router calls the model again instead of replaying the recorded result. Done
stages are respected; reviewer stages stay closed (the two-round ceiling and
its durable decision are contracts); there is no retry cap.
Delivery closes the director root after all stages and review gates finish.
`WorkflowService.start` no longer refuses when a repository already has an
active chain (encargo H punto 2): a chain whose driver already stopped is
closed automatically with the same native path `goncloud_local_close` uses
(the not-done stages archived, the reason left as a close comment), and the
new chain starts; a chain whose driver may still be working — a running
stage, a live subscription lease, or a live running mark — leaves the new
one queued instead of failing, and the old chain's driver launches the
queue's head itself (FIFO, one chain) when it writes its final outcome —
the next run (or `resume`) is only the fallback. Two drivers never edit the
same checkout at once. A chain sitting in the queue never had a driver: its
durable queue marker is the close evidence, so `goncloud_local_close` takes
it out of the queue and `goncloud_local_status` announces it. The marker
retires the moment the chain launches (review F13) — a launched chain has a
driver, and neither the stale marker nor anything short of the driver's own
stop evidence lets the close archive it between stages.
The claw profile can still release a chain with `goncloud_local_close`
(`workflow_id`, `reason`): the tool requires durable evidence that the
driver is gone — one
of the driver's own stopped outcomes (paused, blocked, waiting or error) as
the result file's last write, a running mark whose pid is gone, and no live
subscription lease — and it
archives the root and the stages that are not done through the native
`archive` (one native transaction per task, never nested in one of its own),
keeps the done stages as evidence,
records the reason as a comment on the root, and returns a receipt derived
from the resulting durable state, so closing an already-closed chain returns
the same receipt. A root stuck in `triage` without its stage graph (a
decomposition that failed after the root insert) closes the same way, since
it never had a driver. `goncloud_local_status` announces the close with a
`close_hint` only under the same evidence: a live lease or a live driver
never advertises the close.

Reviewer envelopes remain available as bounded handoff/status evidence,
including evidence needed by a corrective implementer. Decisions still come
only from the review service's metadata. Local complete/block and director
closure use connection-local SQL guards evaluated inside the native Kanban
UPDATE, so reassignment, a changed status/contract, or withdrawn approval cannot
slip between authorization and the transition. Native lifecycle hooks retain
their own commit boundary.

All command results are JSON, including sanitized errors. Exit status is 0 for
delivery, pause, pending/waiting/running state; 1 for blocked work; 2 for invalid
requests or runtime errors. Status includes stages, slot/adapter/model/billing
attempt evidence, and durable review decisions, without assignment titles or
raw provider diagnostics. No login, deployment, external message, or Telegram
cutover is authorized by running these commands.
