## Snapshot

- `/data/CoordExp/serena-light`: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`. Relevant files `runtime.py`, `navigation.py`, `global_symbols.py`, and `test_workspace_runtime_semantics.py` appeared modified; the worktree also contained unrelated modifications/untracked files.
- `/data/CoordExp/.worktrees/research-probes`: HEAD `ccdadc4e2d8c00a091dde8d684a14982f05715f2`. None of the relevant backend, runner, or cited test files appeared modified, although the worktree was dirty elsewhere.
- Analysis describes the live working trees, not merely HEAD.

## Question 1 — `WorkspaceRuntime.find_symbol`

Every call enters `_tool_envelope`, which invokes `ensure_fresh()` before the operation. The runtime wrapper requires a running runtime and delegates to `FreshnessCoordinator.ensure_fresh`.

Branches:

- Exact file: when `relative_path` is an inventory path, `_route` authorizes it and surfaces its family-local attribution error, then `DocumentNavigationService.find_symbol` loads and searches exactly that document.
- Directory: a non-file scope is resolved only through `inventory.paths_under`; no filesystem walk occurs. The sorted, deduplicated inventory-selected documents are searched by `find_symbol_in_documents`. Files from unavailable families are removed. If every selected file belongs to unavailable families, the result is typed `SCOPE_INCOMPATIBLE`; in a mixed directory, healthy-family files are searched and unavailable-family files are omitted. A nondefault `max_candidates_per_adapter` is rejected because that bound applies only globally.
- Global: only installed adapters participate, each within its configured-program scope. If every attributed family is unavailable, `SCOPE_INCOMPATIBLE` is returned; partial unavailable families are absent from the provider set. Each active adapter supplies at most the requested candidate cap (default 128; accepted range 1–256), followed by document-symbol verification. Provider omissions and output-budget omissions are reported as truncation. Unsupported workspace-symbol capability yields `UNSUPPORTED`; readiness/generation drift yields retryable `NOT_READY`.

Freshness coordination:

- Concurrent callers share exactly one `_SharedScan`; joiners wait and receive the same result object or failure. A finished scan is not time-cached: the next operation scans again.
- `_scan_git` first retries pending adapter restarts and watched-file reconciliations. It rebuilds the lexical inventory, classifying created/deleted paths and rejected symlink membership. For surviving files, targeted content identities identify content changes independently of membership. Native-config identities are tracked separately.
- Unsafe source/config observation fails `NOT_READY` before freshness state is committed.
- Pure content changes do not rebuild projections; they advance family generations and deliver changed-file notifications.
- Membership or symlink changes replace inventory and reattribute affected families. Attribution failures install family-local unavailable state while healthy-family facts advance.
- Native-config changes reattribute affected families and restart their adapters before new readiness. Restart ownership is published as `NOT_READY`; stop timeouts/failures remain typed and retryable through pending restart state.
- `_apply_events` invalidates all affected trackers before notification submission. Running adapters receive family-scoped watched-file batches; created files are opened only up to the controlled-open limit. Submission/delivery failure becomes `BUSY` or `NOT_READY`, preventing stale success. The code does not promise delivery to non-running adapters.

Current tests and invariants:

1. `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse`: concurrent callers share one scan; later calls rebuild.
2. `test_freshness_reports_create_change_delete_and_notifies_running_adapters`: inventory, family-scoped reattribution, generations, watcher delivery, refresh/open/close behavior.
3. `test_freshness_detects_symlink_substitution_and_native_config_change`: symlink substitution removes the path; config creation is separately reported and reattributed.
4. `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk`: only `src/**` inventory files are loaded; siblings are excluded.
5. `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope`: unavailable attribution remains observable and selected/global semantic scope fails typed.

Prevented failure: a directory query for `src` cannot escape into an inventory sibling such as `sibling/c.py`.

Explicit limitation: a global success may be truncated by per-adapter candidate or answer budgets and therefore is not necessarily exhaustive.

| claim | file | symbol | line range | line convention |
|---|---|---|---|---|
| pre-operation freshness; branches | `src/serena_light/workspace/runtime.py` | `_tool_envelope`; `find_symbol` | 2060–2086; 1694–1780 | MCP 0-based |
| exact-family routing | same | `_route` | 1644–1670 | MCP 0-based |
| directory search | `src/serena_light/tools/navigation.py` | `find_symbol_in_documents` | 160–243 | MCP 0-based |
| global bounds/verification | `src/serena_light/tools/global_symbols.py` | `find_symbol` | 205–387 | MCP 0-based |
| numeric global cap | same | constant assignment | 53 | shell 1-based |
| concurrent scan | `src/serena_light/workspace/runtime.py` | `FreshnessCoordinator.ensure_fresh` | 728–761 | MCP 0-based |
| change classification/install | same | `_scan_git`; `install_freshness` | 812–906; 1244–1374 | MCP 0-based |
| watcher consequences | same | `_apply_events` | 908–1019 | MCP 0-based |
| cited tests | `tests/unit/test_workspace_runtime.py` | four named tests above | 381–418; 841–907; 1118–1158; 1161–1189 | MCP 0-based |
| directory test | `tests/unit/test_workspace_runtime_semantics.py` | named test above | 1377–1412 | MCP 0-based |

## Question 2 — Exact-history evidence seam

`prepare_exact_history` materializes the multimodal request, validates the executed prompt IDs against vocabulary size, retains native multimodal tensors in a session-private context, and registers a frozen public `HFExactHistory` by object identity. Its ordered token tuple and canonical JSON-token SHA-256 are duplicated in private state.

`extend_exact_history` validates the parent against that registry, validates literal vocabulary IDs, and creates a new registered history containing tuple concatenation. It never decodes or re-tokenizes and does not mutate the parent. Forged equal-valued objects and histories from another session lack registry identity and fail `hf_backend.exact_history_session`; altered fields/digest/content also fail validation. Closed sessions fail `hf_backend.session_closed`, clear the registry/context, and cannot be reused.

For a continuation of length \(N\), `teacher_forced_evidence` forwards the exact history plus continuation, then selects logits positions:

`len(history.conditioning_token_ids) - 1 : + N`

These are the causal positions predicting each continuation token. The slice is detached and converted to contiguous CPU FP32, establishing the method’s advertised raw-FP32 evidence contract and FP32 scalar calculations independent of model storage device/dtype. For each selected token:

- `raw_model_logprob = log_softmax(selected_logits)[token_id]`.
- `candidate_vocab_rank = 1 + count(logit > selected_token_logit)`.
- Because comparison is strictly greater, equal logits receive the same rank.

The runner validates manifest schema, nonempty literal prompt/prefix IDs, their hashes, and the complete-row terminal marker. It independently verifies image hash, active prompt IDs, and executed materialized prompt equality. It extends the history by the literal prefix, then makes two separate one-token evidence calls from the same fixed history: `OBJECT_REF_START` and `im_end`. Thus neither candidate conditions the other.

It converts both returned Python logprobs back through FP32 tensors and performs the legacy subtraction there before scalar extraction. The receipt records both IDs/logprobs/difference, prefix and prompt-plus-prefix hashes, manifest/config/source hashes, resolved/effective configuration identities, observed model dtype and attention implementation, vision parity, backend receipt, shard selection, and the explicit claim boundary: fixed-prefix scores are not free-rollout outcomes. Vocabulary ranks are computed by the API but not recorded by this runner.

Tests:

1. `test_exact_history_prepare_and_literal_immutable_append`: digest identity, immutable literal append, no decoding/re-tokenization, tensor-free public handle.
2. `test_exact_history_rejects_forged_cross_session_and_closed_use`: all three ownership failures occur before model forward.
3. `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence`: correct literal input sequence, logprobs, tie ranks, and evidence-only return.
4. `test_teacher_forced_evidence_rejects_empty_or_invalid_before_forward`: fail-fast continuation validation.
5. `test_complete_row_prefixes_preserve_every_literal_boundary`: complete-row prefixes preserve literal cumulative boundaries.
6. `test_locality_and_owner_runners_use_the_same_stable_sharding`: deterministic shared shard assignment.

| claim | file | symbol | line range | line convention |
|---|---|---|---|---|
| prepare/extend/evidence | `src/inference/hf_backend.py` | three public methods | 127–280 | MCP 0-based |
| immutable public identity | same | `HFExactHistory` | 55–61 | MCP 0-based |
| registry/digest validation | same | `_new_exact_history`; `_validated_exact_history` | 670–715 | MCP 0-based |
| closed behavior | same | `close`; `_require_live_session` | 323–340; 629–634 | MCP 0-based |
| canonical digest | `src/inference/backend.py` | `token_ids_sha256` | 25–31 | MCP 0-based |
| runner seam/receipt | `scripts/research/run_continuation_locality_boundary_scoring.py` | `run` | 74–291 | MCP 0-based |
| manifest validation | same | `_validated_boundaries` | 46–71 | MCP 0-based |
| backend tests | `tests/inference/test_hf_exact_history_evidence.py` | four named tests | 214–243; 262–290; 330–398 | MCP 0-based |
| research tests | `tests/research/test_continuation_locality_owner_compositionality.py` | two named tests | 30–41 | MCP 0-based |

## Verdicts A–D

- **A — YES.** Literal prompt equality, literal tuple extension, digest validation, and receipt hashes support fixed-prefix identity.
- **B — YES.** Both chosen tokens are scored independently from the identical fixed prefix; their FP32 logprobs and subtraction support relative preference between those two tokens.
- **C — NO.** No token is sampled or autoregressively rolled out; the receipt explicitly disclaims free-rollout conclusions.
- **D — NO.** Only chosen-token logprobs/IDs and their difference are retained; the full logits/probability vector is intentionally absent.

## Ordered tool ledger

1. shell `sed` — read locked prompt — success, required instruction artifact.
2. tool registry inspection — locate canonical Serena — success.
3. Serena `initial_instructions`; `activate_project(serena-light)` — initialized/activated.
4. shell Git HEAD/status — snapshot metadata — success.
5. Serena `find_symbol`: `find_symbol`, `_tool_envelope`, runtime `ensure_fresh`; class overview — success.
6. Serena `find_symbol`: coordinator `ensure_fresh`, `_scan_git`, `_apply_events`, `install_freshness` — success.
7. Serena `find_symbol`: document exact/directory and global services — success.
8. Serena references: runtime `find_symbol`, coordinator/runtime freshness — success.
9. Serena `find_symbol` ×5 — Q1 test bodies — success.
10. Serena `activate_project(research-probes)` — success.
11. Serena `find_symbol` ×3 — exact-history public seam — success.
12. Serena `find_symbol` ×5 — identity/validation/close internals — success.
13. Serena `find_symbol` ×2 — token validation/digest — success.
14. Serena overview + `find_symbol` ×3 — runner and helpers — success.
15. Serena references ×3 — production callers — success.
16. Serena overview expected runner-test path — file absent.
17. shell `rg` — **fallback:** Serena lacks repository text/file search and expected test path was absent; located tests — success.
18. Serena overviews ×2; `find_symbol` ×6 — test inventory/bodies — success.
19. shell scoped Git status — relevant-file modification snapshot — success.
20. Serena `find_symbol` ×2 while wrong project active — failed path lookup; no substitution.
21. Serena `activate_project(serena-light)`; `find_symbol` `_route`, `_warm_global_candidates` — success.
22. Serena `find_symbol` constant — location only; value omitted.
23. shell `sed` — **fallback:** Serena returned the constant name/location but not assignment value; recovered cap 256 — success.

Confidence: **high** on live control flow and receipt semantics. No tests were executed because the benchmark forbids state mutation; cited tests were read only. Remaining ambiguity: the code performs CPU-FP32 conversion and advertises FP32 evidence, but gives no more specific rationale for choosing CPU beyond that observable contract.
