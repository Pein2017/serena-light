## Context

`ScopeProjection.from_attribution` (in `workspace/scope.py`) already computes
everything an Agent would need to fix a `SCOPE_INCOMPATIBLE` failure: the
language family, project kind, selected native config path, and a bounded
`configured_program_outside_trust` set with reasons, via the existing
`bounded_difference_status` helper. `get_runtime_status`'s `_unavailable_family_status`
already renders this full detail for debug/status use. The actual tool-call
failure path does not: `_projection_error` builds a `WorkspaceRuntimeError`
with only `paths`, and every conversion site (`WorkspaceRuntime._tool_envelope`,
`envelopes.from_readiness_result`, and the three `WorkspaceRuntimeError` catch
sites in `daemon/service.py`) renders only `{"paths": ...}`.

Separately, `_workspace_metadata_for_binding` in `daemon/service.py` builds the
`workspace` metadata attached to a rich bound query failure. It reads
`working_subdirectory` with `getattr(runtime_identity, "working_subdirectory",
binding.working_subdirectory)`. Because `WorkspaceRuntime` is shared across all
leases on one physical root and is deliberately not lease-aware, its own
`identity.working_subdirectory` is fixed at construction to the physical root
(`WorkspaceIdentity(root=root, kind=kind, working_subdirectory=root)`,
`runtime.py:4262`) and is never the caller's actual activation subdirectory.
Since `runtime_identity` always has this attribute, the `getattr` always
succeeds and the correct fallback (`binding.working_subdirectory`, the real
per-lease binding recorded in the daemon's `WorkspaceBinding`) is never used.

## Goals / Non-Goals

**Goals:**

- Make the `SCOPE_INCOMPATIBLE` tool-call failure payload carry the same
  already-owned projection facts `get_runtime_status` already renders, bounded
  and reused rather than recomputed.
- Make a rich bound query failure report the true per-lease
  `working_subdirectory` when two leases share one physical root.
- Prove, with a deterministic test, that a blocked family still fails after
  the call-time freshness preflight and before any adapter start/warm.

**Non-Goals:**

- Making `WorkspaceRuntime` lease-aware, or moving scope tracking into the
  daemon/service layer.
- Changing `get_runtime_status`'s existing status shape, the executor queue
  boundary, or adding any new tool.
- Adding free-form remediation text, a new recovery action, or a mega-envelope.
- Touching successful envelopes, public tool schemas, or the historical
  80-second incident (no reproducer is in scope here).

## Decisions

### 1. Carry `ScopeError` itself on `WorkspaceRuntimeError`, not a re-derived summary

`ScopeError` (in `scope.py`) gains `language`, `project_kind`,
`selected_config_path`, and `configured_program_outside_trust`
(a `BoundedDifferenceStatus`), populated once in `ScopeProjection.from_attribution`
using the existing `bounded_difference_status(outside)` call. `WorkspaceRuntimeError`
gains an optional `scope_error: ScopeError | None` attribute. `_projection_error`
passes `projection.error` straight through.

Alternative considered: re-derive the same fields independently at each
conversion site from a passed-in `ScopeProjection`. Rejected because it would
duplicate the exact bounding/digest logic `bounded_difference_status` already
owns in three separate files, and because `_route` currently only has access
to the previously-stored `WorkspaceRuntimeError`, not the original projection,
by the time it raises.

When a `SCOPE_INCOMPATIBLE` `WorkspaceRuntimeError` is built from something
other than a real scope difference (no trusted source paths at all, an adapter
construction exception, or an aggregate of multiple already-unavailable
families), `scope_error` stays `None` and rendering falls back to the existing
bare `paths` list — satisfying "retain a concise reason/paths without
fabricating config facts."

### 2. One shared rendering helper in `tools/envelopes.py`

A new `scope_error_details(scope_error) -> dict[str, JsonValue]` function in
`envelopes.py` (lazy-importing `ScopeError` from `workspace.scope`, matching
the existing `from_workspace_error` import pattern so `envelopes.py` keeps no
import-time dependency on the workspace package) renders `language`,
`project_kind`, `selected_config_path` (only when not `None`), and
`configured_program_outside_trust`. `WorkspaceRuntime._tool_envelope`,
`envelopes.from_readiness_result`, and the three `WorkspaceRuntimeError` catch
sites in `daemon/service.py` all call it when `scope_error` is present, merged
with the existing `paths` key for backward compatibility. This keeps the
detail shape identical regardless of which layer converts the error.

### 3. Fix `_workspace_metadata_for_binding` to prefer the caller's own binding

Change the `working_subdirectory` selection to use `binding.working_subdirectory`
unconditionally instead of preferring `runtime_identity.working_subdirectory`.
`root` and `kind` keep coming from the shared runtime identity since those are
physical-root facts common to every lease on that root; only
`working_subdirectory` is per-lease state, and `WorkspaceBinding` (at the
daemon/service lease boundary) is its one authoritative source. This is a
one-line fix at the exact boundary the task requires — no lease-awareness is
added to `WorkspaceRuntime` itself.

### 4. Blocked-family test proves existing ordering rather than adding new code

Tracing the call path (`_run_fresh_read` → `_admit_fresh_read` preflight →
`operation()` → `_route`, which raises `self._family_errors[family]` before
any `adapter.warm_global`/`submit_read`) shows the freshness preflight already
runs before the blocked-family check, and the check already short-circuits
before adapter admission. No production change is needed here; the task adds
a deterministic test (fake adapter recording start/warm/submit calls, fake
clock/no real sleep) that pins this ordering as a regression guard.

## Risks / Trade-offs

- [Risk] Adding fields to the `SCOPE_INCOMPATIBLE` `details` payload could be
  read as an ambient invitation to also add non-bounded fields later. →
  Mitigation: only the four named fields are added, all sourced from existing
  bounded projection evidence; `scope_error_details` is the single choke point
  reviewers can inspect.
- [Risk] `_workspace_metadata_for_binding`'s fix changes an observable field
  (`workspace.working_subdirectory`) on existing bound-query error responses. →
  Mitigation: existing tests that assert the pre-fix value all use resolvers
  where `binding.working_subdirectory` already equals the runtime's identity
  attribute (single lease, single subdirectory per root), so their assertions
  are unaffected; the new two-lease test specifically exercises the case where
  they diverge.

## Migration Plan

No data migration. This is a source-only build-slot change to the daemon
Python package; existing leases are unaffected until the daemon process is
restarted onto the new build, consistent with prior Serena Light rollovers.

## Open Questions

None outstanding; if the census/strict-validation pass surfaces a case this
design does not cover, stop and revise before continuing implementation.
