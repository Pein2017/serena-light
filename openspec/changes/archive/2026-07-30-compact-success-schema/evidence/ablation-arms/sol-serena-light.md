# Serena Light Ablation Arm — SOL

Archival copy of the locked-ablation analytical response, preserved verbatim below.

## Snapshot

- `/data/CoordExp/serena-light` HEAD: `9e4987e9f2190a4ff03cb7a35359483a5387f327`
  - Relevant modified files: `src/serena_light/workspace/runtime.py`, `src/serena_light/tools/navigation.py`, `src/serena_light/tools/global_symbols.py`, `tests/unit/test_workspace_runtime_semantics.py`.
- `/data/CoordExp/.worktrees/research-probes` HEAD: `ccdadc4e2d8c00a091dde8d684a14982f05715f2`
  - No relevant files appeared modified.

All evidence ranges below are Serena Light MCP ranges with **0-based line numbers**. No shell source fallback was used.

## Question 1 — `WorkspaceRuntime.find_symbol`

`find_symbol` wraps its closure in `_tool_envelope`; `_tool_envelope` calls `self.ensure_fresh()` before invoking the operation. On Git roots, that delegates to `FreshnessCoordinator.ensure_fresh`; exact routing later calls `ensure_path_fresh`, which is a no-op for Git and a targeted single-file stat for allowlisted non-Git roots.

Branches:

- **Exact file:** a `relative_path` present in `inventory.paths` is routed by `_route`, then delegated to `DocumentNavigationService.find_symbol`. Scope is exactly one authorized inventory file. A stored attribution failure for that file’s family is raised before routing.
- **Directory:** a non-file `relative_path` is resolved only through `inventory.paths_under`; there is no workspace walk. It delegates to `DocumentNavigationService.find_symbol_in_documents` over the sorted, deduplicated inventory-selected files whose families have live adapters. Mixed scopes omit unavailable families; if the selected directory contains only unavailable families, it raises `SCOPE_INCOMPATIBLE`. Supplying the global-only candidate option here returns `UNSUPPORTED`.
- **Global:** no `relative_path` warms candidates and delegates to `GlobalSymbolService.find_symbol` over live adapters only. Scope is each adapter’s configured program. Workspace-symbol candidates are bounded to 128 per adapter by default and at most 256, then verified against document symbols and generation identity. Unsupported workspace-symbol service yields `UNSUPPORTED`; unready adapters or generation drift yield retryable `NOT_READY`. If every attributed family is unavailable, it yields `SCOPE_INCOMPATIBLE`; with some healthy families, unavailable families are excluded.

Freshness coordination:

- Concurrent callers join one `_SharedScan`, wait for the same result/failure, and do not start a second rebuild. Completed scans are never time-cached: the next operation scans again.
- `_scan_git` rebuilds inventory, computes `created/deleted` from membership, `changed` by content identity for retained paths, and detects rejected-set changes separately. `symlinked` reports rebuilt rejected paths whose reason begins with `symlink`; it is not itself a “newly changed only” list. Native configs have separate observed identities and `config_changed`.
- Membership or native-config change rebuilds affected projections; pure content change does not reattribute. Native-config changes mark affected families for adapter restart.
- `install_freshness` publishes the new inventory. Attribution failure removes/retire-reconciles that family’s adapter and installs its typed error. Native-config restart removes the adapter under explicit pending ownership, exposes `NOT_READY`, stops it, and constructs the replacement; timeout/failure remains typed and retry-owned.
- Watched-file generations advance synchronously before delivery. Created/changed/deleted events are family-scoped; changed open documents are refreshed, bounded newly created files may be opened, and deleted files closed. Queue/submission/delivery failures surface as `BUSY`/`NOT_READY` rather than stale success. Unsafe source/config observation yields `NOT_READY`; exact unavailable-family scope yields its stored typed error.

Current tests:

