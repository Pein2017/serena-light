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
lookup; and host `rg`/`find` owns file and text discovery before Serena Light
overview and symbol tools take over.

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
binding owner. A rich bound-query failure's reported `working_subdirectory`
SHALL come from the failing caller's own lease binding, never from the shared,
lease-agnostic workspace runtime's construction-time identity, even when
another lease shares the same physical root.

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

#### Scenario: Two leases share one physical root at different subdirectories
- **WHEN** two leases activate different subdirectories of the same Git root and a rich operational failure (for example `SCOPE_INCOMPATIBLE` or `INVALID_PATH`) occurs on one lease's bound query
- **THEN** the failing response's `workspace.working_subdirectory` equals that lease's own activated subdirectory, and the other lease's binding and reported `working_subdirectory` remain unchanged and correct

### Requirement: Workspace activation selects one Conda environment
`activate_workspace` SHALL accept an optional `python_environment` Conda environment name. Omitting it SHALL select `ms`. The service SHALL resolve the selected environment to its exact interpreter, validate it before changing the lease binding, and keep that selection fixed for the resulting binding.

#### Scenario: Caller omits the environment
- **WHEN** a client activates any workspace without `python_environment`
- **THEN** the binding uses the `ms` Conda environment and preserves existing caller behavior

#### Scenario: Caller selects another environment
- **WHEN** a client activates a workspace with `python_environment="llm-framework-study"`
- **THEN** the bound Python runtime uses that environment's validated interpreter

#### Scenario: Selected environment is invalid
- **WHEN** the supplied environment name is invalid, missing, or lacks a usable interpreter
- **THEN** activation returns a typed failure and retains the caller's prior binding unchanged

#### Scenario: Same root is activated with another environment
- **WHEN** a caller reactivates the same root with a different valid `python_environment`
- **THEN** the lease switches to a separately keyed runtime rather than reusing the old environment's Pyright state

### Requirement: Workspace identity is normalized deterministically
The system SHALL normalize a path inside a Git repository to its resolved Git top-level identity and SHALL use the exact resolved activation path for any existing non-Git directory. A non-Git identity SHALL be reported as `non_git_read_only` and SHALL never become editable.

#### Scenario: Activation moves within one Git root
- **WHEN** a session activates two different subdirectories of the same Git repository with the same Python environment
- **THEN** the workspace runtime is reused and only session working-subdirectory metadata changes

#### Scenario: Arbitrary non-Git directory is activated
- **WHEN** a session activates an existing absolute directory that has no owning Git repository
- **THEN** that exact resolved directory becomes the read-only non-Git workspace identity

#### Scenario: Entire site-packages directory is activated
- **WHEN** an Agent chooses to activate an existing Conda `site-packages` directory
- **THEN** the directory is accepted as the non-Git workspace root without an allowlist or package-root restriction

### Requirement: Same-root sessions share and cross-root sessions isolate runtimes
The daemon SHALL reuse one `WorkspaceRuntime` and its warm language adapters for active leases with the same physical root, workspace kind, and selected Python interpreter. It SHALL use independent runtimes, locks, adapters, crash state, and cooldown state when any of those identity facts differ.

#### Scenario: Two sessions use one root and environment
- **WHEN** two sessions bind the same Python workspace with the same selected environment
- **THEN** one Pyright process serves both sessions and each session retains a separate lease

#### Scenario: Two sessions use one root with different environments
- **WHEN** two sessions bind the same physical root with different selected environments
- **THEN** each environment has an isolated Pyright runtime and neither session changes the other's binding

#### Scenario: Different roots execute concurrently
- **WHEN** semantic calls target two different workspace identities at the same time
- **THEN** the calls may execute concurrently without changing each other's binding or adapter state

#### Scenario: One root enters cooldown
- **WHEN** the TypeScript adapter repeatedly crashes for one workspace
- **THEN** other roots and the Python adapter remain available

### Requirement: Calls are serialized within a workspace
The system SHALL admit every content-bearing semantic query, same-root
activation refresh, and guarded symbol edit to one bounded FIFO transaction
owner for the bound workspace identity. The transaction owner SHALL order the
complete operation: freshness preflight, semantic work, generation mutation,
response witnessing and postflight for reads, or commit-state resolution for
edits. Same-workspace work admitted after the fixed bound is full SHALL return
typed `BUSY` and SHALL NOT execute later. Readiness waits that participate in a
semantic result SHALL retain their FIFO position, while lease operations,
heartbeats, runtime status, and work for other workspace identities SHALL remain
responsive.

