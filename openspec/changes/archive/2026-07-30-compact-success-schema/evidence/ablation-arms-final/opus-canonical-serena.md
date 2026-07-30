## Snapshot

| target | Git HEAD | relevant files modified? |
|---|---|---|
| `/data/CoordExp/serena-light` | `9e4987e9f2190a4ff03cb7a35359483a5387f327` | **Yes.** `src/serena_light/workspace/runtime.py`, `tools/navigation.py`, `tools/global_symbols.py`, `tests/unit/test_workspace_runtime_semantics.py` all show ` M` (uncommitted). `tests/unit/test_workspace_runtime.py` is clean. Q1 evidence is the working tree, not HEAD. |
| `/data/CoordExp/.worktrees/research-probes` | `ccdadc4e2d8c00a091dde8d684a14982f05715f2` | **No.** `src/inference/hf_backend.py`, `scripts/research/run_continuation_locality_boundary_scoring.py`, `tests/inference/test_hf_exact_history_evidence.py`, `tests/research/test_continuation_locality_owner_compositionality.py` are all clean at HEAD. |

Line convention: every MCP-derived range is **0-based** (Serena `body_location`); the two shell reads are **1-based** and labelled.

---

## Question 1 — `WorkspaceRuntime.find_symbol`

**1. Freshness before the operation.** The entire method body is a closure `operation()` handed to `self._tool_envelope(operation)`. `_tool_envelope` calls `self.ensure_fresh()` *first*, then `operation()`, and maps escapes to typed envelopes (`WorkspaceError` passthrough; `WorkspaceRuntimeError` → its `ErrorCode`, retryable for `BUSY`/`NOT_READY`/`TIMED_OUT`; bare `TimeoutError` → `TIMED_OUT`; `OSError/TypeError/ValueError` → `INVALID_INPUT`). `WorkspaceRuntime.ensure_fresh` is `_require_running()` then `FreshnessCoordinator.ensure_fresh()`. Additionally the **exact-file branch only** calls `self._route(...)`, which calls `_freshness.ensure_path_fresh(normalized)` — the targeted single-operand stat used on the non-Git read-only root.

**2. The three branches.**

*Exact file* (`relative_path` present and in `inventory.paths`): `_route` normalizes, stats the operand, rejects unnormalized `..`, authorizes the path against `WorkspacePathPolicy` bounded by the inventory, **raises the stored `self._family_errors[family]` if that family is unavailable**, and requires exactly one routing adapter (else `UNSUPPORTED`). Delegates to `DocumentNavigationService.find_symbol` — one document.

*Inventory-bounded directory* (`relative_path` present, not a file): `inventory.paths_under(scope)`; candidates are filtered to `_family_of(path) in self._adapters`. If the selection is non-empty but **no** path has an available family → `WorkspaceRuntimeError(SCOPE_INCOMPATIBLE, "directory scope contains only unavailable language families")` carrying the union of `_family_errors[...].paths`. Otherwise it delegates to `DocumentNavigationService.find_symbol_in_documents(available, …)`, which searches exactly that explicit set (sorted, deduplicated), never walking the workspace, and truncates by `max_answer_chars` with `TruncationMetadata`. Note the partial-availability case is *silent*: unavailable families are simply dropped from the searched set. `max_candidates_per_adapter != 128` with any `relative_path` is rejected as `UNSUPPORTED` (global-only knob).

*Global*: if `not self._adapters and self._family_errors` → `SCOPE_INCOMPATIBLE, "all attributed language families are unavailable"`. Otherwise `_warm_global_candidates` polls not-yet-ready adapters within `min(30s, future_timeout) − 0.5s`, using one deterministic configured-program document per family as a readiness witness (never `__init__.*` when avoidable), then `GlobalSymbolService` runs over one `_GlobalProvider` per **available** adapter. That service bounds `max_candidates_per_adapter` to `[1, MAX_CANDIDATES_PER_ADAPTER]`, returns `UNSUPPORTED` if any provider lacks `workspace/symbol`, `NOT_READY` (retryable, min `retry_after_seconds`) if any provider is not `global_ready`, scopes candidates to each adapter's `configured_program`, loads `documentSymbol` only for candidate files, and re-checks adapter/scope/generation equality before *and* after each provider — any drift yields `_generation_not_ready`.

