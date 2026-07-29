# Parallel client registration

`serena-light` is an acceptance-only parallel MCP server. Keep the canonical
`serena` entry unchanged. Use the distinct name `serena-light`; do not replace,
rename, or remove `serena`.

The connector executable used by live Codex/Claude configurations is the
service-owned, build-identity-scoped path
`/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light`
with no arguments — **not** the repository `.venv`. The dependency-digest
segment (`34cb2511...`) and the current build identity
(`6abd545dbc1d232b662ff06e1f777a6091356994c5cff12c3fde2c89a1736599`) are tied
to a versioned daemon slot. A source/schema-only rollover reuses the dependency
directory but gets a new build slot; a lock change also installs a new digest
directory. Old build slots coexist until their holders and grace expire, so
re-check the registration path after a dependency-lock change rather than
assuming the digest is permanent. Using an absolute service-owned path avoids
dependence on a client's ambient `PATH` or `/root` configuration.

Registering the connector does not bind a workspace. MCP tool listing (the
handshake a client performs to discover `serena-light`'s tools) never binds
anything. Binding happens lazily: the **first workspace-dependent tool call**
auto-binds the connector process's inherited startup cwd, equivalent to the
old Serena `--project-from-cwd` behavior but implemented as connector
behavior — do **not** pass `--project-from-cwd`, `--context`, or old Serena
dashboard/logging arguments. A later shell `cd` is not observed. To use a
nested Git root or switch to a different root entirely, call
`activate_workspace` with an absolute path explicitly.

## Codex

Add this parallel entry to `/data/CoordExp/.codex/config.toml` only when
running the acceptance procedure:

```toml
[mcp_servers.serena-light]
command = "/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light"
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
      "command": "/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light",
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
      "command": "/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light",
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

Fresh-session tool, cwd, navigation, diagnostics, warm-daemon reuse across
versioned build slots, and cleanup acceptance pass for Codex, Claude Code, and
CC Agent as of 2026-07-29. `replace_symbol_body` is agent-public again after
the full fault matrix and a poisoned-proxy real stdio hash edit/release passed.
Clients that negotiated tools while containment was active must be restarted
before the restored tool appears. Canonical-name switching remains separately
unapproved.
