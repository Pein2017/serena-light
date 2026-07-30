# Four-Arm Semantic MCP Ablation — Arm: `serena-light`

## Snapshot

| Target | Git HEAD | Working tree |
|---|---|---|
| `/data/CoordExp/serena-light` | `9e4987e9f2190a4ff03cb7a35359483a5387f327` | Dirty. Relevant files **modified**: `src/serena_light/workspace/runtime.py`, `tools/navigation.py`, `tools/global_symbols.py`, `tests/unit/test_workspace_runtime_semantics.py`. `tests/unit/test_workspace_runtime.py` was **not** listed as modified. Untracked: `openspec/changes/compact-success-schema/`, `src/serena_light/tools/compact*.py`. |
| `/data/CoordExp/.worktrees/research-probes` | `ccdadc4e2d8c00a091dde8d684a14982f05715f2` | Dirty, but **none** of the Q2 files are modified: `src/inference/hf_backend.py`, `scripts/research/run_continuation_locality_boundary_scoring.py`, `scripts/research/run_next_row_likelihood_change.py`, `tests/inference/test_hf_exact_history_evidence.py`, `tests/research/test_continuation_locality_owner_compositionality.py` are all clean at HEAD. |

**Line convention: every line range below is 0-based** (as returned by Serena Light MCP). No shell source read/search was performed, so no 1-based evidence appears.

---

## Question 1 — `WorkspaceRuntime.find_symbol` contract

**1. Freshness before the operation.** `find_symbol` never runs its branch logic directly: it defines a closure `operation()` and returns `self._tool_envelope(operation)`. `_tool_envelope` calls `self.ensure_fresh()` **before** `operation()`, so a full Git scan is reconciled first, and only then does branch selection happen. A second, narrower reconciliation exists on the path-bearing branches: `_route` calls `self._freshness.ensure_path_fresh(normalized)`. For a non-Git (allowlisted read-only) identity, `ensure_fresh` returns an empty `FreshnessScan` by design and the targeted stat in `ensure_path_fresh` is the only freshness — so the global branch has no per-path freshness there.

**2. The three branches.**

*Exact file* (`relative_path` names an inventory path). Guard first: `max_candidates_per_adapter != 128` with any `relative_path` returns `UNSUPPORTED` (`max_candidates_per_adapter_applies_only_to_global_scope`). Then `self._route(normalized_scope)` fixes exactly one family+adapter and delegates to `DocumentNavigationService(self).find_symbol`. Scope is one document. Unavailable family: `_route` re-raises the stored `self._family_errors[family]` typed error; if the authorized path routes to ≠1 adapter it raises `UNSUPPORTED`.

*Inventory-bounded directory* (`relative_path` not an inventory path). `self.inventory.paths_under(normalized_scope)` selects the prefix set without walking the filesystem; a `ValueError` is converted by calling `self._route(...)` to raise the policy-typed error (with an `AssertionError` guard if policy wrongly accepts). Candidates are then filtered to `available = paths whose _family_of(path) in self._adapters`. If **every** selected path belongs to unavailable families → `WorkspaceRuntimeError(SCOPE_INCOMPATIBLE, "directory scope contains only unavailable language families")` carrying the union of `_family_errors[...].paths`. Otherwise `find_symbol_in_documents(available, ...)` loads only that explicit set, sorts by `(relative_path, symbol order)`, returns `SYMBOL_NOT_FOUND` with `scope: "directory"` when empty, fails closed on `include_body` when any match has `body_incomplete_reason`, and truncates against `max_answer_chars` with `TruncationMetadata`.

