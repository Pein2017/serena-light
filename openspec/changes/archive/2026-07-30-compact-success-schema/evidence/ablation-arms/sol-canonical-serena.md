# Canonical Serena SOL Arm

Archived verbatim from the locked semantic MCP ablation response; no rescoring or reinterpretation.

## Snapshot

- `/data/CoordExp/serena-light`: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`. Relevant files appearing modified: `runtime.py`, `navigation.py`, `global_symbols.py`, and `test_workspace_runtime_semantics.py`. Relevant freshness tests in `test_workspace_runtime.py` and `test_bounded_freshness_guarded_edit.py` did not appear modified.
- `/data/CoordExp/.worktrees/research-probes`: HEAD `ccdadc4e2d8c00a091dde8d684a14982f05715f2`. None of the four relevant implementation/test files appeared modified.
- Analysis was read-only; tests were inspected, not executed.

## Question 1 — `WorkspaceRuntime.find_symbol`

Every call enters `_tool_envelope`, which calls `ensure_fresh()` before the branch operation. On Git roots this reaches `FreshnessCoordinator.ensure_fresh`; exact-file routing also calls `ensure_path_fresh`, which is a no-op for Git and a serialized targeted stat for allowlisted non-Git roots.

Branch contract:

- Exact file: one normalized, authorized operand; delegates to `DocumentNavigationService.find_symbol`. `_route` raises the attributed family’s installed typed error when that family is unavailable.
- Directory: `inventory.paths_under(scope)` supplies the complete candidate set—no workspace walk. It searches only files whose family has a live adapter through `find_symbol_in_documents`. If selected files exist but all belong to unavailable families, it raises `SCOPE_INCOMPATIBLE`; in a mixed scope, unavailable-family files are excluded while healthy ones are searched. `max_candidates_per_adapter` is rejected outside global scope.
- Global: delegates through `GlobalSymbolService` over one or two healthy adapters and their configured-program scopes. Default candidate bound is 128 per adapter; accepted range is 1–256. Candidate and response truncation are reported. No adapters plus attributed failures yields `SCOPE_INCOMPATIBLE`; a present adapter lacking workspace-symbol support yields `UNSUPPORTED`, and not-ready/generation drift yields `NOT_READY`.

Concurrent Git callers join one `_SharedScan`, wait for its exact result/failure, and do not start another rebuild. Completed results are not time-cached: the next operation scans again. `_scan_git` first retries pending restart/reconcile ownership, rebuilds the lexical inventory, then separates:

- content change: same-member `content_identity` differs;
- membership/symlink change: created/deleted paths or changed rejected-entry set; symlink rejections are reported separately;
- native-config change: observed config identity differs.

Unsafe source/config observation fails `NOT_READY` before installing facts. Membership/config changes rebuild affected family projections; content-only changes do not. Installation publishes the new inventory, reattributes affected families, retires incompatible adapters, and restarts families affected by native config. Tracker generations advance synchronously before delivery. Per-family watched-file batches are owned before submission; created files receive bounded controlled open/close handling. Busy, submission, delivery, stop, and restart failures remain typed; healthy-family facts/events from the same scan may advance even when another family’s restart fails.

Current tests protect distinct invariants:

- `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk`: only inventory members under the prefix are queried.
- `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse`: concurrent sharing, followed by mandatory rescan.
- `test_freshness_detects_symlink_substitution_and_native_config_change`: symlink removal/rejection and config-driven reattribution.
- `test_freshness_runs_before_a_semantic_operation_on_the_real_git_root`: create notification/open/close precedes semantic success.
- `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope`: unavailable attribution remains visible and fails selected/global scope typed.
- `test_config_timeout_does_not_lose_same_scan_healthy_family_events`: one family’s restart timeout does not erase healthy-family generation/event progress.

Prevented failure: a directory query cannot drift into an untracked sibling or return against a pre-scan symlink/member set. Explicit limitation: global lookup is bounded (maximum 256 candidates per adapter plus answer-size truncation), so it is not an exhaustive proof that no further match exists.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| pre-operation reconciliation | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime/_tool_envelope` | 2056–2082 | 0-based |
| three branches/unavailable handling | same | `WorkspaceRuntime/find_symbol` | 1692–1776 | 0-based |
| exact routing | same | `WorkspaceRuntime/_route` | 1642–1668 | 0-based |
| concurrent scan sharing | same | `FreshnessCoordinator/ensure_fresh` | 726–759 | 0-based |
| change classification/install/delivery | same | `FreshnessCoordinator/_scan_git` | 810–904 | 0-based |
| generation and watched delivery | same | `FreshnessCoordinator/_apply_events` | 906–1017 | 0-based |
| adapter restart/reattribution | same | `WorkspaceRuntime/install_freshness` | 1242–1372 | 0-based |
| global bounds | `src/serena_light/tools/global_symbols.py` | constants; `GlobalSymbolService/find_symbol` | 52–53; 205–387 | constants 1-based shell; method 0-based MCP |
| cited tests | `tests/unit/test_workspace_runtime.py` | named tests above | 381–418; 1118–1189; 1492–1563 | 0-based |
| directory/integration tests | `tests/unit/test_workspace_runtime_semantics.py`; `tests/integration/test_bounded_freshness_guarded_edit.py` | named tests above | 1214–1249; 365–386 | 0-based |

