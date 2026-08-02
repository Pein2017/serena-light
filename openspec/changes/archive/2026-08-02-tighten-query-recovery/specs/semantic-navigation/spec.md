## ADDED Requirements

### Requirement: Deterministic navigation failures guide one safe recovery
Serena Light SHALL add bounded machine-readable recovery evidence only to
deterministic navigation failures for which one existing public action is safe.
Recovery SHALL use the closed `details.next_action` values
`get_symbols_overview` and `activate_workspace_if_other_root`; it MUST NOT
contain free-form advice, execute the action, dispatch another semantic
operation, guess a target root, perform fuzzy or lexical symbol matching, or
select an ambiguous candidate.

When `find_symbol` returns `SYMBOL_NOT_FOUND` for one existing authorized file,
the error SHALL retain that `relative_path` and
`next_action=get_symbols_overview`. A directory-scoped or global miss MUST NOT
advertise the file-only overview action.

When a bound semantic call returns `INVALID_PATH`, the compact error SHALL
retain the active physical workspace root and
`next_action=activate_workspace_if_other_root`. This action is conditional and
MUST NOT claim that the attempted path exists under another root. At the
512-character public answer floor, error code, message, active workspace, and
`next_action` SHALL survive before long echoed query or path fields.

`AMBIGUOUS_SYMBOL` SHALL keep its existing bounded deterministic candidates and
remain a failure. Agent-facing `find_symbol` metadata SHALL direct callers to
retry with one returned qualified name path rather than guessing aliases.

#### Scenario: Exact symbol name is guessed incorrectly in one file
- **WHEN** `find_symbol` completes against one authorized source file with no matching name path
- **THEN** it returns typed `SYMBOL_NOT_FOUND` with the file, original name path, and `next_action=get_symbols_overview` without a second semantic dispatch

#### Scenario: Directory-scoped symbol lookup has no match
- **WHEN** `find_symbol` finds no symbol under an authorized directory scope
- **THEN** it remains a typed miss and does not advertise the file-only overview action

#### Scenario: Global symbol lookup has no match
- **WHEN** a ready global `find_symbol` query has no semantic result
- **THEN** it preserves the existing global result semantics and does not fabricate an overview target or fuzzy candidate

#### Scenario: Relative path is resolved under the wrong active root
- **WHEN** a bound semantic call receives a relative path that is invalid under the current workspace
- **THEN** it returns typed `INVALID_PATH`, the active workspace, and `next_action=activate_workspace_if_other_root` without changing the lease

#### Scenario: Invalid path is merely misspelled
- **WHEN** the attempted path does not exist under the active root or any caller-known root evidence
- **THEN** the conditional activation action does not claim another root exists and no filesystem-wide discovery is performed

#### Scenario: Symbol name is ambiguous
- **WHEN** multiple current symbols match an unqualified name path
- **THEN** the existing bounded candidates remain authoritative and no candidate is selected automatically

#### Scenario: Recovery error approaches the minimum answer budget
- **WHEN** long echoed path or query fields would make the deterministic error exceed 512 characters
- **THEN** whole optional echoes are removed before active workspace or `next_action`, and the canonical text remains valid JSON within the bound

#### Scenario: Recovery metadata is inspected through FastMCP
- **WHEN** a real daemon client receives either recovery failure
- **THEN** `content[0].text` remains canonical JSON equal to `structuredContent`, `isError` remains false, and no success, engine, generation, or configured-program metadata is added
