## Context

Serena Light already converts LSP coordinates through an immutable `FileSnapshot` and `PositionMapper`, but each tool currently renders public positions independently. Navigation, references, declarations, and diagnostics add one to line and column values, while a raw reference fallback can expose the original LSP character next to an adjusted line. This creates a mixed coordinate contract even though the underlying mapper is lossless across UTF-8/16/32, BOM, CRLF, and non-BMP text.

The pinned Pyright server may describe a module assignment symbol with an identifier-only document-symbol range. The pinned TypeScript server also reports identifier-only destructured bindings and identifier-start plain bindings whose ranges omit `export`/`declare` and `const`/`let`/`var`. `find_symbol(include_body=true)` would otherwise faithfully slice an incomplete range rather than the complete assignment statement. Reference results also faithfully report only the active server's semantic answers but do not tell the caller which trusted files were in that server's configured program.

The affected stakeholders are Codex, Claude Code, and cc-plugin Agents using the shared daemon across sessions and workspaces. This change must preserve lease reuse, workspace isolation, freshness generations, capability/readiness gates, guarded-edit hashes, external-root read-only rules, and rich typed failures. It repairs semantics before the separately planned success-schema compaction.

## Goals / Non-Goals

**Goals:**

- Establish one public coordinate contract: 0-based decoded-text lines and 0-based Unicode code-point columns, derived from the exact verified snapshot.
- Ensure every existing text and byte offset describes the same snapshot and boundary as its public line/column pair.
- Return a complete Python or TypeScript/JavaScript assignment statement when a caller explicitly requests the body of a supported module variable or constant.
- Make the coverage of a successful semantic reference query explicit, bounded, and reproducible from current configured-program evidence.
- Prove the behavior at the real connector/MCP serialization boundary, including multi-session reuse, workspace switching, cold readiness, and Unicode fixtures.

**Non-Goals:**

- Compacting success envelopes, grouping results by file, changing kind encoding, or enforcing a final serialized response budget; those belong to `compact-success-schema`.
- Combining `rg` or another lexical search with semantic references, creating a residual language-server program, or claiming coverage outside the active configured program.
- Adding languages, raw LSP RPC access, broad editing, UI, memories, telemetry, or upstream synchronization.
- Changing the editing coordinate/hash contract, daemon lifecycle, trust roots, or readiness state machine.

## Decisions

### 1. One owned public-position renderer

Introduce one Serena-Light-owned renderer at the tools/LSP boundary. It accepts a verified `FileSnapshot`, negotiated `PositionEncoding`, and LSP range, maps both endpoints through `PositionMapper`, and emits:

- `line`: 0-based decoded-text line;
- `column`: 0-based Unicode code-point index within that decoded line;
- existing `text_offset`: decoded Python-string boundary; and
- existing `byte_offset`: physical UTF-8 file boundary, including any BOM offset.

Navigation, global-symbol, reference, declaration/implementation, and diagnostic success paths must use this renderer. There is no arithmetic adjustment in individual tools. A location lacking an authorized current snapshot is either represented with an explicit unmapped/read-only-external form whose coordinate basis is named, or rejected with the existing typed failure; a mixed-base location is forbidden.

Alternative rejected: documenting the existing 1-based output. Agent prompts, tool descriptions, diagnostic inputs, and Serena conventions already use 0-based positions, and the raw-reference path is internally inconsistent even under a 1-based interpretation.

### 2. Preserve snapshot and concurrency invariants

The workspace freshness preflight, workspace lock, adapter executor, document generation, and snapshot verification remain the owners of concurrency. Rendering receives the exact snapshot already validated for the semantic response and performs no second filesystem read. If freshness or a generation transition invalidates that snapshot, the operation returns its existing typed stale/not-ready/timed-out failure instead of publishing coordinates from two generations.

Cross-file declaration, implementation, and reference locations use a bounded
two-request transaction on one adapter executor. The first response discovers
at most 64 unique target URIs across workspace and external locations; a
deterministic larger set returns non-retryable `UNSUPPORTED` before any target
snapshot is read or opened. Serena Light captures and opens the authorized target
snapshots, then repeats the identical semantic request. Success requires the
canonicalized location multiset and the exact adapter runtime token, complete
raw/derived capability identity, encoding, and
trust/program/document/index generations to remain unchanged; rendering uses
only the snapshots retained for that replay. A mismatch returns retryable
`NOT_READY` and is never remapped through bytes first read after the response.
An allowlisted read-only external location that cannot participate in this
document-owned replay may retain its path only with the explicit raw LSP basis;
body/info and reference-snippet requests fail typed rather than claim a
verified decoded-text mapping.

