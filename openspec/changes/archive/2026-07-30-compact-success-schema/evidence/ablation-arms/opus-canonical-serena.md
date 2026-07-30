# Opus 5 High — Canonical Serena

This is the retained outer-assistant response from the locked read-only arm.
The answer below is preserved verbatim; the heading and this note are archival
metadata, not participant output.

---

## Snapshot

| Target | Git HEAD | Working tree (relevant files) |
|---|---|---|
| `/data/CoordExp/serena-light` | `9e4987e9f2190a4ff03cb7a35359483a5387f327` | Dirty. **Modified**: `src/serena_light/workspace/runtime.py`, `tools/navigation.py`, `tools/global_symbols.py`, `tests/unit/test_workspace_runtime_semantics.py`. **Clean**: `src/serena_light/workspace/inventory.py`, `tests/unit/test_workspace_runtime.py`. Untracked `openspec/changes/compact-success-schema/`, `tools/compact*.py`. |
| `/data/CoordExp/.worktrees/research-probes` | `ccdadc4e2d8c00a091dde8d684a14982f05715f2` | Dirty overall, but **all Q2 files are tracked and clean**: `src/inference/hf_backend.py`, `scripts/research/run_continuation_locality_boundary_scoring.py`, `tests/inference/test_hf_exact_history_evidence.py`, `tests/research/test_continuation_locality_owner_compositionality.py`. |

**Line convention.** All Serena MCP `body_location` ranges below are **0-based**. Verified against shell: MCP `start_line = 700` for `class FreshnessCoordinator` ↔ 1-based `sed -n '701p'`; MCP `1692` for `def find_symbol` ↔ 1-based `1693`; MCP `163` for `def teacher_forced_evidence` ↔ 1-based `164`. Shell output quoted anywhere below is **1-based** and labelled.

---

## Question 1 — `WorkspaceRuntime.find_symbol` contract

**1. Freshness before the operation.** `find_symbol` wraps its whole body in a closure and returns `self._tool_envelope(operation)`. `_tool_envelope` calls `self.ensure_fresh()` *first*, then `operation()`, and only then maps exceptions to typed envelopes (`WorkspaceRuntimeError` → same code + `retry` for `BUSY`/`NOT_READY`/`TIMED_OUT`; bare `TimeoutError` → `TIMED_OUT`; `OSError`/`TypeError`/`ValueError` → `INVALID_INPUT`). `WorkspaceRuntime.ensure_fresh` calls `_require_running()` then `FreshnessCoordinator.ensure_fresh()`. A second, path-targeted reconciliation happens inside `_route` (`self._freshness.ensure_path_fresh(normalized)`), which every per-document load reaches via `_load_document` → `_route`. So: one whole-workspace Git scan per tool call, plus one targeted stat per operand on the non-Git read-only root.

**2. The three branches.**

*Exact file* (`relative_path` names an inventory path): rejects a non-default `max_candidates_per_adapter` with `UNSUPPORTED` (`max_candidates_per_adapter_applies_only_to_global_scope`), calls `self._route(normalized_scope)`, then delegates to `DocumentNavigationService(self).find_symbol(...)`. Scope bound = exactly one document. Unavailable family: `_route` raises the stored `self._family_errors[family]` typed error before any adapter access; `_route` also raises `UNSUPPORTED` if the authorized path routes to ≠1 adapter.

*Inventory-bounded directory* (`relative_path` is not an inventory path but `inventory.paths_under(scope)` is non-empty; `paths_under` is a prefix walk over the trust tree, never a filesystem walk): filters to `available = [p for p in selected if _family_of(p) in self._adapters]`. If **no** path survives → `WorkspaceRuntimeError(SCOPE_INCOMPATIBLE, "directory scope contains only unavailable language families")` carrying the union of `_family_errors[...].paths`. Otherwise delegates to `DocumentNavigationService.find_symbol_in_documents(available, ...)`, which loads each selected document, collects depth-first matches, sorts by `(relative_path, symbol order)`, and returns `SYMBOL_NOT_FOUND` with `scope: "directory"` or a `max_answer_chars`-truncated success. If `paths_under` raises `ValueError`, `_route` is called to produce the authoritative policy error. Note the *partial*-availability case: files of an unavailable family are silently dropped from `available` while healthy families still answer.