#### Scenario: Parallel reads on one shared runtime
- **WHEN** multiple sessions issue semantic reads concurrently against the same workspace and no external source change occurs
- **THEN** the reads execute in FIFO transaction order and none fails because a sibling read advanced adapter or document generation

#### Scenario: Query overlaps an edit on the same root
- **WHEN** a query and `replace_symbol_body` arrive concurrently for the same workspace
- **THEN** one complete transaction finishes before the other performs its freshness preflight or observes workspace state

#### Scenario: Queued stale-hash edit reaches the front
- **WHEN** a guarded edit waits behind another same-workspace transaction and its expected hash is stale when it begins
- **THEN** it returns the existing stale-hash failure and does not overwrite the newer file

#### Scenario: Same-root activation overlaps a semantic read
- **WHEN** one lease activates the already-bound Git root while another lease is reading that root
- **THEN** the forced activation refresh and the complete read transaction execute in FIFO order without invalidating each other

#### Scenario: Queries target different roots
- **WHEN** operations target different workspace identities at the same time
- **THEN** neither workspace transaction owner blocks the other

#### Scenario: Cold global wait precedes a path query
- **WHEN** one same-workspace global query is admitted before a path query while global readiness is cold
- **THEN** the path query waits behind the global transaction and starts after that transaction has produced a result

#### Scenario: Runtime control remains responsive
- **WHEN** a same-workspace semantic transaction is blocked in readiness or LSP work
- **THEN** connector heartbeats, lease inspection, runtime status, and semantic calls for other roots remain responsive

#### Scenario: Blocking LSP call runs on one root
- **WHEN** a fake LSP request blocks one workspace executor for longer than a heartbeat interval
- **THEN** another root, runtime status, and connector heartbeats continue without event-loop delay

#### Scenario: Same-workspace transaction queue is saturated
- **WHEN** one transaction is running and the fixed number of ordinary queue entries are already waiting
- **THEN** the next semantic call returns typed `BUSY` and that rejected work never starts later

#### Scenario: Queued request is cancelled
- **WHEN** a client cancellation is accepted before its bounded transaction entry starts
- **THEN** the entry is removed without running freshness, mutating adapter state, or retaining a workspace lock

#### Scenario: Queued edit reaches its timeout
- **WHEN** an edit is proven not to have started and is cancelled in either owned queue
- **THEN** it returns `TIMED_OUT` and can never execute later

#### Scenario: Running edit reaches its timeout
- **WHEN** an edit has started or its commit state cannot be proven
- **THEN** it returns `UNCERTAIN`, is not replayed, and requires a fresh hash read

#### Scenario: Ordinary request queue is saturated during shutdown
- **WHEN** all ordinary LSP queued-work slots are occupied and both fixed language adapters require cleanup
- **THEN** their two cleanup obligations remain admissible without increasing ordinary request capacity, while a third cleanup submission fails explicitly

#### Scenario: Runtime stops with queued transactions
- **WHEN** a workspace runtime begins shutdown while semantic transactions are queued
- **THEN** queued reads and edits are cancelled before execution, the running transaction is settled, and all owned transaction and LSP workers reach a bounded terminal state

### Requirement: Query and edit roots obey a fixed trust policy
The system SHALL accept path operands only from the active workspace inventory. It SHALL return any existing language-server location outside the active workspace as `read_only_external` rather than rejecting it by a query-root allowlist. It SHALL permit activation of any existing absolute directory, use a bounded no-symlink inventory for non-Git roots, permit edits only inside resolved Git workspaces below `/data`, and reject symlink escapes.

#### Scenario: Query reaches the selected environment
- **WHEN** a Python definition resolves into the selected environment's standard library or site-packages
- **THEN** the location is returned as a read-only external result

#### Scenario: Query reaches another existing external root
- **WHEN** a language server returns an existing location outside the active workspace and selected environment
- **THEN** the location remains visible with `read_only_external` metadata and cannot expand the active workspace inventory

