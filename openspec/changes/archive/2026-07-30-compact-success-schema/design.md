## Context

The navigation tools currently build a bounded inner `data` fragment and then wrap it with `SuccessEnvelope`, which repeats workspace, adapter, generation, configured-program, path, hash, and truncation metadata. FastMCP subsequently serializes returned dictionaries with indented JSON and also exposes structured output. As a result, `max_answer_chars` is not a bound on the text a Codex, Claude Code, or CC Agent client actually receives: a nominal 50,000-character overview has produced 99,918 characters in `CallToolResult.content[0].text`.

The correctness change `fix-position-and-coverage-contract` owns 0-based decoded-text coordinates, complete assignment bodies, and honest reference coverage. This change may start only after that change is accepted, synced, and archived. The shared daemon, connector leases, workspace identities, trust boundaries, readiness/cooldown states, and guarded-edit semantics are already correct owners and must remain unchanged.

## Goals / Non-Goals

**Goals:**

- Make semantic navigation success compact by default without a mode flag.
- Remove repeated operational metadata while retaining the minimum authority needed to interpret each answer.
- Group result records by file and use stable compact ranges and kind strings.
- Apply match and answer limits deterministically after semantic deduplication.
- Guarantee the success-text limit against the exact JSON placed in the real MCP `CallToolResult.content` block.
- Preserve structured content for clients that consume it and retain rich typed failures.

**Non-Goals:**

- Compacting runtime status, diagnostics, guarded editing, workspace lifecycle, daemon control-plane responses, or error envelopes.
- Changing semantic readiness, freshness, program coverage, trust, concurrency, proxy, lease, or edit/hash behavior.
- Truncating source bodies or hover text inside a successful record, hiding symbol kinds by default, or adding lexical fallbacks.
- Adding a `compact` parameter, legacy dual schema, arbitrary LSP tunnel, language, UI, memories, or per-session language server.

## Decisions

### 1. One compact success envelope for navigation only

