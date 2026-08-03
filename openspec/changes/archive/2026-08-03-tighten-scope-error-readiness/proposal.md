## Why

`SCOPE_INCOMPATIBLE` is the one semantic failure that should hand an Agent
everything it needs to fix its own environment, but the actual tool-call path
(`find_symbol`, `get_symbols_overview`, references, diagnostics) currently
returns only a bare `paths` list, dropping the language family, selected native
config, project kind, and bounded outside-trust reasons that `get_runtime_status`
already computes for the same projection. Separately, when two leases share one
physical Git root but activated different subdirectories, a rich operational
failure on one lease can report the physical root instead of that lease's own
`working_subdirectory`, because the shared, lease-agnostic `WorkspaceRuntime`
identity's own placeholder subdirectory is read ahead of the caller's real
binding. Both defects reduce an otherwise-actionable, non-secret error to
something an Agent cannot safely act on or attribute to the right lease.

## What Changes

- Carry the already-computed `ScopeProjection` evidence (language family,
  project kind, selected native config path when present, and bounded
  `configured_program_outside_trust` items with path/reason/total/digest/
  omitted_count) through `WorkspaceRuntimeError` into the `SCOPE_INCOMPATIBLE`
  tool envelope built by `WorkspaceRuntime._tool_envelope` and by the daemon
  service's equivalent conversion sites, reusing `scope.bounded_difference_status`
  rather than recomputing or rerunning any probe. When a `SCOPE_INCOMPATIBLE`
  failure has no backing projection (for example, a family with no trusted
  source paths, or a directory/global scope spanning multiple unavailable
  families), keep the existing concise reason/paths without fabricating config
  facts. Never label the interpreter or language-server executable as the
  incompatible "program"; `status`/`get_runtime_status` remains the sole owner
  of engine/interpreter detail.
- Fix `_workspace_metadata_for_binding` in the daemon service so a rich bound
  query failure (`INVALID_PATH`, `SCOPE_INCOMPATIBLE`) reports the calling
  lease's own `working_subdirectory` from its `WorkspaceBinding`, not the
  shared runtime identity's construction-time root placeholder. Root and kind
  keep coming from the shared runtime identity since those are physical-root
  facts; only `working_subdirectory` is per-lease.
- Add a deterministic regression test proving that once a language family is
  blocked (recorded in `_family_errors`), the call-time freshness preflight
  still runs first, and the blocked family then fails in `_route` before any
  adapter start/warm/executor submission, with no new caching path that would
  let a stale blocked result skip that preflight.
- Add focused tests for the full `SCOPE_INCOMPATIBLE` payload (both through
  `WorkspaceRuntime` directly and through the daemon service), for the
  two-lease/one-root `working_subdirectory` fix, and for the blocked-family
  ordering guarantee.

Explicit non-goals: no new public tool, no change to public tool count, input
schemas, successful compact envelopes, schema version, initialization text, or
editing scope; no lease-aware `WorkspaceRuntime`; no free-form remediation
prose, mega-envelope, or new recovery action; no change to the historical
80-second incident without a reproducer.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `workspace-runtime`: `SCOPE_INCOMPATIBLE` failures must carry bounded
  projection evidence reused from the existing scope projection, and a rich
  bound query failure must report the caller's own lease `working_subdirectory`
  rather than the shared runtime's physical-root placeholder.
- `semantic-navigation`: the rich `SCOPE_INCOMPATIBLE` error detail contract is
  tightened to require language family, project kind, selected native config
  when present, and bounded `configured_program_outside_trust` evidence,
  while continuing to forbid engine/interpreter identity in the payload.

## Impact

- `src/serena_light/workspace/scope.py`: `ScopeError` gains bounded projection
  fields; `ScopeProjection.from_attribution` populates them.
- `src/serena_light/workspace/runtime.py`: `WorkspaceRuntimeError` gains an
  optional `scope_error` attribute; `_projection_error` sets it;
  `_tool_envelope` renders it into `details`.
- `src/serena_light/tools/envelopes.py`: a shared `scope_error_details` helper
  renders the bounded evidence for both `_tool_envelope` and
  `from_readiness_result`.
- `src/serena_light/daemon/service.py`: the three `WorkspaceRuntimeError`
  conversion sites reuse the same helper; `_workspace_metadata_for_binding`
  is corrected to source `working_subdirectory` from the caller's
  `WorkspaceBinding`.
- Tests: `tests/unit/test_workspace_scope.py`,
  `tests/unit/test_workspace_runtime.py`,
  `tests/unit/test_workspace_runtime_semantics.py`,
  `tests/unit/test_daemon_semantic_api.py`.