**3. `ensure_fresh` / `_scan_git` coordination.** On a non-Git root `ensure_fresh` returns an empty `FreshnessScan` immediately. On Git, `_in_flight` holds one `_SharedScan` (a `threading.Event` plus `result`/`failure`): the first caller owns the scan, later callers block on `done` and receive the *identical* result object or the owner's raised exception. `finally` clears `_in_flight`, commits `_last` only on success, then sets `done`. There is no time-based cache, so every operation rebuilds.

`_scan_git` order: `retry_pending_restarts()` → `_settle_pending_reconciles()` → `rebuild_inventory()`. It then distinguishes three change classes:

- **Content**: `content_identity` differs for a path present before and after → `changed`.
- **Membership/symlink**: set difference of `inventory.paths` → `created`/`deleted`; `symlinked` = rejected entries whose reason starts with `symlink` (a substituted file leaves `paths`, enters `rejected`, and is reported as *both* `deleted` and `symlinked`); `membership_changed` also fires on any change to the rejected set.
- **Native config**: `_native_config_candidates` derives a bounded watch set from each trusted source's *directory ancestry* plus each projection's `selected_config_path`; absent candidates stay in the state map so a newly created nearer config is visible on the next scan without a background watcher.

Any untrusted source, or any config state with a reason other than `missing`, raises `NOT_READY` with the offending paths **before** state is committed. If nothing changed, an empty scan returns.

Observable consequences the code establishes:
- **Inventory**: swapped atomically inside `install_freshness` under `_state_lock`; a stopping/stopped runtime raises `STOPPED`.
- **Adapter restart / reattribution**: `restart_families = _affected_families((), config_changed)` — only native-config changes restart adapters. For each, the tracker's projection is updated and, if the `configured_program` generation is unchanged, synthetic `may_change_program=True` watched events are applied; the adapter is popped and a `_PendingAdapterRestart` plus a `NOT_READY` "adapter restart is in progress" family error are published in one step, so no ownerless gap exists. Stop timeout installs `TIMED_OUT` and retries on a later scan. A failed attribution retires the adapter and installs the typed `_family_errors` entry instead.
- **Watched-file delivery**: `_apply_events` advances every affected family's generation *unconditionally first* (events partitioned per family, so one family's churn cannot invalidate another's configured program), publishes `_pending_reconciles` ownership for all runnable batches before admitting any to the executor, opens at most `MAX_CONTROLLED_OPENS` created files and reports the rest as `unopened`. When installation failed, `wait_for_delivery=False`, so healthy-family facts still advance and enqueue exactly once before the family-local failure is raised.
- **Typed failures**: `BUSY` (executor full, or a batch still pending), `NOT_READY` (submission/settle failure, or duplicate pending ownership for a family), `STOPPED` re-raised; the first failure wins. A failed future is retired so a later scan can retry.

**4. Tests.**

| test | invariant |
|---|---|
| `test_concurrent_freshness_callers_share_one_scan_and_no_time_cache_authorizes_reuse` (0-based 1118–1158) | Two concurrent callers cause exactly one rebuild and receive the *same object*; a completed scan is never reused (third call rebuilds). |
| `test_freshness_detects_symlink_substitution_and_native_config_change` (1161–1189) | Symlink substitution yields `symlinked` + `deleted`, removal from `inventory.paths`, and a `("main.py","symlink")` rejection; creating `pyrightconfig.json` yields `config_changed` and `reattributed == (PYTHON,)`. |
| `test_directory_find_symbol_is_bounded_by_inventory_prefix_without_workspace_walk` (1377–1412) | Directory scope issues exactly two `documentSymbol` requests (both under `src/`, never the sibling) and rejects `max_candidates_per_adapter` with `UNSUPPORTED` **without** issuing further requests. |
| `test_global_find_symbol_only_loads_workspace_symbol_candidates` (1090–1115) | Global scope loads only the `workspace/symbol` candidate document; request order is exactly `workspace/symbol` then `textDocument/documentSymbol`. |

