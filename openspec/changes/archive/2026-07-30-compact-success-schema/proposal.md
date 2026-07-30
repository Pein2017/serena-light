## Why

Serena Light's semantic answers repeat workspace, adapter, generation, path, hash, and multi-coordinate metadata at several nesting levels, so agents spend far more tokens on operational metadata than on the symbols they requested. After the correctness contract is repaired and archived, navigation success should become compact by default and its size limit must apply to the actual MCP text received by clients.

## What Changes

- Make archived acceptance of `fix-position-and-coverage-contract` a hard prerequisite; do not implement or benchmark this change against mixed coordinate/body/coverage semantics.
- **BREAKING**: Replace navigation success envelopes with `{"ok":true,"data":{...}}`, moving the workspace root into `data` and removing success-only adapter, generation, configured-program, and truncation objects.
- Group matches, references, declarations, and implementations by file so path and optional language/external/hash metadata appear once per file rather than once per item.
- Represent public ranges as `[[start_line,start_column],[end_line,end_column]]` using the repaired 0-based decoded-text contract; omit text/byte offsets from navigation success.
- Make symbol kinds stable lowercase strings and make overview nodes default to only `name`, `kind`, and non-empty `children`.
- Add a real public `max_matches` limit to `find_symbol`, defaulting to 20 and capped at 100; keep candidate fan-out limits internal.
- Enforce `max_answer_chars` on deterministic compact JSON in the actual MCP `CallToolResult.content` text after all fields are present, and report one integer `omitted` count.
- Keep errors rich. Keep runtime status, diagnostics, guarded editing, workspace activation/release, and control-plane results out of this schema compaction because their authority, generation, retry, and lifecycle evidence is operationally necessary.
- Do not add a `compact=true` switch or compatibility shim; Serena Light's internal agent-facing default becomes compact.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-navigation`: Replace repeated navigation success metadata with deterministic file-grouped compact results, bounded matches, compact ranges/kinds, and a true client-visible response limit.

## Impact

This change affects navigation DTOs/rendering, tool schemas and descriptions, FastMCP result conversion at the daemon boundary, answer-budget logic, compatibility/build identity, client fixtures, and ablation evidence. Consumers of navigation success must migrate to the new `data.workspace`, `data.files`, and `data.omitted` layout; errors and non-navigation contracts remain unchanged.

Admission measurements from the repaired-v1 baseline path show the current actual MCP text at roughly 924 characters for an exact symbol without body, 5,407 with body, 2,733 for a global symbol, 20,674 for a default large-file overview, and 12,613 for default references. Setting `max_answer_chars=50_000` produced a 99,918-character overview because the existing bound is applied before FastMCP adds and pretty-serializes the complete response. This change addresses duplicated metadata before lowering or enforcing budgets, so useful bodies are not prematurely discarded.

The work does not add languages, tools, arbitrary LSP RPC, UI, memories, telemetry, broad editing, proxy changes, daemon-per-session processes, or upstream synchronization. It must be deployed as a schema/build-identity rollover, verified with fresh Codex, Claude Code, and CC Agent clients, and measured by rerunning the same ablation tasks only after the final contract is fixed.