## Question 2 — exact-history evidence seam

`HFExactHistory` is frozen and identity-keyed (`eq=False`). `prepare_exact_history` materializes the request’s native multimodal inputs and verified executed prompt IDs, then registers an object-identity-owned state plus canonical ordered-token SHA-256. `extend_exact_history` validates the parent and literal vocabulary IDs, concatenates IDs without decoding/re-tokenizing, and returns a new registered immutable history sharing the live context; the parent is unchanged.

Validation requires a live session, registry membership, exact request/token/digest agreement, and recomputation of the digest. A forged lookalike or history from another session is absent from that session’s registry and raises `hf_backend.exact_history_session`; closed-session use first raises `hf_backend.session_closed`. Closing clears registered contexts and model ownership.

For continuation length `n`, the model receives `history_ids + continuation_ids`. The selected logits are batch 0 at positions `[len(history)-1 : len(history)-1+n]`: first after the fixed history, then after each prior teacher-forced token. They are detached and copied contiguously to CPU FP32, making the returned scalar math explicitly FP32 and removing live device tensors from the evidence object. `raw_model_logprob` is `log_softmax` over the full selected vocabulary row, gathered at the chosen ID. Rank is `1 + count(logit > chosen_logit)`; tied logits share the same rank.

The runner validates manifest schema, nonempty literal IDs, base/prefix digests, complete-row terminator, image hash, and active prompt equality. It prepares the exact base history, verifies its IDs, appends the literal prefix, and independently scores `OBJECT_REF_START` and `im_end` from that same history. It round-trips both returned floats through CPU FP32 tensors and preserves `row_entry - terminal` subtraction exactly. The receipt records manifest/config/source hashes, effective config fingerprint, runtime/model/attention/parity/backend receipts, token IDs, prefix hash, actual prompt-plus-prefix hash, and the explicit claim boundary that fixed-prefix scores are not free-rollout outcomes.

Tests:

- `test_exact_history_prepare_and_literal_immutable_append`: identity digest, immutability, and no decode/re-tokenize.
- `test_exact_history_rejects_forged_cross_session_and_closed_use`: all three ownership/liveness failures occur before forward.
- `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence`: position inputs, FP32 logprobs, tie ranks, and absence of logits.
- `test_teacher_forced_evidence_rejects_empty_or_invalid_before_forward`: fail-fast continuation validation.
- `test_owner_state_score_preserves_existing_reduction_from_token_evidence`: evidence seam reproduces existing terminal/candidate reductions.
- `test_locality_and_owner_runners_use_the_same_stable_sharding`: stable cross-runner shard assignment.

