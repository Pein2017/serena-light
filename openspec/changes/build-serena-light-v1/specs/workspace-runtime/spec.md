## ADDED Requirements

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

#### Scenario: Omitted trusted file changes
- **WHEN** a trusted file outside the configured program changes without changing native program membership
- **THEN** its path-scoped document generation is invalidated without falsely invalidating or expanding configured-program global readiness

#### Scenario: Concurrent calls observe external change
- **WHEN** multiple semantic calls arrive after one external filesystem change
- **THEN** they share one synchronous in-flight freshness scan and none may
  return success using a stale time-cache entry

#### Scenario: Same root is activated again
- **WHEN** a bound session activates another path in the same Git root
- **THEN** the runtime performs an immediate refresh before returning reuse

#### Scenario: Native-config adapter stop times out
- **WHEN** a changed native config requires adapter restart but the exact old adapter stop does not reach its bounded terminal state
- **THEN** that family becomes explicitly `TIMED_OUT` and retryable, remains unpublished, and every later freshness preflight retries the same pending cleanup even if filesystem facts are unchanged

#### Scenario: Runtime retires with a pending adapter restart
- **WHEN** the last holder releases a runtime after a config restart timed out
- **THEN** runtime shutdown retains and settles the pending adapter cleanup responsibility, never republishes the old adapter, and never installs a replacement after the runtime is stopped

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
