## Context

The correctness/freshness repairs at schema 3 make Serena Light safe for current Python and JavaScript/TypeScript semantic reads, but live connector measurements show that presentation overhead is still material: diagnostics and overview repeat engine/generation/runtime facts that do not help an Agent consume a successful result, overview depth pruning is reported as if data were lost, and diagnostics apply their budget before the final envelope is constructed. The MCP initialize response also has no instructions, so every client must rediscover workspace binding and efficient call patterns.

Official Serena at pinned reference commit `9a9d07e83d8c1cba3458992707f440c624446c6d` implements `ListDirTool`, `FindFileTool`, and `SearchForPatternTool`, but its Codex context excludes directory/file discovery and its Claude Code context excludes all three because host tools already own that surface. This change adopts the same agent-facing boundary rather than copying capability merely because it exists upstream.

The constraints are:

- semantic results and freshness evidence must not become weaker to save tokens;
- current tool names and query controls remain stable;
- status and operational errors remain the owners of runtime truth;
- schema changes must use build-identity rollover rather than mutating a live daemon contract;
- the work must remain a focused presentation change, not a new proxy, hook, watcher, or generic LSP abstraction;
- the accepted contract has `maximum_production_lines=null`; LOC is reported but is not a stop gate.

## Goals / Non-Goals

**Goals:**

- Put short, actionable use instructions in both MCP initialize surfaces.
- Make common successful overview, reference, symbol, and diagnostics calls compact by default without losing semantic entities, ranges, bodies, hashes, freshness, or incomplete-coverage truth.
- Make every advertised answer budget measure the exact connector-visible JSON text.
- Preserve typed, actionable failures while removing operational metadata from deterministic misses and input errors where it has no explanatory value.
- Roll the breaking public contract to schema 4 and verify fresh Codex/Claude/CC Agent clients.

**Non-Goals:**

- File enumeration, filename lookup, text search, dynamic-import discovery, or revival of `add-lexical-discovery`.
- Diagnostic hooks, automatic post-edit notification/context injection, or a new public notify tool.
- RTK integration or shell-command rewriting.
- Daemon, lease, freshness, adapter, trust, lifecycle, or guarded-edit redesign.
- A verbose/compact flag, a second response schema, tool renames, a raw LSP tunnel, or a new summary/status tool.
- A third language, UI, JetBrains integration, memories, telemetry, or upstream synchronization.

## Decisions

### 1. One canonical presenter owns client-visible JSON

All affected tool results will cross one presentation boundary that builds the complete response object, serializes it as canonical minified UTF-8 JSON (`ensure_ascii=false`, deterministic keys/order, compact separators), and installs that exact string as `CallToolResult.content[0].text`. `structuredContent` will represent the same JSON value. Budgeting therefore happens after workspace, file grouping, coverage, omission counts, and error fields are present.

The presenter will remove only complete trailing atomic records or overview subtrees and will reserialize after each deterministic reduction. It will never slice a string, range, source body, diagnostic message, snippet, path, or JSON token. Optional error evidence will be bounded in a fixed priority order while the typed code, message, phase/retry facts required by that error, and truthful candidate omitted count remain. For deterministic correction errors only, query echoes and workspace are optional and may disappear whole when the caller's budget cannot contain them; their absence is not a claim that no workspace was bound.

Alternative considered: lower `max_answer_chars` inside each existing tool. Rejected because it truncates useful content earlier while leaving duplicated outer metadata untouched and does not constrain the actual MCP payload.

### 2. Successful data is compact; operational truth stays diagnostic

Navigation and diagnostics success will use the same outer form:

```json
{"ok":true,"data":{"workspace":"/abs/root","files":[],"omitted":0}}
```

Paths, hashes, language/read-only identity, and advisory authority appear once at the narrowest shared owner. Adapter phase, engine version, interpreter, configured-program detail, generations, request echoes, and query plans stay out of success. They remain available in `get_runtime_status` and in operational errors when they explain how to recover.

Deterministic `INVALID_INPUT`, `INVALID_PATH`, and `SYMBOL_NOT_FOUND` failures will keep a typed code, concise message, workspace when bound, and bounded field/candidate details, but omit adapter/generation/configuration facts. `AMBIGUOUS_SYMBOL` keeps bounded candidates. `NOT_READY`, `BUSY`, `COOLDOWN`, `TIMED_OUT`, `SCOPE_INCOMPATIBLE`, and `UNCERTAIN` retain rich phase, retry, adapter, and generation evidence.

Alternative considered: keep all failures rich for uniformity. Rejected because deterministic misses currently repeat facts that cannot change the caller's next action. Operational failures remain deliberately rich.

### 3. Overview defaults express requested scope, not loss

`get_symbols_overview` will default to `max_depth=0`. Root symbols remain complete across kinds. If a caller explicitly requests descendants, descendant `variable` and `constant` nodes are suppressed by default because language servers commonly expose method locals and implementation fields as a large flat list. A caller can request those kinds explicitly with `include_kinds`; existing post-order ancestor retention and `exclude_kinds` precedence remain.

`data.omitted` will mean results removed by a public result/budget bound or by an upstream semantic cap. Descendants outside caller-selected depth, explicit kind-filter removals, and the default descendant-noise policy are selection semantics and will not increment it. This prevents a depth-0 overview from falsely looking truncated.

