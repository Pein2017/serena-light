## ADDED Requirements

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