*Global* (`relative_path is None`): if `not self._adapters and self._family_errors` → `SCOPE_INCOMPATIBLE, "all attributed language families are unavailable"`. Otherwise `_warm_global_candidates(name_path)` polls every non-`ready` adapter round-robin inside a shared budget `min(30.0, _future_timeout) - 0.5`, ordered by ascending configured-program size, using one deterministic non-`__init__` configured-program document as a readiness witness; a family that becomes `ready` contributes a `_WarmGlobalSeed` (witness query, candidates, document, generations) so the seeded `workspace/symbol` and `documentSymbol` results are reused instead of re-requested. Then `GlobalSymbolService` runs over one `_GlobalProvider` per adapter in `self._adapters`. Scope bound = `max_candidates_per_adapter` (1…`MAX_CANDIDATES_PER_ADAPTER`) per adapter plus `max_answer_chars`; candidates are filtered against the configured-program scope, grouped by file, and each grouped file's document symbols are verified against the candidate before rendering. Unavailability surfaces as `UNSUPPORTED` (any adapter lacks `workspace/symbol`), `NOT_READY` (any adapter not `global_ready`, with `retry_after_seconds`), or `_generation_not_ready` if generations move mid-answer. Families held in `_family_errors` but absent from `_adapters` are simply not queried — the global answer is scoped to healthy adapters without a partial-scope marker.

**3. `ensure_fresh` / `_scan_git` coordination.**

Concurrency: `ensure_fresh` returns `FreshnessScan()` immediately for a non-Git root. For a Git root it takes `_lock`, installs a `_SharedScan` if none is in flight, and either owns it or waits on `shared.done`; joiners re-raise the owner's failure or return the *identical* result object. `finally` clears `_in_flight`, publishes `_last` only on success, and sets `done`. There is deliberately no time-based success cache, and the scan runs on the calling thread (never the shared LSP executor, which would deadlock).

Distinctions inside `_scan_git`: it first calls `runtime.retry_pending_restarts()` and `_settle_pending_reconciles()`, rebuilds the inventory, then computes `created`/`deleted` from set difference; `changed` from `ContentIdentity` inequality on paths present before and after; `symlinked` from `rebuilt.rejected` entries whose reason starts with `"symlink"`; `config_changed` from `ContentIdentity` inequality over `_native_config_candidates`. Any untrusted source state, or an untrusted config state whose reason is not `"missing"`, raises `NOT_READY` **before** `self._states`/`_config_states` are committed. `membership_changed = created or deleted or rejected-set change`. If nothing changed it returns an empty `FreshnessScan`.

Observable consequences: (a) *Inventory* — `install_freshness` swaps `self.inventory` and per-family projections under `_state_lock`; the rebuilt lexical inventory is never reverted by a family-local failure. (b) *Reattribution/restart* — `_affected_families` reattributes only families whose membership paths or watched native-config filenames moved; `restart_families = _affected_families((), config_changed)` is config-only, so a native-config change pops the adapter, publishes a `_PendingAdapterRestart`, and installs `NOT_READY "<family> adapter restart is in progress"` in `_family_errors` until the restart completes; an attribution error retires the adapter and installs its typed error. (c) *Watched-file delivery* — `_apply_events` advances tracker generations for every affected family unconditionally in phase one (each family sees only its own events), publishes one `_PendingWatchedReconcile` per family (a duplicate raises `NOT_READY … already has pending ownership`), then admits all batches before waiting on any; created files are opened up to `MAX_CONTROLLED_OPENS`, the rest reported in `unopened`. When `install_failure is not None`, `wait_for_delivery=False`, so bookkeeping and admission still happen but the install failure is raised. (d) *Typed failures* — `BUSY` when the bounded executor rejects submission, `NOT_READY` on submission/settle failure or timeout inside `_settle_pending_reconcile` (which nulls a failed future so a later scan retries), `STOPPED` re-raised immediately.

