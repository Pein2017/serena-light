## ADDED Requirements

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

## MODIFIED Requirements

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