Successful `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, `find_declaration`, and `find_implementations` calls return exactly two top-level fields:

```json
{"ok":true,"data":{"workspace":"/absolute/root","files":[],"omitted":0}}
```

`data.workspace` is the physical active workspace root as one string. `data.files` is ordered by normalized path. `data.omitted` is one non-negative integer counting atomic result records excluded by `max_matches`, overview depth or kind filters, upstream semantic bounding, and/or the final answer budget. A genuine empty semantic result has `files=[]` and `omitted=0`; readiness or capability failures remain errors.

Each file group contains `path` once and a tool-specific records array. It includes `sha256` once only when a returned record contains source body text. It includes `language` only when a response combines language families or the path/identifier cannot identify the family. A trusted external file's identity is its own authoritative absolute `path` plus an optional `read_only=true` marker once per file group; the schema introduces no second non-forgeable external identifier and no alternate trust owner. Adapter phase, runtime generation, configured-program detail, query echoes, URI, repeated path, and repeated hash are omitted. Reference `coverage` is retained once at `data.coverage` because it changes the meaning of an empty answer.

Alternative rejected: a public `compact=true` option would double the internal contract and let agents continue paying for the verbose default. Applying the generic compact envelope to diagnostics/edit/status would remove decision-critical generation, authority, retry, and outcome evidence.

### 2. Tool-specific compact records

- Overview file groups use `symbols`; each node contains only `name`, lowercase `kind`, and `children` when non-empty. Overview omits ranges, selection ranges, detail, name paths, per-node hashes, and boolean child flags. Optional `include_kinds` and `exclude_kinds` accept stable lowercase kind strings so an agent can narrow a large overview without changing the default complete kind set. Filtering is post-order: a non-matching ancestor is retained only as the structural path to a retained descendant, `exclude_kinds` wins when one node appears in both filters, and every node removed solely by filtering contributes to `omitted`. Intrinsic-match provenance remains presentation-internal so final budget pruning also removes and counts a structural ancestor when its last matching descendant disappears.
- `find_symbol` file groups use `symbols`; each record contains `name_path`, lowercase `kind`, and `range` as `[[start_line,start_column],[end_line,end_column]]`, plus `body` or `info` only when requested. The whole-file `sha256` is on the file group only when body is present.
- Reference file groups use `references`; each record contains compact `range`, optional containing `symbol`, and optional bounded `snippet`. External/read-only identity is on the file group. Correctness-change coverage remains once at `data.coverage`.
- Declaration and implementation file groups use `targets`; each target contains compact `range` and only available `name_path`, lowercase `kind`, `body`, or semantic `info.detail`. Response-owned target document symbols supply those optional fields from the same verified snapshot when requested. Implementation kind filters count every filtered target in `omitted`; an unknown kind cannot satisfy a positive include filter and cannot be rejected by an exclude filter. `find_declaration` gains the same `max_answer_chars` input as the other bounded navigation tools.

Kinds use a fixed lower-snake-case mapping of known LSP `SymbolKind` values; an unknown numeric kind is rendered as `unknown:<integer>` rather than silently dropped. Range arrays use the repaired 0-based decoded-text/Unicode-column contract. Navigation success no longer exposes text or byte offsets; guarded editing keeps its internal exact mapper and file hash.

A reference or declaration/implementation record backed by an exact response-owned snapshot uses compact `range`. A read-only external target that lacks an exact response-owned snapshot for that response instead carries `raw_range` (the same `[[start_line,start_column],[end_line,end_column]]` shape) plus a `position_basis` string naming the raw LSP coordinate system it was reported in; the two fields are mutually exclusive per record, and a raw-basis record MUST NOT be relabelled as decoded-text or silently blended with a mapped range. A malformed workspace containment tree may lose container metadata but retains its response-owned mapper; a workspace location that cannot map returns retryable `NOT_READY` rather than raw coordinates. `find_symbol` and overview records only ever address trusted workspace-owned snapshots, so they carry `range` only.

Alternative rejected: a universal record containing every optional field recreates the current metadata bloat. Overview start lines are also omitted by default because a follow-up `find_symbol` provides the precise location when needed.

### 3. Deterministic match selection and grouping

All flat-result tools deduplicate and sort atomic results before limiting or grouping. The stable order is normalized path, start line, start column, name path/container, kind, then a deterministic final tie-breaker. `find_symbol.max_matches` defaults to 20, must be between 1 and 100, and applies to file, directory, and global scopes after semantic filtering/deduplication. Adapter candidate fan-out remains an internal safety setting and disappears from the public schema.

Overview preserves the language-server sibling order within a document and uses stable preorder for budgeting. Depth exclusions, kind-filter exclusions, upstream truncation, and final-budget pruning contribute disjoint counts to the single `omitted` total. A final-budget removal removes the node's whole remaining subtree; kind filtering is post-order so a retained descendant keeps the minimum ancestor path and is never orphaned. Reference and target records are atomic. Empty file groups are removed.

Alternative rejected: truncating while adapters race would make repeated calls unstable, and cutting serialized strings can produce invalid JSON or partial source bodies.

### 4. Budget the exact MCP text, not an inner object

A Serena-Light-owned navigation renderer constructs the complete compact dictionary, serializes it once as UTF-8-preserving canonical JSON (`ensure_ascii=false`, no insignificant whitespace, fixed field insertion order), and uses that exact string as the sole text content block of a returned MCP `CallToolResult`. The same dictionary is placed in `structuredContent`, preserving typed clients without asking FastMCP to generate a second indented text representation.

`max_answer_chars` defaults to 12,000 and accepts a bounded range of 512 through 50,000. The renderer first applies `max_matches`, then repeatedly removes trailing whole atomic records in stable order, updates `omitted`, removes empty file groups, and reserializes until `len(CallToolResult.content[0].text)` is within the requested bound. No body, hover text, snippet, path, or record is cut mid-string. If the first eligible stable record cannot fit, the call returns bounded `INVALID_INPUT` with `field=max_answer_chars`, the characters required for that reachable one-record prefix, and the original workspace/adapter/generation authority available at the presentation boundary; multi-adapter global errors carry a deterministic bounded `details.authorities` list instead of inventing one top-level owner. It does not return a misleading empty success or advertise a smaller later record that stable-prefix pruning cannot reach.

The large private internal success budget is not an error budget. Candidate-bearing path-scoped errors, especially `AMBIGUOUS_SYMBOL`, retain the caller's public budget for their bounded evidence and report truthful truncation/omission counts, preventing compact success plumbing from reintroducing an unbounded agent response.

Errors continue through the current rich error renderer. Tests compare the predicted text byte-for-byte with the connector-observed `CallToolResult.content[0].text`, and separately assert `structuredContent` is the same JSON value.

Alternatives rejected: budgeting `SuccessEnvelope.to_json()` is insufficient because it is not the FastMCP client-visible representation; lowering the numeric default without removing duplication discards useful results sooner; disabling structured output would break existing typed clients unnecessarily.

### 5. Compatibility and build rollover are explicit

This is an internal breaking schema revision with no compatibility shim. The compatibility document describes old-to-new field mappings and examples for all five tools. `PUBLIC_TOOL_SCHEMA_VERSION` is incremented, which changes build identity and starts a versioned daemon slot. Existing clients and old build leases drain normally; fresh clients bind only to the matching schema/build. Canonical Serena is not renamed or stopped.

The response builder, grouping, kind mapping, and exact MCP renderer are new Serena-Light-owned code. No source needs to be copied from official Serena; if implementation later copies any mechanism, census/provenance manifests and exact hashes must be updated before acceptance.

## Risks / Trade-offs

- [Removing success generations makes stale reasoning harder] → navigation freshness remains mandatory; generation details stay available in `get_runtime_status` and all typed errors, while body results retain the current file hash.
- [A compact conditional field can surprise parsers] → compatibility examples define every optional field and tests cover absence/presence; core keys remain fixed.
- [MCP SDK serialization behavior may change] → construct `CallToolResult` explicitly and assert exact connector-observed text under the pinned SDK; a dependency-lock change also changes build identity and reruns acceptance.
- [Tree pruning can miscount descendants] → use one preorder accounting function and golden nested-tree tests with exact omitted counts.
- [A very large exact body cannot fit] → return `INVALID_INPUT` with the measured minimum rather than truncate source or lie with empty success.
- [Structured content duplicates wire bytes even if agent text is compact] → preserve it for compatibility in this change and measure end-to-end client token use; remove it only in a separately approved contract if clients prove it unnecessary.

## Migration Plan

1. Verify `fix-position-and-coverage-contract` is accepted, synced, and archived; otherwise stop.
2. Capture fixed-fixture real MCP baselines for all five tools and the original four-arm ablation snapshot.
3. Add compact DTO, grouping, kind, selection, and exact renderer tests before replacing success output.
4. Implement one navigation tool at a time, with golden JSON and exact MCP content/structured-content checks.
5. Update schemas, compatibility examples, build identity, README, and client-registration documentation.
6. Start a new build daemon and fresh Codex, Claude Code, and CC Agent clients; verify old holders and workspaces remain isolated until normal retirement.
7. Run the full acceptance suite and then rerun the same ablation tasks, comparing correctness first and client-visible tokens/calls/wall time second.

Rollback selects the prior build/schema registration for fresh clients while existing new-build holders drain. Because this is a breaking shape change, code and client expectations must roll back together; no daemon is killed by name and canonical Serena is untouched.

## Open Questions

None. The schema, limits, pruning order, compatibility boundary, and prerequisite are fixed by this change.
