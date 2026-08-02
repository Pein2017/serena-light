## MODIFIED Requirements

### Requirement: MCP initialization teaches the efficient Serena Light workflow
The outer stdio connector and inner daemon SHALL publish one byte-identical,
source-owned MCP initialization instruction string no longer than 220
characters. The instruction SHALL state that Serena Light provides Python and
JavaScript/TypeScript semantic navigation and diagnostics; shell `cd` does not
rebind the current lease; switching roots requires `activate_workspace` with an
absolute path; unfamiliar files should be overviewed before exact symbol
lookup; and host shell/file tools own lexical search.

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
