## MODIFIED Requirements

### Requirement: Adapter startup and readiness are explicit
The system SHALL register Python and JS/TS adapters without starting them. It
SHALL start an adapter when required by foreground work or by the finite
low-priority prewarm policy, and SHALL expose readiness phases that distinguish
document readiness from global-index readiness. Global readiness SHALL cover
only the current native configured-program and adapter index generations.
Path-scoped readiness SHALL be tracked independently for trusted files served
through configured, inferred, or transient engine projects. Prewarm MUST NOT
claim global readiness, bypass ordinary capability/generation barriers, or make
an incompatible family available.

Every content-bearing semantic-navigation, diagnostics, or lexical read SHALL
receive a unique synchronous freshness preflight whose guarded scan begins
after that call arrives; a later call MUST NOT accept a scan that was already in
progress when it arrived. Before returning source-derived success, the system
SHALL run a second guarded freshness scan. It SHALL also compare the byte
identity of every internal response-owned source snapshot that contributed
content, a range, or diagnostic authority with the final guarded identity for
that workspace or trusted-external path. If the relevant workspace identity,
generation, or response witness changes, it SHALL discard the entire result and
replay the complete read transaction at most once. A second raced attempt SHALL return retryable
`NOT_READY` with reason `workspace_changed_during_read` and MUST NOT return
stale, mixed-snapshot, or empty success. Heartbeats, lease control, and bounded
runtime status are not content-bearing reads. Editing SHALL remain outside this
read replay boundary and MUST NOT be automatically replayed.

After workspace activation has validated, refreshed, and atomically committed
the binding, the daemon MAY enqueue one finite prewarm plan containing only
Python and JS/TS families present in the current semantic source inventory. One
daemon-wide low-priority scheduler SHALL start at most one adapter at a time.
Before each start it SHALL revalidate runtime ownership, family attribution and
compatibility, and an empty foreground semantic queue. It MUST NOT wait for a
global-index sentinel. Foreground work SHALL remain normally admissible; once
foreground work appears, the scheduler SHALL finish only an already admitted
atomic start and abandon remaining prewarm steps.

#### Scenario: Python-only workspace is activated
- **WHEN** a successfully activated workspace contains Python sources and no
  JS/TS sources
- **THEN** finite prewarm may start Pyright to ordinary document capability and
  the TypeScript language server remains stopped

#### Scenario: Mixed workspace is activated while idle
- **WHEN** current source attribution contains compatible Python and JS/TS
  families and no foreground work is queued
- **THEN** the scheduler starts at most one adapter at a time, does not wait for
  global readiness, and completes at most those two finite plan entries

#### Scenario: Repeated activation proposes duplicate prewarm
- **WHEN** the same runtime is activated again while its current plan is queued
  or an attributed adapter is already running
- **THEN** the scheduler coalesces the plan and does not enqueue or start a
  duplicate adapter instance

#### Scenario: Foreground work appears during prewarm
- **WHEN** a semantic request arrives while one prewarm adapter start is already
  admitted and another family remains pending
- **THEN** the admitted start reaches a bounded terminal state, remaining
  prewarm is abandoned, and the foreground request waits for at most that one
  already-admitted start and never for another prewarm step, scheduler decision,
  or global sentinel

#### Scenario: Attribution changes before a prewarm step
- **WHEN** freshness removes or makes a pending language family incompatible
  before its prewarm start
- **THEN** the scheduler drops that entry without starting the adapter or
  claiming readiness

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

### Requirement: Leases bound runtime lifetime
Each connector SHALL renew a daemon-issued lease every 15 seconds. A lease SHALL
expire after 60 seconds without renewal. After the last lease is released or
expired without an immediate release, the runtime SHALL enter the daemon-local
zero-holder warm pool. The daemon SHALL retain no more than three zero-holder
runtimes, each for no more than 30 minutes, and SHALL apply a 1.5 GiB
(1,610,612,736-byte) soft cap to the summed resident set size of their
recursively owned language-server process trees. Count or RSS pressure SHALL
retire zero-holder runtimes in least-recently-used order; TTL SHALL retire each
expired runtime. Exact workspace identity SHALL break equal-age ties. A
zero-holder candidate whose recursively owned process identity remains
unvalidated at sweep sampling SHALL be retired in that sweep and MUST NOT be
counted as zero bytes.

