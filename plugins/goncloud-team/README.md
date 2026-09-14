# Goncloud team plugin

This native Hermes plugin registers five tools in the `goncloud_team` toolset:

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
  substituting native `kanban_complete` for the required durable review tool;
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

`--revision` defaults to the repository's current Git commit. Optional
`--sources-file` reads a JSON list of `reference`/`observed_at` records; otherwise
the runner records the local owner assignment and its observation time.
`--request-key` enables repeatable start requests; the same key must retain the
same contract and step limit. `--max-steps` accepts integers from 1 through 200
and persists with the workflow. `--affects-business-rules` enables the existing
business acceptance direction. Use the same Kanban database and subscription
database across commands (`HERMES_KANBAN_DB` and `GONCLOUD_SUBSCRIPTION_DB` can
select isolated local stores).

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

Approval always passes through `WorkflowService.review` and its native run
metadata. A first high/critical rejection opens exactly one corrective
`implementer -> verifier -> adversary -> reviewer` round; any unresolved second
review blocks delivery. Other rejected or blocked stages stop the runner.
External dependencies return `waiting`; concurrent router ownership returns
`running`. A checkpoint returns `paused`; `resume` continues from durable state.
Delivery closes the director root after all stages and review gates finish.

All command results are JSON, including sanitized errors. Exit status is 0 for
delivery, pause, pending/waiting/running state; 1 for blocked work; 2 for invalid
requests or runtime errors. Status includes stages, slot/adapter/model/billing
attempt evidence, and durable review decisions, without assignment titles or
raw provider diagnostics. No login, deployment, external message, or Telegram
cutover is authorized by running these commands.
