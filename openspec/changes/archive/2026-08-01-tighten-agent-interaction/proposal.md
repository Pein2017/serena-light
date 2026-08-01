## Why

Serena Light now returns current, semantically correct Python and JavaScript/TypeScript results, but several common successful calls still spend most of their agent-visible tokens on repeated runtime authority, presentation metadata, and misleading omission counts. This change closes that interaction gap without broadening the product: keep semantic quality and freshness, make the default success path concise, and teach clients how to use the existing tools efficiently.

## What Changes

- **BREAKING** Advance the public tool/schema version from 3 to 4 so fresh clients receive compact success contracts and old schema-3 holders drain through the existing build rollover instead of observing an in-place schema change.
- Publish one short, byte-identical initialization instruction block from both the outer stdio connector and inner daemon. It explains workspace binding, explicit cross-root activation, semantic versus lexical tool ownership, efficient query scoping, on-demand diagnostics, and status-as-debug guidance; it does not add a hook or another public tool.
- Make symbol overview start at depth 0, suppress noisy descendant variables/constants unless the caller explicitly asks for those kinds, and stop counting caller-selected depth or kind filtering as truncation. Preserve the existing compact tree shape and explicit controls.
- Make successful file and symbol diagnostics use the same compact, file-grouped envelope as navigation. Keep TypeScript's advisory authority once per affected file, but move engine, interpreter, generation, and configured-program detail to runtime status and operational errors.
- Enforce `max_answer_chars` against the exact canonical JSON placed in MCP `content[0].text`, with `structuredContent` representing the same value. Apply this to diagnostics as well as navigation and render JSON without pretty-print whitespace.
- Exclude the declaration from reference results and collapse healthy complete coverage to a single `complete=true` fact. Return bounded uncovered evidence only when coverage is incomplete; retain full projection evidence in runtime status and rich operational errors.
- Compact deterministic query/input errors where runtime authority does not explain the failure, while preserving rich typed evidence for ambiguity and operational failures such as not-ready, incompatible scope, busy, cooldown, timeout, or uncertain state.
- Tighten public tool and field descriptions around the few agent decisions that matter, without renaming tools, merging tools, or exposing a raw LSP tunnel.
- Keep `add-lexical-discovery` and `improve-warm-runtime-reuse` independent and deferred. Official Serena implements lexical file/search tools, but its Codex and Claude Code contexts intentionally hide some or all of them when host shell/file tools already own that work; this change follows that agent-facing boundary.

Explicit non-goals are lexical file enumeration or text search, diagnostic hooks or automatic context injection, RTK integration, daemon/freshness/lifecycle redesign, editing changes, a new status/summary tool, support for another language, and a compatibility flag for verbose schema-3 success.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-navigation`: Change overview defaults and omission meaning, compact reference coverage and deterministic errors, exclude declarations from references, and make final MCP text budgeting exact.
- `diagnostics-status`: Define compact successful diagnostics, preserve bounded authority where it affects interpretation, and apply the final serialized answer budget.
- `workspace-runtime`: Publish concise initialization instructions through both MCP layers and roll the public schema/build identity safely.

## Impact

- Affected code is expected in the connector/daemon MCP construction, public tool schemas and descriptions, compact renderers, overview and reference presentation, diagnostics presentation, and build/schema identity constants.
- Existing semantic adapters, freshness coordination, snapshot ownership, workspace leases, and guarded editing are not redesigned.
- Fresh Codex, Claude Code, and CC Agent clients are required to observe schema 4. Existing schema-3 clients remain isolated on their old build until their leases drain.
- No external dependency is added. The official Serena checkout at `/data/CoordExp/external/serena` remains read-only reference material, not an upstream runtime dependency.
- Admission requires exact connector-visible response-budget fixtures, real Python and TypeScript query/diagnostics smokes, build rollover evidence, the existing full quality/provenance gates, and a fresh-client instruction/tool-list check. Production LOC remains informational; there is no hard LOC stop.
