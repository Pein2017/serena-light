## Why

The first Luna V2 paired navigation benchmark found no semantic-correctness gap
between Serena Light and official Serena, but the Serena Light arm spent seven
calls on nonexistent symbols or the wrong active root. At the same time, the
current 562-character source instruction appears as a 564-character prefix on
all 11 Codex tool descriptions, adding about 5,640 redundant description
characters per fresh tool surface; query recovery and one-time metadata are
now higher-value targets than another semantic feature.

## What Changes

- Compress the source-owned initialization guidance while preserving language
  scope, explicit workspace switching, overview-first recovery, and host-owned
  lexical search.
- Move depth, snippet, diagnostics, and status advice to the existing owning
  tool descriptions so guidance remains discoverable without repeating the
  whole workflow on every tool.
- Add bounded, machine-readable recovery hints to file-scoped
  `SYMBOL_NOT_FOUND` and bound-workspace `INVALID_PATH` errors.
- Make `find_symbol`, `get_symbols_overview`, and `activate_workspace` metadata
  teach overview-before-guessing, qualified ambiguity retry, and persistent
  lease binding across shell `cd`.
- Record fresh Codex and Claude/CC metadata receipts plus one Light-only
  Luna/medium smoke as observational evidence; deterministic tests, not a
  stochastic Agent call count, own acceptance.
- Keep public tool names, inputs, successful response shapes, schema version 4,
  semantic dispatch, freshness, diagnostics, editing, and lifecycle behavior
  unchanged.

Explicit non-goals are fuzzy symbol matching, guessed candidate aliases,
automatic workspace rebinding, lexical search or file-enumeration tools,
diagnostics hooks, generic batch RPC, `structuredContent` removal, success-shape
flattening, RTK integration, and any change to official Serena assets or client
registration.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Make concise initialization and tool-local metadata
  jointly own the Agent workflow without changing explicit lease-scoped
  workspace binding.
- `semantic-navigation`: Make deterministic symbol/path failures actionable and
  bounded without fuzzy fallback, silent selection, or automatic root changes.

## Impact

The change is limited to source-owned instructions, existing MCP tool/field
descriptions, deterministic error construction/presentation, focused tests,
and compatibility/acceptance documentation. Runtime source changes select a
new build identity through the existing build-slot mechanism, but the public
tool schema remains version 4 because all response additions are optional
error-only correction details and no success or input contract changes.

Admission starts from the recorded Luna benchmark: equal final code facts,
official 42 MCP plus 15 shell calls, Serena Light 43 MCP plus 11 shell calls,
four guessed-name misses, three stale-root misses, one correct ambiguity, and
one orchestration-layer truncation. Implementation is admitted only if compact
typed errors, exact semantic results, explicit activation, and current
freshness remain unchanged, the source-owned instruction is at most 220
characters, and fresh-client metadata shows a material reduction without a new
public tool or hook.
