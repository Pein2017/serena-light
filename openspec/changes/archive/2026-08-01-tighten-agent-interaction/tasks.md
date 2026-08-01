## 1. Baseline, containment, and routing

- [x] 1.1 Record the exact source commit, schema-3 build identity, dependency-lock digest, clean/dirty ownership, and connector-visible baseline fixtures for missing initialize instructions, overview depth/omission, reference declaration/coverage, deterministic symbol miss, and both diagnostics tools at `max_answer_chars=512`; do not use `cc-plugin-codex` as a test workspace.
- [x] 1.2 Add red tests proving the current failures: outer and inner instructions are absent or differ, default overview is deeper/noisier than specified, caller-selected depth inflates `omitted`, references include the declaration and verbose complete coverage, and final diagnostics MCP text exceeds its advertised bound.
- [x] 1.3 Use the `agent-routing` skill to issue self-contained, non-overlapping implementation briefs to at most one Claude worker and one Codex worker when parallel ownership is safe; keep schema/integration/acceptance with the lead, prohibit whole-change duplicate implementations, and record model, effort, task class, completion, verifier outcome, lead corrections, wall time, scope control, and interaction friction without claiming a matched ranking.
- [x] 1.4 Treat any loss of semantic entities, decoded-text range fidelity, exact body/hash, incomplete-coverage truth, freshness admission, or typed operational failure as a stop condition; revert the affected schema-4 lane before continuing rather than accepting token reduction as compensation.

## 2. Canonical presentation and schema rollover

- [x] 2.1 Advance the public tool/schema version from 3 to 4 and add focused build-identity tests proving schema-4 connectors select a new build while a leased schema-3 daemon remains isolated and retires only through existing zero-holder policy.
- [x] 2.2 Implement one canonical minified JSON presentation boundary whose exact value is shared by `content[0].text` and `structuredContent`, with deterministic UTF-8/key/order behavior and no FastMCP pretty-print expansion.
- [x] 2.3 Move final answer-budget enforcement to that boundary for navigation and diagnostics; remove only whole trailing records/subtrees, reserialize after every reduction, preserve stable-prefix/minimum-required semantics, and bound optional error evidence without cutting JSON or source-derived strings.
- [x] 2.4 Split deterministic input/path/not-found error presentation from rich ambiguity and operational error presentation, preserving bounded candidates, retry/phase, adapter/generation, and scope evidence only where the contract requires it.
- [x] 2.5 Add renderer/envelope tests for Unicode, CRLF/BOM ranges, 512/12,000/50,000 bounds, one-record minimum errors, multi-adapter authority, structured/text equality, and deterministic repeat serialization.

## 3. Initialization and public tool guidance

- [x] 3.1 Add one concise source-owned instruction constant covering Python/JS/TS semantic scope, startup-cwd binding, explicit absolute cross-root activation, scoped queries, depth-0 overview, opt-in reference snippets, explicit diagnostics, debug-only status, and host-owned lexical search.
- [x] 3.2 Wire the exact same instruction bytes into the outer stdio connector and inner daemon initialize responses without adding a hook, automatic diagnostics injection, or public instructions tool.
- [x] 3.3 Tighten tool/field descriptions only where they affect Agent choices: Serena name-path syntax, file/directory/global scope, overview depth and kind filters, severity numbering, snippet opt-in, absolute activation, and immediate release; do not rename or merge tools.
- [x] 3.4 Add direct-daemon and stdio black-box tests for instruction byte identity, schema-4 tool listing, absence of lexical/hook tools, and concise schema descriptions.

## 4. Overview interaction

- [x] 4.1 Change `get_symbols_overview` defaults in runtime dispatch and public schema to `max_depth=0` while retaining every root symbol kind.
- [x] 4.2 When descendant depth is requested, suppress descendant `variable`/`constant` nodes by default, allow explicit `include_kinds` to recover them through minimum ancestor paths, and preserve `exclude_kinds` precedence.
- [x] 4.3 Redefine overview `omitted` to count only answer-budget removal and upstream semantic caps, not caller-selected depth, explicit kind filters, default descendant-noise selection, or removed structural ancestors.
- [x] 4.4 Add Python and TypeScript fixtures covering root variables/constants, class/method descendants, noisy locals, explicit variable inclusion, nested include/exclude filters, stable preorder pruning, and exact omitted counts.

## 5. Reference interaction