*Global search* (`relative_path is None`). If `not self._adapters and self._family_errors` → `SCOPE_INCOMPATIBLE("all attributed language families are unavailable")`. Otherwise `_warm_global_candidates(name_path)` polls each not-yet-ready adapter inside one shared budget `min(30.0, future_timeout) - 0.5`, round-robin with a per-family turn deadline, using one deterministic configured-program document as the readiness witness; LSP response/protocol/transport errors propagate, other exceptions requeue the family. Then `GlobalSymbolService` over one `_GlobalProvider` per **available** family delegates to `.find_symbol`, bounded by `max_candidates_per_adapter` (1…`MAX_CANDIDATES_PER_ADAPTER`) and scoped to `configured_program`. That service returns `UNSUPPORTED` if any adapter lacks `workspace/symbol`, `NOT_READY` (with `retry_after_seconds`) if any adapter is not `global_ready`, and re-checks adapter/scope/generation identity before *and* after each provider's batch, so a mid-flight restart degrades to `NOT_READY` instead of a stale success. Only candidate files get `textDocument/documentSymbol`.

**3. `ensure_fresh` / `_scan_git` coordination.**
`ensure_fresh` holds `self._lock` only long enough to publish or join one `_SharedScan` in `_in_flight`. The first caller owns the scan; joiners block on `shared.done` and receive the **identical result object** or re-raise the owner's exception. The `finally` block clears `_in_flight`, stores `_last` only on success, and sets `done`. There is no time-based cache: once a scan completes, the next operation rebuilds.

`_scan_git` first calls `runtime.retry_pending_restarts()` and `self._settle_pending_reconciles()` — unsettled restart/reconcile ownership must be resolved before an otherwise-unchanged scan can authorize another semantic operation. It rebuilds the inventory and distinguishes three change classes:
- **content**: `changed` = paths in both before/after whose `content_identity` differs from the cached `self._states`;
- **membership/symlink**: `created`/`deleted` set differences plus `set(previous.rejected) != set(rebuilt.rejected)`; `symlinked` = rejected items whose reason starts with `"symlink"`;
- **native config**: `config_changed` = watched config files whose `content_identity` differs from `self._config_states`.

Any untrusted source state, or any config state with a non-`"missing"` reason, raises `NOT_READY` ("workspace paths changed or became unsafe during freshness observation") before anything is installed. If none of the three classes fired, an empty `FreshnessScan` is returned with no installation and no events.

Observable consequences:
- **Inventory**: swapped inside `install_freshness` under `_state_lock`. A file replaced by a symlink leaves `inventory.paths` and appears in `inventory.rejected` with reason `"symlink"`; the scan reports it in both `deleted` and `symlinked`.
- **Adapter restart / reattribution**: `restart_families = _affected_families((), config_changed)` — only native-config changes force restarts. For each, the tracker's projection is updated (and, if the configured-program generation did not move, a synthetic `may_change_program=True` watched event is applied), the adapter is popped into a `_PendingAdapterRestart`, and `_family_errors[family]` is set to `NOT_READY("<family> adapter restart is in progress")` in the *same* publication so no ownerless gap exists. Stop timeout → `TIMED_OUT` family error that the next scan retries. Attribution errors retire the adapter and install the typed error without blocking healthy families. `scan.reattributed = sorted(projections)`.
- **Watched-file delivery**: `_apply_events` phase one applies `apply_did_change_watched_files` per family — synchronous, unconditional, and strictly family-scoped, so one family's churn cannot invalidate another family's configured program. Phase two publishes `_pending_reconciles` ownership (a duplicate raises `NOT_READY`), submits `notify_watched_files` to the bounded executor (`ExecutorBusyError` → `BUSY`, anything else → `NOT_READY`), and only then waits. Newly created files are opened up to `MAX_CONTROLLED_OPENS`; the remainder are reported in `unopened`.
- **Typed failures**: `_tool_envelope` maps `WorkspaceRuntimeError.code` to `ErrorCode`, attaching `RetryMetadata(retryable=True)` for `BUSY`/`NOT_READY`/`TIMED_OUT`; a bare `TimeoutError` keeps `TIMED_OUT`; `OSError`/`TypeError`/`ValueError` become `INVALID_INPUT`. An `install_freshness` failure other than `STOPPED` is retained so healthy-family events still advance exactly once before it is raised.

