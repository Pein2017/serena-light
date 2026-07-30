# Final Independent RUNTIME/EVIDENCE Audit — `compact-success-schema`

**Subject:** `/data/CoordExp/serena-light`, dirty working tree at HEAD `9e4987e` ("docs: sync and archive serena light v1"), 53 unchanged porcelain entries (digest `81b3e1a643ebf46e5d43469c6aff38b6e32affc98b6fbb770639865ace40d29a`, identical at audit start and end).
**Build identity computed from this tree:** `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242` — exact match to the acceptance target.
**Live subject:** daemon `145cfe14-a52f-4d4c-b8b3-3690f5193408`, endpoint `127.0.0.1:24407`, protocol `2025-11-25`, MCP 1.27.1; connector `…/deps/eff6ebdf…/python/bin/serena-light` editable-installs `/data/CoordExp/serena-light/src` (`_editable_impl_serena_light.pth`), i.e. the probes exercised this dirty tree.
**Method:** 5 fresh real stdio connector sessions → 89 tool calls (67 compact successes, 22 typed errors), plus two full-suite runs, static review, and evidence recomputation. Read-only throughout; no canonical Serena, no subagents, no web.

**VERDICT: PASS** — zero P0, zero P1. Five explicit nonblocking P2s below.

---

## Findings

### P0
None.

### P1
None. Every first-pass blocker was reproduced-as-repaired or falsified at the production boundary (table below).

### P2-1 — Rich errors are still ~2.1× the caller's public budget in client-visible text
`src/serena_light/daemon/server.py:329-353` returns navigation **errors** as plain dicts, so FastMCP pretty-prints them, while success goes through the exact renderer. Measured on `find_symbol relative_path=src/serena_light/workspace/runtime.py name_path="_" substring_matching=true`:

| `max_answer_chars` | candidates | `omitted_count` | canonical details | **client-visible `content[0].text`** |
|---:|---:|---:|---:|---:|
| 512 | 2 | 458 | 571 | **1,615** |
| 12,000 | 49 | 411 | 11,922 | **25,139** |
| 50,000 | 203 | 257 | 50,106 | **103,209** |

Candidate evidence *is* now bounded by the caller's budget (`navigation.py:397-408`), and `candidates + omitted_count = 460` in all three — the requirement in the delta spec ("Candidate-bearing semantic errors SHALL bound their evidence using the caller's public answer budget") is met, and design Decision 4 explicitly keeps errors on the rich renderer. But this is the same FastMCP-expansion failure mode the change fixed for success, and a 50,000-char request still yields 103,209 characters. Inconsistency worth noting: the two compact-owned error paths (`compact.py:551-555` and `compact_adapter.py:503-507`) *do* emit compact text (observed 400 / 432 chars), so the error surface is now mixed.

### P2-2 — `include_info` is still observably inert; one fresh-client receipt overstates it
`_symbol_data` (`tools/navigation.py`) emits `info = {detail, selection_range}`; `_compact_info` (`compact_adapter.py:420-428`) correctly drops the selection range and returns `None` when `detail` is empty. Neither Pyright nor typescript-language-server supplied `detail` in any probe, so `include_info=true` produced **no `info` field** in 5/5 cases (`find_symbol` on a Python class and a TS class, `find_declaration`, `find_implementations`, external declaration). This is spec-conformant ("available bounded semantic detail"). The repair is real but narrower than claimed: targets now carry response-owned `name_path`/`kind` (`impl_include_info` → `{"range":[[4,13],[4,27]],"name_path":"ConcreteRunner","kind":"class"}`).
**Evidence-claim defect:** `evidence/fresh-codex-final-repair.md` states the implementation lookup "retained `ConcreteRunner` with class kind, name path, **and semantic detail when requested**". No `info`/detail is reproducible on this build. The native-Claude and CC-Agent receipts make no such claim.

Related, and worth an explicit note: `find_declaration include_info=true` against the external transformers target returns `UNSUPPORTED / verified_target_snapshot_unavailable` (581 chars) — the whole call fails rather than degrading. That is the correct fail-closed behaviour per `declarations.py:440-448`, but it is not described in `docs/compatibility.json`'s `optional_field_contract`.

### P2-3 — README is stale about the final ablation
`README.md:134`: "The historical four-arm ablation is complete; its final fixed-contract repeat **is in progress**." The final repeat is complete and verified (`evidence/compact-ablation-final-results.md`, `ablation-arms-final/manifest.json` at build `92b2618…`). `docs/roadmap.md:162-173` and `docs/client-registration.md:110-117` are accurate and current.

