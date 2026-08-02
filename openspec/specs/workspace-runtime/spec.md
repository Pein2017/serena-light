# Workspace Runtime Specification

## Purpose

Define shared-daemon workspace identity, trust and program scope, synchronous
freshness, lifecycle, build isolation, proxy boundaries, and process ownership.
## Requirements
### Requirement: Per-client connector uses one shared localhost daemon
The system SHALL expose a stdio MCP connector for each client process and SHALL
proxy requests to one authenticated Streamable HTTP daemon bound only to the
loopback interface. Daemon discovery, authentication material, and runtime state
MUST be owned below `/data/CoordExp/.codex/runtime/serena-light` and MUST NOT
depend on `/root` configuration. The runtime directory SHALL be mode `0700`,
secret-bearing files SHALL be atomically created at mode `0600`, and startup
SHALL reject symlinked runtime paths.
Connector and health-check loopback clients SHALL ignore ambient proxy
configuration, and daemon/LSP subprocess environments SHALL remove all
case-variants of proxy variables. Dependency bootstrap MAY inherit the ambient
proxy for external downloads and SHALL NOT mutate global proxy configuration.

#### Scenario: Two clients connect to the existing daemon
- **WHEN** a second client starts while a healthy daemon is already running
- **THEN** its connector reuses that daemon and receives a distinct daemon-issued lease

#### Scenario: Connector starts the daemon once
- **WHEN** concurrent connectors start and no healthy daemon exists
- **THEN** the startup lock permits exactly one daemon creation and all connectors attach to it

#### Scenario: Unauthenticated local process connects
- **WHEN** a local process calls the HTTP endpoint without the runtime bearer secret
- **THEN** the daemon rejects the request before workspace or language-server work begins

#### Scenario: Loopback proxy environment is poisoned
- **WHEN** ambient HTTP proxy variables point at an unusable proxy
- **THEN** connector, daemon health, and local acceptance traffic still reach
  loopback directly while external bootstrap remains independently configurable

### Requirement: MCP initialization teaches the efficient Serena Light workflow
The outer stdio connector and inner daemon SHALL publish one byte-identical,
source-owned MCP initialization instruction string no longer than 220
characters. The instruction SHALL state that Serena Light provides Python and
JavaScript/TypeScript semantic navigation and diagnostics; shell `cd` does not
rebind the current lease; switching roots requires `activate_workspace` with an
absolute path; unfamiliar files should be overviewed before exact symbol
lookup; and host shell/file tools own lexical search.

The instruction SHALL identify Serena Light as experimental and require the
Agent to report any friction or issue to the user so the MCP can be iterated
and improved.

The initialization instruction and existing tool/field descriptions SHALL
jointly expose the remaining workflow at its owning decision point: startup cwd
is auto-bound; file or directory query scope is preferred when known; overview
defaults to depth 0; reference snippets are opt-in; diagnostics are called
explicitly after a meaningful edit group; and runtime status is for
debug/build/readiness rather than routine preflight.

The system MUST NOT add an initialization hook, automatic diagnostic injection,
or a public instructions function. It MUST NOT advise calling runtime status
before every query, claim support outside Python and JavaScript/TypeScript, or
make shell `cd` change the lease binding.

#### Scenario: Fresh stdio client initializes
- **WHEN** a Codex, Claude Code, or CC Agent client initializes through the outer connector
- **THEN** it receives the source-owned instruction of at most 220 characters and the existing tool descriptions collectively expose every workflow decision above

#### Scenario: Direct daemon client initializes
- **WHEN** an authenticated acceptance client initializes directly against the inner daemon
- **THEN** its instruction bytes exactly match the outer connector's instructions

#### Scenario: Client repeats initialization on every tool
- **WHEN** a client surface prefixes the initialization instruction to each public tool description
- **THEN** the repeated source-owned portion remains bounded by the 220-character instruction without adding a hook or instructions tool

#### Scenario: Agent enters an unfamiliar file
- **WHEN** the Agent reads `get_symbols_overview` and `find_symbol` metadata
- **THEN** the metadata directs it to start with a depth-0 overview before guessing an exact symbol and to retry ambiguity with a returned qualified name path

#### Scenario: Agent changes shell directory
- **WHEN** the Agent reads initialization or `activate_workspace` metadata after shell `cd` to another repository
- **THEN** it is told that the lease remains on its current root and that an absolute activation is required to switch or return