**5.** *Prevented failure*: a tracked source replaced by a symlink is dropped from the inventory and delivered as `DELETED` by the same-operation scan, so a subsequent directory- or global-scope `find_symbol` cannot resolve a symbol through an untrusted path. *Remaining limitation*: only the **exact-file** branch calls `_route`/`ensure_path_fresh`. On a non-Git allowlisted read-only root `ensure_fresh` is a no-op, so directory-scope and global-scope queries revalidate **nothing** on that root.

### Q1 evidence

| claim | file | symbol | line range (0-based) |
|---|---|---|---|
| branch structure, scope guards | `src/serena_light/workspace/runtime.py` | `WorkspaceRuntime/find_symbol` | 1694–1780 |
| freshness precedes operation; error mapping | same | `WorkspaceRuntime/_tool_envelope` | 2060–2086 |
| exact-file routing, family-error raise | same | `WorkspaceRuntime/_route` | 1644–1670 |
| shared in-flight scan, no cache | same | `FreshnessCoordinator/ensure_fresh`, `_SharedScan` | 728–761, 691–699 |
| change classes, unsafe-before-commit | same | `FreshnessCoordinator/_scan_git` | 812–906 |
| generation-first, ownership, opens | same | `FreshnessCoordinator/_apply_events` | 908–1019 |
| typed BUSY/NOT_READY settle | same | `FreshnessCoordinator/_settle_pending_reconcile` | 1035–1097 |
| restart/retirement ownership | same | `WorkspaceRuntime/install_freshness` | 1244–1374 |
| config watch set / affected families | same | `_native_config_candidates`, `_affected_families` | 3254–3278, 3289–3296 |
| explicit document set, no walk | `src/serena_light/tools/navigation.py` | `DocumentNavigationService/find_symbol_in_documents` | 160–243 |
| global readiness + generation checks | `src/serena_light/tools/global_symbols.py` | `GlobalSymbolService/find_symbol` | 205–387 |

---

## Question 2 — Exact-history evidence seam

**1. Identity, immutable extension, digests, forgery.** `HFExactHistory` is `@dataclass(frozen=True, eq=False)` with only `request_id`, `conditioning_token_ids`, `conditioning_token_ids_sha256` — identity-based equality/hash, no tensors, no session field. The session holds `WeakKeyDictionary[HFExactHistory, _HFExactHistoryState]`. `prepare_exact_history` materializes native inputs once from the `DecodeRequest`, validates the executed prompt IDs, and registers state with a live `_HFExactHistoryContext`. `extend_exact_history` validates the handle, validates the appended IDs, and returns a **new** handle sharing the same context — no decode, no re-tokenization, parent untouched. `_validated_token_ids` rejects `bool`, non-`int`, negative, and `>= vocab_size` (`hf_backend.invalid_token_id`), and fails closed when vocabulary size is unknown (`hf_backend.vocab_size_unavailable`). `_validated_exact_history` requires the object to be a registered key *and* `request_id`, tuple, stored digest, and **recomputed** digest to all agree — a field-identical forged copy fails on identity, a foreign session fails on registry lookup, both with `hf_backend.exact_history_session`; a closed session fails earlier with `hf_backend.session_closed`. `close()` nulls each distinct context's `native_inputs`, clears the registry, and drops model/processor/tokenizer.

