# Final Static Correctness Audit

## Scope

- Target: complete live dirty tree at `/data/CoordExp/serena-light`
- Mode: read-only implementation-vs-OpenSpec audit
- Fixed point: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`, branch `main`
- Live identity: schema `3`, build `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
- Dirty-tree inventory: 111 tracked/untracked porcelain entries; status digest `540e7532db5a8b42418eb1091ce5288141b259e3b5d82710f2615cc6c8cba275`
- Line convention: 1-based, inclusive, from the live working tree
- Constraints observed: no edits, staging, commits, installs, fetches, web, memory, subagents, or full-suite rerun

## Findings

### P0

None.

### P1

None.

### P2

1. README incorrectly says the final fixed-contract four-arm repeat is still in progress.

   - Evidence: [README.md](/data/CoordExp/serena-light/README.md:134) says the repeat “is in progress,” immediately before saying the only remaining gates are the two audits.
   - Contradicting current owners:
     - [tasks.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/tasks.md:57) marks task 7.4 complete.
     - [compact-ablation-final-results.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/evidence/compact-ablation-final-results.md:68) records the final repeat decision.
     - [roadmap.md](/data/CoordExp/serena-light/docs/roadmap.md:163) says the locked four-arm ablation is complete.
   - Impact: non-runtime documentation/progress contradiction; it does not invalidate implementation or evidence.
   - Disposition: `fix` before archive by replacing the stale “in progress” sentence with the completed status.
   - Verification: search the four current owners for a single consistent ablation state.

No engineering-standard P2 was found beyond this documentation-state mismatch.

## Prior-Blocker Disposition

| First-pass blocker | Disposition | Production evidence |
|---|---|---|
| Workspace references could fall back to raw coordinates | **Closed** | Raw location rendering is selected only for `READ_ONLY_EXTERNAL` at [runtime.py](/data/CoordExp/serena-light/src/serena_light/workspace/runtime.py:2183), while missing/mismatched workspace snapshots return retryable `NOT_READY` at lines 2192–2197. The reference renderer rejects a raw document for a workspace target at [references.py](/data/CoordExp/serena-light/src/serena_light/tools/references.py:316), retains a valid mapper despite a malformed symbol tree at lines 381–401, and converts mapping/URI/range failures to authority-bearing retryable `NOT_READY` at lines 331–356 and 435–456. |
| Final overview pruning could leave a childless nonmatching structural ancestor | **Closed** | Filtering records `intrinsic_match` at [compact_adapter.py](/data/CoordExp/serena-light/src/serena_light/tools/compact_adapter.py:182). Final pruning removes the structural parent when its final retained child disappears at [compact.py](/data/CoordExp/serena-light/src/serena_light/tools/compact.py:829), counting both removed nodes. |
| `minimum_required_chars` could advertise a smaller later record | **Closed** | Exhausted flat results call `_minimum_flat_success_chars` at [compact.py](/data/CoordExp/serena-light/src/serena_light/tools/compact.py:449); that helper constructs the reachable prefix from `records[0]`, with truthful omissions, at lines 787–800. Overview minimums similarly follow the first reachable structural path at lines 841–865. |
| Multi-adapter budget errors lost per-item authority | **Closed** | Missing top-level global authority triggers item-authority extraction before rendering at [compact_adapter.py](/data/CoordExp/serena-light/src/serena_light/tools/compact_adapter.py:109). `_symbol_authorities` deterministically deduplicates and bounds the two supported owners at lines 369–397; rendering forwards them at lines 122–132 and the rich error places them in `details.authorities` at [compact.py](/data/CoordExp/serena-light/src/serena_light/tools/compact.py:537). |
| Malformed-success conversion discarded independently parsed authority | **Closed** | Workspace, adapter, and generation authority are parsed independently at [compact_adapter.py](/data/CoordExp/serena-light/src/serena_light/tools/compact_adapter.py:75); any successfully parsed values survive the conversion call at lines 133–140 and are emitted by `_malformed_result` at lines 484–507. |
| File/directory ambiguity evidence used the private success budget | **Closed for every actual candidate-bearing path** | The daemon preserves the caller’s public budget separately as `_error_max_answer_chars` at [server.py](/data/CoordExp/serena-light/src/serena_light/daemon/server.py:890), while retaining the large private success budget. File-scoped ambiguity passes that public value into `_bounded_candidates` at [navigation.py](/data/CoordExp/serena-light/src/serena_light/tools/navigation.py:330); the helper returns a deterministic prefix plus truthful omission count at lines 397–408. Directory scope is intentionally a multi-record success, not an `AMBIGUOUS_SYMBOL` path, and is bounded through final compact rendering at lines 161–244. |
| Implementation kind/name/info semantics were incomplete | **Closed** | Target document-symbol capture occurs inside the response-owned replay transaction at [runtime.py](/data/CoordExp/serena-light/src/serena_light/workspace/runtime.py:2377). Response-owned target selection derives exact or containing symbol metadata at lines 3446–3463; normalization emits kind, requested name path, and bounded detail at lines 2198–2213. Unknown kinds fail positive includes, survive excludes, and increment omissions at [declarations.py](/data/CoordExp/serena-light/src/serena_light/tools/declarations.py:328). |

