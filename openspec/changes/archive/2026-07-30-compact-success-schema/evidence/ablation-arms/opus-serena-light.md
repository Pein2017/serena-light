# Opus 5 High — Serena Light

This is the retained outer-assistant response from the locked read-only arm.
The answer below is preserved verbatim; the heading and this note are archival
metadata, not participant output.

---

## Snapshot

| target | Git HEAD | working tree |
|---|---|---|
| `/data/CoordExp/serena-light` | `9e4987e9f2190a4ff03cb7a35359483a5387f327` | dirty. Q1 files **modified**: `src/serena_light/workspace/runtime.py`, `src/serena_light/tools/navigation.py`, `src/serena_light/tools/global_symbols.py`, `tests/unit/test_workspace_runtime_semantics.py`. **Unmodified**: `tests/unit/test_workspace_runtime.py`, `tests/integration/test_bounded_freshness_guarded_edit.py` |
| `/data/CoordExp/.worktrees/research-probes` | `ccdadc4e2d8c00a091dde8d684a14982f05715f2` | dirty overall, but **every Q2 file is unmodified**: `src/inference/hf_backend.py`, `scripts/research/run_continuation_locality_boundary_scoring.py`, `scripts/research/run_next_row_likelihood_change.py`, `tests/inference/test_hf_exact_history_evidence.py`, `tests/research/test_continuation_locality_owner_compositionality.py`, `tests/research/test_complete_candidate_row_scoring.py` |

All line numbers below are **0-based** (serena-light MCP convention).

---

## Question 1 — `WorkspaceRuntime.find_symbol` contract

**1. Freshness before the operation.** The whole body is a closure `operation()` returned through `self._tool_envelope(operation)` (1776). `_tool_envelope` (2056–2082) calls `self.ensure_fresh()` *first*, then `operation()`, so no branch can run against an unreconciled workspace. `ensure_fresh` (1200–1204) asserts `_require_running()` and delegates to `FreshnessCoordinator.ensure_fresh` (726–759). A second, targeted reconciliation happens inside `_route` (1642–1668), which calls `self._freshness.ensure_path_fresh(normalized)`; that path is a no-op on Git roots and is the *only* freshness mechanism on allowlisted read-only non-Git roots (761–795).

**2. The three branches.**

*Exact file* — `relative_path` is an inventory path. `max_candidates_per_adapter != 128` is rejected up front as `UNSUPPORTED` (`max_candidates_per_adapter_applies_only_to_global_scope`). Then `self._route(scope)` authorizes the operand through `WorkspacePathPolicy`, and delegates to `DocumentNavigationService.find_symbol` (navigation.py 134–156) — exactly one document, one name-path resolution. If the path's family is in `_family_errors`, `_route` re-raises that stored typed error (unavailable family ⇒ the operation fails, it does not degrade).

*Inventory-bounded directory* — scope not in `inventory.paths` but `inventory.paths_under(scope)` non-empty. Candidates are narrowed to `available = paths whose _family_of(path) is in self._adapters`. If that set is empty, it raises `SCOPE_INCOMPATIBLE` ("directory scope contains only unavailable language families") whose `paths` are drawn from `_family_errors[family].paths` for every family present in the scope. Otherwise `DocumentNavigationService.find_symbol_in_documents` (navigation.py 158–237) loads *only* the enumerated documents — no workspace walk — sorts matches by `(relative_path, symbol order)`, fails closed on incomplete bodies when `include_body`, and truncates by `max_answer_chars` with an omitted count. Note the asymmetry: a directory containing *some* unavailable families silently searches only the available ones. If `paths_under` raises `ValueError`, `_route(scope)` is invoked to produce the typed policy error (an `AssertionError` guards the impossible acceptance).

