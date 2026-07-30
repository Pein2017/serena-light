## Snapshot

- `/data/CoordExp/serena-light`: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`. Relevant files modified: `workspace/runtime.py`, `tools/navigation.py`, `tools/global_symbols.py`, and `test_workspace_runtime_semantics.py`; `test_workspace_runtime.py` was clean. Serena Light snapshot SHA examples: runtime `2a34c8b…`, navigation `4a54384…`, global symbols `ea814a7…`.
- `/data/CoordExp/.worktrees/research-probes`: HEAD `ccdadc4e2d8c00a091dde8d684a14982f05715f2`. None of the relevant backend, runner, or cited test files appeared modified. Snapshot SHAs: `hf_backend.py` `e21beb1…`, runner `747a4ef…`, exact-history tests `5b90b6b…`.
- Final Serena Light runtime status for research-probes: clean freshness result; Git trust inventory 1,067 paths, SHA `cd529e89…`; Python configured program 1,061/1,065 trusted Python files. The workspace was released before completion.

## Question 1 — `WorkspaceRuntime.find_symbol`

Freshness occurs in `_tool_envelope`: `ensure_fresh()` completes before the operation closure runs. Workspace errors become typed tool envelopes; `BUSY`, `NOT_READY`, and `TIMED_OUT` are retryable.

Branches:

- Exact file: when `relative_path` is an inventory member, `_route` selects its attributed family, then `DocumentNavigationService.find_symbol` loads exactly that document. A non-default `max_candidates_per_adapter` is rejected because that option is global-only. An unavailable selected family fails through routing; scope is not widened.
- Directory: a non-file scope is resolved only through `inventory.paths_under`; no filesystem walk occurs. `DocumentNavigationService.find_symbol_in_documents` loads the sorted, deduplicated, inventory-selected documents. Files whose family lacks an adapter are excluded. If all selected files belong to unavailable families, it raises `SCOPE_INCOMPATIBLE` with attributed failure paths. In a mixed directory, available families are searched and unavailable-family files are omitted.
- Global: `_warm_global_candidates` prepares readiness witnesses, then `GlobalSymbolService` queries one `_GlobalProvider` per available adapter over each configured program. Default bound is 128 candidates per adapter; accepted range is 1–256. Candidates from `workspace/symbol` are revalidated against document symbols and generation identity. All-unavailable attribution produces `SCOPE_INCOMPATIBLE`; with at least one healthy adapter, only healthy adapters participate. Unsupported workspace-symbol capability, non-readiness, generation drift, body incompleteness, and response-size truncation remain explicit typed outcomes.

Freshness coordination:

`FreshnessCoordinator.ensure_fresh` permits one in-flight Git scan. Concurrent callers wait for and receive the same result or failure; a completed scan is not time-cached, so the next operation scans again.

`_scan_git` first settles pending restart/reconcile ownership, rebuilds the trust inventory, and compares:

- content identities for retained paths (`changed`);
- created/deleted membership plus rejected-entry changes, including symlink rejection;
- native-config content identities (`config_changed`).

Unsafe observations fail `NOT_READY` before freshness state is committed. Membership/config changes reattribute affected families and publish the rebuilt inventory. Attribution failures retire that family’s adapter and remain typed while healthy families can advance. Native-config changes force the affected adapter through explicit `NOT_READY` restart ownership; stop timeout/failure remains pending and is retried before a later unchanged scan can succeed. Created/changed/deleted events first advance per-family generations, then are delivered to running adapters; controlled opens apply only to created files. Admission/delivery failures surface as `BUSY` or `NOT_READY`, preventing stale success.

Tests:

- `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk`: searches only `src/**`, excludes a sibling, and rejects global-only candidate tuning.
- `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse`: concurrent callers share one scan object; the next call rebuilds.
- `test_freshness_detects_symlink_substitution_and_native_config_change`: symlink substitution removes the file from inventory, records rejection, and native config reattributes Python.
- `test_same_stat_native_config_rewrite_restarts_the_running_adapter`: content identity detects a rewrite despite fixed stat times and replaces the adapter.
- `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope`: unavailable attribution remains visible and selected/global operations fail `SCOPE_INCOMPATIBLE`.

Prevented failure: a trusted file replaced by a symlink cannot remain searchable from stale inventory.

Explicit limitation: global discovery is candidate- and response-bounded, so it is not guaranteed exhaustive; omitted results are represented by truncation metadata.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| freshness precedes operation | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime/_tool_envelope` | 2060–2086 | MCP 0-based |
| three branches and unavailable-family handling | same | `WorkspaceRuntime/find_symbol` | 1694–1780 | MCP 0-based |
| concurrent scan ownership | same | `FreshnessCoordinator/ensure_fresh` | 728–761 | MCP 0-based |
| change classification/reconciliation | same | `FreshnessCoordinator/_scan_git` | 812–906 | MCP 0-based |
| adapter reattribution/restart | same | `WorkspaceRuntime/install_freshness` | 1244–1374 | MCP 0-based |
| watched delivery | same | `FreshnessCoordinator/_apply_events` | 908–1019 | MCP 0-based |
| directory bound | `tests/unit/test_workspace_runtime_semantics.py` | named directory test | 1377–1412 | MCP 0-based |
| concurrency/symlink/config tests | `tests/unit/test_workspace_runtime.py` | named tests | 1118–1189, 1301–1335 | MCP 0-based |

## Question 2 — Exact-history evidence seam

`HFExactHistory` is frozen and `eq=False`; each session stores private state in a `WeakKeyDictionary` keyed by object identity. `prepare_exact_history` materializes verified multimodal inputs and executed prompt IDs. `extend_exact_history` validates the originating live-session object and vocabulary IDs, then creates a new history containing the literal appended IDs, sharing native context without decoding or re-tokenizing; the parent remains unchanged.

Validation compares request ID, token tuple, stored digest, and a recomputed canonical SHA-256 against private state. A forged equal-valued object and cross-session object are absent from the owning registry and fail `hf_backend.exact_history_session`. Closed-session use fails `hf_backend.session_closed`; close clears registries, native context, and model references.

For conditioning length `L` and continuation length `N`, logits `[0, L-1:L-1+N, :]` are selected: position `L-1` predicts continuation token 0, the next position predicts token 1, and so on. They are detached and converted to contiguous CPU FP32 before calculation, making the returned scalars FP32 even if model execution used another dtype. `raw_model_logprob` is `log_softmax` gathered at the selected token ID. Rank is `1 + count(logit > selected_logit)`; ties are not counted as greater, so tied tokens share rank.

The runner validates manifest schema, unique IDs, prompt/prefix digests, and a complete-row terminal prefix. It reconstructs image and active prompt, verifies image hash/grid and exact materialized base-prompt IDs, then literally extends with `prefix_token_ids`. From the same fixed history it separately scores `OBJECT_REF_START` and `im_end`. It converts both returned Python floats back through FP32 tensors and subtracts in FP32, preserving the legacy scalar round-trip.

The receipt records both token IDs, both log probabilities, their difference, prefix SHA, actual prompt-plus-prefix SHA, manifest/config/source hashes, resolved/effective configuration identity, backend receipt, model dtype, attention implementation, vision parity, and the explicit claim boundary: fixed-prefix scores are not free-rollout outcomes.

Tests:

- `test_exact_history_prepare_and_literal_immutable_append`: immutable literal append, digest identity, no tokenizer decode.
- `test_exact_history_rejects_forged_cross_session_and_closed_use`: all three ownership/lifetime failures occur before forward.
- `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence`: position construction, multi-step alignment, FP32 log-probabilities, tie ranks, and no logits exposure.
- `test_exact_history_rejects_unverifiable_vocabulary_before_forward`: absent vocabulary authority fails before inference.
- `test_exact_history_registry_releases_dead_cases_and_clears_live_context`: weak ownership and close-time context invalidation.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| identity structures | `src/inference/hf_backend.py` | `HFExactHistory`, `HFBackendSession/__init__` | 55–61, 91–111 | MCP 0-based |
| prepare/immutable extension | same | `prepare_exact_history`, `extend_exact_history` | 127–161 | MCP 0-based |
| logits/evidence computation | same | `teacher_forced_evidence` | 163–280 | MCP 0-based |
| digest/session validation | same | `_new_exact_history`, `_validated_exact_history` | 670–715 | MCP 0-based |
| manifest and receipt seam | `scripts/research/run_continuation_locality_boundary_scoring.py` | `_validated_boundaries`, `run` | 46–71, 74–291 | MCP 0-based |
| ownership/evidence tests | `tests/inference/test_hf_exact_history_evidence.py` | named tests | 214–327, 330–380 | MCP 0-based |

## Verdicts A–D

- A — **YES.** Manifest digests, active-prompt equality, session identity, literal extension, and actual combined-history SHA support literal fixed-prefix identity.
- B — **YES.** Both selected next tokens are scored from the same fixed history, and their FP32 log-probability difference is recorded.
- C — **NO.** No token is sampled or rolled out; the receipt explicitly disclaims free-rollout interpretation.
- D — **NO.** Only selected-token scalars are returned, and the runner records two of them. Logits/full probabilities are deliberately absent.

## Ordered tool ledger

`tool | purpose | outcome`

1. Tool registry | locate Serena Light only | found.
2. Shell `sed` | read authoritative prompt | success; instruction file, not source fallback.
3. Shell Git HEAD/status | Q1 snapshot | success.
4. `activate_workspace` | bind Q1 | success.
5–9. `find_symbol` | `find_symbol`, envelope, runtime/coordinator freshness, `_scan_git` | success.
10–13. `find_symbol` | install, event delivery, restart retry, projections | success.
14–17. `find_symbol` | exact/directory/global services and warming | success.
18. `find_declaration` | candidate constant | declaration omitted value.
19. `find_symbol` | candidate constant body | `256`.
20–22. `find_referencing_symbols` | runtime `find_symbol` references | two `NOT_READY`, then success.
23–24. `find_referencing_symbols` | freshness references | success.
25–29. `find_symbol` | five Q1 test bodies | success.
30. `release_workspace` | release Q1 | success.
31. Shell Git HEAD/status | Q2 snapshot | success.
32. `activate_workspace` | bind Q2 | success.
33–38. `find_symbol` | prepare, extend, evidence, creation, validation, live check | success.
39–42. `find_symbol` | history/evidence classes, close, digest lookup | three success; local digest lookup not found.
43. Global `find_symbol` | digest implementation | success.
44. `get_symbols_overview` | runner structure | success.
45–46. `find_symbol` | runner and manifest validation | success.
47–50. `find_referencing_symbols` | seam references | one `NOT_READY`; remaining success.
51–52. Global `find_symbol` | discover exact-history/evidence tests | success.
53. Global `find_symbol` | discover locality tests | not found.
54. `get_symbols_overview` | guessed runner-test path | invalid path.
55. Shell `rg --files` | **fallback:** MCP lacks file enumeration | found locality test.
56–60. `find_symbol` | five backend test bodies | success.
61. Shell `rg -n` | **fallback:** semantic references missed dynamic runner import | blocked by read-hook; output line convention would be shell 1-based.
62. Shell `rg -n` retry | same fallback | success, 1-based output.
63–65. `get_symbols_overview`/`find_symbol` | locality test inventory and two bodies | success.
66–69. `find_symbol` | registry/context/token validation | success.
70. Shell scoped Git status | exact relevant-file modification check | success.
71. `get_runtime_status` | final semantic snapshot | success.
72. `release_workspace` | release Q2 | success.

**Confidence:** High on implementation and test contracts; medium-high on completeness of runner-specific tests because no direct end-to-end test of this exact runner was found. No files, repositories, or processes were modified.