#### Scenario: Agent needs references, diagnostics, status, or lexical search
- **WHEN** the Agent reads the owning tool metadata
- **THEN** snippets remain opt-in, diagnostics remain explicit, status remains debug-only, and host tools retain lexical ownership

#### Scenario: Agent encounters Serena Light friction
- **WHEN** a Serena Light call, workflow, result, or recovery action creates friction or exposes an issue
- **THEN** the initialization metadata directs the Agent to report it to the user for further MCP iteration

### Requirement: Workspace binding is session-scoped
The system SHALL keep the active workspace binding on the daemon-issued
connector lease, SHALL use that lease as the sole lifetime authority, and MUST
NOT use an MCP HTTP session or daemon-global active workspace pointer as the
binding owner.

#### Scenario: Sessions bind different roots
- **WHEN** session A activates `/data/CoordExp` and session B activates `/data/ms-swift`
- **THEN** each session continues resolving paths and symbols against its own root

#### Scenario: Connector auto-binds startup cwd
- **WHEN** a connector starts from a directory inside a trusted Git workspace
- **THEN** it automatically binds that Git workspace before the first semantic call

#### Scenario: Shell changes to another root
- **WHEN** the client shell changes cwd to a different Git repository after connector startup
- **THEN** semantic calls remain on the old binding until the client explicitly calls `activate_workspace` with the new absolute path

#### Scenario: Cross-root activation fails
- **WHEN** validation, runtime acquisition, lease creation, or mandatory refresh for a new root fails
- **THEN** the connector retains its previous lease and workspace binding unchanged

#### Scenario: Same-root refresh fails
- **WHEN** reactivation inside the current Git root cannot complete its mandatory freshness pass
- **THEN** the existing registry lease, working subdirectory, binding, and holder counts remain unchanged and the call returns a typed failure

#### Scenario: Failed switch borrowed a warm target runtime
- **WHEN** a cross-root refresh failure used a pre-existing zero-holder runtime still retained for warm grace
- **THEN** only the provisional candidate lease is aborted; the warm runtime remains retained and is not stopped as an attempt-created orphan

#### Scenario: Expired lease is used
- **WHEN** an MCP HTTP session remains connected after its connector lease expires
- **THEN** the next tool call returns `LEASE_EXPIRED` and does not silently recreate a binding

### Requirement: Workspace identity is normalized deterministically
The system SHALL normalize a path inside a Git repository to its resolved Git
top-level identity and SHALL use the exact resolved activation path for an
explicitly trusted non-Git root.

#### Scenario: Activation moves within one Git root
- **WHEN** a session activates two different subdirectories of the same Git repository
- **THEN** the workspace runtime is reused and only session working-subdirectory metadata changes

#### Scenario: Transformers package is activated
- **WHEN** the exact trusted transformers package directory is activated
- **THEN** that package directory becomes the non-Git workspace identity and the enclosing site-packages directory is not scanned

### Requirement: Same-root sessions share and cross-root sessions isolate runtimes
The daemon SHALL reuse one `WorkspaceRuntime` and its warm language adapters for
all active leases on the same workspace identity. It SHALL use independent
runtimes, locks, adapters, crash state, and cooldown state for different
identities.

#### Scenario: Two sessions use one Python root
- **WHEN** two sessions bind the same Python workspace
- **THEN** one Pyright process serves both sessions and each session retains a separate lease

#### Scenario: Different roots execute concurrently
- **WHEN** semantic calls target two different workspace identities at the same time
- **THEN** the calls may execute concurrently without changing each other's binding or adapter state

#### Scenario: One root enters cooldown
- **WHEN** the TypeScript adapter repeatedly crashes for one workspace
- **THEN** other roots and the Python adapter remain available

### Requirement: Calls are serialized within a workspace
The system SHALL execute the synchronous LSP stack outside the daemon event loop
through one bounded single-worker executor per workspace. Actual LSP dispatch,
generation mutation, and editing SHALL be ordered through a workspace-owned
lock, while readiness waits, heartbeats, lease operations, status, and work for
other roots SHALL remain responsive. Queue saturation SHALL return `BUSY`.

#### Scenario: Query overlaps an edit on the same root
- **WHEN** a query and `replace_symbol_body` arrive concurrently for the same workspace
- **THEN** one completes before the other observes or changes workspace state

#### Scenario: Queries target different roots
- **WHEN** operations target different workspace identities
- **THEN** neither workspace lock blocks the other