*Global* — `relative_path is None`. If `not self._adapters and self._family_errors`, it raises `SCOPE_INCOMPATIBLE` over the union of all failing families' paths. Otherwise `_warm_global_candidates` (1778–1859) polls only non-ready adapters inside a shared budget `min(30.0, future_timeout)` minus 0.5 s, round-robin ordered by configured-program size with a per-family turn deadline, using the first symbol of one deterministic configured-program document as the readiness witness; LSP response/protocol/transport errors propagate, everything else re-queues. Then `GlobalSymbolService.find_symbol` (global_symbols.py 205–387) runs one `_GlobalProvider` per *available* adapter: bound is `max_candidates_per_adapter` (1…`MAX_CANDIDATES_PER_ADAPTER`, default 128) per adapter, scope is each adapter's configured program. It returns `UNSUPPORTED` if any adapter lacks `workspace/symbol`, `NOT_READY` (with `retry_after_seconds`) if any adapter is not globally ready, and `NOT_READY` on any generation drift observed at batch, document, or the post-loop re-check.

**3. `ensure_fresh` / `_scan_git` coordination.** `ensure_fresh` publishes one `_SharedScan` under `_lock`. The first caller owns it; late callers wait on `shared.done` and receive the *identical* result object or re-raise the identical exception. `finally` clears `_in_flight` and updates `_last` only on success — a completed scan is never reused, so the next operation rescans.

`_scan_git` (810–904) first calls `runtime.retry_pending_restarts()` and `self._settle_pending_reconciles()`, so an unsettled cleanup blocks a new authorization even when nothing changed. It then distinguishes:

- **Content**: `changed` = inventory paths whose `content_identity` differs from `self._states`. Emits `CHANGED` watched events.
- **Membership / symlink**: `created`/`deleted` from the path-set diff; `symlinked` from `rebuilt.rejected` entries whose reason starts with `symlink`; `membership_changed` is also true when the rejected sets differ.
- **Native config**: `config_changed` from `_config_states_for`; `restart_families = _affected_families((), config_changed)`.

Unsafe observation (an untrusted source state, or a config state with a reason other than `missing`) raises `NOT_READY` **before** `_states`/`_config_states` are committed. If nothing changed at all, an empty `FreshnessScan()` is returned.

Observable consequences. *Inventory*: swapped atomically under `_state_lock` inside `install_freshness` (1242–1372); symlinked paths leave `inventory.paths` and appear in `inventory.rejected`. *Adapter restart/reattribution*: for a config-affected family the adapter is popped, a `_PendingAdapterRestart` is published as one atomic ownership publication, and `_family_errors[family]` is set to `NOT_READY "adapter restart is in progress"` — so that family cannot report readiness against stale native config, while healthy families keep serving; stop-future timeout becomes `TIMED_OUT`, stop failure `UNSUPPORTED`; attribution errors retire the adapter as `SCOPE_INCOMPATIBLE`. *Watched-file delivery*: `_apply_events` (906–1017) is strictly two-phase — every affected family's generations are advanced unconditionally first, then per-family ownership is published in `_pending_reconciles` (duplicate ownership ⇒ `NOT_READY`), then each batch is admitted (executor full ⇒ `BUSY`, other submission failure ⇒ `NOT_READY`) before any family is awaited. When installation already failed, `wait_for_delivery=False`: healthy-family events are still enqueued exactly once and the install failure is raised only after the scan facts are recorded. *Typed failures*: `_tool_envelope` maps `WorkspaceRuntimeError` codes to envelope codes and attaches `RetryMetadata(retryable=True)` for `BUSY`/`NOT_READY`/`TIMED_OUT`; `TimeoutError` keeps `TIMED_OUT`; other `OSError`/`TypeError`/`ValueError` become `INVALID_INPUT`.

**4. Current tests.**

- `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse` (test_workspace_runtime.py 1118–1158) — a joined caller never starts a second rebuild, both get the *same* object, and a completed scan is never cached for reuse.
- `test_freshness_detects_symlink_substitution_and_native_config_change` (1161–1189) — symlink substitution yields `symlinked`+`deleted`, drops the path from inventory into `rejected`; a new `pyrightconfig.json` yields `config_changed` **and** `reattributed`.
- `test_same_stat_source_rewrite_is_reconciled_before_a_semantic_result` (1192–1230) — identity is content-based, not stat-time-based: a same-mtime rewrite is still `changed` and the adapter receives the new full text.
- `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk` (test_workspace_runtime_semantics.py 1214–1249) — exactly the two in-scope documents are requested, the sibling directory is never touched, and `max_candidates_per_adapter` in directory scope is `UNSUPPORTED` with no extra document request.
- `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope` (test_workspace_runtime.py 381–418) — an all-unavailable workspace still binds for `status()`, and global `find_symbol` returns `SCOPE_INCOMPATIBLE`.