### P2-4 — `porcelain_sha256_at_freeze` is not independently reproducible
`ablation-arms-final/manifest.json` records `f5e33220b6fb389cc4142460066959739b6e90ed77addbdf6afcba903750805a`. I could not reproduce it with any of 10 standard formulations (`--porcelain`, `=v1`, `=v2`, `-uall`, `-z`, `--short`, sorted, no-trailing-newline). The current tree yields `81b3e1a6…` (53 entries). Sol's first pass raised the same non-reproducibility. The substantive freeze evidence does verify — prompt SHA-256, all four arm receipt SHA-256s, and all nine subject-file SHA-256s across both worktrees match the current on-disk files — so this is provenance hygiene, not a correctness defect. Recommend replacing it with a stated command or dropping it.

### P2-5 — One ambiguity path is not public-budget bounded (pre-existing)
`src/serena_light/tools/declarations.py:515-522` (`_resolve_symbol`) emits `details.candidates` as an unbounded list of name-path strings, reached by `find_implementations(name_path=…)` and `find_declaration(containing_symbol_name_path=…)`. Unlike `find_symbol`'s `AMBIGUOUS_SYMBOL`, it receives no `_error_max_answer_chars`. Bounded only by symbols-per-file. Not reachable in live probes here (Pyright reports `implementationProvider: false`), and not a regression from this change. Also informational: overview final-budget pruning is O(n²) reserialization — `runtime.py` overview at `max_depth=20` cost 0.93 s at 50,000 chars vs 1.77 s at 512 (1,085 nodes pruned), ≈0.8 s of pruning CPU; directory-scope `find_symbol` cost is dominated by LSP document loading (18.7 s at both 512 and 50,000), not by pruning.

---

## Prior-blocker disposition

| # | First-pass blocker (Sol-xhigh / Opus-max) | Disposition | Runtime / static receipt |
|---|---|---|---|
| S1 / O1 | Workspace `ReferenceDocumentInput` could degrade to `raw_range` | **REPAIRED — falsified** | `workspace/runtime.py:2970-2983` emits `RawReferenceDocumentInput` only for `target.read_only_external`; unbound workspace target → `_reference_not_ready`. Three defence layers: `tools/references.py:316-322`, `:410-412` (→`PositionError`→NOT_READY at `:351-356`), `_DocumentTree.from_input:382-401` keeps the mapper when only the symbol tree is malformed. Live: 5 workspace reference records across 3 files, all `range`; only the transformers target carries `raw_range` + `position_basis:"lsp_zero_based_line_utf16_code_unit_character"` with `read_only:true` and its absolute path. |
| S2 / O2 | Filtered-overview ancestor survived budget pruning of its last descendant | **REPAIRED — falsified** | `compact.py:829-838` drops a non-`intrinsic_match` ancestor when its last child goes (cascades upward); `compact_adapter.py:182-185` sets it; `compact.py:159-163` keeps it out of the wire. Live sweep, 10 budgets 512→12,000 on `tools/compact.py` with `include_kinds=["method"]`: **0** childless non-method leaves, **0** empty `children` arrays, `nodes + omitted == 294` invariant at every budget. `exclude_kinds=["method","class"]` and `include∩exclude` both → `files:[]`, `omitted:21`. |
| S3 / O3 | Minimum reported from a later smaller record | **REPAIRED — falsified** | `compact.py:787-800` (`records[0]`) and `:841-865` (first-reachable overview prefix). Live: `find_referencing_symbols max_answer_chars=512` → `minimum_required_chars=729`, byte-exactly the 729-char response returned at budget 900. `find_symbol include_body WorkspaceRuntime` at 512 → 85,820. Every observed minimum > the failing budget. |
| S4 / O4 | Multi-adapter budget error lost per-item authority | **REPAIRED — mechanism verified live; 2-owner composition not reproducible here** | `compact_adapter.py:110-111, 369-397` (dedup, deterministic sort, `_MAX_ERROR_AUTHORITIES=2`), emitted at `compact.py:541-542`. Live: global `find_symbol` budget error returned `details.authorities=[{adapter:pyright, generations:{…scope:"configured_program"}}]` with **no** fabricated top-level adapter. Two-family case covered by `tests/unit/test_compact_adapter.py:365`; not reproducible live because this workspace's TypeScript configured program holds 1 file (runtime status), so a global query never returns TS records. |
| O5 | File-scoped `AMBIGUOUS_SYMBOL` used the 2,147,483,647 internal budget | **REPAIRED** (residual → P2-1) | `daemon/server.py:902-903` forwards the public budget as `_error_max_answer_chars`; `navigation.py:311-347, 397-408` consumes it. Was 839 candidates / ~424,683 chars at every budget; now scales 512→2 / 12,000→49 / 50,000→203 with truthful `truncated=true` and exact `omitted_count`. |
| S-P2a | Malformed-success conversion discarded parsed authority | **REPAIRED** | `compact_adapter.py:66-91, 133-140, 484-507`; test `test_malformed_success_preserves_independently_valid_authority_fields`. |
| S-P2b | Focused tests lacked the four contract-critical compositions | **REPAIRED** | `test_overview_budget_removes_newly_childless_structural_ancestors_and_counts_them`, `test_descendant_only_overview_budget_never_returns_a_lone_structural_ancestor`, `test_flat_minimum_budget_uses_the_first_stable_record_not_a_smaller_later_record`, `test_multi_adapter_minimum_budget_error_preserves_all_item_authorities`. |
| O-P2a | Implementation kind filters erased results without counting | **REPAIRED** | `declarations.py:317` (`capture_target_symbols=include_info or bool(included) or bool(excluded)`), `:328-350` counts every removal; unknown kind fails include, survives exclude. Live: `[5]`→target with `kind:"class"`, omitted 0; `[13]`→`files:[]`, omitted 1; `exclude [5]`→`files:[]`, omitted 1. |
| O-P2b | `include_info` inert | **PARTIAL** → P2-2 | Targets gained `name_path`/`kind`; `info` still never emitted. |
| O-P2c | Acceptance must keep build provenance explicit | **DONE — verified** | See evidence table. |
| O-P2d | Ablation predated the repairs | **REPAIRED** | `ablation-arms-final/` reran at `92b2618…`; prompt hash + 4 receipt hashes + 9 subject-file hashes all verify against current files. |

