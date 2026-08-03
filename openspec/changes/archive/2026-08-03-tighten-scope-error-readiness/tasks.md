## 1. Extend scope evidence at its owning source

- [x] 1.1 In `src/serena_light/workspace/scope.py`, add `language`, `project_kind`, `selected_config_path`, and `configured_program_outside_trust` (a `BoundedDifferenceStatus`) fields to `ScopeError`, and populate them in `ScopeProjection.from_attribution` using the existing `bounded_difference_status(outside)` call. Do not change `ScopeCode`, `ReadinessCode`, or any other public shape in this module.
- [x] 1.2 Update `tests/unit/test_workspace_scope.py` so `test_supported_program_path_outside_trust_is_scope_incompatible` also asserts the new `ScopeError` fields (language, project_kind, selected_config_path, and the bounded `configured_program_outside_trust` shape matching `bounded_difference_status`).

## 2. Carry the evidence through `WorkspaceRuntimeError` and render it

- [x] 2.1 In `src/serena_light/workspace/runtime.py`, add an optional `scope_error: ScopeError | None = None` attribute to `WorkspaceRuntimeError.__init__` and pass `scope_error=projection.error` from `_projection_error`. Leave every other `WorkspaceRuntimeError` construction site (no-trusted-paths, adapter-construction-failed, directory/global multi-family unavailable, restart/timeout) with `scope_error` unset.
- [x] 2.2 In `src/serena_light/tools/envelopes.py`, add `scope_error_details(scope_error: Any) -> dict[str, JsonValue]` that lazily imports `ScopeError` from `serena_light.workspace.scope` (matching the existing `from_workspace_error` lazy-import pattern) and renders `language`, `project_kind`, `configured_program_outside_trust`, and `selected_config_path` only when not `None`.
- [x] 2.3 Update `WorkspaceRuntime._tool_envelope` (runtime.py) to merge `scope_error_details(caught.scope_error)` into `details` when `caught.scope_error is not None`, keeping the existing `paths` key and the existing non-retryable behavior for `SCOPE_INCOMPATIBLE`.
- [x] 2.4 Update `envelopes.from_readiness_result` to merge the same helper's output when `result.scope_error is not None`, in addition to its existing `paths` details.
- [x] 2.5 Update the three `WorkspaceRuntimeError` catch sites in `src/serena_light/daemon/service.py` (`_activation_error` and both branches of `_runtime_value`) to merge `scope_error_details(exc.scope_error)` when present, keeping the existing bare-`paths` fallback when it is `None`.

## 3. Fix the lease working_subdirectory authority

- [x] 3.1 In `src/serena_light/daemon/service.py`, change `_workspace_metadata_for_binding` so `working_subdirectory` always comes from `binding.working_subdirectory` (the caller's own lease binding) instead of preferring `runtime_identity.working_subdirectory` (the shared runtime's construction-time root placeholder). Keep `root`/`kind` sourced from the runtime identity as before.
- [x] 3.2 Extend `_enrich_bound_query_error` (or its condition) so a `SCOPE_INCOMPATIBLE` bound-query failure is enriched with `workspace` metadata the same way `INVALID_PATH` already is, using the corrected `_workspace_metadata_for_binding`.

## 4. Tests

- [x] 4.1 In `tests/unit/test_workspace_runtime_semantics.py` (or `test_workspace_runtime.py`), extend/add a test modeled on `test_incompatible_python_does_not_block_healthy_typescript_references` that asserts the full `SCOPE_INCOMPATIBLE` `details` payload from `WorkspaceRuntime._tool_envelope`: language, project_kind, selected_config_path, bounded `configured_program_outside_trust` (items with path+reason, total, digest, omitted_count matching `bounded_difference_status`), `retry is None`, and no `engine`/`interpreter`/`executable` key anywhere in the response.
- [x] 4.2 In `tests/unit/test_daemon_semantic_api.py`, add a test using a `FakeRuntime`-style stand-in (or the real `WorkspaceRuntime`) that raises a `SCOPE_INCOMPATIBLE` `WorkspaceRuntimeError` carrying a `scope_error`, and assert the daemon-service envelope (through `_runtime_value`/`_activation_error` as applicable) carries the same full evidence.
- [x] 4.3 In `tests/unit/test_daemon_semantic_api.py`, add a two-lease test: activate two leases on different subdirectories of the same physical Git root, trigger a rich bound-query failure (`SCOPE_INCOMPATIBLE` or `INVALID_PATH`) on one lease, and assert its response `workspace.working_subdirectory` equals that lease's own activated subdirectory while the other lease's `get_runtime_status`/binding still reports its own unchanged subdirectory.
- [x] 4.4 In `tests/unit/test_workspace_runtime.py` (or `test_workspace_runtime_semantics.py`), add a deterministic test proving that once a family is recorded in `_family_errors`, a subsequent bound call still runs its freshness preflight (assert the preflight/scan hook fires) and then fails via `_route` before any `warm_global`/`submit_read`/adapter-start call is recorded on the fake adapter, using a fake or monotonic clock rather than real sleeps so the test is not wall-clock flaky.

## 5. Verification

- [x] 5.1 Run the focused suites touched above: `pytest tests/unit/test_workspace_scope.py tests/unit/test_workspace_runtime.py tests/unit/test_workspace_runtime_semantics.py tests/unit/test_daemon_semantic_api.py tests/unit/test_tool_envelopes.py -q`. All must pass, including the pre-existing exact-match compact-success and status-shape assertions unchanged.
- [x] 5.2 Run `ruff check` on every changed Python file, and `ty check` if proportionate to the diff size.
- [x] 5.3 Run `openspec validate tighten-scope-error-readiness --strict`.
- [x] 5.4 Report exact files changed, every command run with its result, and any unresolved issue; do not archive, sync specs, commit, or push, and do not broaden scope to the historical 80-second incident without a reproducer.