**5.** Prevented failure: a same-mtime, same-size in-place rewrite (or a source file swapped for a symlink) can no longer be answered from an adapter's stale open document — content-identity reconciliation and the watched-file/generation barrier force a fresh document or a typed `NOT_READY` instead of a stale symbol range. Remaining limitation: the directory branch *silently narrows* to available families — only a scope with **no** available family raises `SCOPE_INCOMPATIBLE`; likewise the global branch searches only running adapters and raises only when every family is unavailable, so a partially degraded workspace returns a successful, quietly incomplete result set.

### Q1 evidence table

| claim | file | symbol | line range | convention |
|---|---|---|---|---|
| operation wrapped in envelope | src/serena_light/workspace/runtime.py | `WorkspaceRuntime/find_symbol` | 1692–1776 | 0-based |
| `ensure_fresh()` runs before `operation()` | src/serena_light/workspace/runtime.py | `WorkspaceRuntime/_tool_envelope` | 2056–2082 | 0-based |
| shared single in-flight scan | src/serena_light/workspace/runtime.py | `FreshnessCoordinator/ensure_fresh` | 726–759 | 0-based |
| targeted stat on non-Git root | src/serena_light/workspace/runtime.py | `FreshnessCoordinator/ensure_path_fresh` | 761–795 | 0-based |
| change classification + restart trigger | src/serena_light/workspace/runtime.py | `FreshnessCoordinator/_scan_git` | 810–904 | 0-based |
| two-phase watched delivery | src/serena_light/workspace/runtime.py | `FreshnessCoordinator/_apply_events` | 906–1017 | 0-based |
| restart/retire + typed family errors | src/serena_light/workspace/runtime.py | `WorkspaceRuntime/install_freshness` | 1242–1372 | 0-based |
| routing raises stored family error | src/serena_light/workspace/runtime.py | `WorkspaceRuntime/_route` | 1642–1668 | 0-based |
| bounded global warm | src/serena_light/workspace/runtime.py | `WorkspaceRuntime/_warm_global_candidates` | 1778–1859 | 0-based |
| single-document branch | src/serena_light/tools/navigation.py | `DocumentNavigationService/find_symbol` | 134–156 | 0-based |
| enumerated-document branch | src/serena_light/tools/navigation.py | `DocumentNavigationService/find_symbol_in_documents` | 158–237 | 0-based |
| per-adapter candidate bound, readiness/generation gates | src/serena_light/tools/global_symbols.py | `GlobalSymbolService/find_symbol` | 205–387 | 0-based |

---

## Question 2 — exact-history evidence seam

**1. Identity, immutability, digests, failure modes.** `prepare_exact_history` (hf_backend.py 127–146) requires a live session, materializes native inputs once, validates the executed prompt IDs, wraps them in `_HFExactHistoryContext(native_inputs=…)`, and registers state via `_new_exact_history` (670–689), which stores `(request_id, conditioning_token_ids, sha256, context)` in a **`WeakKeyDictionary`** keyed by the returned `HFExactHistory` (`__init__` 91–111). `extend_exact_history` (148–161) validates the parent, validates appended IDs against the vocabulary, and returns a **new** history sharing the same context — no decoding, no re-tokenizing, parent untouched. `_validated_exact_history` (691–715) rejects anything not registered in this session, and re-derives `token_ids_sha256(history.conditioning_token_ids)`, so a forged object with copied fields, a history used on another session, and a tampered token tuple all fail as `hf_backend.exact_history_session`. `_require_live_session` on a closed session fails as `hf_backend.session_closed`; `close` (323–340) additionally nulls each distinct `context.native_inputs` and clears the registry, so even a retained handle loses its multimodal context (`hf_backend.exact_history_session`). `_validated_token_ids` (636–668) rejects bools, non-ints, negatives and out-of-vocabulary IDs *before* any forward pass, and fails as `hf_backend.vocab_size_unavailable` when the vocabulary cannot be established.