- [x] 5.1 Disable declaration inclusion in every Python and TypeScript reference-dispatch path and confirm `find_declaration` remains the sole declaration owner.
- [x] 5.2 Keep snippets absent at the existing zero default and include them only for a positive `max_snippet_chars`, preserving exact snapshot mapping and per-reference bound.
- [x] 5.3 Render full coverage as exactly `{"complete":true}` and incomplete coverage as `complete=false`, total uncovered files, a deterministic bounded path/reason sample, and omitted count; retain full counts/digests/projection evidence in status and rich scope failures.
- [x] 5.4 Add complete/incomplete/empty/multi-file reference tests proving no lexical fallback, no declaration record, truthful coverage, stable sample order, and connector-visible budget compliance.

## 6. Diagnostics interaction

- [x] 6.1 Replace successful file diagnostics with the shared compact workspace/files/omitted envelope, one requested-path file group, and flat finding records containing severity, compact range, message, and optional symbol/source/code; represent clean only as an empty current-generation array.
- [x] 6.2 Apply the same compact file grouping to symbol diagnostics while retaining exact symbol-range filtering and typed ambiguity; remove success-only hash, generation, adapter, engine, interpreter, URI, and offset repetition.
- [x] 6.3 Put `authority=advisory` once on successful TypeScript file groups, omit Pyright/interpreter facts from Python success, and preserve both languages' engine/interpreter/native-authority facts in runtime status and rich operational errors.
- [x] 6.4 Add current findings/clean, severity threshold, file-level finding, symbol filter/ambiguity, stale publication, timeout, cold adapter, TypeScript advisory, Python external import, Unicode/CRLF, and exact 512-character final-envelope tests for both diagnostics tools.

## 7. Integrated acceptance

- [x] 7.1 Run focused tests for every changed presenter/tool path, then the complete pytest suite, Ruff, Ty, bootstrap, source-ownership/direct-dependency/provenance/census checks, production-LOC information report, and strict OpenSpec validation; fix all regressions before client testing.
- [x] 7.2 Through the real daemon and stdio connector, compare schema-4 results with the recorded schema-3 fixtures and prove identical semantic entities, bodies, hashes, decoded-text ranges, diagnostics findings, and incomplete-coverage meaning while reporting actual characters/tokens/calls/wall time as evidence rather than a hard efficiency gate.
- [x] 7.3 Run real Python smokes in `/data/CoordExp`, the live `/data/ms-swift` Git root (there is no `/data/CoordExp/ms-swift` path), the `conda ms` transformers package, and `/data/CoordExp/.worktrees/research-probes`; run real JavaScript/TypeScript smokes in the read-only `/data/CoordExp/external/codexUI` reference checkout or isolated Serena Light integration fixtures, never in the actively developed `cc-plugin-codex` repository.
- [x] 7.4 Verify another Agent's create/change/delete between calls is observed by the next overview, symbol, reference, and diagnostics read without manual refresh, and verify explicit `activate_workspace("/data/ms-swift")` switches only the calling lease after shell cwd changes.
- [x] 7.5 Start fresh Codex and Claude/CC Agent clients on schema 4; confirm both receive the instructions, prefer Serena Light for semantic work, use host tools for lexical work, call diagnostics explicitly rather than through hooks, and can release/rebind without affecting another client.
- [x] 7.6 Verify a live schema-3 holder and new schema-4 clients coexist, schema-4 source/lock/schema changes never reuse schema-3 discovery, and isolated test-owned build daemons retire only after their own holders and warm grace end without touching canonical Serena; record pre-existing host daemon accumulation separately rather than claiming a host-wide no-orphan state.

## 8. Review, evidence, and release

- [x] 8.1 Update README, compatibility/client-registration documentation, `docs/roadmap.md`, public schema examples, and acceptance evidence with the final compact shapes, instruction text, schema-4 migration, measured payloads, and explicit exclusions for lexical discovery, hooks, and RTK.
- [x] 8.2 Rebase the planning-only `add-lexical-discovery` change away from its stale schema-4 reservation and leave both it and `improve-warm-runtime-reuse` unimplemented/deferred; validate that neither change's tools or lifecycle behavior leaked into this diff.
- [x] 8.3 Request the previously selected final reviews only after implementation is green: Sol-xhigh for static contract/correctness and Opus-max for runtime/evidence. The lead SHALL disposition every finding against code, specs, and reproduced evidence; Fable is not part of the final gate.
- [x] 8.4 Summarize worker delivery evidence from task 1.3, distinguish task differences from model effects, and update shared routing priors only if the observation materially changes a future route; do not force-stop a productive worker solely to equalize runtime.
- [x] 8.5 Re-run all gates after review fixes, sync the accepted delta specs to stable specs, archive this change with the OpenSpec archive workflow, then commit and push only the scoped Serena Light changes to the configured repository.