Multi-session leases may share the same daemon and adapter but never share an unqualified path: workspace identity plus relative path and generation remain part of snapshot selection. Read-only external definitions remain navigable only through their existing allowlisted roots.

Alternative rejected: reading the file again inside each renderer, because it opens a time-of-check/time-of-use split between the LSP result, body/hash, and public offsets.

### 3. Adapter-owned assignment range recovery

For Python variable/constant document symbols whose server range is identifier-only, the Pyright adapter performs syntax-aware recovery against the same snapshot using Python's standard `ast` positions. It selects the unique enclosing module-executed `Assign` or `AnnAssign` whose target contains the server selection range. Module-executed control-flow suites (`if`, `try`, `with`, loops, and `match`) remain eligible, while function and class scopes stay excluded. The recovered full statement becomes the symbol/body range; the original identifier remains the selection range and name-path anchor. Decorated definitions and non-assignment symbols are unchanged. Ambiguous or invalid recovery fails closed to the original semantic range unless `include_body=true`, in which case an incomplete candidate must not be advertised as a complete body and returns a typed unsupported/incomplete-range failure.

The TypeScript/JavaScript empirical fixture demonstrates both identifier-only destructured bindings and identifier-start plain bindings. The adapter requests the pinned server's `textDocument/selectionRange` syntax ancestry for only those candidates, requires the exact selected snapshot slice to equal the semantic candidate name, selects one containing top-level variable statement with `export`/`declare` plus `const`/`let`/`var`, preserves a terminal semicolon reported by that syntax ancestry, and preserves the server identifier selection. Plain or destructured candidates whose name and selected identifier disagree fail closed before statement recovery. The read and guarded-edit bridges use the same recovery and fail-closed filter. It does not implement a second JavaScript parser or introduce a generic cross-language range heuristic.

Alternatives rejected: line-based expansion can absorb comments or adjacent statements; treating the identifier slice as a complete body violates the tool contract; parsing all languages behind one abstraction invents variation without a third consumer.

### 4. Reference coverage is semantic-program evidence

Every successful reference result includes one bounded `coverage` object derived from the adapter's current configured-program projection and trust inventory generation. It records the language/adapter, project or scope kind, configured-program file count and digest, trusted supported-language inventory count and digest, uncovered trusted file count, and a deterministically bounded uncovered-path sample with total and omitted count. Exact field names are fixed in the delta spec and compatibility record.

An empty reference list is valid only together with this coverage evidence. Files outside the configured program are not searched and are not implied covered. Existing `SCOPE_INCOMPATIBLE`, `NOT_READY`, timeout, cooldown, and capability failures remain typed errors and cannot become empty success. Coverage is computed from already maintained runtime projections, not from a new scan or background watcher.

Alternatives rejected: lexical fallback changes the meaning of the tool; a second inferred program changes language-server authority and resource use; returning every uncovered path is unbounded and wastes tokens.

### 5. Compatibility is an explicit semantic revision

The compatibility record gains a semantic-contract revision describing the coordinate migration and reference coverage. The public tool/schema version is bumped so build identity rolls to a new daemon slot; existing leased daemons may drain under the established rollover contract. This repair preserves the current success-envelope layout and the current detailed position fields, allowing the following compact change to measure and own the response-shape break independently.

Clients must stop subtracting one from result positions. No dual-mode or `one_based` option is provided. Errors remain rich and stable.

### 6. Acceptance targets the real public boundary

Unit tests cover mapping and AST recovery, but acceptance must invoke the real connector and inspect the actual MCP `CallToolResult`, not merely an internal Python dictionary. Required fixtures include astral Unicode before a symbol/reference/diagnostic, CRLF, UTF-8 BOM, module assignments, a native config that excludes trusted test files, and both Python and TypeScript repositories. Tests also cover two client leases sharing one workspace, a client switching workspaces, cold/not-ready behavior, and a stale-hash edit after a semantic read to prove the repair does not weaken editing.

Fresh-client acceptance additionally exposed a TypeScript diagnostics race: unchanged reads manufactured document generations, and an asynchronous publication could lose its correlation after document-symbol readiness. Diagnostics therefore reuse the exact open snapshot/generation when bytes are unchanged and retain a diagnostics-specific publication owner through readiness. A bounded timeout releases only the synchronous waiter; the current owner remains eligible for a late matching publication. Real content changes still advance generation and cannot reuse an older clean publication.

