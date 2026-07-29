# Parallel client registration

`serena-light` is an acceptance-only parallel MCP server. Keep the canonical
`serena` entry unchanged. Use the distinct name `serena-light`; do not replace,
rename, or remove `serena`.

The connector executable used by live Codex/Claude configurations is the
service-owned, build-identity-scoped path
`/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light`
with no arguments — **not** the repository `.venv`. The dependency-digest
segment (`eff6ebdf...`) and the current repair-candidate build identity
(`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`) are tied
to a versioned daemon slot. A source/schema-only rollover reuses the dependency
directory but gets a new build slot; a lock change also installs a new digest
directory. Old build slots coexist until their holders and grace expire, so
re-check the registration path after a dependency-lock change rather than
assuming the digest is permanent. Using an absolute service-owned path avoids
dependence on a client's ambient `PATH` or `/root` configuration.

The v1 dependency slot is an editable install: this service-owned executable
imports Serena Light from `/data/CoordExp/serena-light/src`. Build identity
detects covered `.py`/`.mjs` source changes and rolls to a new daemon slot, but
the slot is not a frozen source copy. Local rollback therefore includes
checking out the intended commit before restarting clients.

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
command = "/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light"
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
      "command": "/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light",
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
      "command": "/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light",
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
For an immediate acceptance cleanup, call `release_workspace(immediate=true)`
and then stop the client. It stops the runtime only for the final holder; other
holders remain served. `runtime_stopped=true` means cleanup actually settled;
`runtime_stop_pending=true` means the detached runtime remains daemon-owned and
will be retried while status remains non-idle. The shared daemon may stay warm
after a normal release.

To roll back a parallel trial, stop the affected client(s), remove only the
`serena-light` registration you added, and restart those clients. Leave
canonical `serena`, its hooks, its command, and its runtime untouched. If the
daemon remains after clients have stopped, use normal process inspection and
stop only the identified `serena-light` daemon; never stop canonical Serena as
part of this rollback.

Fresh Codex/Terra, native Claude Code/Sonnet 2.1.220, and CC Agent/Sonnet clients
passed the prior single-byte-pass candidate identity
`500f841f5826bd15a5332f6e30a968d846c600d0ce9cb6e3b6715f0243514c0d`.
All three explicitly switched across `/data/CoordExp`, `cc-plugin-codex`,
`/data/ms-swift`, and the read-only conda `ms` transformers package; each
resolved an `ms-swift` declaration into transformers with
`location_kind=read_only_external`. Each also used `replace_symbol_body` in a
separate isolated Git fixture, verified the replacement, restored the original
body using the new current hash, confirmed clean content, and released with
`runtime_stop_pending=false`. The native run was a new non-persistent `claude
-p` session with built-in file/shell/edit tools disabled, not a CC Agent. All
three test-only fixtures were removed after exact restoration. Those receipts
remain historical until they are repeated for the current double-pass build
`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`. The Terra
fixture moved `79cd2a41...` to `4e465daf...` and back; the native and CC Agent
fixtures moved `098c7ba9...` to `b60bef43...` and back. One interrupted CC
setup turn created the latter clean baseline before the newly spawned CC Agent
independently verified its commit/hash/clean state and used it; no production
repository was edited.

Historical fresh Codex and CC Agent query receipts match the prior repair identity
`eaa691e2425e7466f2f9c3d18666a050cfd53e8153de0c6db9a6f50c1538c3f5` across
`/data/CoordExp`, `cc-plugin-codex`, `/data/ms-swift`, and the read-only conda
`ms` transformers package. Both clients released each binding immediately;
the final release reported zero holders and a stopped runtime. A separate fresh
real-stdio client at the same identity advertised the required
`find_declaration(relative_path, regex, ...)` schema, resolved
`GenerationConfig` from `/data/ms-swift` into conda transformers, and released
with zero holders. That CC Agent made five declaration calls whose regexes all
had zero capture groups, so `INVALID_INPUT` was correct. The current MCP schema
now describes every tool and states the one-capture-group contract directly.
These older receipts remain tied to that prior identity.

Earlier native Claude Code and three-client guarded-edit receipts match
historical post-restoration build identity
`f4ee8a248a8cd2389b7b2d95083fd0d409548421b4933ac29a138ef0badf8721` and
remain historical. All model-facing clients retained the ambient external-network
`9090` proxy. Clean and poisoned internal environments are instead exercised by
the real service-executable stdio acceptance, which passes the exact environment
to the connector child and verifies loopback bypass. The prior exact-build
real-stdio suite also covers the restored guarded edit; it does not relabel the
older three-client edits as current. Current-build Codex, native Claude Code,
and CC Agent clients are accepted; the final dual audit remains open.
Canonical-name switching is unapproved.