#### Scenario: Trusted path belongs to another workspace
- **WHEN** a caller directly supplies a path outside the active workspace inventory
- **THEN** the tool returns `OUT_OF_WORKSPACE` with the active identity and an absolute activation hint

#### Scenario: Edit targets any non-Git workspace
- **WHEN** an editing tool targets a file in a bound non-Git root
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

A `SCOPE_INCOMPATIBLE` failure returned from a bound semantic or diagnostics
call SHALL remain `ok=false`, non-retryable, and bounded, and, whenever it is
backed by an actual scope projection, SHALL reuse that projection's own
already-computed facts rather than rerunning a probe: the language family, the
selected native config path when the project kind is configured, the project
kind when known, and the `configured_program_outside_trust` differences bounded
by path and reason with an accurate `total`, `digest`, and `omitted_count`. It
MUST NOT label the interpreter or language-server executable as the
incompatible "program"; engine and interpreter identity remain owned by
`status`/`get_runtime_status`. When a `SCOPE_INCOMPATIBLE` failure has no
backing projection (for example, a language family with no trusted source
paths, or a directory/global scope spanning more than one already-unavailable
family), it SHALL retain its existing concise reason and bounded paths without
fabricating config facts it does not have.

Once a language family is recorded unavailable after a native-program
attribution or reattribution, a later bound call for that family SHALL still
run its own call-time freshness preflight before the family's `SCOPE_INCOMPATIBLE`
failure is raised, so another agent's concurrent config or trust fix remains
observable; that preflight MUST NOT be skipped by treating the recorded
unavailability as an already-fresh cached result. After that preflight, a
family still recorded unavailable SHALL fail before starting, warming, or
submitting any work to that family's adapter, and MUST NOT require an
executor-dependent status wait to do so.

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

#### Scenario: A bound query hits a scope-incompatible configured program
- **WHEN** `find_symbol`, `get_symbols_overview`, a reference/declaration/implementation lookup, or diagnostics targets a language family whose configured program contains paths outside trust
- **THEN** the `SCOPE_INCOMPATIBLE` error details include the language family, project kind, the selected native config path when present, and bounded `configured_program_outside_trust` items with path, reason, total, digest, and omitted_count, with no engine or interpreter field and no retry metadata

#### Scenario: Scope incompatibility has no backing projection
- **WHEN** a `SCOPE_INCOMPATIBLE` failure arises from a family with no trusted source paths, a failed adapter construction, or a directory/global scope spanning more than one already-unavailable family
- **THEN** the error details keep the existing concise reason and bounded paths without inventing a language, project kind, or selected config value

#### Scenario: A blocked family is queried again after another agent's fix
- **WHEN** a bound call targets a language family already recorded unavailable from a prior scan
- **THEN** the call still runs its own call-time freshness preflight before failing, and if that family remains unavailable it fails in routing before any adapter start, warm, or submitted work, without waiting on the executor queue

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

### Requirement: Workspace activation warns about an evident Conda path mismatch
After validating the caller-selected `python_environment`, `activate_workspace`
SHALL compare the resolved activation path with the installed Conda environment
prefixes already owned by Serena Light. When that path is unambiguously below
one installed environment but the selected environment has another name, a
successful activation SHALL retain the selected binding and add exactly one
bounded warning containing `code=PYTHON_ENVIRONMENT_PATH_MISMATCH`, the selected
environment, the path-indicated environment, and a `next_action` that tells the
Agent to reactivate the same absolute path with the path-indicated environment
if that environment owns the target semantics.

The warning SHALL be advisory: it MUST NOT change the selected environment,
interpreter, workspace identity, editability, or lease. No warning SHALL be
emitted when the names match, when the path is not below one unambiguous
installed Conda environment, or when activation fails. Serena Light MUST NOT
infer an environment from project files, ambient shell state, imports, or PATH.

#### Scenario: Default environment disagrees with a site-packages path
- **WHEN** the caller omits `python_environment` while activating a resolved path below the installed `llm-framework-study` environment
- **THEN** activation succeeds on the `ms` binding and returns one `PYTHON_ENVIRONMENT_PATH_MISMATCH` warning whose next action names `llm-framework-study`

#### Scenario: Explicit environment matches the activated path
- **WHEN** the caller activates a path below the installed `llm-framework-study` environment with `python_environment="llm-framework-study"`
- **THEN** activation succeeds without an environment mismatch warning

