## MODIFIED Requirements

### Requirement: Adapter startup and readiness are explicit
The system SHALL register Python and JS/TS adapters without starting them, SHALL
start an adapter lazily when required, and SHALL expose readiness phases that
distinguish document readiness from global-index readiness. Global readiness
SHALL cover only the current native configured-program and adapter index
generations. Path-scoped readiness SHALL be tracked independently for trusted
files served through configured, inferred, or transient engine projects.

Every content-bearing semantic-navigation or diagnostics read SHALL receive a
unique synchronous freshness preflight whose guarded scan begins after that
call arrives; a later call MUST NOT accept a scan that was already in progress
when it arrived. Before returning source-derived success, the system SHALL run a
second guarded freshness scan. It SHALL also compare the byte identity of every
internal response-owned source snapshot that contributed content, a range, or
diagnostic authority with the final guarded identity for that workspace or
trusted-external path. If the relevant workspace identity, generation, or
response witness changes, it SHALL discard the entire result and replay the
complete read transaction at most once. A second raced attempt SHALL return retryable
`NOT_READY` with reason `workspace_changed_during_read` and MUST NOT return
stale, mixed-snapshot, or empty success. Heartbeats, lease control, and bounded
runtime status are not content-bearing reads. Editing SHALL remain outside this
read replay boundary and MUST NOT be automatically replayed.

#### Scenario: Python-only workspace is activated
- **WHEN** only Python operations are requested
- **THEN** Pyright starts and the TypeScript language server remains stopped

#### Scenario: Global query arrives during cold indexing
- **WHEN** a global query cannot reach global-ready within the bounded wait
- **THEN** the tool returns `NOT_READY` with phase and retry metadata rather than an empty result

#### Scenario: Document operation arrives before global readiness
- **WHEN** the target document is ready but the workspace-symbol sentinel is still running
- **THEN** a path-scoped operation may proceed without claiming global readiness

#### Scenario: External file change invalidates global readiness
- **WHEN** a create, change, or delete changes or may change the configured-program generation beyond the adapter index generation
- **THEN** global queries wait for the new-generation barrier or return `NOT_READY` and never return stale empty success

#### Scenario: An already-open document changes outside Serena Light
- **WHEN** freshness observes a change to a URI that the adapter still has open
- **THEN** the adapter sends a full-text `didChange` from the observed snapshot, or `didClose` if that snapshot cannot be represented, before a current-generation semantic success is authorized

#### Scenario: Watcher reconciliation cannot settle
- **WHEN** watched-file delivery, open-document reconciliation, executor admission, or its retained future fails or times out
- **THEN** the operation returns retryable `BUSY` or `NOT_READY`, retains the exact event batch for retry, and an unchanged later scan cannot authorize success until that batch settles

#### Scenario: One language-family delivery fails before another family
- **WHEN** one freshness scan changes multiple language families and an earlier family's watched-file delivery fails
- **THEN** every affected family has already advanced its generation, all later-family deliveries are admitted or explicitly retained before the failure is returned, and no family can serve a stale current-generation success

#### Scenario: Omitted trusted file changes
- **WHEN** a trusted file outside the configured program changes without changing native program membership
- **THEN** its path-scoped document generation is invalidated without falsely invalidating or expanding configured-program global readiness

#### Scenario: Later call arrives during another freshness scan
- **WHEN** semantic call B arrives after call A's freshness scan has begun
- **THEN** B waits for A's scan to settle, runs its own guarded scan that begins after B arrived, and cannot use A's scan as its admission evidence

#### Scenario: Source bytes change without a stat-identity change
- **WHEN** a trusted tracked or untracked source is rewritten in place with the
  same size, inode, and observable timestamp values
- **THEN** guarded byte identity reports the path as changed, advances the
  required generation, and reconciles the adapter before semantic success

#### Scenario: Consecutive byte observations disagree or path identity changes
- **WHEN** two guarded full-file byte passes disagree, or the file, its lexical
  entry, or an ancestor directory changes across either pass
- **THEN** the scan returns retryable `NOT_READY` before committing inventory,
  state, generations, or watched-file events; a later preflight observes afresh

#### Scenario: Foreign write completes during a read
- **WHEN** another process completes a relevant workspace write after a read's
  preflight and before its final guarded validation
- **THEN** the system discards the source-derived result, reconciles the change,
  and replays the complete read transaction once

#### Scenario: Foreign write is reverted before postflight
- **WHEN** an operation captures response-owned bytes B after preflight, another
  process restores bytes A before postflight, and the aggregate preflight and
  postflight workspace identities both describe A
