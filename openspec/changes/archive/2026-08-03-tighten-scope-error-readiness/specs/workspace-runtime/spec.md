## MODIFIED Requirements

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