#### Scenario: Cold global wait overlaps a path query
- **WHEN** one session waits for global readiness and another session requests a ready document in the same workspace
- **THEN** the readiness wait does not hold the workspace lock and the path-scoped request may complete

#### Scenario: Blocking LSP call runs on one root
- **WHEN** a fake LSP request blocks one workspace executor for longer than a heartbeat interval
- **THEN** another root, runtime status, and connector heartbeats continue without event-loop delay

#### Scenario: Queued request is cancelled
- **WHEN** a client cancels work before its bounded executor entry starts
- **THEN** the entry is removed without mutating adapter state or retaining the workspace lock

#### Scenario: Queued edit reaches its timeout
- **WHEN** an edit is proven not to have started and is cancelled in the queue
- **THEN** it returns `TIMED_OUT` and can never execute later

#### Scenario: Running edit reaches its timeout
- **WHEN** an edit has started or its commit state cannot be proven
- **THEN** it returns `UNCERTAIN`, is not replayed, and requires a fresh hash read

#### Scenario: Ordinary request queue is saturated during shutdown
- **WHEN** all ordinary queued-work slots are occupied and both fixed language
  adapters require cleanup
- **THEN** their two cleanup obligations remain admissible without increasing
  ordinary request capacity, while a third cleanup submission fails explicitly

### Requirement: Query and edit roots obey a fixed trust policy
The system SHALL accept path operands only from the active workspace inventory.
It SHALL permit returned semantic locations below `/data` or below the
standard-library, purelib, and platlib roots reported by the pinned conda `ms`
interpreter, marking locations outside the active workspace
`read_only_external`. It SHALL permit non-Git activation only for the exact
trusted transformers package, permit edits only inside resolved Git workspaces
below `/data`, and reject symlink escapes.

#### Scenario: Query reaches transformers
- **WHEN** a Python definition resolves into the exact trusted transformers package
- **THEN** the location is returned as a read-only external result

#### Scenario: Query reaches another conda dependency
- **WHEN** a Python definition resolves into the pinned `ms` environment's `torch`, `numpy`, or standard-library tree
- **THEN** the location is returned with `read_only_external` metadata and remains ineligible for editing

#### Scenario: Trusted path belongs to another workspace
- **WHEN** a path is below `/data` but is outside the active workspace inventory
- **THEN** the tool returns `OUT_OF_WORKSPACE` with the active identity and an absolute activation hint

#### Scenario: Definition escapes every query root
- **WHEN** a language server returns a location outside `/data` and outside the pinned interpreter roots
- **THEN** the tool returns `UNTRUSTED_ROOT` rather than silently dropping the location

#### Scenario: Edit targets site-packages
- **WHEN** an editing tool targets transformers or another site-packages path
- **THEN** the tool returns `READ_ONLY_ROOT` without writing

#### Scenario: Resolved path escapes an edit root
- **WHEN** an in-root symlink resolves outside the authorized Git workspace
- **THEN** authorization fails before any temporary file or language-server mutation is created

### Requirement: Trust inventory and semantic-program scope are distinct
For Git workspaces, the system SHALL derive a trust inventory from cached and
untracked non-ignored Git files without enumerating ignored directory contents.
It SHALL separately discover the native configured semantic program selected by
the applicable `tsconfig.json`, `jsconfig.json`, or `pyrightconfig.json`, and
MUST NOT generate an overlay that changes native project semantics to force set
equivalence. For non-Git roots, it SHALL use a bounded walk that does not follow
symlinks and SHALL report the engine program as a separate projection. Both
inventories SHALL omit tracked-but-deleted paths and symlink escapes.

#### Scenario: Mixed source and data directory is authorized
- **WHEN** a Git directory contains tracked Python files and a large ignored data subtree
- **THEN** tracked and eligible untracked Python files enter the trust inventory while the ignored subtree is pruned before traversal

#### Scenario: New untracked source file appears
- **WHEN** a supported untracked file is created after activation
- **THEN** freshness handling adds it to the trust inventory, recomputes its configured-program relationship, and notifies the owning adapter only as required by that relationship

#### Scenario: Tracked file is deleted before activation
- **WHEN** `git ls-files --cached` reports a tracked path that no longer exists
- **THEN** the path is absent from the trust inventory and cannot be authorized even if stale engine state mentions it

#### Scenario: Native config omits a Git-visible source
- **WHEN** a native project config intentionally excludes a trusted supported-language file
- **THEN** status reports the file as `trusted_not_in_configured_program`, global readiness and search do not claim it, and explicit path-scoped operations may use an engine-owned inferred or transient project when supported

