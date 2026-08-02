## 1. Freeze the admission baseline and red contracts

- [x] 1.1 Record the implementation-start Serena Light commit, dirty-file ownership, schema version, source build identity, instruction byte length/hash, public tool count, aggregate description characters, repeated-prefix characters, and the two accepted failure payloads from the paired Luna benchmark. Keep official Serena and `cc-plugin-codex` read-only and out of this change.
- [x] 1.2 Add failing metadata tests that require one byte-identical initialization instruction at the outer connector and inner daemon boundaries, cap that instruction at 220 characters, keep schema version 4 and the existing 11-tool surface, and prove that the owning tool descriptions jointly retain every required workflow instruction.
- [x] 1.3 Add failing recovery tests for a file-scoped `SYMBOL_NOT_FOUND`, a directory/global miss, a bound-workspace `INVALID_PATH`, an unchanged `AMBIGUOUS_SYMBOL`, and a 512-character error budget. Assert the exact closed `details.next_action` values and that workspace/action survive before echoed query fields.
- [x] 1.4 Add negative assertions that recovery presentation performs no semantic dispatch, filesystem discovery, lease mutation, automatic activation, fuzzy lookup, lexical search, or retry. If any proposed implementation changes success payloads, freshness, diagnostics, editing, lifecycle, supported languages, tool inputs, or tool count, stop and revise the change before implementation.

## 2. Tighten initialization and tool-local guidance

- [x] 2.1 Replace `AGENT_INSTRUCTIONS` with the approved 214-character text from `design.md`, preserving exact bytes across source, connector initialization, daemon initialization, and every repeated public-tool prefix.
- [x] 2.2 Move operation-specific advice into the existing descriptions for `activate_workspace`, `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, both diagnostics tools, and `get_runtime_status`: startup-cwd binding and explicit absolute-root switching, depth-0 overview before an unfamiliar exact lookup, qualified-candidate ambiguity retry, opt-in snippets, explicit post-edit diagnostics, and debug-only status.
- [x] 2.3 Extend `tests/unit/test_daemon_server.py` and connector/stdio acceptance coverage to prove fresh metadata contains the complete workflow without adding a hook, a separate instructions call, automatic diagnostics, or any public tool/schema/input change.
- [x] 2.4 Re-run the description census and record the new instruction length, common-prefix length, total description characters, and repeated-prefix characters. Treat the measured reduction as evidence, not as a permanent compatibility constant or a substitute for semantic tests.

## 3. Add bounded deterministic recovery hints

- [x] 3.1 Introduce one presentation-owned closed recovery-action representation whose serialized values are exactly `get_symbols_overview` and `activate_workspace_if_other_root`; reject free-form advice and unknown action values in tests.
- [x] 3.2 For an existing authorized file scoped by `find_symbol`, enrich only typed `SYMBOL_NOT_FOUND` with `details.next_action="get_symbols_overview"`. Preserve the original path/name echo, adapter authority, generations, and error code internally, and add no file-only overview action to directory-scoped or workspace-global misses.
- [x] 3.3 At the bound query boundary, enrich typed `INVALID_PATH` with the active workspace and `details.next_action="activate_workspace_if_other_root"`. Scope this to semantic/diagnostic query calls; do not alter activation validation, editing errors, lease state, root allowlists, or infer a target workspace from the rejected path.
- [x] 3.4 Update deterministic error bounding so code, message, active workspace, and a recognized `next_action` are retained ahead of long echoed path/name/query values at the 512-character minimum public budget. Keep non-deterministic operational errors rich and unchanged.
- [x] 3.5 Preserve strict ambiguity behavior and its bounded qualified candidates exactly; tests must demonstrate that retry guidance comes from returned candidates/tool metadata rather than automatic selection or another RPC.
- [x] 3.6 Cover Python and TypeScript through unit/integration fixtures and the real FastMCP boundary. For each new deterministic failure, assert canonical text equals `structuredContent`, `isError` remains false for the typed application error, and serialization respects `max_answer_chars`.

## 4. Prove regression safety and build isolation

- [x] 4.1 Run the focused instruction, daemon-server, error-presentation, document-navigation, compact-navigation, connector-contract, and stdio-connector suites; capture exact commands and results in the change acceptance evidence.
- [x] 4.2 Run the complete `pytest`, Ruff, Ty, bootstrap, direct-dependency/source-ownership, copied-source provenance/hash, census/manifest, informational LOC, and strict OpenSpec gates. A production import/provenance mismatch or any failed deterministic contract is blocking.
- [x] 4.3 Verify the source edit selects a new existing build-identity slot while a leased prior build remains usable and isolated; retire only through normal zero-holder/grace behavior and do not kill or broadly clean legacy or canonical Serena processes.
- [x] 4.4 Exercise real Serena Light connector calls in `/data/CoordExp/serena-light` and `/data/CoordExp/.worktrees/research-probes`: unfamiliar-file overview to exact lookup, qualified ambiguity retry, an intentional file-scoped miss, an intentional stale-root path followed by explicit `activate_workspace`, and a return activation to the first root. Confirm intervening shell `cd` never changes the binding.
- [x] 4.5 Inspect the scoped diff and runtime/tool census for unintended behavior or surface growth. Treat MCP/server faults as blockers; label caller orchestration truncation separately and do not add a batch RPC or retry protocol to solve it.

## 5. Fresh-client evidence and release discipline

- [x] 5.1 Restart fresh Codex and Claude Code/CC Agent clients against the exact new build and record their initialization instructions, schema version, 11 public tools, resolved build identity, active workspace behavior, and absence of canonical-name/client-registration changes.
- [x] 5.2 Run one bounded Serena-Light-only Luna/medium smoke over the same two-repository workflow used for the baseline. Record semantic correctness, MCP calls, shell calls, guessed-name misses, stale-root misses, ambiguity, truncation, and recovery-action use; these counts are observational and cannot override deterministic acceptance.
- [x] 5.3 Stop after reporting the smoke if semantic correctness regresses, a recovery hint is wrong or non-deterministic, or any automatic rebind/retry occurs. Otherwise do not start another ablation or tune to the sampled model behavior in this change.
- [x] 5.4 Update README, client-registration/compatibility material, roadmap, and acceptance evidence with the compact workflow, explicit activation rule, closed recovery actions, build identity, environment preconditions, and the distinction between MCP failure and model/orchestration behavior.
- [x] 5.5 After every blocker is dispositioned and strict validation passes, sync the two delta specs, archive `tighten-query-recovery`, create an intentional local commit, and push only the scoped Serena Light change to the configured GitHub repository.