- **THEN** the B response witness disagrees with the final A byte identity, so
  the result is discarded and the complete read transaction replays once

#### Scenario: Workspace changes during both read attempts
- **WHEN** relevant workspace identity changes before final validation on both
  the original read and its one allowed replay
- **THEN** the call returns retryable `NOT_READY` with reason
  `workspace_changed_during_read` and no stale or partial success payload

#### Scenario: A foreign write occurs after final validation
- **WHEN** a non-cooperating external writer changes a file only after the
  returning read's second matching guarded postflight has crossed that byte
- **THEN** the already-linearized read is not retroactively invalidated, and the
  next call's own synchronous preflight must observe the new byte identity

#### Scenario: Read replay cannot replay an edit
- **WHEN** `replace_symbol_body` starts, commits, times out, loses its response,
  or returns `UNCERTAIN`
- **THEN** the freshness read-replay mechanism never invokes that edit again

#### Scenario: Stable config deletion or source symlink rejection is observed
- **WHEN** a native config is stably absent or a formerly trusted source is
  stably replaced by a rejected symlink before the scan begins
- **THEN** the new absence/rejection is committed as a config or membership
  change rather than being retained forever as an unstable observation

#### Scenario: Same root is activated again
- **WHEN** a bound session activates another path in the same Git root
- **THEN** the runtime performs an immediate per-call refresh before returning reuse

#### Scenario: Targeted transformers path is read repeatedly
- **WHEN** repeated content-bearing calls query explicitly selected files in the
  trusted non-Git transformers workspace
- **THEN** each call validates its requested path before and after the read
  without performing a full-package filesystem walk

#### Scenario: Global transformers query is requested
- **WHEN** a content-bearing semantic query claims global coverage in the exact
  trusted non-Git transformers workspace without an explicit target path
- **THEN** the call performs a bounded full-root no-symlink guarded preflight and
  postflight and cannot use targeted-path freshness to authorize global success

#### Scenario: Native-config adapter stop times out
- **WHEN** a changed native config requires adapter restart but the exact old adapter stop does not reach its bounded terminal state
- **THEN** that family becomes explicitly `TIMED_OUT` and retryable, remains unpublished, and every later freshness preflight retries the same pending cleanup even if filesystem facts are unchanged

#### Scenario: Runtime retires with a pending adapter restart
- **WHEN** the last holder releases a runtime after a config restart timed out
- **THEN** runtime shutdown retains and settles the pending adapter cleanup responsibility, never republishes the old adapter, and never installs a replacement after the runtime is stopped

#### Scenario: Reattribution makes a running family incompatible
- **WHEN** freshness removes a running adapter because its new native-program
  attribution is incompatible while runtime shutdown begins concurrently
- **THEN** removal and pending-retirement publication are atomic, both paths
  share the exact cleanup future, and `stopped` is not published before cleanup
  settles

#### Scenario: Cleanup admission fails during runtime shutdown
- **WHEN** an owned adapter stop cannot enter even the reserved cleanup queue
- **THEN** shutdown returns a failure without publishing `stopped`, retains the
  exact cleanup owner, and a later shutdown attempt retries admission

#### Scenario: An admitted cleanup future fails transiently
- **WHEN** a restart, retirement, or runtime-shutdown owner observes a completed failed or cancelled adapter-stop future
- **THEN** it retains the sealed adapter, never publishes stopped or a replacement prematurely, and the next bounded cleanup attempt invokes the adapter stop retry rather than awaiting the same failed future forever

#### Scenario: Ordinary work races an admitted adapter stop
- **WHEN** an ordinary adapter operation was queued before stop or is submitted after stop is requested
- **THEN** stop seals ordinary admission synchronously, the queued worker rechecks the seal, no provider can start or restart after the request, and a failed cleanup admission/future remains retryable without reopening admission

#### Scenario: Registry retirement detaches a runtime before cleanup fails
- **WHEN** lease policy atomically removes an idle runtime and its first stop
  attempt fails
- **THEN** the daemon service retains that detached runtime as pending cleanup,
  reports the build non-idle, and retries it on a later sweep

#### Scenario: Immediate release decides a stop that has not settled
- **WHEN** a last-holder immediate release detaches a runtime but its stop attempt fails
- **THEN** the response reports `runtime_stopped=false` and `runtime_stop_pending=true`, migration status remains non-idle, and later unrelated roots continue operating while a sweep retries cleanup
