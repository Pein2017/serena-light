## Why

Serena Light currently lets a later request await and accept a freshness scan
that began before that request arrived, and content-bearing reads have no final
filesystem validation before their result is returned. A concurrent agent can
therefore complete a write while another call is in flight and the call can
still return an older or mixed snapshot; freshness correctness must take
priority over avoiding a duplicate scan.

## What Changes

- Give every content-bearing read call its own synchronous freshness preflight
  whose validation starts after that call arrives. A later caller may wait for
  an older in-flight scan to finish, but must then run its own validation rather
  than accepting the older scan as its admission evidence.
- Preserve the existing guarded two-pass byte-identity validation, including
  detection of same-stat rewrites, Git create/change/delete/config changes, and
  targeted validation for path-scoped reads in the explicitly trusted non-Git
  transformers root. Global reads on that root use a bounded full-root guarded
  preflight and postflight because they cannot claim global freshness from one
  targeted path.
- Add a bounded postflight to semantic navigation and diagnostics reads. If the
  relevant filesystem identity changed, discard the result and replay the
  complete read transaction once; if the replay also races with a change,
  return retryable `NOT_READY` instead of stale or mixed success.
- Compare every internal response-owned source snapshot used by a successful
  result with the final observed byte identity, so a write followed by a revert
  cannot validate bytes that differ from the returning response.
- Keep editing on its existing non-replayable commit-point contract. This
  change must not replay `replace_symbol_body` or weaken `UNCERTAIN` handling.
- State the linearization boundary explicitly: a foreign write completed before
  a call's final validation must be observed by that call, while a write after
  final validation is observed by the next call and is not retroactively part
  of the completed call.
- Keep the mechanism request-driven. Do not add an authoritative watcher,
  background polling loop, persistent content database, filesystem snapshot,
  or cross-agent cooperative write lock.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Strengthen request-arrival admission and final validation
  for content-bearing reads without changing the edit replay boundary.

## Impact

This change affects `FreshnessCoordinator`, the workspace read envelope,
diagnostics/navigation call routing, freshness telemetry exposed by runtime
status, and concurrency/fault tests. It adds no dependency, public tool, public
success-schema field, language, hook, or client configuration surface. Existing
clients remain compatible, although a call racing repeated external writes can
now take one bounded replay and return retryable `NOT_READY` rather than older
success.

Admission evidence is the current production path and unit contract:
`FreshnessCoordinator.ensure_fresh` shares one in-flight scan across callers,
the tool envelope validates only before dispatch, and the existing concurrency
test requires the later caller to reuse the older scan. Implementation must
replace that contract and pass deterministic race tests plus real connector
smokes before either dependent change begins. Production LOC remains audit
information only; forbidden ownership, dependency, and provenance checks remain
hard gates.

This is the first change in the sequence. `add-lexical-discovery` and
`improve-warm-runtime-reuse` must not be implemented until this change is
accepted, synced, and archived.