#### Scenario: Ordinary repository has no path-indicated environment
- **WHEN** the caller activates a Git or non-Git directory that is not below one installed Conda environment prefix
- **THEN** activation uses the selected or default environment without fabricating a detected environment or warning

#### Scenario: Warning does not authorize an automatic switch
- **WHEN** activation reports a path mismatch warning
- **THEN** subsequent semantic calls continue using the selected binding until the Agent explicitly calls `activate_workspace` again

### Requirement: Runtime status is compact when healthy and issue-rich when needed
`get_runtime_status` SHALL return one compact, binding-local status containing
the workspace root, caller lease's working subdirectory, workspace kind,
selected Python environment, build identity, server and protocol versions, one
state for each attributed Python or JavaScript/TypeScript language family, a
bounded executor summary, and an `issues` array. Language state SHALL use the
stable values `cold`, `warming`, `ready`, `cooldown`, `failed`, or `unavailable`.
Normal lazy `cold` state SHALL not by itself be an issue.

When no issue exists, `issues` SHALL be empty and status MUST NOT return lease
UUIDs or timestamps, daemon UUID, interpreter or executable paths, inventory or
configured-program digests, adapter generations, transition history, crash
history, or per-family capability matrices. When an issue exists, `issues`
SHALL contain only bounded records for the affected family or executor. Each
record SHALL contain a stable code, retryability, remediation, and only the
phase, retry delay, program/interpreter identity, scope evidence, or current
failure detail required to act on that issue. Status MUST NOT dump unrelated
healthy-family detail or unbounded history.

Runtime status SHALL remain control-plane inspection rather than a
content-bearing semantic read, SHALL not start or warm an adapter, and SHALL not
replace call-time freshness or semantic readiness checks.

#### Scenario: Both configured language families are healthy
- **WHEN** an Agent inspects a binding whose attributed families are cold, warming, or ready without a failure, cooldown, incompatibility, or saturated executor
- **THEN** status returns the compact binding/build/language summary with `issues=[]` and none of the prohibited debug history or identity fields

#### Scenario: One family is scope-incompatible
- **WHEN** Python is unavailable because its configured program is outside trust while TypeScript remains healthy
- **THEN** status keeps the compact TypeScript state and adds one bounded Python issue with the selected configuration, offending-path evidence, and remediation needed to correct or bypass that family

#### Scenario: Adapter is cooling down
- **WHEN** one adapter is in cooldown after repeated crashes
- **THEN** its language state is `cooldown` and its issue includes retry timing and current crash reason without returning transition or crash history

#### Scenario: Runtime status is called during semantic work
- **WHEN** a same-workspace semantic transaction is active or queued
- **THEN** status reports a bounded executor summary without entering that queue or delaying other roots

#### Scenario: Lease is intentionally unbound
- **WHEN** an Agent calls runtime status after `release_workspace`
- **THEN** status returns `workspace=null`, no language states, and one bounded non-retryable `WORKSPACE_UNBOUND` issue whose remediation is `activate_workspace`

### Requirement: Agent metadata makes normalized coordinate usage explicit
The byte-identical initialization guidance and owning navigation tool metadata
SHALL jointly state that normalized Serena Light ranges use 0-based decoded-text
line numbers and 0-based Unicode code-point columns, and that an editor or
`nl -ba` display line is the returned line plus one. The source-owned
initialization string SHALL remain no longer than 220 characters.

The system MUST NOT add a caller-selectable 1-based mode, duplicate a position
basis field into ordinary compact success, or relabel an external raw LSP range
whose existing `position_basis` states another coordinate system.

#### Scenario: Fresh client receives coordinate guidance
- **WHEN** a Codex, Claude Code, or CC Agent client initializes and inspects a navigation tool
- **THEN** it can determine the normalized line/column basis and editor-line conversion without calling another tool

#### Scenario: Normalized navigation succeeds
- **WHEN** a workspace-owned semantic target is returned with compact `range`
- **THEN** its schema remains unchanged and the Agent applies the statically documented 0-based decoded-text convention

#### Scenario: External location retains a raw position basis
- **WHEN** an external read-only target lacks an exact response-owned snapshot and returns `raw_range`
- **THEN** its explicit raw `position_basis` remains authoritative and the normalized-coordinate guidance does not override it