**4. Tests and invariants.**

| Test | Invariant |
|---|---|
| `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse` | A joined caller starts no second rebuild and receives the *same object* (`results[0] is results[1]`); a completed scan is never reused (`rebuilds == 3`). |
| `test_freshness_detects_symlink_substitution_and_native_config_change` | Symlink substitution yields `symlinked`+`deleted`, drops the path from `inventory.paths`, and records reason `"symlink"`; a new `pyrightconfig.json` yields `config_changed` and `reattributed == (PYTHON,)`. |
| `test_freshness_reports_create_change_delete_and_notifies_running_adapters` | Create/change/delete classification, notification order (`didChangeWatchedFiles` → `didChange` → `didOpen` → `didClose`), generation advance, and TypeScript generations left untouched by Python churn. |
| `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk` | Directory scope loads exactly the two `src/` documents and never `sibling/c.py`; `max_candidates_per_adapter` with `relative_path` is `UNSUPPORTED` and issues no extra request. |
| `test_global_find_symbol_only_loads_workspace_symbol_candidates` | Global scope loads only `workspace/symbol` candidate files (`document_loads == ["src/candidate.py"]`), never a full scan. |

**5. One prevented failure / one limitation.**
*Prevented*: a source file replaced by a symlink between two calls cannot be read through the symlink — the rebuild rejects it, `deleted`+`symlinked` are reported, and a later directory-scoped `find_symbol` over that prefix cannot include symbols from the substituted target.
*Limitation*: the directory branch silently narrows to `available` families. When a directory contains both a healthy and an unavailable family, the result is a plain success over the surviving families with no per-family unavailability in the payload; only the all-unavailable case raises `SCOPE_INCOMPATIBLE`. The global branch has the same shape (families in `_family_errors` are simply absent from `self._adapters`).

### Q1 evidence table