---

## Evidence-claim disposition

| Claim | Status | Receipt |
|---|---|---|
| Build identity `92b2618…` | **VERIFIED** | Recomputed from the dirty tree via the service Python: exact match. Schema 3, algorithm 3. |
| Nine-case runtime recap (recorded at pre-repair `ad9a3630…`) applies to the shipping build | **VERIFIED — gap closed by re-capture** | I replayed all nine cases live at `92b2618…`: chars 235 / 337 / 235 / 961 / 1293 / 195 / 201 / 321 / 85 and all nine SHA-256s **byte-for-byte identical** to the recorded recap. |
| `a9d7e7ae…` is a hash-equality recapture of `4ff261a7…` for its 8 non-reference payloads | **VERIFIED** | All 8 SHA-256s equal. |
| Deterministic payload gates (5 rows) | **VERIFIED — all pass** | no-body 235/904 = 26.0% ≤50%; global 236/2,727 = 8.7% ≤40%; overview 961/22,686 = 4.2% ≤25%; references 947/3,026 = 31.3% ≤40% (from `final-schema3-current-build-recap.json`); body-external 321/922 = 34.8% ≤50%. Baseline arguments match the schema-3 arguments per case; the disclosed `Calculator` vs `ANSWER` global divergence is real and correctly disclosed. |
| Exact MCP text budget, one text block, text == canonical JSON of `structuredContent` | **VERIFIED at scale** | Across **67** compact successes in 89 calls: 0 budget overruns, 0 canonical mismatches, all `block_count == 1`, top level exactly `{ok,data}`, `data` keys ⊆ `{workspace,files,omitted,coverage}`, no adapter/generations/uri/text_offset/byte_offset/selection_range/relative_path/detail anywhere outside the sanctioned `data.coverage` object. All 22 error responses satisfy `json.loads(text) == structuredContent`. |
| Full suite: 821 passed, 1 opt-in skip, 1 deprecation warning | **VERIFIED ×2** | Two independent runs with live `/data/CoordExp`, `cc-plugin-codex`, `ms-swift`, transformers snapshot vars: `821 passed, 1 skipped, 1 warning in 370.38s` and `…in 370.56s`. |
| Opt-in transformers performance case | **VERIFIED** | `SERENA_LIGHT_RUN_PERFORMANCE_ACCEPTANCE=1` → `1 passed in 30.75s` (recorded 32.52 s). |
| Ruff / Ty / bootstrap / OpenSpec strict / `git diff --check` / JSON parse | **VERIFIED** | All pass; `openspec validate --all --strict` → 5 passed, 0 failed; bootstrap `--check --json` reports service CPython 3.12.12 and dep slot `eff6ebdf…`. |
| Source budget / provenance / census | **VERIFIED** | `current_local_production_lines=18340`, `maximum_production_lines=null`, `forbidden_imports=[]`, `undeclared_external_imports=[]`, `census_manifest_agreement=true`, `copied_source_hashes_verified=9`, `reference_commit 9a9d07e83d8c…`, `status=pass`. |
| Rollover / versioned coexistence (recorded at `a9d7e7ae…`) | **VERIFIED + independently corroborated now** | Three live coexisting build daemons observed during this audit: schema-2 `481c45e4…` (pid 1811592), `ad9a3630…` (pid 3733335), and `92b2618…` (pid 3952280). Fresh connectors bound only `92b2618…`. `test_real_versioned_rollover_acceptance.py` is inside the passing suite. |
| Fresh Codex / native Claude Code / CC Agent receipts at `92b2618…` | **VERIFIED, one overstatement** | All three cite the correct build (and Codex/CC-Agent the correct daemon id). I independently reproduced their overview `omitted=287` figure and the compact ambiguity behaviour. `fresh-codex-final-repair.md`'s "semantic detail when requested" is not reproducible → P2-2. `fresh-client-schema3.json` is at pre-repair `a9d7e7ae…` and is correctly described as superseded. |
| Docs old→new mappings for all five tools | **VERIFIED** | `docs/compatibility.json` `migration_examples` payloads for `find_symbol` (body), `find_declaration` (workspace **and** external), and `find_implementations` match my live captures exactly; `find_referencing_symbols` matches the configured-root evidence workspace. Schema version 3 consistent with `PUBLIC_TOOL_SCHEMA_VERSION`. |
| Lifecycle / lease cleanup | **VERIFIED** | All 5 probe sessions: `released=true, bound=false, active_holders=0, runtime_stopped=false, runtime_stop_pending=false`. No orphan `serena`/`pyright`/`tsserver` processes; only the pre-existing daemon, this session's connector, and the untouched canonical Serena remain. `tasks.md` 7.6 correctly remains `[ ]`; stable specs for this change correctly remain unsynced (the modified `openspec/specs/*` belong to the archived prerequisite). |
| Prerequisite `fix-position-and-coverage-contract` archived (task 1.1) | **VERIFIED** | Present under `openspec/changes/archive/2026-07-30-…/`, absent from active changes. |
| "A combined focused repair run passed 143 cases" | **NOT SEPARATELY REPRODUCED** | Subsumed by two clean 821-test full-suite runs; no attempt made to reconstruct the exact 143-case selection. |

