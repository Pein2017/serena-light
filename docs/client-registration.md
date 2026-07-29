# Parallel client registration

`serena-light` is an acceptance-only parallel MCP server. Keep the canonical
`serena` entry unchanged. Use the distinct name `serena-light`; do not replace,
rename, or remove `serena`.

The connector executable is
`/data/CoordExp/serena-light/.venv/bin/serena-light` with no arguments. Using
the absolute repository-owned environment avoids dependence on a client's
ambient `PATH` or `/root` configuration. It inherits the
client process's startup cwd and automatically activates that root. This is the
equivalent of the old Serena `--project-from-cwd` behavior, but it is connector
behavior: do **not** pass `--project-from-cwd`, `--context`, or old Serena
dashboard/logging arguments. A later shell `cd` is not observed; cross-root use
requires `activate_workspace` with an absolute path.

## Codex

Add this parallel entry to `/data/CoordExp/.codex/config.toml` only when
running the acceptance procedure:

```toml
[mcp_servers.serena-light]
command = "/data/CoordExp/serena-light/.venv/bin/serena-light"
args = []
```

Restart Codex after changing its configuration. Start the new task from the
target workspace directory; the connector binds that inherited cwd. Verify the
MCP server/tool list contains both canonical `serena` and parallel
`serena-light`, then call `get_runtime_status` on the latter. Clients may
normalize the server name when constructing an internal tool prefix. Do not edit the
existing `[mcp_servers.serena]` block or its `serena-hooks` entries.

## Claude Code

Claude Code's local native configuration is `/data/CoordExp/.claude/.claude.json`.
Merge this sibling entry into its `mcpServers` object for parallel acceptance:

```json
{
  "mcpServers": {
    "serena-light": {
      "command": "/data/CoordExp/serena-light/.venv/bin/serena-light",
      "args": []
    }
  }
}
```

Stop and restart the Claude Code session from the desired workspace directory.
Verify both MCP server names are reported, then call `get_runtime_status` on
`serena-light`. Do not alter the existing canonical
Serena registration or SessionStart behavior.

## CC Agent

CC Agents launch native Claude Code with `CLAUDE_CONFIG_DIR=/data/CoordExp/.claude`.
Register the same `serena-light` object in that native Claude configuration;
do not add it to `cc-plugin-codex/plugins/cc-for-pein/.mcp.json`, which owns
only the `cc_for_pein` plugin server:

```json
{
  "mcpServers": {
    "serena-light": {
      "command": "/data/CoordExp/serena-light/.venv/bin/serena-light",
      "args": []
    }
  }
}
```

Stop the affected CC Agent and start a new agent from the desired workspace
cwd. Its child Claude process inherits that cwd, so the connector auto-binds
it. Verify `get_runtime_status` is available from `serena-light` to the new
agent. This is not fresh-session acceptance; task 10.2 owns that test.

## Stop, rollback, and limits

For a normal stop, exit the client session; its connector releases its lease.
For an immediate acceptance cleanup, call `release_workspace` and then stop
the client. The shared daemon may stay warm after the last normal lease.

To roll back a parallel trial, stop the affected client(s), remove only the
`serena-light` registration you added, and restart those clients. Leave
canonical `serena`, its hooks, its command, and its runtime untouched. If the
daemon remains after clients have stopped, use normal process inspection and
stop only the identified `serena-light` daemon; never stop canonical Serena as
part of this rollback.

Fresh-session tool, cwd, navigation, diagnostics, hash-guarded edit, daemon
reuse, and cleanup acceptance passed on 2026-07-28 for Codex, Claude Code, and
CC Agent. Canonical-name switching remains separately unapproved.