| claim | file | symbol | line range | line convention |
|---|---|---|---:|---|
| prepare/extend identity | `src/inference/hf_backend.py` | `prepare_exact_history`; `extend_exact_history` | 127–161 | 0-based |
| positions/logprob/rank | same | `teacher_forced_evidence` | 163–280 | 0-based |
| registry/digest validation | same | `_new_exact_history`; `_validated_exact_history` | 670–715 | 0-based |
| closed-session cleanup | same | `close`; `_require_live_session` | 323–340; 629–634 | 0-based |
| canonical token digest | `src/inference/backend.py` | `token_ids_sha256` | 25–31 | 0-based |
| runner seam/receipt | `scripts/research/run_continuation_locality_boundary_scoring.py` | `run` | 74–291 | 0-based |
| manifest literal validation | same | `_validated_boundaries` | 46–71 | 0-based |
| core HF tests | `tests/inference/test_hf_exact_history_evidence.py` | named tests above | 214–290; 330–398 | 0-based |
| reduction/sharding tests | `tests/research/test_continuation_locality_owner_compositionality.py` | named tests above | 37–41; 88–194 | 0-based |

## Verdicts A–D

- **A — YES.** Ordered literal IDs are validated, hashed, registry-bound, checked against the materialized prompt, and receipt-hashed after prefix extension.
- **B — YES.** Both selected tokens are scored at the same fixed-prefix logits position; their FP32 logprobs and exact subtraction support relative preference between those two tokens.
- **C — NO.** No token is sampled or rolled forward; the receipt explicitly disclaims free-rollout outcomes.
- **D — NO.** Only chosen-token ID, logprob, and rank are returned. Tests assert no logits field; rank plus one probability cannot reconstruct the vocabulary distribution.

## Tool ledger

1. shell `sed ablation-prompt.md` | setup read | complete; not source fallback.
2. Serena `initial_instructions` | required initialization | complete.
3. Serena `activate_project(serena-light)` | Q1 root | active.
4. shell `git rev-parse/status` | Q1 snapshot | HEAD/status captured.
5–8. Serena `find_symbol` | `find_symbol`, `_tool_envelope`, runtime `ensure_fresh`, coordinator overview | found.
9–11. Serena `find_symbol` | coordinator `ensure_fresh`, `_scan_git`, `_apply_events` | bodies found.
12–14. Serena `find_symbol` | exact, directory, global services | bodies found.
15. Serena `find_symbol` | candidate constant | declaration found, initializer omitted.
16. Serena `find_declaration` | recover initializer | still omitted.
17. Serena `find_referencing_symbols` | recover bound usage | usages found, value absent.
18. shell `sed global_symbols.py` | **fallback:** Serena omitted assignment initializer | value 256 recovered.
19–20. Serena `find_symbol` | `install_freshness`, `_route` | bodies found.
21–23. Serena `find_referencing_symbols` | runtime/freshness call sites | references found.
24–25. Serena `find_symbol` | freshness/directory test discovery | tests found.
26–31. Serena `find_symbol` | six Q1 test bodies | found.
32. Serena `activate_project(research-probes)` | Q2 root | active.
33. shell `git rev-parse/status` | Q2 snapshot | HEAD/status captured.
34–36. Serena `find_symbol` | prepare/extend/evidence | bodies found.
37–42. Serena `find_symbol` | history class, new/validate/close/live/token validation | bodies found.
43. Serena `get_symbols_overview` | runner map | complete.
44–46. Serena `find_symbol` | runner `run`, boundary and ID validation | bodies found.
47–50. Serena `find_referencing_symbols` | three session methods and runner | references found; runner tests not exposed.
51–55. Serena `find_symbol` | exact-history/teacher/locality/boundary test discovery | relevant tests found; exact runner import unresolved.
56. shell `rg` | **fallback:** semantic references missed module import | importing test file found.
57. Serena `get_symbols_overview` | runner-test map | complete.
58–60. Serena `find_symbol` | three runner-related tests | bodies found.
61–65. Serena `find_symbol` | five HF evidence tests | bodies found.
66. Serena `find_symbol` | digest-named test search | no match.
67–68. shell `git status -- relevant paths` | modification check | Q1 four modified; Q2 none.
69–70. Serena `find_symbol` | canonical digest and evidence record | bodies found.
71. Serena `find_symbol` | non-Git targeted freshness | failed: Q1 path under active Q2 root.
72. Serena `activate_project(serena-light)` | restore Q1 root | active.
73–75. Serena `find_symbol` | targeted freshness, global warming, global constructor | bodies found.

Confidence: **high**. Remaining ambiguity: no live test execution was requested or performed, and bounded global truncation means absence of a returned symbol is not exhaustive absence.