## Additional Contract Checks

- Exact final MCP success text is correctly owned by one renderer: [compact.py](/data/CoordExp/serena-light/src/serena_light/tools/compact.py:410) serializes canonical UTF-8 JSON once, and lines 417–426 place that exact string in the sole text block with the same value in `structuredContent`.
- The production daemon applies public validation before semantic dispatch and separates the large private success budget from the public error budget at [server.py](/data/CoordExp/serena-light/src/serena_light/daemon/server.py:836) and lines 890–906.
- Only successful navigation responses cross the compact boundary; rich errors and non-navigation results remain unchanged at [server.py](/data/CoordExp/serena-light/src/serena_light/daemon/server.py:320).
- Public schemas expose the required limits and filters at [server.py](/data/CoordExp/serena-light/src/serena_light/daemon/server.py:384): `find_symbol.max_matches`, overview string-kind filters, declaration budgeting, implementation integer-kind filters, and reference snippet/budget controls. No compact flag or public adapter-candidate limit is present.
- Success metadata boundaries are exact: `CompactNavigationSuccess.to_dict()` emits only `ok/data`, with workspace/files/optional reference coverage/omitted at [compact.py](/data/CoordExp/serena-light/src/serena_light/tools/compact.py:354). Rich errors retain workspace/adapter/generations through [envelopes.py](/data/CoordExp/serena-light/src/serena_light/tools/envelopes.py:249).
- Stable-spec sync remains intentionally pending rather than stale: task 7.6 is unchecked at [tasks.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/tasks.md:59), so the active delta remains the candidate authority until the second final audit passes.

## Evidence-Claim Disposition

| Claim | Disposition |
|---|---|
| Current shipping candidate identity is `92b2618…` | **Supported.** Live recomputation produced that exact identity; schema version is `3`, and the dependency-lock digest is `eff6ebdf…`. Runtime source closure includes untracked `.py`/`.mjs` files through [build_identity.py](/data/CoordExp/serena-light/src/serena_light/build_identity.py:28). |
| Fixed-fixture payload reductions pass | **Supported as explicitly bounded.** [acceptance.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/acceptance.md:3) truthfully distinguishes the retained full-text capture, later hash-equality recap, paired reference capture, historical post-repair recap, and final-build smokes. It does not falsely claim a new full-text nine-case capture at `92b2618…`. |
| Final fixed-contract four-arm ablation belongs to the final build | **Supported.** [manifest.json](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/evidence/ablation-arms-final/manifest.json:8) names `92b2618…`; all prompt, subject-file, and four receipt hashes independently matched the live files. The call totals, fallback totals, accuracy averages, word counts, and cautious parity claim in [compact-ablation-final-results.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/evidence/compact-ablation-final-results.md:29) agree with the manifest. |
| Fresh Codex, native Claude Code, and CC Agent exercised the repaired boundaries | **Supported at receipt level.** All three final-repair reports name `92b2618…` and agree on the bounded ambiguity, structural overview, implementation metadata/filter, minimum-budget authority, and release outcomes. These are independent summary receipts rather than raw transcripts; acceptance describes them as current-build smokes, not substitutes for paired fixture captures. |
| Schema/build rollover is proven | **Supported with explicit provenance split.** The generic reproducible rollover test and live coexistence evidence in [actual-schema-rollover.json](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/evidence/actual-schema-rollover.json:3) belong to earlier temporary/current candidate builds. Final-build selection is separately covered by the three `92b2618…` fresh-client receipts and the recorded final suite. |
| Full suite, lint, type, bootstrap, provenance, and performance gates passed | **Recorded and internally coherent, not rerun in this static lane.** [acceptance.md](/data/CoordExp/serena-light/openspec/changes/compact-success-schema/acceptance.md:108) records 821 tests plus the opt-in performance case and supporting checks. No conflicting receipt was found. |

## Focused Verification

Executed without bytecode or pytest cache writes:

- Compact renderer/adapter, navigation, references, declarations, real FastMCP boundary, and public version tests: **91 passed**
- Response-owned declaration/implementation/reference and directory-scope runtime compositions: **30 passed**
- `openspec validate compact-success-schema --strict`: **valid**
- All change JSON files and `docs/compatibility.json`: parsed successfully
- `git diff --check`: passed
- Live build identity, lock digest, final-ablation subject hashes, prompt hash, and four receipt hashes: matched

The full 821-test suite, live external clients, poisoned-proxy suite, performance run, and runtime rollover were intentionally not repeated.

## Verdict

**PASS — no P0 or P1 correctness defect remains.**

The static lane is ready to close. The overall change should remain on its existing HOLD until the independent Opus-max runtime/evidence audit completes. The stale README ablation sentence is an explicit nonblocking P2 to correct before archive.