**4. Tests.**

| Test | Invariant protected |
|---|---|
| `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse` | Concurrent callers join one in-flight rebuild and receive the *same* object; a completed scan is never reused (third call rebuilds again). |
| `test_freshness_reports_create_change_delete_and_notifies_running_adapters` | Create/change/delete classification, family-scoped reattribution and generation bumps, exact notification order (`didChangeWatchedFiles`, `didChange`, `didOpen`, `didClose`), and non-contamination of the other family's configured program. |
| `test_freshness_detects_symlink_substitution_and_native_config_change` | Symlink substitution is reported as `symlinked` + `deleted` and evicted from the inventory; a new `pyrightconfig.json` yields `config_changed` + Python-only `reattributed`. |
| `test_unstable_byte_observation_fails_before_freshness_state_is_committed` | An unstable byte observation raises `NOT_READY` with inventory, adapter generations and notifications untouched; the next scan recovers and reports the change. |
| `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk` | Directory scope loads exactly the inventory-prefixed documents (never the sibling), and `max_candidates_per_adapter` with `relative_path` is `UNSUPPORTED` with no extra document request. |
| `test_global_find_symbol_only_loads_workspace_symbol_candidates` | Global scope loads only `workspace/symbol` candidate files, in the order `workspace/symbol` → `textDocument/documentSymbol`. |
| `test_all_incompatible_families_bind_for_status_and_fail_only_selected_scope` | An all-unavailable workspace still binds for `status()` but `find_symbol` returns `SCOPE_INCOMPATIBLE`. |
| `test_global_warm_round_robin_does_not_starve_later_family` | Warm polling round-robins so a later family still reaches `ready`; the still-starting family yields `NOT_READY` rather than a partial success. |

**5. Prevented failure / remaining limitation.** *Prevented*: a file rewritten between two tool calls cannot be answered from a stale open buffer — `ensure_fresh` detects `changed`, bumps the document generation and delivers `didChangeWatchedFiles`+`didChange` before the symbol query runs, and if that delivery cannot be admitted the call degrades to `BUSY`/`NOT_READY` instead of a stale success (`test_unstable_byte_observation…`, `test_freshness_reports_…`). *Limitation*: partial family unavailability is silent. In the directory branch, unavailable-family files are filtered out of `available` and the success envelope reports only `scope: "directory"`; in the global branch, families in `_family_errors` but absent from `_adapters` are never queried and `adapters` lists only healthy ones. A caller cannot distinguish "no such symbol in scope" from "part of the scope was not searched."

### Q1 evidence table

