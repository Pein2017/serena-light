## ADDED Requirements

### Requirement: MCP initialization teaches the efficient Serena Light workflow
The outer stdio connector and inner daemon SHALL publish one byte-identical,
source-owned MCP initialization instruction string. The instructions SHALL be
concise and SHALL state that Serena Light provides Python and
JavaScript/TypeScript semantic navigation and diagnostics; startup cwd is
auto-bound but shell `cd` does not rebind; a cross-root switch requires
`activate_workspace` with an absolute directory path; file or directory query
scope is preferred when known; overview defaults to depth 0; reference snippets are opt-in;
diagnostics are called explicitly after a meaningful edit group; runtime status
is for debugging/build/readiness rather than routine preflight; and host
shell/file tools own lexical file enumeration and text search.

The system MUST NOT add an initialization hook, automatic diagnostic injection,
or a public instructions function. The instruction text MUST NOT advise calling
runtime status before every query and MUST NOT claim support outside Python and
JavaScript/TypeScript.

#### Scenario: Fresh stdio client initializes
- **WHEN** a Codex, Claude Code, or CC Agent client initializes through the outer connector
- **THEN** it receives the concise workflow instructions before choosing tools

#### Scenario: Direct daemon client initializes
- **WHEN** an authenticated acceptance client initializes directly against the inner daemon
- **THEN** its instruction bytes exactly match the outer connector's instructions

#### Scenario: Agent changes shell directory
- **WHEN** the instruction text explains a shell `cd` to another repository
- **THEN** it directs the Agent to call `activate_workspace` with an absolute directory path and never implies automatic rebinding

#### Scenario: Agent needs lexical search or diagnostics
- **WHEN** the instruction text covers file/text discovery and post-edit diagnostics
- **THEN** it assigns lexical work to host tools and leaves diagnostics as an explicit Agent-chosen Serena Light call without a hook
