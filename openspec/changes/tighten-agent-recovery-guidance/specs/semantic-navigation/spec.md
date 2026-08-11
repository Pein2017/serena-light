## ADDED Requirements

### Requirement: Unsupported implementation lookup gives an honest fallback
When `find_implementations` is unavailable because the selected language server
does not advertise `implementationProvider`, its existing rich `UNSUPPORTED`
error SHALL include `reason=implementation_provider_unavailable` and
`next_action=find_referencing_symbols`. The error SHALL retain the response-owned
capability evidence needed to explain the boundary and MUST NOT claim that
references, type hierarchy, text matches, or inferred subclasses are
implementations.

No implementation fallback SHALL dispatch automatically. TypeScript or a future
adapter that advertises `implementationProvider` SHALL continue using the real
LSP implementation request without the fallback guidance appearing in success.

#### Scenario: Pyright implementation lookup is requested
- **WHEN** Pyright does not advertise `implementationProvider` and the Agent calls `find_implementations`
- **THEN** the tool returns rich `UNSUPPORTED` with the provider evidence, `reason=implementation_provider_unavailable`, and `next_action=find_referencing_symbols`

#### Scenario: Agent follows the suggested reference lookup
- **WHEN** the Agent calls `find_referencing_symbols` after the unsupported implementation result
- **THEN** that independent reference query keeps its normal semantics and no reference is relabeled as an implementation

#### Scenario: TypeScript implementation provider is available
- **WHEN** the TypeScript adapter advertises `implementationProvider`
- **THEN** `find_implementations` dispatches the real provider and its compact success contains no fallback metadata

### Requirement: Oversized exact symbol bodies prescribe the narrowest safe recovery
When an exact `find_symbol(include_body=true)` match cannot fit as one complete
record, the existing bounded `INVALID_INPUT` error SHALL retain
`field=max_answer_chars` and `minimum_required_chars` and SHALL add one bounded
`next_action`. If the matched symbol is a structural container with known child
symbols, the next action SHALL direct the Agent to call
`get_symbols_overview(max_depth=1)` for the same file and then request an exact
child `name_path`. If the exact body has no known child expansion and its
measured minimum is within the public maximum, the next action SHALL direct a
retry with that exact `max_answer_chars`. If neither recovery applies, the next
action SHALL direct the Agent to repeat the exact `find_symbol` without body,
then use that compact range for an exact host file read rather than a broad
scan.

The error MUST NOT include partial body text, invent child symbols, echo adapter
or generation metadata not already required by the measured error, increase the
public maximum, or introduce body slicing, pagination, AST-node editing, or a
new tool.

#### Scenario: Oversized class has known methods
- **WHEN** an exact class body exceeds the answer budget and its verified symbol tree contains children
- **THEN** the error recommends a depth-1 overview followed by exact child `find_symbol`, without returning a partial class body

#### Scenario: Oversized leaf fits a legal larger budget
- **WHEN** an exact function body exceeds the caller's budget but its `minimum_required_chars` is at most 50000
- **THEN** the error recommends retrying with that measured value and does not recommend nonexistent children

#### Scenario: Exact body exceeds the public maximum
- **WHEN** a leaf body requires more than 50000 characters and has no known child expansion
- **THEN** the error recommends an exact no-body symbol lookup followed by a host range read and does not raise the public limit or return truncated source

#### Scenario: Compact exact body fits
- **WHEN** the requested exact symbol body fits the caller's answer budget
- **THEN** normal compact success is unchanged and contains no recovery metadata