#### Scenario: Tsconfig includes ignored supported-language files
- **WHEN** the resolved TypeScript program contains an ignored JS/TS subtree outside the Git inventory
- **THEN** the adapter returns `SCOPE_INCOMPATIBLE` before global readiness and does not serve that configured program

#### Scenario: Scope status is requested
- **WHEN** an agent inspects a bound workspace
- **THEN** status reports the trust inventory and configured-program counts and digests, selected config path, bounded file-level projection evidence, both difference sets with reasons, and incompatible extras

#### Scenario: Read-only non-Git root is queried repeatedly
- **WHEN** repeated targeted queries use the transformers workspace
- **THEN** the system avoids a full filesystem freshness walk on every call

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
when it arrived. Before returning any source-derived result, the system SHALL
run a second guarded freshness scan. A result is source-derived when it is a
success, or when it is a failure that states what the source contains—a symbol
that was not found, an ambiguous candidate set, an unresolvable body or range, a
target snapshot or external byte witness that could not be established, source
bytes that disappear or change identity while an operation acquires its exact
snapshot, or a response-owned target set that overran its bound. It SHALL also compare the byte
identity of every internal response-owned source snapshot that contributed
content, a range, candidate evidence, or diagnostic authority with the final
guarded identity for that workspace path, and SHALL compare, for every trusted
read-only external target it renders, guarded byte identities observed both
immediately before and after the authoritative response with a final guarded
identity for that exact path. If the relevant workspace identity, generation, or
response witness changes, it SHALL discard the entire result and replay the
complete read transaction at most once. A second raced attempt SHALL return retryable
`NOT_READY` with reason `workspace_changed_during_read` and MUST NOT return
stale, mixed-snapshot, or empty success, nor either attempt's source-derived
payload, candidates, or raw locations. A trusted read-only external target whose
exact bytes cannot be observed SHALL fail typed rather than render an
unwitnessed raw range. A failure whose authority is the adapter's own
condition—cold, cooling, unsupported, busy, or timed out—SHALL keep its single
preflight and MUST NOT be replayed. Heartbeats, lease control, and bounded
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

#### Scenario: Concurrent calls observe external change
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

#### Scenario: Source-derived failure is raced
- **WHEN** a read answers that a symbol is missing, that a candidate set is
  ambiguous, or that a body or range cannot be resolved, and a relevant source
  write completes before that answer's final guarded validation
- **THEN** the failure is discarded whole and the complete read transaction
  replays once; a second race returns retryable `NOT_READY` with reason
  `workspace_changed_during_read` and neither attempt's candidate, range, or
  body evidence

#### Scenario: Source snapshot acquisition races with replacement
- **WHEN** a trusted source is deleted or replaced after preflight but before
  the operation can acquire its exact snapshot
- **THEN** that source-derived failure still runs guarded postflight and the
  complete read replays once; if snapshot acquisition races again, the call
  returns retryable `NOT_READY` without stale or partial source payload

#### Scenario: Trusted external target is rewritten during the authoritative response
- **WHEN** a read renders a raw LSP range for a trusted read-only external
  target and another process rewrites that exact file between the guarded byte
  identity observed before the authoritative response and the one observed
  after it
- **THEN** the read replays once and returns only a raw range that its final
  guarded external identity still supports; a second race returns retryable
  `NOT_READY` naming that external path and no raw location, and a target whose
  exact bytes cannot be observed at all fails typed instead

#### Scenario: Workspace changes during both read attempts
- **WHEN** relevant workspace identity changes before final validation on both
  the original read and its one allowed replay
- **THEN** the call returns retryable `NOT_READY` with reason
  `workspace_changed_during_read` and no stale or partial success payload

#### Scenario: A foreign write occurs after the final verified byte
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

### Requirement: Build identity isolates daemon generations
The connector and daemon SHALL compute the same build identity from sorted
runtime source path and bytes, dependency lock digest, public tool/schema
version, and build-identity algorithm version. Discovery, bearer, startup lock,
nonce, and logs SHALL live under `builds/<build_identity>`. A connector SHALL
attach only to an exact identity match.

#### Scenario: Runtime source changes while an old client holds a lease
- **WHEN** a new connector computes a different build identity
- **THEN** it starts or joins the new build daemon and does not kill or reuse the
  leased old-build daemon

#### Scenario: Executed non-Python helper changes
- **WHEN** the packaged Pyright `.mjs` helper is modified or a new packaged
  `.mjs` helper is added