---

## Additional production-boundary checks (no defects found)

- Public input validation fires **before** dispatch (~0.06 s): `max_answer_chars` 511/50001 → `INVALID_INPUT field=max_answer_chars`; `max_matches` 0/101 → `field=max_matches`; unknown overview kind → `field=include_kinds or exclude_kinds`; `find_declaration max_answer_chars=100` → rejected (parameter present and enforced).
- No public `compact` flag and no `max_candidates_per_adapter` in any listed tool schema; `find_symbol.max_matches` 1–100/default 20 and lowercase-string overview filters present.
- `omitted` arithmetic exact in every sweep: directory scope `kept + omitted == 689` at 6 budgets and `== 2847` at `max_matches=100`; global `run` `1 + 877 == 878`; overview `nodes + omitted == 294` at 10 budgets.
- Errors never become compact empty success: `SYMBOL_NOT_FOUND`, `INVALID_PATH`, `UNSUPPORTED`, `AMBIGUOUS_SYMBOL`, `NOT_READY` all retain rich workspace/adapter/generation authority where dispatch reached the adapter. Genuine empty is exactly `{"files":[],"omitted":0}` (85 chars).
- Non-navigation contracts intact: `get_diagnostics_for_file` still returns engine name/version/interpreter/external root, `diagnostics_generation`, adapter and generations; `get_runtime_status` unchanged.
- Snippet bounding is honest — `max_snippet_chars=5` renders `"ANSW…"` with a visible ellipsis, not a silent cut. `.mjs` overview returns compact name/kind/children per the spec scenario. TypeScript multi-line assignment body returns the complete statement with the file hash.

---

## Bottom line

**PASS.** All five first-pass P1 blockers are repaired and independently falsified at the real daemon/connector boundary on build `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`; both first-pass P2 test/authority gaps and both executable-contract P2s are addressed; the build-provenance gap between the recorded `ad9a3630…` recap and the shipping build is closed by my own byte-identical re-capture; and the full suite, performance case, static gates, provenance, rollover, ablation hashes, and three-surface fresh-client receipts all verify. The five P2s above are nonblocking but should be dispositioned by the lead before archive — in particular P2-2 (a fresh-client receipt claim that does not reproduce) and P2-3 (stale README sentence), both of which are one-line evidence/doc corrections, and P2-1, which is a scoped-out-by-design residual that a lead may nonetheless want recorded as a known limitation.
