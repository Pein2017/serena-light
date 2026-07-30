## 1. Freeze Baseline and Add Failing Contract Tests

- [x] 1.1 Record the current source commit, dependency-lock digest, public schema/build identity, pinned Pyright and TypeScript engine versions, and actual MCP `CallToolResult.content[0].text` examples for one navigation, reference, and diagnostic query; keep production LOC informational and do not use it as a stop gate.
- [x] 1.2 Add table-driven `PositionMapper` and public-renderer tests for UTF-8/UTF-16/UTF-32 server positions, astral Unicode, CRLF/LF, UTF-8 BOM, empty lines, end-of-line boundaries, and invalid code-unit splits.
- [x] 1.3 Add failing public-contract tests proving navigation, global symbol, reference, declaration/implementation, and diagnostic positions are 0-based decoded-text line/Unicode-column values whose text and byte offsets identify the same snapshot boundary.
- [x] 1.4 Add failing Python fixtures for simple, annotated, multiline, tuple/chained, and ambiguous module assignments, verifying that `include_body=true` never advertises an identifier-only slice as a complete body.
- [x] 1.5 Probe the pinned TypeScript server with accepted `.js`, `.mjs`, `.ts`, and `.tsx` top-level variable fixtures; record whether its symbol ranges already cover complete statements and add a failing recovery test only for demonstrated incomplete cases.
- [x] 1.6 Add failing reference tests for a native project that excludes trusted tests, asserting explicit bounded coverage on non-empty and empty success and typed failures for cold, incompatible, timed-out, cooldown, and unsupported states.

## 2. Centralize Exact Public Position Rendering

- [x] 2.1 Add one Serena-Light-owned public position/range renderer that accepts the exact verified `FileSnapshot` and negotiated encoding and emits 0-based decoded-text line/Unicode-column plus the existing coherent text and physical-byte offsets.
- [x] 2.2 Remove local base arithmetic from document navigation and global-symbol rendering; route overview and file/directory/global `find_symbol` locations through the shared renderer without a second file read.
- [x] 2.3 Route reference, declaration, and implementation locations through the same renderer, including trusted read-only external locations; replace raw fallback with an explicitly named raw-basis representation or a typed failure.
- [x] 2.4 Route diagnostic ranges, containing-symbol overlap, and snippets through the same snapshot mapping while preserving generation, engine, interpreter, and advisory-authority metadata.
- [x] 2.5 Add invariant tests that fail if any tool renderer applies `+1`/`-1`, mixes raw LSP characters with decoded columns, or renders a range from a snapshot/generation different from the returned body, hash, snippet, or diagnostic.

## 3. Recover Complete Assignment Bodies

- [x] 3.1 Implement Pyright-adapter-owned recovery from an identifier-only variable range to one unique enclosing module-level `ast.Assign` or `ast.AnnAssign` statement in the same snapshot, preserving the server identifier as the selection range.
- [x] 3.2 Define and test fail-closed behavior for unsupported, syntax-invalid, or ambiguous assignment recovery: ordinary symbol lookup may retain an accurately labelled semantic identifier range, but `include_body=true` must return a typed incomplete-range failure rather than incomplete success.
- [x] 3.3 If and only if task 1.5 demonstrates a TypeScript/JavaScript gap, implement the smallest adapter-owned syntax-aware recovery for the observed forms and tests; otherwise record the engine evidence and add no recovery abstraction.
- [x] 3.4 Verify complete body text, recovered range, unchanged name path/selection range, and current whole-file SHA-256 through both direct runtime calls and the real connector/MCP boundary.
- [x] 3.5 Bind each TypeScript/JavaScript recovered assignment to the exact selected identifier named by the semantic candidate; fail closed for plain or destructured name/anchor disagreement, and prove body lookup plus guarded editing cannot read or replace a different statement.

## 4. Disclose Semantic Reference Coverage

- [x] 4.1 Build a reference coverage value from the same current freshness/configured-program generation used for dispatch, containing adapter, language, scope kind, configured-program count/digest, trusted-language inventory count/digest, and uncovered count.
- [x] 4.2 Add a fixed-size, deterministically sorted uncovered-path sample with full-set digest, total, and omitted count; reuse maintained runtime projections and do not trigger a new workspace scan.
- [x] 4.3 Attach coverage once per successful reference result, including empty success; prove that it neither repeats per reference nor claims coverage for trusted files outside the configured program.
- [x] 4.4 Add negative tests forbidding lexical `rg` merging, a residual second semantic program, and conversion of not-ready/scope-incompatible/timeout/cooldown/capability failures into empty success.
- [x] 4.5 Verify per-language isolation in a mixed Python/TypeScript workspace: each query reports only its owning adapter's configured program, and an incompatible family does not prevent a healthy family from serving requests.