A runtime with one or more active holders MUST NOT be evicted, treated as
reclaimable, or counted against those zero-holder limits. Immediate release
SHALL bypass the warm pool. Pool selection, atomic detach, and pending cleanup
SHALL remain owned by the existing registry and lifecycle sweep. Each build
daemon SHALL enforce its limits independently and MUST NOT kill or mutate a
runtime owned by another build.

#### Scenario: Client exits normally
- **WHEN** a connector closes its session
- **THEN** its lease is released without affecting other leases on the same root

#### Scenario: Client dies without releasing
- **WHEN** heartbeats stop for more than the lease timeout
- **THEN** the daemon expires that lease and admits the runtime to the warm pool
  only if it was the last holder

#### Scenario: Warm runtime is reacquired
- **WHEN** a connector activates a zero-holder workspace before atomic retirement
- **THEN** the registry removes it from the warm pool, reuses the exact runtime,
  preserves its healthy adapters, and gives the connector a distinct lease

#### Scenario: Fourth zero-holder runtime enters the pool
- **WHEN** three zero-holder runtimes are retained and another last-holder
  transition occurs
- **THEN** the registry atomically detaches the least recently used zero-holder
  runtime and never evicts an active-holder runtime

#### Scenario: Warm runtime reaches 30 minutes
- **WHEN** a zero-holder runtime's monotonic idle age reaches the fixed TTL
- **THEN** lifecycle sweep detaches it and settles or retains its cleanup through
  the existing pending-cleanup owner

#### Scenario: Warm process trees exceed the RSS cap
- **WHEN** validated recursive language-server RSS across zero-holder runtimes
  exceeds 1,610,612,736 bytes
- **THEN** sweep retires least-recently-used zero-holder runtimes until the known
  retained sum is at or below the soft cap

#### Scenario: TypeScript launcher has a grandchild
- **WHEN** a retained TypeScript language-server process owns a `tsserver`
  descendant through an intermediate launcher
- **THEN** the descendant's validated RSS contributes exactly once to that warm
  runtime's process-tree sample

#### Scenario: Warm process identity cannot be measured
- **WHEN** a zero-holder adapter PID/create-time identity is unreadable or its
  recursive tree cannot be validated
- **THEN** the candidate is treated as unknown resource pressure and is retired
  in that sweep rather than being counted as zero bytes or retained indefinitely

#### Scenario: Active runtime exceeds the soft cap
- **WHEN** one or more active-holder runtimes use more than 1.5 GiB
- **THEN** the warm-pool policy does not detach them and applies the cap only
  after a runtime becomes zero-holder

#### Scenario: Immediate workspace release
- **WHEN** the last holder calls `release_workspace(immediate=true)`
- **THEN** that binding is detached, its daemon lease remains active and
  unbound, and the runtime stops its language servers without entering the warm
  pool

#### Scenario: Non-last holder releases immediately
- **WHEN** one of multiple same-root holders calls `release_workspace(immediate=true)`
- **THEN** only that binding is detached, its daemon lease remains active and
  unbound, and the shared runtime remains available to the other holders

#### Scenario: Long operation does not starve heartbeat
- **WHEN** a workspace operation remains active for more than 60 seconds
- **THEN** the connector renews its lease independently and the daemon does not expire a live session

#### Scenario: Two build daemons overlap
- **WHEN** an old build retains leases while a new build maintains its own warm
  candidates
- **THEN** each daemon enforces its own fixed pool and retires only its owned
  runtimes and process identities