- **THEN** the computed build identity changes just as it does for a Python
  runtime-source change

#### Scenario: Source changes during startup
- **WHEN** daemon recomputation differs from the identity selected by its connector
- **THEN** startup fails before publishing discovery

#### Scenario: Daemon is started without connector authorization
- **WHEN** no valid one-time startup nonce exists in the locked build slot
- **THEN** the daemon refuses to publish discovery

#### Scenario: Last build lease and warm grace end
- **WHEN** no holder or warm workspace remains for a build
- **THEN** that build daemon exits without deleting discovery owned by a successor

#### Scenario: Legacy v1 status reports zero holders
- **WHEN** authenticated legacy discovery, holder count, PID, and create time
  match but the protocol cannot atomically freeze new lease acquisition
- **THEN** migration returns `atomic_retirement_unsupported`, sends no signal,
  and requires explicit operator coordination

### Requirement: Runtime executables are service-owned
The service SHALL install its pinned CPython below
`/data/CoordExp/.codex/runtime/serena-light/python`, materialize dependencies by
lock digest, and launch daemon and LSP children with locked executable paths, a
service-owned HOME, and a minimal environment allowlist. Daemon and service
venv executables MUST NOT resolve below `/root/.local/share/uv`.

#### Scenario: Service runtime is inspected after bootstrap
- **WHEN** the connector reports its Python, daemon, and language-server launch environment
- **THEN** Python is owned below the shared Serena Light runtime, HOME is
  service-owned, executable paths are locked, and no child proxy variable or
  `/root/.local/share/uv` executable is present

#### Scenario: Editable v1 service install rolls over
- **WHEN** the service-owned executable resolves Serena Light imports to the
  live repository checkout and covered source bytes change
- **THEN** the connector selects a new build identity, and rollback requires
  restoring the intended local source revision rather than reusing a slot as a
  source snapshot

### Requirement: Leases bound runtime lifetime
Each connector SHALL renew a daemon-issued lease every 15 seconds. A lease SHALL
expire after 60 seconds without renewal. After the last lease is released or
expired, the runtime SHALL remain warm for ten minutes and then stop its
language servers. Immediate release SHALL bypass the grace period.

#### Scenario: Client exits normally
- **WHEN** a connector closes its session
- **THEN** its lease is released without affecting other leases on the same root

#### Scenario: Client dies without releasing
- **WHEN** heartbeats stop for more than the lease timeout
- **THEN** the daemon expires that lease and begins warm grace only if it was the last lease

#### Scenario: Immediate workspace release
- **WHEN** the last holder calls `release_workspace(immediate=true)`
- **THEN** that binding is detached, its daemon lease remains active and
  unbound, and the runtime stops its language servers without waiting for warm
  grace

#### Scenario: Non-last holder releases immediately
- **WHEN** one of multiple same-root holders calls `release_workspace(immediate=true)`
- **THEN** only that binding is detached, its daemon lease remains active and
  unbound, and the shared runtime remains available to the other holders

#### Scenario: Long operation does not starve heartbeat
- **WHEN** a workspace LSP operation remains active for more than 60 seconds
- **THEN** the connector renews its lease independently and the daemon does not expire a live session

### Requirement: Process failure is isolated and recoverable
The system SHALL restart and retry a failed read-only language-server request at
most once, SHALL never automatically replay an edit, and SHALL place repeated
per-adapter failures into a visible cooldown. Owned language-server processes
MUST terminate when the daemon dies. A connector that observes a changed daemon
identity or lost HTTP session SHALL create a new lease and restore its last
validated binding before any new call; it MUST NOT resend an in-flight edit.

#### Scenario: Language server crashes during a query
- **WHEN** a read-only operation loses its adapter process
- **THEN** the adapter may restart and retry the operation once and records the restart in status

#### Scenario: Repeated crashes open the circuit breaker
- **WHEN** an adapter crosses the configured crash threshold
- **THEN** subsequent calls return `COOLDOWN` until the cooldown expires or the runtime is explicitly released

#### Scenario: Daemon is killed
- **WHEN** the daemon receives an ungraceful process death
- **THEN** parent-death and process-tree cleanup leave no owned Pyright, TypeScript language-server, or tsserver process running

#### Scenario: Daemon dies during an edit
- **WHEN** daemon identity is lost after an edit may have been accepted
- **THEN** the connector returns `UNCERTAIN`, does not replay the edit, and requires a current hash reread