| Claim | File (`serena-light/`) | Symbol | Line range | Convention |
|---|---|---|---|---|
| Freshness runs before the operation | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime/_tool_envelope` | 2056–2082 | 0-based |
| `ensure_fresh` entry + `_require_running` | same | `WorkspaceRuntime/ensure_fresh` | 1200–1204 | 0-based |
| Three branches, `UNSUPPORTED` guard, `SCOPE_INCOMPATIBLE` | same | `WorkspaceRuntime/find_symbol` | 1692–1776 | 0-based |
| Per-path freshness + family-error gate + 1-adapter routing | same | `WorkspaceRuntime/_route` | 1642–1668 | 0-based |
| Every document load routes | same | `WorkspaceRuntime/_load_document` | 2634–2713 | 0-based |
| Shared scan, no time cache | same | `FreshnessCoordinator/ensure_fresh` | 726–759 | 0-based |
| Targeted external-root stat | same | `FreshnessCoordinator/ensure_path_fresh` | 761–795 | 0-based |
| change / membership / symlink / config classification | same | `FreshnessCoordinator/_scan_git` | 810–904 | 0-based |
| Generation-first, family-scoped delivery; `BUSY`/`NOT_READY` | same | `FreshnessCoordinator/_apply_events` | 906–1017 | 0-based |
| Retry/settle ownership | same | `FreshnessCoordinator/_settle_pending_reconcile` | 1033–1095 | 0-based |
| Restart/retire installation | same | `WorkspaceRuntime/install_freshness` | 1242–1372 | 0-based |
| Watcher batch + bounded open/close | same | `WorkspaceRuntime/notify_watched_files` | 1541–1594 | 0-based |
| Round-robin warm within shared budget | same | `WorkspaceRuntime/_warm_global_candidates` | 1778–1859 | 0-based |
| Seed reuse / fail-closed document re-check | same | `_GlobalProvider` | 2959–3039 | 0-based |
| Affected-family selection | same | `_affected_families` | 3245–3252 | 0-based |
| Directory search without walking | `src/serena_light/tools/navigation.py` | `DocumentNavigationService/find_symbol_in_documents` | 158–237 | 0-based |
| Exact-file delegation | same | `DocumentNavigationService/find_symbol` | 134–156 | 0-based |
| Prefix bound is tree-based | `src/serena_light/workspace/inventory.py` | `TrustInventory/paths_under` | 154–155 | 0-based |
| `UNSUPPORTED`/`NOT_READY`/generation guards | `src/serena_light/tools/global_symbols.py` | `GlobalSymbolService/find_symbol` | 205–387 | 0-based |
| Tests (in order of table above) | `tests/unit/test_workspace_runtime.py` | 1118–1158, 841–907, 1161–1189, 1233–1298, 381–418 | | 0-based |
| Tests | `tests/unit/test_workspace_runtime_semantics.py` | 1214–1249, 927–952, 1604–1643 | | 0-based |

---

## Question 2 — exact-history research evidence seam

**1. Identity, immutable extension, digests, forged/cross-session/closed.** `HFExactHistory` is a `frozen=True, eq=False` dataclass holding only `request_id`, `conditioning_token_ids`, `conditioning_token_ids_sha256` — identity-hashed, no tensors, no session handle. `_new_exact_history` computes `token_ids_sha256(conditioning_token_ids)` and registers the *private* `_HFExactHistoryState` (same three fields plus `_HFExactHistoryContext.native_inputs`) in a `WeakKeyDictionary`. `prepare_exact_history` requires a live session, materializes native inputs for one `DecodeRequest`, and validates the executed prompt IDs. `extend_exact_history` validates the parent, validates appended IDs, and returns a **new** history with `(*parent, *appended)` — no decode, no re-tokenize, parent unchanged. `_validated_token_ids` rejects bools, non-ints, negatives, and IDs ≥ vocab size (`hf_backend.invalid_token_id`), and raises `hf_backend.vocab_size_unavailable` when vocabulary size cannot be established. `_validated_exact_history` fails closed with `hf_backend.exact_history_session` when the object is not registered (forged), belongs to another session, or when any of `request_id`, token tuple, stored digest, or recomputed digest disagree. A closed session raises `hf_backend.session_closed` first (`_require_live_session`); `close()` also nulls every `context.native_inputs` and clears the registry, so surviving handles can no longer be used.

**2. Logits selection, CPU FP32, scalars.** `teacher_forced_evidence` builds `full_token_ids = conditioning + continuation`, an all-ones attention mask, and Qwen position IDs derived *at use* via `get_rope_index` with the retained `image_grid_thw` (required; missing → `hf_backend.exact_history_image_grid`). The forward runs under `torch.inference_mode()` with `use_cache=False`, `return_dict=True`, `logits_to_keep=0`, after stripping caller-supplied `input_ids`/`attention_mask`/`position_ids`/`token_type_ids`/`cache_position`/`rope_deltas` from the native inputs. Selection: `boundary = len(conditioning) - 1`; `logits[0, boundary : boundary + len(continuation), :]`. Because logits at index *i* predict token *i+1*, row *k* of the slice is the next-token distribution after `conditioning + continuation[:k]`. The slice is `.detach().to(device="cpu", dtype=torch.float32).contiguous()` so the arithmetic is host-side FP32 regardless of model dtype/device, and a length mismatch raises `hf_backend.teacher_forced_alignment`. `raw_model_logprob = F.log_softmax(selected_logits, dim=-1).gather(1, ids)` — full-vocabulary normalized log-probability of the chosen token. `candidate_vocab_rank = (selected_logits > selected_values).sum(dim=1) + 1` — strictly-greater count plus one, i.e. competition ranking: tied tokens all receive the same best rank, ties never inflate the rank. Only `HFChosenTokenEvidence(token_id, raw_model_logprob, candidate_vocab_rank)` is returned; logits are not retained.

**3. The boundary-scoring runner.** `_validated_boundaries` enforces `MANIFEST_SCHEMA_VERSION`, unique non-empty `boundary_id`, well-formed `base_prompt_token_ids`/`prefix_token_ids`, recomputed SHA-256 equality for both, and `prefix[-1] == 151649` (complete-row boundary). Selection is `_stable_shard(boundary_id, shard_count) == shard_index` plus optional cohort filter, sorted by `boundary_id`; an empty shard is fatal, and an existing output path is refused (`FileExistsError`, immutable receipt). Per boundary the runner re-plans the image, asserts `image_content_sha256` equality, rebuilds the prompt, asserts `base_prompt == manifest.base_prompt_token_ids`, then `prepare_exact_history` (asserting `history.conditioning_token_ids == base_prompt`) and `extend_exact_history(history, prefix_token_ids)`. It then calls `teacher_forced_evidence(history, (OBJECT_REF_START,))` and `teacher_forced_evidence(history, (terminal_id,))` — **the same frozen history**, two single-token continuations, so both scores come from the identical conditioning position `len(conditioning)-1`. Subtraction semantics are preserved by round-tripping both scalars through `torch.tensor(..., dtype=torch.float32)` and reporting `row_entry_log_probability`, `terminal_log_probability`, `row_entry_minus_terminal`. Provenance recorded: manifest path+SHA, `prefix_token_ids_sha256`, `actual_prompt_plus_prefix_token_ids_sha256`, authored/effective config SHAs and resolved fingerprint, source JSONL SHA, model dtype, attention implementation, processor–model vision parity, full backend session receipt, `eos_token_id`, `row_entry_token_id`, shard/limit/cohort runtime, and the literal `claim_boundary: "fixed-prefix boundary scores are not free-rollout outcomes"`.

**4. Tests.**

| Test | What it proves |
|---|---|
| `test_exact_history_prepare_and_literal_immutable_append` | Append is literal and immutable (parent unchanged, `child is not parent`), digests match `token_ids_sha256`, no decode call occurs, and the public surface carries no session binding and no tensors. |
| `test_exact_history_rejects_forged_cross_session_and_closed_use` | Forged, cross-session, and post-`close()` handles raise the typed codes with **zero** forward calls on either model. |
| `test_exact_history_rejects_invalid_literal_token_ids_before_forward` | `-1`, out-of-vocab `32`, `True`, `1.5`, `"1"` all raise `hf_backend.invalid_token_id` before any forward. |
| `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence` | Position IDs are built per call from exactly the teacher-forced `input_ids`; log-probs equal `log_softmax` over the full vocabulary; ranks are `[2, 2, 4]` under a tie (two tokens at logit 2.0 both rank 2) — competition ranking; the returned object has exactly three fields and no `logits`. |
| `test_exact_history_registry_releases_dead_cases_and_clears_live_context` | The weak registry drops dead handles after GC and `close()` nulls live `native_inputs`. |
| `test_complete_row_prefixes_preserve_every_literal_boundary` | Row prefixes end on the literal complete-row boundary token the runner requires. |
| `test_locality_and_owner_runners_use_the_same_stable_sharding` | The locality runner's `_stable_shard` is bit-identical to the owner probe's, so shards are comparable across runners. |
| `test_owner_state_score_preserves_existing_reduction_from_token_evidence` | The `HFChosenTokenEvidence` → terminal-boundary reduction reproduces the legacy direct-logits `terminal_boundary_score`, and emits exactly `prefix_token_ids_sha256`, `actual_prompt_plus_prefix_token_ids_sha256`, `terminal_boundary`, `candidate_score`. |

**5. Verdicts.**

- **(A) Literal fixed-prefix identity — YES.** Manifest SHAs are recomputed and compared (`_validated_boundaries`), the materialized prompt is asserted equal to `base_prompt`, `_validated_exact_history` re-verifies `token_ids_sha256(history.conditioning_token_ids)` against session state, and the receipt stores `prefix_token_ids_sha256` and `actual_prompt_plus_prefix_token_ids_sha256` alongside config/data/backend fingerprints.
- **(B) Fixed-prefix relative preference between the two selected next tokens — YES.** Both calls pass the *same* history object with a single-token continuation, so both read logits at `len(conditioning)-1`; under causal attention that row depends only on the shared frozen prefix. Both log-probs are full-vocabulary `log_softmax` values computed in CPU FP32, so `row_entry_minus_terminal` is a valid same-distribution comparison of `<|object_ref_start|>` (151646) versus `im_end`.
- **(C) Free-rollout behavior — NO.** Every score is a teacher-forced single forward at a frozen literal prefix with `use_cache=False`; no sampling or generation loop runs (`max_new_tokens=1` is only carried on the `GenerationPolicy` used to materialize the request), and the receipt itself records `claim_boundary: "fixed-prefix boundary scores are not free-rollout outcomes"`.
- **(D) Reconstruction of the full vocabulary distribution — NO.** Only two scalars per token survive; the FP32 logit slice is local to `teacher_forced_evidence` and never returned or persisted (asserted by `set(vars(...)) == {token_id, raw_model_logprob, candidate_vocab_rank}` and `not hasattr(..., "logits")`). Rank is additionally lossy under ties.

### Q2 evidence table

| Claim | File (`research-probes/`) | Symbol | Line range | Convention |
|---|---|---|---|---|
| Audit-safe public identity | `src/inference/hf_backend.py` | `HFExactHistory` | 55–61 | 0-based |
| Three-scalar evidence record | same | `HFChosenTokenEvidence` | 64–70 | 0-based |
| Private state + live context | same | `_HFExactHistoryState`, `_HFExactHistoryContext` | 73–78, 81–83 | 0-based |
| Weak registry | same | `HFBackendSession/__init__` | 91–111 | 0-based |
| Session-owned materialization | same | `HFBackendSession/prepare_exact_history` | 127–146 | 0-based |
| Literal immutable append | same | `HFBackendSession/extend_exact_history` | 148–161 | 0-based |
| Position selection, CPU FP32, logprob, rank, ties | same | `HFBackendSession/teacher_forced_evidence` | 163–280 | 0-based |
| Context nulling on close | same | `HFBackendSession/close` | 323–340 | 0-based |
| Closed-session guard | same | `HFBackendSession/_require_live_session` | 629–634 | 0-based |
| Token-ID validation | same | `HFBackendSession/_validated_token_ids` | 636–668 | 0-based |
| Digest computation | same | `HFBackendSession/_new_exact_history` | 670–689 | 0-based |
| Forged / cross-session / digest mismatch | same | `HFBackendSession/_validated_exact_history` | 691–715 | 0-based |
| Position IDs derived at use | same | `_derive_qwen_position_ids` | 786–832 | 0-based |
| Manifest schema, hash and complete-row gate | `scripts/research/run_continuation_locality_boundary_scoring.py` | `_validated_boundaries` | 46–71 | 0-based |
| Stable sharding | same | `_stable_shard` | 34–35 | 0-based |
| History construction, dual scoring, subtraction, receipt | same | `run` | 74–291 | 0-based |
| `OBJECT_REF_START`/`MANIFEST_SCHEMA_VERSION` imports; `RECEIPT_SCHEMA_VERSION` literal | same | module header | 17–24 (1-based `sed`) | **1-based (shell fallback)** |
| `OBJECT_REF_START = 151646` | `src/analysis/sampled_rescue_transition/comparison.py` | module constant | line 11 (1-based `rg`) | **1-based (shell fallback)** |
| Tests | `tests/inference/test_hf_exact_history_evidence.py` | 214–243, 246–259, 262–290, 310–327, 330–380 | | 0-based |
| Tests | `tests/research/test_continuation_locality_owner_compositionality.py` | 30–34, 37–41, 88–194 | | 0-based |

---

## Tool ledger (execution order)

| Tool | Purpose | Outcome |
|---|---|---|
| `Read` (host) | Read the locked ablation prompt (task instructions, not source under analysis) | OK |
| `ToolSearch` ×3 | Load Serena tool schemas (harness requirement before any MCP call) | Loaded `activate_project`, `find_symbol`, `get_symbols_overview`, `find_referencing_symbols`, `initial_instructions`. **Not available in this Serena configuration**: `read_file`, `search_for_pattern`, `list_dir`, `find_file` |
| `Bash` `pwd; git -C … rev-parse/status` | Snapshot HEADs and dirtiness | Non-source environment check (permitted directly) |
| `mcp__serena__initial_instructions` | Required init | OK; manual states 0-based lines and directs broad search to host tools |
| `mcp__serena__activate_project` `/data/CoordExp/serena-light` | Absolute activation | OK |
| `find_symbol` `WorkspaceRuntime` (depth 1) | Class map | OK |
| `find_symbol` ×2 bodies: `find_symbol`, `_warm_global_candidates` | Branch contract, warm loop | OK |
| `find_symbol` ×4: `_tool_envelope`, `ensure_fresh`, `_route`, `FreshnessCoordinator` (depth 1) | Freshness seam | OK |
| `find_symbol` `FreshnessCoordinator` (body, 60k) | Full coordinator | OK |
| `find_symbol` ×4: `DocumentNavigationService` (d1), `GlobalSymbolService` (d1), `_affected_families`, `_family_of` | Delegates + attribution | OK |
| `find_symbol` ×3 bodies: `DocumentNavigationService/find_symbol`, `/find_symbol_in_documents`, `GlobalSymbolService/find_symbol` | Branch services | OK |
| `find_symbol` ×3: `install_freshness`, `notify_watched_files`, `_GlobalProvider` | Restart/delivery/seed | OK |
| `find_referencing_symbols` `WorkspaceRuntime/find_symbol` | Locate current tests | OK — 4 test files enumerated |
| `find_symbol` ×4 test bodies (`…inventory_prefix…`, `…incompatible_families…`, `…only_loads_workspace_symbol_candidates`, `…round_robin…`) | Invariants | OK |
| `find_symbol` `fresh` (substring, `tests/unit`, kind 12) | Discover freshness tests without text search | OK — 14 hits |
| `find_symbol` ×4 freshness test bodies | Invariants | OK |
| `find_symbol` ×2: `TrustInventory/paths_under`, `FreshnessScan` | Scope bound, scan surface | OK |
| `mcp__serena__activate_project` `/data/CoordExp/.worktrees/research-probes` | Switch to Q2 target | OK |
| `find_symbol` `HFBackendSession` (depth 1) | Class map | OK |
| `find_symbol` ×6 bodies: `prepare_exact_history`, `extend_exact_history`, `teacher_forced_evidence`, `_new_exact_history`, `_validated_exact_history`, `_validated_token_ids` | Core seam | OK |
| `find_symbol` ×6: `HFExactHistory`, `_HFExactHistoryState`, `_HFExactHistoryContext`, `close`, `_require_live_session`, `HFChosenTokenEvidence` | Identity/lifecycle | OK |
| `find_symbol` `__init__` + `get_symbols_overview` runner | Weak registry; runner map | OK |
| `find_symbol` `run` (body, 60k) | Runner contract | OK |
| `find_symbol` ×2 + `find_referencing_symbols teacher_forced_evidence` | Manifest validation; consumers | OK — only the runner references it (no test call sites) |
| `find_symbol` `exact_history` / `locality_boundary` / `boundary` (substring, tests) | Discover tests | OK / empty / OK |
| `get_symbols_overview` ×2 test files + `find_symbol` ×5 test bodies | Invariants | OK |
| `find_symbol` `OBJECT_REF_START` (max 5), `RECEIPT_SCHEMA_VERSION`, `_derive_qwen_position_ids` | Constants + rope | Partial: 15 matches shortened; **constant body returned only the name, not its value** |
| **`Bash` `sed -n '1,45p' …run_continuation_locality_boundary_scoring.py` + `rg -n "^OBJECT_REF_START…"`** | **fallback** — read module header/imports and constant values | Needed because this Serena configuration exposes no `read_file`/`search_for_pattern`, and `find_symbol(include_body)` on a `Constant` returns the identifier without its literal value; imports are not symbols at all |
| **`Bash` `rg` + `git ls-files` + `git status` + `sed -n '701p' / '1693p' / '164p'`** | **fallback (source lines) + non-source Git metadata** — establish the 0-vs-1-based mapping and confirm the Q2 files are tracked and clean | Confirmed MCP `N` ↔ 1-based `N+1`; all four Q2 files clean. Serena offers no way to state its own line convention empirically |
| **`Bash` `rg -n "OBJECT_REF_START" …run_complete_candidate_row_scoring.py`** | **fallback** — resolve the re-export chain to `= 151646` | `find_symbol` cannot resolve a re-exported module constant's value |
| `mcp__serena__activate_project` `/data/CoordExp/serena-light` | Return for one open Q1 question | OK |
| `find_symbol` ×2: `DocumentNavigationService/_load`, `WorkspaceRuntime/_load_document` | Confirm every document load passes `_route` (and thus `ensure_path_fresh` + family-error gate) | OK — confirmed |

No Serena Light (`mcp__serena-light__*`) call was made; no other MCP was used. Nothing was edited, run, or committed.

---

## Confidence and ambiguities

**High confidence** on: the three `find_symbol` branches and their typed failures; `_tool_envelope` → `ensure_fresh` ordering; `_SharedScan` join semantics and the absence of a time cache; the change/membership/symlink/config distinctions; the exact-history identity/digest/lifecycle contract; the `boundary = len(conditioning) - 1` selection, CPU-FP32 conversion, `log_softmax` gather, and competition-rank tie behavior; verdicts A–D.

**Ambiguities / residual uncertainty.**
1. `MAX_CONTROLLED_OPENS`, `MAX_CANDIDATES_PER_ADAPTER`, `_NATIVE_CONFIG_WATCH` and `_FAMILY_EXTENSIONS` are module constants whose literal values I did not resolve (same Serena limitation as `RECEIPT_SCHEMA_VERSION`); I describe their role, not their numbers.
2. `serena-light` is dirty in exactly the files Q1 analyses, so this contract is a working-tree state, not `9e4987e9f`.
3. `_validated_boundaries` requires `prefix[-1] == 151649`; I confirmed the same literal terminates `_complete_row(...)` in the compositionality test but did not resolve the token's human-readable name.
4. Verdict (B) additionally assumes bitwise-stable causal-prefix logits across the two forwards (same weights, dtype, attention implementation, no cache). The code makes this structurally sound and the receipt records dtype/attention for audit, but the repository establishes no numerical-determinism test for it, so (B) is a contract-level YES rather than a measured-reproducibility YES.
5. `find_referencing_symbols` on `teacher_forced_evidence` found no test call sites because the tests exercise it through fixture sessions and a duck-typed `EvidenceSession`; the four-plus tests I cite were located by symbol-name substring search instead.