**2. Position selection, CPU FP32, logprob and rank.** `teacher_forced_evidence` (163–280) builds `full_token_ids = conditioning + continuation`, an all-ones attention mask, derives Qwen rope positions at use time from the retained `image_grid_thw`, drops cache-bearing keys, and runs one forward with `use_cache=False`, `return_dict=True`, `logits_to_keep=0` under `torch.inference_mode()`. Selection is `boundary = len(conditioning) - 1` and the slice `logits[0, boundary : boundary + len(continuation), :]` — i.e. the next-token distributions *predicting* each continuation token, teacher-forced. That slice is `.detach().to(device="cpu", dtype=torch.float32).contiguous()`: FP32 on CPU makes `log_softmax` numerically stable and reproducible independently of the model's compute dtype/device, and detaching to host memory bounds retained GPU memory. A shape mismatch raises `hf_backend.teacher_forced_alignment`. `raw_model_logprob` = `F.log_softmax(selected_logits, -1).gather(1, ids)` — a full-vocabulary normalized log-probability. `candidate_vocab_rank` = `(selected_logits > selected_values).sum(dim=1) + 1` — **strictly-greater** counting, so ties share the same (best) rank and ranks are not dense. Only `HFChosenTokenEvidence(token_id, raw_model_logprob, candidate_vocab_rank)` escapes; no logits, no tensors.

**3. Boundary-scoring runner.** `run` (run_continuation_locality_boundary_scoring.py 74–291) validates the manifest schema, boundary uniqueness, `base_prompt`/`prefix` digests, and that each prefix ends in `151649` (complete-row boundary) via `_validated_boundaries` (46–71); shards deterministically and refuses to overwrite an existing receipt. Per boundary it re-plans the image, re-derives the prompt record, and hard-fails on image-hash, active-prompt, grid-rank, or materialized-prompt mismatch. It then calls `prepare_exact_history`, `extend_exact_history(history, prefix_token_ids)`, and **two single-token** `teacher_forced_evidence` calls on the *same* history — `(OBJECT_REF_START,)` and `(terminal_id,)` — both therefore scored at the identical boundary position. Subtraction semantics are preserved by round-tripping each scalar through a `torch.float32` tensor and emitting `row_entry_log_probability`, `terminal_log_probability`, `row_entry_minus_terminal`, matching `terminal_boundary_score` (run_next_row_likelihood_change.py 225–241) field for field. Provenance recorded: manifest path+sha256, prefix and prompt+prefix digests, config path/authored sha256/resolved fingerprint/effective config sha256, source JSONL + sha256, dtype mode, observed model dtype and attention implementation, vision parity, full backend session receipt, EOS and row-entry token IDs, and the literal `claim_boundary: "fixed-prefix boundary scores are not free-rollout outcomes"`.

**4. Current tests.**

- `test_exact_history_prepare_and_literal_immutable_append` (test_hf_exact_history_evidence.py 214–243) — append is literal and non-mutating, digests match `token_ids_sha256`, no tokenizer decode occurs, and the public surface is exactly the three fields with no tensors and no session binding.
- `test_exact_history_rejects_forged_cross_session_and_closed_use` (262–290) — forged, cross-session and post-close use each raise the expected typed code **with zero forward calls on either model**.
- `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence` (330–380) — rope positions are built at use, forward flags are `use_cache=False/return_dict=True/logits_to_keep=0`, logprobs equal the FP32 `log_softmax` reference, ranks are `[2, 2, 4]` (proving tie-sharing), and only the three evidence fields exist.
- `test_owner_state_score_preserves_existing_reduction_from_token_evidence` (test_continuation_locality_owner_compositionality.py 88–194) — evidence-derived scoring reproduces `terminal_boundary_score` and `_score_candidate` exactly, including the prefix and prompt+prefix digests.
- `test_terminal_score_is_separate_from_raw_candidate_row_score` (test_complete_candidate_row_scoring.py 153–167) — the terminal margin stays a distinct field and no probability matrix leaks into the row score.