**2. Positions, CPU FP32, reductions.** `full_token_ids = conditioning + continuation`; one forward with `use_cache=False`, `return_dict=True`, `logits_to_keep=0`, and `position_ids` derived *at use time* by `_derive_qwen_position_ids` from `image_grid_thw` (missing → `hf_backend.exact_history_image_grid`). `boundary = len(conditioning) - 1`; the slice is `logits[0, boundary : boundary+len(continuation), :]` — the next-token distribution at the last conditioning position plus one per continuation token except the last. A shape mismatch raises `hf_backend.teacher_forced_alignment`. The slice is `.detach().to(device="cpu", dtype=torch.float32).contiguous()`: the reduction is done off-device in a fixed precision regardless of the model's runtime dtype (bf16/fp16), and the returned evidence carries no device tensors and no live graph. `raw_model_logprob` = `F.log_softmax(selected_logits, dim=-1)` gathered at the chosen ID (full-vocabulary normalization). `candidate_vocab_rank` = `(selected_logits > selected_values).sum(dim=1) + 1` — a **strict** greater-than count, so all tied logits receive the same, best rank; rank 1 means no strictly larger logit exists.

**3. The boundary-scoring runner.** `_validated_boundaries` enforces the manifest schema, unique non-empty `boundary_id`, well-formed token-ID lists, both `base_prompt`/`prefix` sha256 digests, and `prefix[-1] == 151649` (`box_end`, i.e. a complete-row boundary). Selection is a stable sha256 shard over `boundary_id`; the output path must not exist. Backend must be `hf`; `--runtime-dtype fp32` overrides `model.dtype`. Per boundary the image plan is rebuilt and its `image_content_sha256` and the reconstructed `base_prompt_token_ids` are asserted against the manifest. The `DecodeRequest` uses `max_new_tokens=1, temperature=0.0, top_p=1.0, repetition_penalty=1.0, include_raw_model_logprob=True`. Then `prepare_exact_history` (asserting materialized conditioning == base prompt), `extend_exact_history(prefix)`, and **two separate single-token** `teacher_forced_evidence` calls on the *same* history: `(OBJECT_REF_START=151646,)` and `(terminal_id=im_end,)`. Because causal attention makes the appended token irrelevant to position `boundary`, both are scored from the same distribution at the same index. Subtraction semantics are preserved by round-tripping both scalars through `torch.float32` tensors and recording `row_entry_log_probability`, `terminal_log_probability`, and `row_entry_minus_terminal`. Provenance recorded: manifest path+sha, authored/effective/resolved config fingerprints, source JSONL + sha, `runtime_dtype_mode`, observed `model_dtype`, attention implementation, processor–model vision parity, the full backend-session receipt, `eos_token_id`, `row_entry_token_id`, `physical_batch_size: 1`; per record, `prefix_token_ids_sha256` and a recomputed `actual_prompt_plus_prefix_token_ids_sha256`. The receipt carries the literal `claim_boundary`: *"fixed-prefix boundary scores are not free-rollout outcomes"*.

**4. Tests.**

| test | what it proves |
|---|---|
| `test_exact_history_prepare_and_literal_immutable_append` (0-based 214–243) | Append is literal and immutable: parent stays `(11,12)`, child is `(11,12,13,1,3)` with the matching digest, tokenizer `decode_calls` unchanged, public fields exactly the three audit fields, no tensors, no `_session_binding`. |
| `test_exact_history_rejects_forged_cross_session_and_closed_use` (262–290) | Forged copy and cross-session handle both raise `hf_backend.exact_history_session`; post-`close` raises `hf_backend.session_closed`; **no forward ran** in either model. |
| `test_teacher_forced_evidence_builds_positions_at_use_and_returns_only_evidence` (330–380) | Position IDs are built per call; logprobs equal FP32 `log_softmax` of the reference logits; ranks `[2,2,4]` show tie-sharing; the record exposes only the three fields (`not hasattr(..., "logits")`); forwards use `use_cache=False`, `logits_to_keep=0`. |
| `test_owner_state_score_preserves_existing_reduction_from_token_evidence` (88–194) | The evidence-based state score reproduces the legacy `terminal_boundary_score`/`_score_candidate` reductions exactly, with call order `[(terminal_id,), row]` and both prefix digests. |
| `test_locality_and_owner_runners_use_the_same_stable_sharding` (37–41) | The locality runner's `_stable_shard` matches the owner probe's, so shard assignment is reproducible across runners. |

