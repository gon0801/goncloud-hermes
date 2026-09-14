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
Kanban operations, and the native atomic decomposition builder. It does not run
a scheduler; Hermes' dispatcher remains authoritative for dependency promotion,
claims, retries, concurrency, and process launch.

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