### Q2 evidence table

| claim | file | symbol | line range | convention |
|---|---|---|---|---|
| session-owned registration | src/inference/hf_backend.py | `HFBackendSession/prepare_exact_history` | 127–146 | 0-based |
| immutable literal append | src/inference/hf_backend.py | `HFBackendSession/extend_exact_history` | 148–161 | 0-based |
| digest/identity validation | src/inference/hf_backend.py | `HFBackendSession/_validated_exact_history` | 691–715 | 0-based |
| weak registry | src/inference/hf_backend.py | `HFBackendSession/__init__` | 91–111 | 0-based |
| close clears context | src/inference/hf_backend.py | `HFBackendSession/close` | 323–340 | 0-based |
| position slice, CPU FP32, logprob, rank | src/inference/hf_backend.py | `HFBackendSession/teacher_forced_evidence` | 163–280 | 0-based |
| pre-forward token validation | src/inference/hf_backend.py | `HFBackendSession/_validated_token_ids` | 636–668 | 0-based |
| manifest/prefix validation | scripts/research/run_continuation_locality_boundary_scoring.py | `_validated_boundaries` | 46–71 | 0-based |
| history construction, dual scoring, receipt | scripts/research/run_continuation_locality_boundary_scoring.py | `run` | 74–291 | 0-based |
| reference subtraction semantics | scripts/research/run_next_row_likelihood_change.py | `terminal_boundary_score` | 225–241 | 0-based |

---

## Verdicts A–D

- **(A) Literal fixed-prefix identity — YES.** The receipt carries `prefix_token_ids_sha256` (manifest-verified) and `actual_prompt_plus_prefix_token_ids_sha256 = token_ids_sha256([*base_prompt, *prefix])`, and the run aborts on prompt, image-hash, or materialized-prompt mismatch; history identity is digest-checked on every call.
- **(B) Fixed-prefix relative preference between the two selected next tokens — YES.** Both evidences are taken at the same `boundary = len(conditioning) - 1` on the identical history, each a full-vocabulary FP32 `log_softmax` value; `row_entry_minus_terminal` is their exact FP32 difference and matches the single-tensor reference reduction.
- **(C) Free-rollout behavior — NO.** Nothing is generated: `max_new_tokens=1` is only carried on the `DecodeRequest`, `decode` is never called, and both scored tokens are teacher-forced continuations of a fixed prefix. The receipt itself asserts `claim_boundary: "fixed-prefix boundary scores are not free-rollout outcomes"`.
- **(D) Reconstruction of the full vocabulary distribution — NO.** `HFChosenTokenEvidence` exposes only `token_id`, `raw_model_logprob`, `candidate_vocab_rank`; the logits tensor never leaves the method, and the receipt stores two scalars plus their difference. Ranks are strictly-greater counts and cannot invert the distribution.

---

## Tool ledger (execution order)