### Q2 evidence

| claim | file | symbol | line range (0-based) |
|---|---|---|---|
| audit-only identity fields | `src/inference/hf_backend.py` | `HFExactHistory` | 55–61 |
| weak registry, session state | same | `HFBackendSession/__init__`, `_new_exact_history` | 91–111, 670–689 |
| materialize + retain context | same | `prepare_exact_history` | 127–146 |
| literal immutable append | same | `extend_exact_history` | 148–161 |
| position slice, CPU FP32, logprob/rank | same | `teacher_forced_evidence` | 163–280 |
| digest + identity validation | same | `_validated_exact_history` | 691–715 |
| token-ID domain validation | same | `_validated_token_ids` | 636–668 |
| context teardown on close | same | `close` | 323–340 |
| manifest/prefix validation | `scripts/research/run_continuation_locality_boundary_scoring.py` | `_validated_boundaries` | 46–71 |
| history construction, two scorings, subtraction, receipt | same | `run` | 74–291 |
| `OBJECT_REF_START = 151646` | `scripts/research/run_next_row_likelihood_change.py` | constant (shell, **1-based** line 49) | 49 |

---

## Verdicts A–D

- **(A) Literal fixed-prefix identity — YES.** Manifest digests are verified pre-run; the materialized conditioning is asserted equal to `base_prompt_token_ids`; the prefix is appended as literal vocabulary IDs with no re-tokenization; the receipt stores `prefix_token_ids_sha256` and a recomputed `actual_prompt_plus_prefix_token_ids_sha256`; the handle is bound to live session state by `_validated_exact_history`.
- **(B) Fixed-prefix relative preference between the two selected next tokens — YES.** Both scores are full-vocabulary `log_softmax` values read at index `boundary = len(conditioning) - 1` of the same conditioning; under causal attention the single appended token cannot affect that position, so the two calls score the same distribution, and `row_entry_minus_terminal` is recorded together with both absolutes in FP32. Caveat: this is two forwards, and the code asserts alignment but does not assert bitwise equality of the shared position.
- **(C) Free-rollout behavior — NO.** Nothing generates: `max_new_tokens=1` is carried on the request but decoding is never invoked; only teacher-forced scoring at a frozen prefix occurs, and the receipt states the claim boundary explicitly.
- **(D) Full vocabulary distribution — NO.** `HFChosenTokenEvidence` carries only `token_id`, `raw_model_logprob`, `candidate_vocab_rank`; `selected_logits` are discarded after the reduction and the test asserts no `logits` attribute. Two scalars plus a strict-greater rank cannot reconstruct the ~152k-dim distribution.

---

## Tool ledger (execution order)