| claim | file | symbol | line range | convention |
|---|---|---|---|---|
| freshness precedes the operation | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime._tool_envelope` | 2060–2086 | 0-based |
| three branches + guards | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime.find_symbol` | 1694–1780 | 0-based |
| per-path freshness, family error, single-adapter routing | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime._route` | 1644–1670 | 0-based |
| shared-budget round-robin warm | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime._warm_global_candidates` | 1782–1863 | 0-based |
| single shared scan, no time cache | `src/serena_light/workspace/runtime.py` | `FreshnessCoordinator.ensure_fresh` | 728–761 | 0-based |
| content vs membership/symlink vs config | `src/serena_light/workspace/runtime.py` | `FreshnessCoordinator._scan_git` | 812–906 | 0-based |
| generation-first, family-scoped delivery | `src/serena_light/workspace/runtime.py` | `FreshnessCoordinator._apply_events` | 908–1019 | 0-based |
| restart/retirement ownership | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime.install_freshness` | 1244–1374 | 0-based |
| directory set search, no walk | `src/serena_light/tools/navigation.py` | `DocumentNavigationService.find_symbol_in_documents` | 160–243 | 0-based |
| configured-program bounds, generation re-checks | `src/serena_light/tools/global_symbols.py` | `GlobalSymbolService.find_symbol` | 205–387 | 0-based |
| concurrency invariant | `tests/unit/test_workspace_runtime.py` | `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse` | 1118–1158 | 0-based |
| symlink + config invariant | `tests/unit/test_workspace_runtime.py` | `test_freshness_detects_symlink_substitution_and_native_config_change` | 1161–1189 | 0-based |
| delivery/generation invariant | `tests/unit/test_workspace_runtime.py` | `test_freshness_reports_create_change_delete_and_notifies_running_adapters` | 841–907 | 0-based |
| directory bound invariant | `tests/unit/test_workspace_runtime_semantics.py` | `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk` | 1377–1412 | 0-based |
| global candidate bound | `tests/unit/test_workspace_runtime_semantics.py` | `test_global_find_symbol_only_loads_workspace_symbol_candidates` | 1090–1115 | 0-based |

---

## Question 2 — Exact-history research evidence seam

**1. Identity, immutability, digests, forged/cross/closed.** `HFExactHistory` is `@dataclass(frozen=True, eq=False)`, so equality and hashing are by object identity and the session registry `self._exact_history_states` is keyed by the exact object it minted. `_new_exact_history` computes `token_ids_sha256(conditioning_token_ids)` once and stores both the public handle and the private `_HFExactHistoryState` (which alone holds the `_HFExactHistoryContext` with `native_inputs`). `_validated_exact_history` requires: live session, correct type, registry membership, and equality of `request_id`, token tuple, stored digest, **and** a freshly recomputed digest.

`prepare_exact_history` materializes native inputs once from one `DecodeRequest` and validates the executed prompt IDs. `extend_exact_history` appends **literal vocabulary IDs** — no decode, no re-tokenization — returning a new handle that shares the same `request_id` and the same `context` object; the parent tuple is untouched. `_validated_token_ids` fails before any forward on unresolvable vocab size (`hf_backend.vocab_size_unavailable`) and on `bool`, non-`int`, negative, or `>= vocab_size` values (`hf_backend.invalid_token_id`).

A **forged** field-identical `HFExactHistory` is absent from the registry (`eq=False`) → `hf_backend.exact_history_session`. A **cross-session** handle is absent from the other session's registry → same code. A **closed** session raises `hf_backend.session_closed` from `_require_live_session`; `close()` additionally nulls `native_inputs` on each unique context and clears the registry, so any surviving handle is permanently dead. If a context is alive but `native_inputs is None`, `teacher_forced_evidence` raises `hf_backend.exact_history_session`; a missing model raises `hf_backend.session_closed`.

**2. Position selection, CPU FP32, logprob and rank.** One full forward over `conditioning + continuation` with `use_cache=False`, `return_dict=True`, `logits_to_keep=0`, and `position_ids` derived **at use time** by `_derive_qwen_position_ids` from `image_grid_thw` (required; else `hf_backend.exact_history_image_grid`). Then `boundary = len(conditioning_token_ids) - 1` and `logits[0, boundary : boundary + len(continuation), :]` — i.e. the next-token distribution at the last conditioning position plus one row per forced continuation step. A shape check raises `hf_backend.teacher_forced_alignment` if coverage is short.

The slice is `.detach().to(device="cpu", dtype=torch.float32).contiguous()`. Moving to CPU FP32 makes the log-softmax/gather/comparison reductions independent of the model's runtime dtype (bf16/fp16) and of accelerator kernel nondeterminism, so the recorded scalars are stable and comparable across runs and devices.

- `raw_model_logprob` = `F.log_softmax(selected_logits, dim=-1).gather(1, ids)` — a full-vocabulary log-softmax of the **raw** logits, with no temperature, top-p, or repetition penalty applied. It is the raw channel, not the policy channel.
- `candidate_vocab_rank` = `(selected_logits > selected_values).sum(dim=1) + 1` — a strict-greater count, so it is a 1-based *competition* rank in which **ties do not penalize**: every token tied at the maximum receives rank 1. It is the most optimistic rank consistent with the logits.

**3. The boundary-scoring runner.** `_validated_boundaries` enforces the manifest schema version, non-empty list, unique `boundary_id`, integer token lists, digest equality for both `base_prompt_token_ids` and `prefix_token_ids`, and `prefix[-1] == 151649` (complete-row boundary). `run` shards with `_stable_shard`, filters cohorts, sorts by `boundary_id`, applies `--limit`, refuses an empty shard, and refuses to overwrite an existing receipt.

Per boundary it re-plans the image and asserts `image_content_sha256` matches the manifest; rebuilds the prompt record and asserts `base_prompt == item["base_prompt_token_ids"]`; requires a rank-3 grid. The `DecodeRequest` (`max_new_tokens=1`, `temperature=0.0`, `top_p=1.0`, `repetition_penalty=1.0`, `include_raw_model_logprob=True`) is used **only** to materialize native inputs — nothing is generated. It calls `prepare_exact_history`, asserts the materialized conditioning equals the base prompt, then `extend_exact_history(history, prefix_token_ids)`.

Scoring is two independent single-token calls on the *same* history: `(OBJECT_REF_START,)` = `151646` and `(terminal_id,)` = `special_token_ids["im_end"]`. Both therefore read position `len(prompt+prefix) - 1` — the identical next-token distribution — in two separate forward passes.

Subtraction semantics are preserved by round-tripping both scalars through `torch.tensor(..., dtype=torch.float32)` and subtracting as tensors before `.item()`, matching the legacy `terminal_boundary_score`, which subtracts inside FP32 `log_softmax` space at `boundary_length - 1`.

Provenance per record: `prefix_token_ids_sha256` (manifest) and `actual_prompt_plus_prefix_token_ids_sha256 = token_ids_sha256([*base_prompt, *prefix])`, plus cohort, prefix depth, object-count band, observed next action, remaining owner count, and matched training event. The receipt `runtime` block records shard index/count, limit, cohort filter, record count, `physical_batch_size: 1`, `runtime_dtype_mode`, observed `model_dtype`, config path + `authored_config_sha256` + `resolved_config_fingerprint` + `effective_config_sha256`, source JSONL + sha256, manifest path + sha256, attention implementation, processor/model vision parity, the full backend session receipt, `eos_token_id`, and `row_entry_token_id`. The claim boundary is written literally: `"fixed-prefix boundary scores are not free-rollout outcomes"`.

**4. Tests and what each proves.**

| Test | Proves |
|---|---|
| `test_exact_history_prepare_and_literal_immutable_append` | Parent tuple/digest unchanged by extension; child digest equals `token_ids_sha256` of the concatenation; no tokenizer `decode` call; the public handle exposes exactly three fields and no tensors or session binding. |
| `test_exact_history_rejects_forged_cross_session_and_closed_use` | Forged and cross-session handles → `hf_backend.exact_history_session`; post-`close()` → `hf_backend.session_closed`; **no forward pass ran** in either model. |
| `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence` | Positions built at use (`rope_calls` empty until called, then the exact `input_ids`/`attention_mask` per call); logprobs equal the full-vocab log-softmax; ranks `[2, 2, 4]` demonstrate tie behavior (two tokens tied at 2.0 both rank 2); the evidence object carries only `token_id`/`raw_model_logprob`/`candidate_vocab_rank` and has no `logits`; `use_cache=False`, `return_dict=True`, `logits_to_keep=0`. |
| `test_owner_state_score_preserves_existing_reduction_from_token_evidence` | Scoring from `HFChosenTokenEvidence` reproduces the legacy `terminal_boundary_score` reduction exactly, with the terminal scored before the row tokens and the same prefix/prompt+prefix digests emitted. |
| `test_locality_and_owner_runners_use_the_same_stable_sharding` | The locality runner and the owner runner shard identically, so shard assignment is reproducible across the paired probes. |
| `test_exact_history_rejects_invalid_literal_token_ids_before_forward` / `..._unverifiable_vocabulary_before_forward` | Out-of-vocabulary or unverifiable-vocabulary token IDs fail typed before any forward. |

**5. Verdicts.** See the Verdicts section below.

### Q2 evidence table

| claim | file | symbol | line range | convention |
|---|---|---|---|---|
| identity-based handle, no tensors | `src/inference/hf_backend.py` | `HFExactHistory` | 55–61 | 0-based |
| evidence carries three scalars only | `src/inference/hf_backend.py` | `HFChosenTokenEvidence` | 64–70 | 0-based |
| context holds nullable native inputs | `src/inference/hf_backend.py` | `_HFExactHistoryContext` | 81–83 | 0-based |
| one materialization + validated prompt IDs | `src/inference/hf_backend.py` | `HFBackendSession.prepare_exact_history` | 127–146 | 0-based |
| literal immutable append | `src/inference/hf_backend.py` | `HFBackendSession.extend_exact_history` | 148–161 | 0-based |
| position selection, CPU FP32, logprob, rank | `src/inference/hf_backend.py` | `HFBackendSession.teacher_forced_evidence` | 163–280 | 0-based |
| close nulls contexts and clears registry | `src/inference/hf_backend.py` | `HFBackendSession.close` | 323–340 | 0-based |
| vocab-bounded token validation | `src/inference/hf_backend.py` | `HFBackendSession._validated_token_ids` | 636–668 | 0-based |
| digest computed and registered at mint | `src/inference/hf_backend.py` | `HFBackendSession._new_exact_history` | 670–689 | 0-based |
| forged/cross/closed rejection | `src/inference/hf_backend.py` | `HFBackendSession._validated_exact_history` | 691–715 | 0-based |
| manifest digest + complete-row gate | `scripts/research/run_continuation_locality_boundary_scoring.py` | `_validated_boundaries` | 46–71 | 0-based |
| history construction, two-token scoring, FP32 subtraction, receipt | `scripts/research/run_continuation_locality_boundary_scoring.py` | `run` | 74–291 | 0-based |
| legacy FP32 subtraction semantics | `scripts/research/run_next_row_likelihood_change.py` | `terminal_boundary_score` | 225–241 | 0-based |
| immutability/digest invariant | `tests/inference/test_hf_exact_history_evidence.py` | `test_exact_history_prepare_and_literal_immutable_append` | 214–243 | 0-based |
| forged/cross/closed invariant | `tests/inference/test_hf_exact_history_evidence.py` | `test_exact_history_rejects_forged_cross_session_and_closed_use` | 262–290 | 0-based |
| position/logprob/rank/evidence-only invariant | `tests/inference/test_hf_exact_history_evidence.py` | `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence` | 330–380 | 0-based |
| reduction preservation invariant | `tests/research/test_continuation_locality_owner_compositionality.py` | `test_owner_state_score_preserves_existing_reduction_from_token_evidence` | 88–194 | 0-based |
| shared sharding invariant | `tests/research/test_continuation_locality_owner_compositionality.py` | `test_locality_and_owner_runners_use_the_same_stable_sharding` | 37–41 | 0-based |

---

## Verdicts A–D

**A. Literal fixed-prefix identity — YES.** The manifest re-verifies `base_prompt_token_ids_sha256` and `prefix_token_ids_sha256` before any model work; the runner independently re-derives the prompt and asserts equality, asserts the image content hash, asserts the materialized `history.conditioning_token_ids == base_prompt`, and records `actual_prompt_plus_prefix_token_ids_sha256` computed from the exact concatenation. `_validated_exact_history` additionally recomputes the digest at every use.

**B. Fixed-prefix relative preference between the two selected next tokens — YES.** Both scalars are full-vocabulary FP32 log-softmax values at the same boundary index over the same conditioning, and their difference is taken in FP32 (`row_entry_minus_terminal`), matching `terminal_boundary_score`. Qualifier: the two values come from two separate forward passes on identical inputs rather than one shared slice; they are deterministic teacher-forced forwards, but bitwise identity of the shared prefix logits is not asserted anywhere in code or tests.

**C. Free-rollout behavior — NO.** Nothing is sampled or generated. The `DecodeRequest` exists only to materialize native inputs (`max_new_tokens=1`, `temperature=0.0`), the prefix is forced verbatim from the manifest, and exactly two single-token teacher-forced probes are scored. The receipt states this explicitly: `claim_boundary: "fixed-prefix boundary scores are not free-rollout outcomes"`.

**D. Reconstruction of the full vocabulary distribution — NO.** `HFChosenTokenEvidence` returns only `token_id`, `raw_model_logprob`, `candidate_vocab_rank`; the logits tensor is discarded inside `teacher_forced_evidence` and the test asserts the evidence object has no `logits` attribute. The locality receipt stores only the two chosen-token logprobs and their difference — it does not even persist the ranks. Rank is a tie-optimistic strict-greater count and carries no mass information.

---

## Tool ledger (execution order)

| tool | purpose | outcome |
|---|---|---|
| `Read` (`ablation-prompt.md`) | Read the locked benchmark prompt (task-mandated) | Full prompt obtained |
| `ToolSearch` `select:mcp__serena-light__activate_project` | Locate assigned-MCP activation tool | No match (tool name differs) |
| `Bash`: `pwd; git -C … rev-parse HEAD; git -C … status --porcelain` | Snapshot working dir + both Git HEADs + dirty state | **Non-source environment/Git metadata only — not a source read or search.** Both HEADs and modified-file lists captured |
| `ToolSearch` `+serena-light activate project workspace` | Load assigned-MCP schemas | 10 `serena-light` tools loaded |
| `mcp__serena-light__activate_workspace` (`/data/CoordExp/serena-light`) | Absolute workspace activation, arm 1 | Lease bound |
| `find_symbol` `WorkspaceRuntime/find_symbol` (body) | Branch contract | Body 1694–1780 |
| `get_symbols_overview` `runtime.py` depth 2 | Map runtime seams | Full class/member map (390 omitted) |
| `find_symbol` `WorkspaceRuntime/_tool_envelope` (body) | Freshness ordering + typed mapping | Body 2060–2086 |
| `find_symbol` `FreshnessCoordinator/ensure_fresh` (body) | Shared-scan concurrency | Body 728–761 |
| `find_symbol` `FreshnessCoordinator/_scan_git` (body) | Change classification | Body 812–906 |
| `find_symbol` `WorkspaceRuntime/_warm_global_candidates` (body) | Global warm budget | Body 1782–1863 |
| `find_symbol` `WorkspaceRuntime/_route` (body) | Per-path freshness, family errors | Body 1644–1670 |
| `find_symbol` `DocumentNavigationService/find_symbol_in_documents` (body) | Directory bounds | Body 160–243 |
| `find_symbol` `GlobalSymbolService/find_symbol` (body) | Global bounds/readiness | Body 205–387 |
| `find_symbol` `FreshnessCoordinator/_apply_events` (body) | Delivery semantics | Body 908–1019 |
| `find_symbol` `WorkspaceRuntime/install_freshness` (body) | Restart/retirement | Body 1244–1374 |
| `find_symbol` `test_find_symbol` (global, substring) | Locate tests | `SYMBOL_NOT_FOUND` (tests are outside the configured program) — recovered by directory scope, no shell fallback needed |
| `get_symbols_overview` `tests/unit/test_workspace_runtime_semantics.py` | Enumerate semantics tests | Full list (77 omitted) |
| `find_symbol` `test_freshness*` / `concurrent*` in `tests/unit` (directory scope) | Locate freshness + concurrency tests | 3 + 7 matches |
| `find_symbol` ×5 test bodies (`…symlink…`, `…concurrent…`, `…directory_find_symbol…`, `…create_change_delete…`, `…only_loads_workspace_symbol_candidates`) | Invariant evidence | All bodies retrieved |
| `mcp__serena-light__activate_workspace` (`/data/CoordExp/.worktrees/research-probes`) | Rebind lease to arm-2 target | Lease rebound |
| `find_symbol` `HFBackendSession/prepare_exact_history`, `extend_exact_history`, `teacher_forced_evidence`, `_new_exact_history`, `_validated_exact_history` (bodies) | Session-owned identity + evidence math | All bodies retrieved |
| `find_symbol` `HFExactHistory`, `_require_live_session`, `_validated_token_ids`, `HFChosenTokenEvidence`, `_HFExactHistoryContext`, `open_hf_backend_session` (bodies) | Identity/validation/receipt semantics | All bodies retrieved |
| `find_symbol` `HFBackendSession/close` (global, no path) | Close semantics | Empty result (global index scoping); retried with `relative_path` |
| `get_symbols_overview` `hf_backend.py` depth 2 (kind-filtered) | Confirm module surface | Class/function list |
| `find_symbol` `close` substring in `hf_backend.py` | Disambiguate | `AMBIGUOUS_SYMBOL` with candidates → exact `name_path` |
| `find_symbol` `HFBackendSession/close` (path-scoped, body) | Close semantics | Body 323–340 |
| `get_symbols_overview` + `find_symbol run` on `run_continuation_locality_boundary_scoring.py` | Runner trace | Body 74–291 |
| `find_symbol` `_validated_boundaries` (body) | Manifest gate | Body 46–71 |
| `find_symbol` `exact_history` / `boundary_scoring` / `locality` / `continuation` in `tests` (directory scope, substring) | Locate Q2 tests | 5 + 0 + 1 + 7 matches |
| `find_referencing_symbols` `run` (runner) | Check for a direct runner test | Only `main` in-file; no test imports `run` |
| `find_symbol` ×5 Q2 test bodies | Invariant evidence | All bodies retrieved |
| `find_declaration` `import (OBJECT_REF_START)` | Resolve the row-entry token constant | `run_next_row_likelihood_change.py:49` → `151646` |
| `find_declaration` `(MANIFEST_SCHEMA_VERSION)` | Resolve manifest schema constant | `AMBIGUOUS_SYMBOL` (3 occurrences); not pursued — not decision-bearing |
| `find_symbol` `terminal_boundary_score` (body) | Legacy subtraction semantics | Body 225–241 |
| `mcp__serena-light__release_workspace` (`immediate=true`) | Release the Serena Light workspace before completing | `released: true`, `active_holders: 0`, `runtime_stopped: true` |

**No shell source read or search (`rg`/`sed`/`cat`/`grep`) was used at any point.** The single `Bash` call was Git/environment metadata. Canonical Serena and all other MCP/app tools were never called. Nothing was modified.

---

## Confidence and unresolved ambiguity

**Confidence: high** for Q1 branch structure, freshness coordination, and Q1 test invariants; **high** for Q2 identity/immutability/rank/logprob semantics and verdicts A, C, D; **medium-high** for verdict B.

Unresolved:
1. **Verdict B numeric qualifier.** The two probes run two separate forwards over prefixes that share every conditioning position. Determinism of the shared-prefix logits across those passes is expected but is not asserted by any test or code check I found.
2. **Q1 files are dirty.** `runtime.py`, `navigation.py`, `global_symbols.py` and `test_workspace_runtime_semantics.py` are modified relative to `9e4987e9f`. All Q1 claims describe the **working-tree** state, not HEAD. `test_workspace_runtime.py` (three of my five cited tests) is clean.
3. **No test exercises the boundary-scoring runner's `run` end to end.** `find_referencing_symbols` found no external caller. Coverage of the runner is indirect: `_stable_shard` parity and the reduction-preservation test against `terminal_boundary_score`/`_score_candidate` via a fake session. The manifest-validation path (`_validated_boundaries`) has no direct test that I located.
4. **`MANIFEST_SCHEMA_VERSION` origin unresolved** (ambiguous regex, three occurrences); I did not chase it because the version value does not affect any verdict.
5. **`get_symbols_overview` on `runtime.py` reported 390 omitted symbols** and `hf_backend.py` 319; both were kind- or depth-limited views. I resolved every symbol I needed by exact `name_path`, but I did not exhaustively enumerate either module.