| tool | purpose | outcome |
|---|---|---|
| `Read` ablation-prompt.md | mandated one-time setup read of the locked prompt | ok (explicitly exempted, not a source fallback) |
| `ToolSearch` | load serena-light MCP schemas (harness requirement) | ok, 11 serena-light tools |
| `mcp__serena-light__activate_workspace` `/data/CoordExp/serena-light` | absolute workspace activation | ok, lease bound |
| `Bash` `git rev-parse HEAD` + `git status --porcelain` on both roots | Git metadata / environment check (non-source) | ok, HEADs + dirty lists |
| `find_symbol WorkspaceRuntime/find_symbol` (body) | contract body | ok |
| `find_symbol WorkspaceRuntime/_tool_envelope` (body) | freshness placement, error mapping | ok |
| `find_symbol WorkspaceRuntime/ensure_fresh` (body) | delegation to coordinator | ok |
| `find_symbol FreshnessCoordinator` (info) | locate class range | ok |
| `get_symbols_overview runtime.py` depth 2 | structural map of coordinator/runtime | ok |
| `find_symbol FreshnessCoordinator/ensure_fresh` (body) | shared-scan concurrency | ok |
| `find_symbol FreshnessCoordinator/_scan_git` (body) | change classification | ok |
| `find_symbol FreshnessCoordinator/_apply_events` (body) | watched delivery phases | ok |
| `find_symbol WorkspaceRuntime/install_freshness` (body) | restart/retire, family errors | ok |
| `find_symbol DocumentNavigationService/find_symbol_in_documents` (body) | directory branch | ok |
| `find_symbol DocumentNavigationService/find_symbol` (body) | exact-file branch | ok |
| `find_symbol GlobalSymbolService/find_symbol` (body) | global bounds/readiness | ok |
| `find_symbol WorkspaceRuntime/_route` (body) | authorization + unavailable family | ok |
| `find_symbol WorkspaceRuntime/_warm_global_candidates` (body) | warm budget | ok |
| `find_symbol FreshnessCoordinator/ensure_path_fresh` (body) | non-Git targeted stat | ok |
| `find_referencing_symbols WorkspaceRuntime/find_symbol` | locate current tests | ok, 5 files |
| `find_referencing_symbols FreshnessCoordinator` | ownership check | ok |
| `find_referencing_symbols WorkspaceRuntime/ensure_fresh` | locate freshness tests | ok, 3 files |
| `find_symbol` ×4 test bodies (concurrency, symlink/config, same-stat, directory-bound, all-incompatible) | Q1 invariants | ok (5 calls) |
| `release_workspace` | unbind before switching target | ok |
| `activate_workspace` `/data/CoordExp/.worktrees/research-probes` | absolute activation of Q2 target | ok |
| `find_symbol HFBackendSession/prepare_exact_history` (body) | identity creation | ok |
| `get_symbols_overview src/inference/hf_backend.py` depth 2 | structural map | ok |
| `find_symbol` teacher_forced_evidence / extend / _validated_exact_history / _new_exact_history / close / _validated_token_ids / `__init__` | Q2 part 1–2 | ok (7 calls) |
| `get_symbols_overview` + `find_symbol run` + `_validated_boundaries` (runner) | Q2 part 3 | ok (3 calls) |
| `find_referencing_symbols HFBackendSession/teacher_forced_evidence` | locate consumers/tests | ok — only the runner; no test reference (tests use fakes) |
| `find_symbol exact_history` (global, substring) | locate the exact-history test module | ok |
| `get_symbols_overview tests/inference/test_hf_exact_history_evidence.py` | enumerate tests | ok |
| `find_symbol continuation_locality` / `locality_boundary` (global, substring) | locate runner tests | `SYMBOL_NOT_FOUND` (global search matches symbol names, not filenames) |
| `find_referencing_symbols run` (runner) | locate runner tests | ok, none (tests import by path) |
| `find_symbol boundary relative_path=tests/research` (directory, substring) | recover the runner test module by inventory-bounded scope | ok — this MCP directory branch replaced what would otherwise have been a shell grep |
| `get_symbols_overview` + `find_symbol` ×5 test bodies (Q2) | Q2 part 4 | ok |
| `find_symbol terminal_boundary_score` | subtraction reference | ok |

**Shell fallback: none.** The only `Bash` invocation was Git metadata (HEAD + porcelain status), explicitly permitted as a non-source environment check; no `rg`/`sed`/`cat` source search or read was used. The two failed global substring searches were recovered through the MCP's own inventory-bounded directory branch rather than a shell grep.

## Confidence and unresolved ambiguity

High confidence on every cited body — all were read through the assigned MCP from the live snapshots, with file `sha256` returned per read.

Residual ambiguity: (i) I did not open `TrustInventory.paths_under`/`targeted_states` or `_config_states_for`, so my statements about *how* content identity and config states are observed rest on their call sites, not their implementations; (ii) verdict (B) assumes the model is deterministic across the two separate forward passes the runner issues — the code establishes identical inputs and positions, and the compositionality test proves equality against a single shared logits tensor, but the runner itself does not assert cross-call determinism; (iii) I did not execute any test, so "current tests" means present and semantically inspected, not observed green.
