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

El trabajo lo corre el cron de Hermes, no el gateway: cada vuelta del ticker
concilia las tarjetas con sus intentos, decide y despacha por la bandeja de
salida. El instalador (H4b3) copia `raices/cron_ticker.py` a
`<home>/scripts/ticker-raices.py` y registra el trabajo sin agente en el perfil
default (la raíz del home, sin `-p`; el scheduler le pone `HERMES_HOME` = home del
perfil y `gateway_state.json`, que lee la puerta de H4a, vive en la raíz):

```powershell
hermes cron create "every 1m" --name ticker-raices --script "ticker-raices.py" --no-agent --deliver local
```

`--script` se resuelve contra `<home del perfil>/scripts`
(hermes_cli/cron.py:604-618) y el argumento es el nombre pelado.

La convencion de salida es la de los scripts `no_agent`: nada si la vuelta salio
bien, una linea `ERROR ticker raices: ...` y salida 1 si fallo. Con la ruta de
raices cerrada (H4a: capacidad faltante, par incierto o par sin certificar en
`raices/certificadas.json`) el cron avisa `CAPACIDAD_INCIERTA` una vez por
cambio de causa y sale 0; la vuelta sigue corriendo cerrada, con bloqueos y
conciliacion pero sin decidir ni despachar ni correr la pasada del emisor, y si
falta una capacidad ni siquiera abre el store. `goncloud_root_submit` responde
`ruta-cerrada` con la causa; el par nuevo se acredita con `raices/certificar.py`
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
Delivery closes the director root after all stages and review gates finish.

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
