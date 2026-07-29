# Daemon fault acceptance — bounded evidence

Date: 2026-07-28

Scope: OpenSpec 5.3, 5.9, 5.10, and the lifecycle/fault portions of 9.5 and
9.7.  This lane adds only `tests/acceptance/daemon_fault_driver.py` and
`tests/acceptance/test_daemon_fault_acceptance.py`; it does not modify product
code or `tasks.md`.

## Boundary and model disclosure

The test daemon is a separate detached process.  It uses the current production
`create_daemon_app`, `WorkspaceDaemonService`, `LeaseLifecycle`,
`WorkspaceRuntimeRegistry`, `BoundedLspExecutor`, Linux
`LanguageServerSubprocessLauncher`, `McpSessionFactory`, and `Connector`.
Consequently the daemon-process, loopback Streamable HTTP, MCP session,
connector heartbeat, lease, bounded-executor, and parent-death boundaries are
real.

The runtime's semantic methods are deterministic test doubles only for the
otherwise unforceable timing point: a worker blocks for 62 seconds or until the
parent process is killed.  It starts a real protected child process through the
production parent-death launcher.  No production language server or edit is
used, so the fault test labels its request body/edit phase as *modeled*.  This
is necessary to place SIGKILL after request acceptance without a production
test hook.

Each test captures the daemon PID plus `psutil` creation time, and every owned
descendant's PID, creation time, and process group before sending a signal.  It
only signals those identities; `finally` cleanup only terminates the task-owned
driver PID after rechecking its creation time.

## Commands and results

```text
uv run ty check
PASS — All checks passed.

uv run ruff check tests/acceptance
PASS — no diagnostics.

uv run pytest -q \
  tests/acceptance/test_daemon_fault_acceptance.py::test_detached_daemon_survives_winner_connector_exit_while_second_lease_is_healthy \
  -o timeout=60
PASS — 1 passed.

uv run pytest -q \
  tests/acceptance/test_daemon_fault_acceptance.py::test_real_wall_clock_block_keeps_production_heartbeats_status_and_second_root_responsive \
  -o timeout=110
PASS — 1 passed in 63.57s.

uv run pytest -q \
  tests/acceptance/test_daemon_fault_acceptance.py::test_sigkill_idle_read_and_edit_cleanup_rebind_and_never_replay_edit \
  -o timeout=60
PASS — 1 passed in 6.63s after isolating each SDK HTTP session in its own
owner task.
```

## Accepted evidence

### 5.3 detached starter exit

PASS.  The driver was launched with production `spawn_detached_process`, which
uses a new session and no inherited stdio.  Two real connector/MCP sessions
obtained distinct leases on the same root.  The first connector closed normally;
the detached daemon remained alive at its recorded PID/create-time and the
second lease successfully returned `get_runtime_status`.

### 5.9 real wall-clock blocking request

PASS.  The long request blocked one workspace executor worker for 62 seconds;
the measured caller duration was **63.57 seconds**.  The production connector
used its normal 15-second cadence, and the daemon's observed HTTP heartbeats
for that exact lease recorded at least four renewals.  The first three observed
intervals are asserted to be 12–20 seconds.  While the request was active:

- a separate same-root connector completed real `get_runtime_status` over HTTP;
- a second root completed `find_symbol` over HTTP; and
- daemon status reported `active=true`, queue capacity 2, and exactly one
  `serena-light-lsp:<root>` worker thread for the blocked runtime.

This is real transport and scheduler evidence, with only the request body
modeled as described above.

## 5.10 / 9.7 real daemon SIGKILL recovery

The first strict run exposed that MCP SDK Streamable HTTP and its AnyIO cancel
scope were owned by the connector's main task. A killed daemon could therefore
cancel the same task after a replacement session had been installed. The
production fix gives every SDK HTTP session a dedicated owner task and proxies
session operations through a bounded command queue; the old cancel scope can
terminate without cancelling the connector or its replacement session.

The unchanged strict test then passed all idle, read, and edit phases:

- daemon and exact captured protected descendant identities are no longer live
  after SIGKILL;
- idle/read recover to a new daemon identity and lease while restoring the last
  binding;
- edit returns `UNCERTAIN` with `requires_current_reread=true`;
- only one modeled edit-start event exists (no replay); and
- a current reread succeeds only after recovery.

No unowned process was signalled. Post-run checks found no task-owned driver or
protected-child process remaining.

## Residual decision

5.3, 5.9, 5.10, and the daemon-SIGKILL/no-replay branch of 9.7 have bounded
passing evidence. Other 9.7 fault families remain owned by the complete suite
and final acceptance report.