Document and diagnostic ownership is also language-server-process-owned. A
transport loss immediately drops open-document, document-version, cached-text,
and push-publication state. A replacement process must receive `didOpen`
before diagnostics probing; reuse additionally requires the current runtime
token. Timeout cancellation releases the synchronous waiter but retains the
current document's publication owner, so a late matching publication is cached
for a retry. Cancellation re-samples after that atomic handoff so a publication
that already won the race is not reported as a false timeout; a newer document
target cannot be satisfied or overwritten by the older publication.

Changed-document sequencing depends on the fixed engine's diagnostic wire
contract. When publications carry an integer document version, publication
ownership is installed before `didChange` is sent and an unversioned
publication is rejected. An inline or fast matching publication may consume
the owner during notification, so the caller must not reinstall it afterward.
When an engine publishes no document version, a normal `didChange` cannot prove
whether a delayed publication belongs to the old or new text. Serena Light
therefore forgets the old owner before sending `didClose`, then waits for a
bounded request/response barrier on the same connection. The fixed server
writes its close-generated publication before that later response, and the
client reader dispatches messages serially, so the barrier cannot release until
the close publication has been processed and dropped without an owner. Only
then does Serena Light install the new generation owner and send `didOpen` with
the exact full text. Barrier timeout or transport failure installs no new owner
and sends no reopen. Reopen notification failure uses compare-and-remove
cleanup for only the target that failed delivery. These orders are required
because notification write return alone provides no server acknowledgement or
causal generation evidence.

The barrier obligation also applies before retaining an owner for an unchanged
cached diagnostics snapshot; reuse is an ownership installation even when it
does not send `didOpen`. A watcher-created temporary open/close MUST be skipped
when the current process already owns that URI. If a failed reconciliation ever
leaves an undrained marker and local-open ownership on the same URI, draining
first disowns the local buffer and its publication targets, preserves the marker
across barrier failure, and only a successful same-connection barrier permits a
later reopen or cached-owner installation. This keeps the invariant independent
of whether the next operation enters through open, freshness, or diagnostics
reuse.

## Risks / Trade-offs

- [Python AST end positions can differ from language-server normalization] → constrain recovery to unique module assignment statements, preserve the server selection range, and test multiline/annotated/chained cases explicitly.
- [External locations may lack an immediately cached snapshot] → use the existing trusted read-only snapshot path or return an explicitly typed unmapped form; never synthesize mixed-base coordinates.
- [A target changes after the first semantic response] → bind the discovered
  target snapshots and replay once; accept only a stable response/generation
  set, otherwise return retryable `NOT_READY` without an unbounded retry.
- [A diagnostics request races a transport restart] → make all document and
  publication caches process-owned and require a fresh `didOpen` on the new
  runtime token.
- [Coverage digests can become stale during a file change] → obtain coverage from the same freshness/generation snapshot as reference dispatch and fail with the existing typed generation error on mismatch.
- [The schema/build version bump starts a new daemon] → rely on versioned build slots and lease drain; do not kill canonical Serena or an old Serena Light daemon with holders.
- [The repair can be confused with token optimization] → retain the current envelope and detailed fields in this change and gate `compact-success-schema` on this change's acceptance and archive.

## Migration Plan

1. Add failing unit and black-box tests that capture current mixed-base positions, incomplete assignment bodies, and undisclosed reference coverage.
2. Implement the shared renderer and route every affected semantic/diagnostic success path through it.
3. Add Python assignment recovery and run the TypeScript/JavaScript range probe; implement TypeScript recovery only if the probe fails.
4. Add bounded reference coverage from the current runtime projection.
5. Update tool descriptions, compatibility data, schema/build identity, and acceptance evidence.
6. Start fresh Codex, Claude Code, and CC Agent clients against the new build slot; verify old lease drain and all real-boundary scenarios.
7. Archive this change only after strict OpenSpec and full acceptance pass; then begin `compact-success-schema`.

Rollback is a code/build-slot rollback to the previous Serena Light build. It restores the prior coordinate contract and therefore requires restoring matching client expectations; it does not terminate canonical Serena or delete leased historical build slots.

## Resolved Questions

- The pinned TypeScript server omits declaration syntax for accepted `.js`, `.mjs`, `.ts`, and `.tsx` top-level variable fixtures, so the adapter-owned selection-range recovery is required.
- An incomplete assignment body that cannot be uniquely recovered returns typed `UNSUPPORTED` with `reason=incomplete_assignment_range`; ordinary symbol lookup may retain the accurately labelled server range.