## 5. Compatibility, Lifecycle, and Documentation

- [x] 5.1 Update public tool descriptions and README/compatibility documentation to state 0-based decoded-text line and Unicode code-point columns, coherent compatibility offsets, complete assignment-body behavior, and the exact reference coverage fields.
- [x] 5.2 Increment the semantic/public schema compatibility revision and `PUBLIC_TOOL_SCHEMA_VERSION`; verify build identity changes and a source/schema race cannot publish a daemon under the wrong build slot.
- [x] 5.3 Exercise two concurrent client leases on the new build and one lease on the prior build: the new connector selects only the matching build, the old daemon retains holders, and zero-holder retirement cannot delete successor discovery state.
- [x] 5.4 Re-run workspace activation/switching across `/data/CoordExp/serena-light`, `/data/CoordExp`, `/data/CoordExp/cc-plugin-codex`, `/data/CoordExp/ms-swift`, and the allowlisted read-only transformers root; verify workspace identity, trust, and read-only behavior are unchanged.
- [x] 5.5 Re-run stale-hash and symlink guarded-edit tests after semantic reads to prove position/range repair does not weaken lexical membership, snapshot hash, no-follow, timeout, or `UNCERTAIN` contracts.
- [x] 5.6 Repair the fresh-client TypeScript diagnostics race: unchanged reads reuse the open snapshot/generation, asynchronous publication ownership survives document-symbol readiness until publish/timeout, and freshness-owned changes do not trigger a duplicate `didChange`.
- [x] 5.7 Bind cross-file semantic locations to response-owned target snapshots through one bounded replay, returning raw-basis external locations or retryable typed failure when exact mapping cannot be proven.
- [x] 5.8 Make document/diagnostics caches process-owned across transport restart, re-sample publication after timeout cancellation, and forbid Python assignment recovery when the server selection is outside the matching AST target.
- [x] 5.9 Bind semantic replay to the exact runtime token and full capability identity, enforce the 64-target bound across workspace and external targets before materialization, and return deterministic overflow as non-retryable `UNSUPPORTED`.
- [x] 5.10 Retain late diagnostics publications for retry, apply TypeScript assignment recovery to guarded editing, include terminal semicolons in complete TypeScript/JavaScript statements, and recover Python assignments in module-executed control-flow suites while excluding nested scopes.
- [x] 5.11 Install changed-document diagnostics ownership before `didChange`, avoid resurrecting an owner consumed by an inline publication, and compare-and-remove only the undelivered target when notification fails.
- [x] 5.12 For engines whose diagnostic publications omit document versions, replace changed open buffers through a causal `didClose`/exact-full-text `didOpen` epoch, disown tracking before close, and prove stale/close publications and notification failures cannot satisfy or leave the new generation, including any later retry or reopen after an undrained close.
- [x] 5.13 Insert a bounded same-connection request/response barrier after unversioned `didClose` and before installing the new owner; bind the barrier obligation to explicit per-URI undrained-close state across timeout, response error, transport uncertainty, LRU eviction, watched-file close, and retry, and prove no close publication can consume the later generation.
- [x] 5.14 Prevent watcher-created temporary close/open from overlapping a locally owned URI, drain any surviving current-process close marker before cached diagnostics owner retention, and prove the ordinary two-file watcher timeout/recreate chain cannot produce a sticky false `CLEAN`.

## 6. Acceptance and Archive Gate

- [x] 6.1 Run the targeted unit/integration suite, full pytest, Ruff, Ty, bootstrap, dependency/source-ownership/provenance checks, and strict OpenSpec validation; record exact commands, environment, and results.
- [x] 6.2 Run real-daemon/connector tests under clean and poisoned-proxy environments, proving loopback bypass, no orphan child, cold/readiness errors, freshness after create/change/delete/config change, and exact Unicode positions.
- [x] 6.3 Start fresh Codex, Claude Code, and CC Agent clients and inspect actual MCP `CallToolResult` content for Python and TypeScript navigation, constant body, references/coverage, declarations, and diagnostics; internal dictionary serialization is not sufficient evidence.
- [x] 6.4 Re-run only the correctness portion of the Serena-versus-Serena-Light ablation and preserve the original snapshot as baseline; do not begin the compact efficiency comparison in this change.
- [x] 6.5 Stop and return to design review if correctness requires Serena's agent/modes/project-server architecture, a new language, lexical-semantic result mixing, or a new trust/edit authority; otherwise report production LOC as information only.
- [x] 6.6 Obtain independent Sol-xhigh static-correctness and Opus-max runtime/evidence audits and disposition every blocker with lead-owned evidence.
- [x] 6.7 Synchronize the accepted delta into stable specs, validate the stable and active contracts strictly, and confirm archive readiness before any `compact-success-schema` implementation begins.
