# Task 4 report: bounded subscription process trees

## RED

- The new PID-scoped child/grandchild timeout regression exceeded the cleanup
  bound with the former `subprocess.run` boundary; its descendant remained
  running until the test cleanup terminated that recorded PID tree.
- Deadline validation and adapter-cap regressions also failed before the
  implementation.

## GREEN

- `run_cli` now launches production commands with `Popen` in a fresh process
  group, kills only that tree on timeout (`taskkill /PID ... /T /F` on Windows;
  `killpg` on POSIX), and bounds cleanup to five seconds.
- The regression completed in approximately 1.5 seconds for a one-second
  deadline and verified both recorded PIDs were absent. Its `finally` cleanup
  is PID-scoped.
- Process evidence remains redacted/capped; private prompt files remain
  deleted in `finally`. Injected runners retain their existing contract.
- Adapter invocation deadlines cap at 300 seconds and probe deadlines at 30
  seconds; live probes cannot exceed the 300-second invocation deadline.

## Verification

- `tests.test_subscription_process tests.test_subscription_opencode
  tests.test_subscription_vendor_adapters tests.test_subscription_premium_clis`: 48 passed.
- `uvx pre-commit run --all-files`: passed.
- Commit: `fix: terminate timed-out subscription process trees`.

## Concerns

- No live vendor probes were run; validation uses only safe local subprocesses
  and injected vendor runners.
