## Why

The current Serena setup provides valuable semantic navigation, but its global
active-project model, per-session language-server ownership, broad feature
surface, and `/root`-owned runtime dependencies create avoidable friction across
Codex, Claude Code, CC Agents, workspaces, and sessions. We can replace that
operational shape with a smaller, independently owned service while retaining
the Python and JavaScript/TypeScript semantic operations that agents actually
use.

Section-1 admission established the dependency, source-budget, readiness,
position-encoding, and process-cleanup evidence needed to implement the owned
core, but it also falsified the original Git-exact semantic-program contract.
Native `tsconfig.json` and `pyrightconfig.json` files intentionally select a
semantic program that may be smaller than the Git source inventory, while a
native TypeScript program can also include ignored/generated sources. The
change therefore keeps Git as the trust and discovery boundary, preserves
native project configuration as the semantic authority, and adds one revised
scope-admission gate before Section 2 begins.

## What Changes

- Add an internal Python 3.12 MCP service composed of a shared localhost daemon
  and a small per-client stdio connector.
- Bind workspaces per MCP session rather than through a global active-project
  pointer; reuse one locked runtime for sessions on the same root while allowing
  different roots to run independently.
- Support Python through Pyright and the JS/TS family through a server-owned,
  pinned TypeScript language server, with lazy startup and explicit readiness.
- Add the agent-facing semantic tools `activate_workspace`,
  `release_workspace`, `get_runtime_status`, `get_symbols_overview`,
  `find_symbol`, `find_referencing_symbols`, `find_declaration`,
  `find_implementations`, file/symbol diagnostics, and
  `replace_symbol_body`. The Serena-compatible `find_declaration` name retains
  its upstream definition-resolution semantics and dispatches
  `textDocument/definition`.
- Make capability, engine, readiness, trust, and failure states typed and
  observable. Unsupported and not-ready operations must never appear as empty
  semantic results.
- Use Git-aware discovery as the authorization and source-discovery upper bound
  for Git workspaces, while reporting the native language-server program as a
  separate semantic projection. Use bounded, no-symlink walks for explicitly
  trusted non-Git query roots such as the conda transformers package.
- Reject native programs that add ignored/generated supported-language files
  outside the trusted inventory with typed `SCOPE_INCOMPATIBLE`; do not create
  overlay project files that change repository semantics merely to force set
  equivalence.
- Permit semantic locations returned from the pinned conda `ms` environment's
  standard-library and site-packages trees as typed read-only external results;
  do not expand the set of non-Git roots that may be activated and indexed.
- Guard symbol-body replacement with a whole-file `expected_hash`, an atomic
  file replacement, edit-root authorization, and no automatic edit replay.
- Copy only the required MIT-licensed Serena/SolidLSP mechanisms from commit
  `9a9d07e83d8c`, record per-file provenance, and own the result without an
  upstream synchronization contract.
- Run `serena-light` in parallel with the existing `serena` MCP during
  acceptance. Switching the canonical MCP name is a later, separately approved
  integration action.

### Non-goals

- JetBrains integration, UI/dashboard, memories, telemetry, call audit, or
  verbose logging.
- Completion, rename, formatting, code actions, call/type hierarchy, or
  line-based insertion and deletion.
- Public packaging, user-selectable language packages, or a general-purpose
  configuration framework.
- Byte-for-byte compatibility with Serena's rendered text responses; selected
  tool names and main arguments are retained where useful, while results use
  stable typed JSON.
- Treating `/data/CoordExp/external/serena` as an upstream to merge, rebase, or
  vendor-sync automatically.

## Capabilities

### New Capabilities

- `workspace-runtime`: Shared-daemon connectivity, session-scoped workspace
  binding, trust enforcement, readiness, leases, concurrency, process cleanup,
  and crash recovery.
- `semantic-navigation`: Language-aware symbol overview, lookup, reference,
  declaration, and implementation queries across Python and JS/TS.
- `diagnostics-status`: Explicit runtime/capability/engine status plus bounded
  file- and symbol-scoped diagnostics with documented authority boundaries.
- `guarded-symbol-editing`: Hash-guarded, atomic replacement of complete symbol
  bodies within authorized edit roots.

### Modified Capabilities

None. This is the first change in a new repository.

## Impact

- Adds a new `serena-light` Python package, stdio connector entry point,
  localhost HTTP daemon entry point, adapter seam, and focused test suite.
- Adds pinned runtime dependencies for the Python MCP SDK, Pyright, a TypeScript
  language server with TypeScript 5.9, and an LSP protocol type library.
- Creates runtime state below shared `/data/CoordExp/.codex` ownership rather
  than relying on `/root` configuration; exact state paths will be fixed in the
  design.
- Preserves existing Serena unchanged during implementation and acceptance.
- Requires explicit disclosure that TypeScript LSP diagnostics are advisory:
  the probe found three TS 5.9 errors in `runtime/args.mjs` while the owning
  repository's TS 7 typecheck passed. Repository-native CI remains authoritative.
- Stops the fork before rollout if production code exceeds 12k lines, requires
  Serena's agent/modes/project-server architecture, cannot prevent orphan
  language servers, cannot distinguish trust inventory from native semantic
  program scope, or cannot distinguish cold/unsupported states from empty
  results.
- Requires the final acceptance report to inventory breaking old/new tool and
  hook contracts. An unresolved compatibility delta blocks a later canonical
  MCP-name switch, but does not block implementation or parallel acceptance
  under the distinct `serena-light` name.