| tool | purpose | outcome |
|---|---|---|
| `Read` (host) | read the locked prompt itself | ok |
| `ToolSearch` | load canonical `serena` schemas | ok (serena-light never called) |
| `Bash` `pwd; git -C … rev-parse HEAD; git status --porcelain` ×2 roots | **non-source env/Git metadata** (allowed directly) | HEADs + dirty sets |
| `mcp__serena__initial_instructions` | required init | ok; confirms 0-based lines |
| `mcp__serena__activate_project /data/CoordExp/serena-light` | absolute activation | ok |
| `find_symbol WorkspaceRuntime/find_symbol` (body) + `get_symbols_overview runtime.py` | contract + file map | ok |
| `find_symbol` ×4: `_tool_envelope`, `ensure_fresh`, `_route`, `_warm_global_candidates` | freshness hook, routing, warm-up | ok |
| `find_symbol` ×4: `FreshnessCoordinator/ensure_fresh`, `_scan_git`, `ensure_path_fresh`, `_SharedScan` | concurrency + change classes | ok |
| `find_symbol` ×3 (`_apply_events`, `_settle_pending_reconciles`, `_config_states_for`) + `get_symbols_overview navigation.py` | delivery + typed failures | ok |
| `find_symbol` ×4: `DocumentNavigationService/find_symbol`, `/find_symbol_in_documents`, `GlobalSymbolService/find_symbol`, `_settle_pending_reconcile` | per-branch delegates | ok |
| `find_symbol` ×3 (`_affected_families`, `_native_config_candidates`, `install_freshness`) + `find_symbol "find_symbol" in tests` (substring) | reattribution + test discovery | ok |
| `find_symbol` ×3 substring (`test_fresh`, `test_unavailable`, `test_concurrent`) | locate freshness tests | 2 empty, 1 hit → pointed to `test_workspace_runtime.py` |
| `find_symbol "fresh" in tests/unit/test_workspace_runtime.py` (substring) | enumerate freshness tests | ok |
| `find_symbol` ×4 test bodies | invariants | ok |
| `mcp__serena__activate_project /data/CoordExp/.worktrees/research-probes` | switch target | ok |
| `find_symbol HFBackendSession` (depth 1) | class map | ok |
| `find_symbol` ×4: `prepare_exact_history`, `extend_exact_history`, `teacher_forced_evidence`, `_validated_exact_history` | seam bodies | ok |
| `find_symbol` ×4: `_new_exact_history`, `_validated_token_ids`, `close`, `HFExactHistory` | identity/digest/teardown | ok |
| `find_symbol` ×2 (`__init__`, `_require_live_session`) + `get_symbols_overview` runner | weak registry; runner map | ok |
| `find_symbol run`, `_validated_boundaries` (bodies) | runner trace | ok |
| `find_symbol` ×3 substring in `tests` (`exact_history`, `teacher_forced`, `continuation_locality`) | test discovery | ok |
| `get_symbols_overview` test file + `find_symbol` ×3 test bodies | HF test invariants | ok |
| `find_symbol boundary_scoring in tests`, `find_declaration OBJECT_REF_START`, `find_symbol boundary in tests/research` | locate runner tests / constant | `find_declaration` resolved to `run_next_row_likelihood_change.py:49`, but its symbol body rendered as the bare name — **no value** |
| `find_symbol OBJECT_REF_START` (body) + 2 overviews | retry constant value | **failed again** (assignment body not rendered) |
| `find_symbol` ×3 compositionality test bodies **+ `Bash sed -n '44,52p' …run_next_row_likelihood_change.py; sed -n '1,45p' …boundary_scoring.py`** | constant value + runner imports | **fallback** — justified: this Serena build exposes no `read_file`/`search_for_pattern`, and `find_symbol`/`find_declaration` returned the constant's *name* without its literal value, and module-level imports are not symbols. 1-based lines. |
| `Bash grep -n OBJECT_REF_START/END …run_complete_candidate_row_scoring.py` ×2 | confirm the imported constant is re-exported, not redefined | **fallback** — same MCP limitation (import statements are not addressable symbols). Confirms `OBJECT_REF_START = 151646`. |

No file, process, or repository state was modified.

## Confidence and unresolved ambiguity

**High** for Q1 items 1–2, Q1 item 3's coordination/typed-failure mechanics, all of Q2, and verdicts A, C, D. **Medium-high** for verdict B: the causal argument is sound and the code scores the identical index, but the two forwards are separate calls and the code asserts only shape alignment, not numerical equality across them.

Unresolved: (i) Q1 evidence is read from a **dirty** working tree — `runtime.py`, `navigation.py`, `global_symbols.py` and `test_workspace_runtime_semantics.py` carry uncommitted edits, so this contract is not reproducible from `9e4987e9f`; (ii) no test was found that exercises the boundary-scoring runner's `run()` end-to-end (`tests/analysis/prefix_state_transition_tomography/test_boundary_scoring.py` covers a different scorer), so provenance-field completeness is established by reading `run` rather than by a test; (iii) I did not execute any test, so all cited tests are current *definitions*, not verified-green results.