- `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk`: only `src/**` inventory files are queried, not a sibling; directory candidate override is rejected.
- `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse`: concurrent callers share one result; the next call rebuilds.
- `test_freshness_reports_create_change_delete_and_notifies_running_adapters`: inventory, family-scoped reattribution, watcher delivery, document refresh/open/close, and generation isolation.
- `test_freshness_detects_symlink_substitution_and_native_config_change`: symlink substitution removes/rejects the path; native config change reattributes Python.
- `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope`: unavailable-family status is retained and selected/global scopes fail `SCOPE_INCOMPATIBLE`.

Prevented failure: a directory lookup cannot escape its inventory prefix and inspect a sibling file, while freshness runs before it. Explicit limitation: global discovery is configured-program and candidate bounded; it is not an exhaustive unbounded workspace scan, and output may also truncate by answer budget.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| pre-operation freshness and branches | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime.find_symbol`, `_tool_envelope` | 1692–1776; 2056–2082 | MCP 0-based |
| concurrent reconciliation | same | `FreshnessCoordinator.ensure_fresh` | 726–759 | MCP 0-based |
| change classification | same | `FreshnessCoordinator._scan_git` | 810–904 | MCP 0-based |
| restart/publication semantics | same | `WorkspaceRuntime.install_freshness` | 1242–1372 | MCP 0-based |
| watched delivery | same | `FreshnessCoordinator._apply_events` | 906–1017 | MCP 0-based |
| exact/directory services | `src/serena_light/tools/navigation.py` | two `find_symbol*` methods | 134–237 | MCP 0-based |
| global bounds/readiness | `src/serena_light/tools/global_symbols.py` | `GlobalSymbolService.find_symbol` | 205–387 | MCP 0-based |
| cited tests | `tests/unit/test_workspace_runtime.py`; `tests/unit/test_workspace_runtime_semantics.py` | five tests above | 381–418; 841–907; 1118–1189; 1214–1249 | MCP 0-based |

## Question 2 — Exact-history evidence seam

`prepare_exact_history` materializes verified multimodal inputs and executed prompt IDs, then registers a frozen, `eq=False` public handle in the session’s identity-keyed state map. `_new_exact_history` stores both literal tuple and SHA-256. `extend_exact_history` validates the parent, validates vocabulary IDs, and creates a new history with tuple concatenation—no decode/re-tokenization and no parent mutation. Validation requires the exact live-session object plus matching request, tuple, stored digest, and recomputed digest. Forged or cross-session objects raise `hf_backend.exact_history_session`; closed-session use first raises `hf_backend.session_closed`. `close` clears contexts, registry, model, processor, and tokenizer.

For continuation length `k`, the model receives conditioning plus literal continuation. With `boundary = len(conditioning)-1`, logits positions `[boundary : boundary+k]` are selected: each position predicts its corresponding continuation token. The slice is detached and converted to contiguous CPU FP32. The code establishes FP32 chosen-token evidence and the runner explicitly preserves legacy FP32 subtraction; it gives no broader rationale. `raw_model_logprob` is `log_softmax` over the full selected vocabulary row, gathered at the chosen ID. Rank is `1 + count(logit > chosen_logit)`, so equal logits share rank.

The runner validates manifest schema, unique IDs, prompt/prefix token hashes, and complete-row boundary token. It reconstructs the live prompt/image request, checks image hash, active prompt identity, materialized history equality, then literally appends the manifest prefix. On that identical history it performs two separate one-token calls: `OBJECT_REF_START` and `im_end`. Each scalar is round-tripped through CPU FP32 tensors before computing `row_entry - terminal`, preserving subtraction order and precision. Records include prefix digest, actual prompt-plus-prefix digest, token IDs, both log probabilities and their difference. Runtime provenance includes manifest/config/source hashes, resolved/effective config, model dtype, attention implementation, vision parity, backend receipt, and token IDs. The receipt explicitly says fixed-prefix scores are not free-rollout outcomes.

Current tests:

- immutable prepare/append test proves literal extension, digests, unchanged parent, and no tokenizer decode.
- forged/cross/closed test proves session ownership and no forward execution on invalid handles.
- evidence-position test proves full literal inputs, at-use position derivation, chosen logprobs, tie ranks `[2,2,4]`, and that logits are not exposed.
- empty/invalid test proves fail-fast validation before rope/model forward.
- stable-sharding test proves locality and owner runners select shards identically.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| prepare/append/evidence | `src/inference/hf_backend.py` | three public methods | 127–280 | MCP 0-based |
| session/digest validation | same | `_new_exact_history`, `_validated_exact_history` | 670–715 | MCP 0-based |
| close behavior | same | `HFBackendSession.close`, `_require_live_session` | 323–340; 629–634 | MCP 0-based |
| manifest validation | `scripts/research/run_continuation_locality_boundary_scoring.py` | `_validated_boundaries` | 46–71 | MCP 0-based |
| scoring and receipt | same | `run` | 74–291 | MCP 0-based |
| exact-history tests | `tests/inference/test_hf_exact_history_evidence.py` | four tests above | 214–290; 330–398 | MCP 0-based |
| sharding test | `tests/research/test_continuation_locality_owner_compositionality.py` | stable-sharding test | 37–41 | MCP 0-based |

## Verdicts A–D

- **A — YES.** Live prompt equality, immutable literal append, digest validation, and recorded prefix/full-history digests support fixed-prefix identity.
- **B — YES.** Both selected next tokens are scored from the same validated fixed history with raw full-vocabulary log-softmax values, and their FP32 difference is recorded.
- **C — NO.** Neither candidate is sampled or rolled out; the receipt explicitly disclaims free-rollout interpretation.
- **D — NO.** Only chosen token ID, logprob, and rank are returned; logits/full probabilities are deliberately absent.

## Tool ledger

| tool | purpose | outcome |
|---|---|---|
| shell `sed` | read locked prompt | success; instruction read, not source fallback |
| tool-surface lookup | enumerate Serena Light calls | success |
| `activate_workspace` | bind `serena-light` | success |
| `find_symbol` ×4 | runtime `find_symbol`, envelope, runtime freshness, coordinator | success |
| `get_symbols_overview` | runtime structure | success, truncated; followed by targeted calls |
| `find_symbol` ×3 | coordinator ensure/scan/apply | success |
| `find_symbol` ×3 | exact, directory, global services | success |
| `find_symbol` ×2 | `_route`, `ensure_path_fresh` | success |
| `find_referencing_symbols` ×3 | runtime/freshness references | success |
| `find_symbol` | global substring `fresh` | success |
| `find_symbol` ×5 | Q1 test bodies | success |
| `find_declaration`; `find_symbol` | candidate-limit constant | declaration compact; value resolved as 256 |
| `release_workspace`; `activate_workspace` | switch to research worktree | success |
| `find_symbol` ×3 | prepare/extend/evidence | success |
| transport `wait` | await batched MCP completion | success |
| `find_symbol` ×6 | history/context/new/validation/close/live guard | five success; broad `close` lookup omitted, retried exactly |
| transport `wait` | await batch | success |
| `get_symbols_overview`; `find_symbol` | HF structure, exact close | success |
| `get_symbols_overview`; `find_symbol` | runner structure/body | success |
| `find_symbol` | boundary validator | success |
| `find_referencing_symbols` ×4 | seam references | success |
| `find_symbol` ×3 | exact-history, teacher-forced, locality discovery | success |
| `find_symbol` | `boundary_scoring` discovery | `SYMBOL_NOT_FOUND`; no substitution |
| `get_symbols_overview` | research test structure | success |
| `find_symbol` ×5 | Q2 test bodies | success |
| transport `wait` | await batch | success |
| `get_symbols_overview` | exact-history test inventory | success |
| shell Git command | both HEADs and scoped statuses | success; direct metadata, not fallback |
| shell Git status | disambiguate research relevant status | clean; direct metadata |
| `find_symbol` | install freshness while research bound | `INVALID_PATH`; no evidence used |
| `release_workspace`; `activate_workspace` | switch back to `serena-light` | success |
| `find_symbol` | `install_freshness` | success |

Shell source fallbacks: **none**.

Confidence: **high**. Ambiguities: no tests were run by design; Serena Light files are currently modified, so Q1 describes the live dirty filesystem rather than HEAD. The code documents the FP32 conversion and legacy subtraction behavior, but not a more expansive motivation for CPU placement.
