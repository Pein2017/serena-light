## ADDED Requirements

### Requirement: Full-file trust catalog is distinct from semantic scope
For a Git workspace the system SHALL derive one full-file trust catalog from
cached and untracked non-ignored Git paths, without filtering by language
extension or enumerating ignored directory contents. It SHALL admit only
lexically in-root regular files whose path components pass no-symlink checks.
The existing semantic source inventory SHALL remain the supported Python and
JS/TS projection, and native configured programs SHALL remain separate
adapter-owned projections. The system MUST NOT expand semantic readiness or
editing authority merely because a path is present in the full-file catalog.

For the exact trusted non-Git transformers root, the system SHALL build only a
bounded, no-symlink catalog for the caller's explicit lexical scope. It MUST NOT
implicitly enumerate the enclosing site-packages directory.

#### Scenario: Git workspace contains non-source text
- **WHEN** tracked or eligible untracked documentation, tests, or configuration
  files have unsupported semantic extensions
- **THEN** they enter the full-file catalog but do not enter a language adapter's
  source inventory or configured program

#### Scenario: Ignored subtree is large
- **WHEN** a Git workspace contains a large ignored data or build subtree
- **THEN** Git exclusion authority prevents its contents from being enumerated
  or admitted to the full-file catalog

#### Scenario: Catalog path changes identity
- **WHEN** a cataloged regular file is deleted, renamed, or replaced by a
  symlink or a symlinked ancestor
- **THEN** freshness removes or rejects the path before lexical success and does
  not leave semantic or edit trust stale

#### Scenario: Trust status is requested
- **WHEN** an agent inspects a bound workspace
- **THEN** status reports bounded full-file and semantic-inventory counts and
  digests as distinct values without listing the complete catalog

### Requirement: Lexical runtime and ripgrep are service-owned
The system SHALL use only a pinned Linux ripgrep executable installed below the
current `deps/<lock_digest>/bin` runtime slot. Its version, release asset,
platform, SHA-256, license/provenance, and lock format SHALL be fixed in a
repository-owned lock manifest included in dependency-lock digest and build
identity. Bootstrap SHALL verify the asset before atomic installation. Runtime
calls MUST NOT resolve ambient `PATH`, user ripgrep configuration, user ignore
configuration as an independent authority, or the network.

Each workspace SHALL own one single-worker lexical executor with at most four
queued `search_text` calls and one exact ripgrep process-group owner. Runtime
shutdown SHALL seal admission and settle queued/running lexical work before
publishing `stopped`. Lexical execution SHALL remain independent of the
single-worker semantic LSP executor.

#### Scenario: Ambient ripgrep differs
- **WHEN** the client or daemon `PATH` contains another `rg` version or user
  ripgrep configuration
- **THEN** Serena Light invokes the verified service-owned executable with
  `--no-config` and fixed arguments

#### Scenario: Dependency checksum mismatches
- **WHEN** bootstrap downloads bytes that do not match the lock manifest
- **THEN** installation fails closed, no executable is published, and the
  mismatched bytes cannot enter a build slot

#### Scenario: Dependency lock changes
- **WHEN** the ripgrep version, asset, checksum, platform, or lock algorithm
  changes
- **THEN** dependency-lock digest and build identity change and connectors do
  not reuse the old daemon as the new build

#### Scenario: Lexical process is running
- **WHEN** runtime status is requested during `search_text`
- **THEN** status reports bounded lexical queue/running state and pinned ripgrep
  identity without exposing the pattern, matched source, or subprocess payload

### Requirement: MCP initialization provides shared agent instructions
The client-visible stdio connector and inner daemon MCP server SHALL publish the
same concise initialize-instructions text from one source. It SHALL identify
Serena Light as shared semantic and lexical code navigation, state that
workspace binding is session-scoped, require absolute `activate_workspace`
after switching repositories, prefer semantic tools for symbols, route
file/config/string discovery to `find_paths` and `search_text`, and point agents
to typed readiness/status information. The system MUST NOT add a hook or a
public `initial_instructions` tool.

#### Scenario: Client initializes through stdio proxy
- **WHEN** Codex, Claude Code, or a CC Agent completes MCP initialize with the
  connector that answers locally
- **THEN** it receives the same Serena Light instructions that direct daemon
  initialization would publish

#### Scenario: Agent changes shell directory
- **WHEN** an initialized agent changes from one repository to another
- **THEN** the instructions tell it to call
  `activate_workspace("/absolute/path")` rather than implying that shell `cd`
  changes the existing session binding

#### Scenario: No functional instructions call is requested
- **WHEN** an agent has initialized successfully
- **THEN** all routing guidance is already available without invoking an extra
  public tool or hook

### Requirement: Lexical tools roll over as public schema 4
The daemon and connector SHALL advertise exactly `find_paths` and `search_text`
as new read-only tools, SHALL include their schemas and initialize instructions
version in public tool/schema identity, and SHALL report public schema version
`4`. A connector SHALL attach only to a daemon with the identical build and
public schema identity.

#### Scenario: Schema-3 daemon is still leased
- **WHEN** schema-4 source and dependencies are installed while an older
  schema-3 daemon retains holders
- **THEN** new connectors start or attach to the schema-4 build slot without
  killing the leased schema-3 daemon

#### Scenario: Connector read-only routing is checked
- **WHEN** either lexical tool crosses the connector boundary
- **THEN** it is admitted through the explicit read-only tool allowlist and no
  arbitrary tool name or editing replay path is opened