Alternative considered: replace the tree with an official-Serena-style kind-grouped shape. Rejected for now because it would change more schema and implementation than needed; the current compact tree is usable once its defaults and omission meaning are corrected.

### 4. References report only use sites and actionable coverage

The LSP reference request will exclude the declaration. `find_declaration` already owns declaration lookup, and returning it again makes the common “who uses this?” answer noisier. Snippets remain opt-in through the existing `max_snippet_chars`, whose default stays zero.

Coverage is still mandatory because an empty semantic reference set is not repository-wide proof when native configuration excludes trusted files. Complete coverage becomes `{"complete":true}`. Incomplete coverage becomes a bounded object with `complete=false`, total `uncovered_files`, a deterministic path/reason sample, and `omitted`. Full configured/trust counts and digests remain in runtime status.

Alternative considered: remove coverage entirely. Rejected because that would improve payload size by weakening the meaning of an empty reference result.

### 5. Diagnostics expose findings, not engine internals

Successful diagnostics will be grouped by file. Each record contains severity, compact decoded-text range, message, and optional `symbol`, `source`, and `code`. An empty diagnostics list for the named file is the clean state; timeout, stale, and not-ready are typed failures and can never become an empty success. TypeScript file groups carry `authority="advisory"` once. Python success omits Pyright/interpreter metadata; both languages retain engine/version/interpreter/native-authority facts in runtime status and operational failures.

Alternative considered: keep generation/engine facts on every successful diagnostic response. Rejected because freshness is already a server admission invariant; repeating the proof does not help consume a valid finding. TypeScript advisory authority is retained because it changes interpretation.

### 6. Initialize instructions are static and byte-identical

One source-owned instruction constant, kept concise enough for every session, will be passed to both the outer stdio `Server` and inner daemon `FastMCP`. It will state:

- semantic support is Python and JavaScript/TypeScript only;
- startup cwd auto-binds, shell `cd` does not rebind, and cross-root work requires absolute `activate_workspace`;
- path/directory scope should be preferred when known;
- overview starts at depth 0 and reference snippets are opt-in;
- diagnostics are explicit/on-demand after a meaningful edit group;
- status is for debugging/build/readiness, not a routine preflight;
- host shell/file tools own lexical file and text operations.

No hook or public `get_instructions` function is added. Byte identity is tested so proxy and direct-daemon clients cannot learn different rules.

### 7. Schema 4 uses existing build rollover

The public schema/tool-description version advances from 3 to 4. Since public schema version participates in `build_identity`, new connectors start or join the schema-4 build and never attach to schema 3. Existing holders drain normally; no compatibility flag or in-place discovery rewrite is introduced. The planning-only `add-lexical-discovery` change must rebase its reserved schema version before it can be revived, and `improve-warm-runtime-reuse` remains deferred behind that separate decision.

### 8. Delegation evaluates delivery without duplicate implementations

Implementation will use `agent-routing` only where bounded parallel ownership helps. The lead keeps schema, integration, and final acceptance. One Claude worker and one Codex worker may receive non-overlapping, mechanically verifiable lanes (for example overview/instructions versus diagnostics/presentation), with equivalent brief quality and permissions where practical. Evidence will record completion, verifier pass, lead corrections, wall time, scope control, and communication friction. Because the tasks differ, results inform routing priors but are not presented as a matched model ranking. No agent independently reimplements the entire change.

## Risks / Trade-offs

- **[A compact success hides evidence needed to interpret it]** → Keep TypeScript advisory authority and incomplete reference coverage at the file/query owner; retain full operational truth in status and typed failures.
- **[A new default hides a symbol an Agent expected]** → Hide only descendant variables/constants, never root symbols; explicit kind filters recover them; acceptance compares semantic fixtures before and after.
- **[Exact budgeting recurses into oversized error generation]** → Define a fixed minimal error form and deterministic evidence-pruning order, and test the 512-character boundary through the real stdio connector.
- **[Outer and inner MCP libraries serialize differently]** → Build canonical text once at the presentation boundary and black-box both surfaces; do not rely on default pretty serialization.
- **[Schema-3 and schema-4 daemons are confused]** → Reuse build identity isolation and verify concurrent old/new leases plus zero-holder retirement.
- **[Routing observations are mistaken for a benchmark]** → Record task-relative evidence only and avoid a model ranking unless future work uses matched tasks.
- **[The change grows into lexical discovery or hooks]** → Keep those changes explicitly deferred and fail review if their tools, sidecars, or event surfaces appear in the diff.

## Migration Plan

1. Capture red connector-visible fixtures for current overview omission, diagnostics budget overflow, reference declaration/coverage verbosity, and missing initialization instructions.
2. Implement the schema-4 presentation and instruction changes behind the new build identity; do not mutate schema-3 discovery.
3. Run focused unit and real connector tests, then full lint/type/test/provenance/OpenSpec gates and Python/TypeScript repository smokes.
4. Start fresh Codex and Claude/CC Agent clients, verify the byte-identical instructions and schema-4 tool list, and confirm an existing schema-3 holder remains isolated until release.
5. Update compatibility, README, client-registration, roadmap, and acceptance evidence. Rebase the deferred lexical-discovery plan's future schema reservation without implementing it.
6. If acceptance fails, stop schema-4 clients and restore the prior source revision; schema-3 holders and their daemon remain independently discoverable.

## Open Questions

None. Any later request for lexical discovery, automatic diagnostics injection, RTK, or a new status surface requires its own owner and decision.
