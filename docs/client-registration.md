# Parallel client registration

`serena-light` is an acceptance-only parallel MCP server. Keep the canonical
`serena` entry unchanged. Use the distinct name `serena-light`; do not replace,
rename, or remove `serena`.

The connector executable used by live Codex/Claude configurations is the
service-owned, build-identity-scoped path
`/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light`
with no arguments — **not** the repository `.venv`. The dependency-digest
segment (`eff6ebdf...`) and each build identity installed under it are tied
to a versioned daemon slot. The build identity
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77` is
historical: it names the archived 2026-07-30 position/coverage acceptance and
has been superseded by later source-only rollovers, so read the identity in
effect from `get_runtime_status` rather than treating it as current. A
source/schema-only rollover reuses the dependency
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
`activate_workspace` with an absolute directory path explicitly.

The archived schema-4 `tighten-query-recovery` revision uses source build
`78de9eaeac5c3b8522e568e364ee124aa6dca4b8bb0874eb1b63f51408c6d92c`
with the same dependency digest and registration executable above. Its compact
initialize instruction is source-owned and byte-identical at the outer and
inner boundaries. Tool-local descriptions own depth, ambiguity, snippets,
diagnostics, and debug-status guidance. Deterministic file symbol misses and
bound wrong-root query paths may carry the closed `next_action` values
`get_symbols_overview` and `activate_workspace_if_other_root`; these are advice,
not automatic calls. The change adds no hook, public tool, client registration,
root discovery, or canonical-name switch. Verify the effective identity with
`get_runtime_status` rather than relying on this recorded acceptance value.
Serena Light remains experimental: Agents should report every observed
friction point or issue to the user so the MCP can be iterated and improved.
This instruction-only source revision selects build
`10a465026933abb259b0d43b1df94db5efd066d0cf45d02fe0954204aae95945`;
schema 4, the dependency digest, registration, and the 11-tool surface remain
unchanged. Existing leased clients stay on their prior build slot until restart.

## Codex

Codex should load Serena Light through the branded `serena-light@coordexp-local`
plugin. The local marketplace at `/data/CoordExp/.agents/plugins/marketplace.json`
points to this checkout, and the plugin's `.mcp.json` starts the same
service-owned connector documented above. Install or refresh it with:

```bash
codex plugin add serena-light@coordexp-local
```

The plugin MCP entry intentionally has no `cwd`: the connector inherits the
new task's startup directory and preserves lazy workspace binding. Start a new
Codex task after installation or refresh, then verify `serena-light` appears in
the MCP server/tool list and call `get_runtime_status`.

Do not retain the direct `[mcp_servers.serena-light]` entry while the plugin is
enabled. A same-name user MCP shadows the plugin-provided server and loses the
plugin identity and visual presentation. The direct entry remains a rollback
fallback when the plugin is disabled:

```toml
[mcp_servers.serena-light]
command = "/data/CoordExp/.codex/runtime/serena-light/deps/eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941/python/bin/serena-light"
args = []
```

Restart Codex after changing either registration mode. Start the new task from
the target workspace directory; the connector binds that inherited cwd. Keep
canonical `serena` and its hooks unchanged during the parallel trial.

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

For a one-MCP native acceptance, use a temporary `--strict-mcp-config` that
contains only `serena-light`, pass `--setting-sources ''`, use
`--permission-mode dontAsk`, and allow only the read-only
`mcp__serena-light__...` tools required by the prompt. In the current Claude
Code 2.1.220 acceptance, `--tools ''` also prevented the explicitly allowlisted
MCP tools from being called, so omit that flag; the MCP allowlist plus
`dontAsk` keeps non-allowlisted built-ins unusable. An additional `--settings`
file with empty hook arrays does not reliably override the shared canonical
Serena SessionStart hook. Strict MCP configuration alone is also insufficient;
an accepted isolated run must prove that it created no `.serena/` artifact in
the fixture.

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

## Compact navigation schema rollover (client migration)

Archived OpenSpec change `2026-07-30-compact-success-schema` owns the accepted
schema-3 migration contract. Fresh-client, rollover, full-suite, and ablation
evidence are complete, and post-repair Codex, native Claude Code, and CC Agent
receipts select build `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`.
The first audit blockers are repaired and the final Sol-xhigh and Opus-max
audits both pass with no P0/P1 findings. The accepted schema carries no
compatibility shim: there is no `compact=true` flag and no dual schema. Incrementing
`PUBLIC_TOOL_SCHEMA_VERSION` changes build identity and starts a new versioned
daemon slot exactly like the correctness rollover above — re-check the
registration path in this file rather than assuming the digest or build slot
is unchanged, restart the affected client from its intended workspace cwd, and
re-verify its tool listing and `get_runtime_status` the same way prior
rollovers were verified. Old build slots and their holders keep serving the
prior verbose schema until they drain normally; no daemon is stopped by name to
force the migration.

Every consumer of `get_symbols_overview`, `find_symbol`,
`find_referencing_symbols`, `find_declaration`, and `find_implementations`
success must migrate off the current `{"ok":true,"data":{...},"workspace":
{...},"adapter":{...},"generations":{...}}` shape and onto
`{"ok":true,"data":{"workspace":"<absolute root>","files":[...],"omitted":
<int>}}`, with `data.coverage` added once for references only. Per-record
`path`/`sha256`/`language` repetition, per-record text/byte offsets, adapter
phase, runtime generations, and configured-program detail disappear from
navigation success; a client that inspected those fields directly must instead
read them once per file group or from `get_runtime_status`/typed errors.
Tool schemas gain `find_symbol.max_matches` (1-100, default 20),
`find_declaration.max_answer_chars`, lowercase `include_kinds`/`exclude_kinds`
on `get_symbols_overview`, the existing integer kind filters on
`find_implementations`, and
`find_referencing_symbols.max_snippet_chars` (default `0`, so snippets are
omitted unless a client requests them); there is no public adapter-candidate
fan-out control. Overview kind filtering is post-order: a non-matching ancestor
is retained only when needed to keep a matching descendant reachable, while
every node actually removed by depth, kind filtering, or final budgeting is
included in `data.omitted`. See
[the compatibility inventory](compatibility.json)'s `migration_examples` for
exact old-to-new field mappings and representative payloads for all five
tools, including the `raw_range`/`position_basis` fallback a client must
handle for a read-only external target lacking an exact response-owned
snapshot. This accepted rollover leaves canonical Serena unchanged; known
nonblocking error-budget limitations are recorded in that inventory.

## Call-freshness strengthening rollover (accepted)

The implementation is archived at
`openspec/changes/archive/2026-08-01-strengthen-call-freshness`;
the unimplemented `add-lexical-discovery` and `improve-warm-runtime-reuse`
plans were later retired. Accepted build
`7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80` keeps the
same public tool schema (`3`) and the same dependency digest (`eff6ebdf...`)
as the archived compact-success-schema build above. This is a source-only
build-identity rollover: it does **not** change the registration command or
path in this file, because both already reference the digest directory
rather than a specific build identity. It does mean that a client process
already running from before this rollover remains bound to whichever build
its connector resolved at its own startup — that client keeps serving the
pre-rollover build until it is stopped and a fresh client is started (or
reconnects) from its workspace cwd, at which point the connector's next
startup resolves the current build identity. Verify the actual build
identity in effect by calling `get_runtime_status` after restart, rather
than assuming any particular build is current without checking.

Every content-bearing read now admits under its own FIFO per-call freshness
ticket rather than accepting an already-running scan. Every call receives a
guarded preflight. A source-derived success or failure then retains
response-owned byte witnesses and receives a real postflight before it reaches
a client; invalid locators, trust failures, and adapter-owned
cold/cooldown/busy/timeout conditions remain typed after one preflight. A
change observed at postflight discards that attempt and replays the complete read once; a
second race returns a typed retryable `NOT_READY` with reason
`workspace_changed_during_read` — a well-behaved client should retry such a
call rather than treat it as a hard failure. This guarantee is anchored at
the call's own final guarded byte observation, not at response-delivery
time, and there is no background watcher. Editing remains outside this
replay: `replace_symbol_body` keeps its existing non-replayable commit-point
contract and `UNCERTAIN` handling. The explicitly trusted non-Git conda `ms`
transformers root keeps targeted stat-plus-byte pre/post validation for
scoped/indexed reads and adds a bounded no-symlink full-root pre/post scan
for global, directory, or not-yet-indexed reads on that root.

Acceptance evidence is PASS. The deterministic race-test
suite and a real daemon/connector race harness both pass. A real
shared-daemon acceptance run proved three simultaneous clients across two
roots, same-root reactivation, a partial release, a poisoned-proxy
environment, and zero newly created test-owned LSP process orphans. Prior CC
Sonnet/Opus host receipts remain superseded-build evidence and do not close
the current host matrix after the TypeScript authority root moved to
`/data/CoordExp/external/codexUI`. The current four-snapshot suite passes 875
tests with only the 3 explicit performance cases skipped in 439.52 seconds;
those 3 observation-only cases pass separately in 190.37 seconds, for 878
passing cases in total. This includes isolated cold first-call TypeScript
declaration/reference coverage. Recorded per-call
navigation/diagnostics latency is observation-only (2 samples per call,
sample minimum/maximum, no threshold or percentile interpretation):
`/data/CoordExp` global 11.32/33.27s,
scoped 10.87/11.22s, overview 10.74/11.31s, diagnostics 10.29/10.93s;
`/data/ms-swift` global 1.44/13.16s, scoped 0.45/0.89s, overview 0.57/0.95s,
diagnostics 0.85/0.90s. These historical observations do not establish a
current warm-pool requirement and are not a failure or registration gate. The
shared-daemon acceptance is complete: its process-level shared-client lifecycle
passes, and fresh Sol-xhigh and Opus-max sessions on the final build each pass
all four roots, same-root reactivation, cold TypeScript declaration,
cross-library resolution, and immediate zero-holder release. The full
pytest/Ruff/Ty/bootstrap/provenance and strict OpenSpec gates pass, both final
reviews pass, and this registration path is accepted. Canonical Serena remains
unchanged.

A pre-existing environment separately has 14 flat-layout legacy daemons that
predate the current build-slot registration scheme, with no active
connections; current connectors cannot discover or reuse them, and this
rollover only claims zero *newly created* orphans, not cleanup of that
legacy set. Do not stop them by PID as part of ordinary registration — a
legacy flat artifact is fail-closed, and any manual cleanup requires
separately authorized PID-plus-create-time (or connection) revalidation
outside this rollover. A live build-slot holder, including one created by
this rollover, still retires normally through the existing lease/grace path.

## Current schema-4 interaction migration

The archived sections above describe their own schema-3 acceptance evidence.
The current public tool schema is 4, so a fresh connector resolves a distinct
schema-4 build slot while a leased schema-3 slot drains normally. Do not stop a
daemon by name to force this migration: restart or reconnect only the client
being updated, then verify its tool list and `get_runtime_status`.

The exact MCP initialize text is owned by
`serena_light.instructions.AGENT_INSTRUCTIONS` and is published byte-for-byte
by both the outer stdio `Server` and inner daemon `FastMCP`; the canonical text
is recorded in [the compatibility inventory](compatibility.json). Its operating
rules are consequential for client behavior: startup cwd binds once, shell `cd`
does not rebind, cross-root work requires absolute `activate_workspace`, symbol
overview starts at depth 0, reference snippets need a positive opt-in, and
diagnostics are explicit after a meaningful edit group. Runtime status is for
debug/build/readiness questions, not a routine preflight.

Navigation and diagnostics success now expose only canonical
`{"ok":true,"data":{"workspace":"<absolute root>","files":[...],"omitted":<int>}}`
payloads. Overview depth/kind selection and default descendant
variable/constant suppression are selection semantics, not `omitted` data loss.
References exclude the declaration and use exactly `{"complete":true}` for
complete coverage; incomplete coverage carries only `complete=false`, the
uncovered total, a bounded path/reason sample, and its omitted count. Diagnostics
are file-grouped compact findings, with `authority="advisory"` only once for a
TypeScript file group. Engine, adapter, generation, configured-program digest,
URI, text-offset, and byte-offset details remain in runtime status or typed
operational errors rather than successful results.

Schema 4 does not enable lexical discovery, diagnostics hooks/automatic
injection, or RTK. Continue to use host shell/file tools for lexical file and
text work; do not configure a hook or a second instructions tool.

The accepted `tighten-scope-error-readiness` source-only rollover selects build
`7deb20c2cbff6b6fac622d012ff80a923987efd80b673e14c0bcc3fa9b6e0fcf`.
It preserves this registration command, dependency digest, schema 4, initialize
text, and tool surface. Projection-backed `SCOPE_INCOMPATIBLE` failures now
carry bounded language/project/config/outside-trust evidence, while a bound
failure reports the calling lease's own `working_subdirectory`. Existing
clients remain on their prior build slot; restart the client and verify this
identity with `get_runtime_status` to adopt the release.

## Stop, rollback, and limits

For a normal stop, exit the client session; its connector releases its lease.
For an immediate acceptance cleanup, call `release_workspace(immediate=true)`
and then stop the client. It stops the runtime only for the final holder; other
holders remain served. `runtime_stopped=true` means cleanup actually settled;
`runtime_stop_pending=true` means the detached runtime remains daemon-owned and
will be retried while status remains non-idle. The shared daemon may stay warm
after a normal release.

To roll back a parallel trial, stop the affected client(s), disable the Codex
plugin or remove only the direct `serena-light` registration you added, and
restart those clients. Leave
canonical `serena`, its hooks, its command, and its runtime untouched. If the
daemon remains after clients have stopped, use normal process inspection and
stop only the identified `serena-light` daemon; never stop canonical Serena as
part of this rollback.

Fresh Codex/Terra, native Claude Code/Sonnet 2.1.220, and CC Agent/Sonnet clients
passed the archived v1 double-pass candidate identity
`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`.
All three explicitly switched across `/data/CoordExp`, `cc-plugin-codex`,
`/data/ms-swift`, and the read-only conda `ms` transformers package; each
resolved an `ms-swift` declaration into transformers with
`location_kind=read_only_external`. Each also used `replace_symbol_body` in a
separate isolated Git fixture, verified the replacement, restored the original
body using the new current hash, confirmed clean content, and released with
`runtime_stop_pending=false`. The native run was a new non-persistent `claude
-p` session with built-in file/shell/edit tools disabled, not a CC Agent. All
three test-only fixtures were removed after exact restoration. The Terra
fixture moved `7d180e1a...` to `68fe02ea...` and back; the native fixture moved
`afd3fd8a...` to `d043f961...` and back; the CC Agent fixture moved
`7d180e1a...` to `7ab0150e...` and back. No production repository or external
library was edited, and every release reported `runtime_stop_pending=false`.

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
and CC Agent clients are accepted, and the final Sol-xhigh plus Opus-max audits
both passed exact clean commit `c2dffca`.
Canonical-name switching is unapproved.

The repaired 2026-07-30 correctness revision was inspected by fresh Codex/Sol,
native Claude Code/Sonnet, and CC Agent/Sonnet clients at exact build
`b3b9952e7abcbca7554c8572499c5541888f6ecf3661fe8787dbde629a258f33`.
All three returned coherent 0-based Unicode positions, complete Python and
TypeScript assignment statements, bounded reference coverage, declarations,
and stable repeated TypeScript diagnostics. Codex additionally verified that a
read-only external transformers declaration returns only its explicit raw LSP
range when an exact response-owned snapshot is unavailable. CC Agent acceptance
also used a dedicated nested Git fixture to replace a complete TypeScript
assignment including its terminal semicolon, verify exact bytes and hash, and
restore the original bytes and hash without duplicating declaration syntax. The native client was isolated
with `--strict-mcp-config`, `--tools ''`, and an explicit read-only
`mcp__serena-light__...` allowlist. The last release reported zero holders,
`runtime_stopped=true`, and `runtime_stop_pending=false`. This acceptance does
not change canonical Serena and remained on final independent dual-audit hold
at that predecessor build.

The independently dual-audited correctness candidate of the archived 2026-07-30
position/coverage acceptance was
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`; later
source-only rollovers have superseded it, so the receipts below are historical
evidence for that build rather than a statement about the current one.
Its predecessor `ecc4689b781c2de8c4bf03788a4dc17388c28e402220b99519294f31010dc358`
had a Sol-xhigh PASS but an Opus-max runtime HOLD.
The `481c45e...` audits proved that owner-before-`didChange` is sufficient only
when the engine publishes an integer document version. The locked TypeScript
server does not. For that engine `22c80421...` disowned the old target before
`didClose`, then installed the new generation before exact-full-text `didOpen`;
Pyright retained the versioned `didChange` path and rejected missing version
evidence. Exact review showed that `didClose` delivery alone did not causally
drain the server's unversioned close publication. Build `e26ccf65...` added the
response barrier and exact selected-identifier binding, but its final audits
showed that a first barrier timeout lost the obligation on exact retry. The
`ecc4689b...` repair kept process-tokened undrained-close state until a successful
same-connection barrier and drained LRU/watched close state before reopen, but
its watcher-created temporary lifecycle could record a close marker while the
URI remained locally open, and cached diagnostics reuse did not drain markers.
That `4b0a5e2e...` candidate skips that temporary lifecycle for an owned URI and
drains before retaining any cached diagnostics owner. The
`b3b9952e...`, `481c45e...`, `22c80421...`, and `e26ccf65...` fresh-client
receipts remain predecessor evidence and are not relabelled as a later
build. Exact-build Codex, hooks-isolated native Claude Code 2.1.220, and CC
Agent acceptance now pass the hash edit/restore and four-root semantic matrix
as predecessor evidence. Exact build `4b0a5e2e...` also passes the 747-test
fixed-snapshot suite and fresh Codex, native Claude Code, and CC Agent
diagnostics acceptance. The final Sol-xhigh static-correctness and Opus-max runtime/evidence audits now
both PASS exact fingerprint `ce73c06e...`; stable specs are synchronized and
the owning change is archived. Opus recorded one non-blocking pre-existing P2:
transport/protocol loss may surface as a loud generic MCP error rather than a
typed retry envelope, but cannot become false semantic success.
