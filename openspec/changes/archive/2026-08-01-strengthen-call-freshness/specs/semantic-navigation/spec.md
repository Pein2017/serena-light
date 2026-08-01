## MODIFIED Requirements

### Requirement: Symbol overviews are document-scoped
`get_symbols_overview` SHALL use the owning adapter's document-symbol tree for
one authorized source file and SHALL support a bounded descendant depth. Its
single file group SHALL contain `symbols`; each overview node SHALL contain only
`name`, lowercase `kind`, and `children` when non-empty. It MUST NOT return node
ranges, selection ranges, detail, name paths, hashes, adapter/generation data,
or boolean child flags. Optional `include_kinds` and `exclude_kinds` SHALL accept
stable lowercase kind strings and SHALL narrow the result without changing the
default complete set of symbol kinds. Filtering SHALL be post-order: a
non-matching ancestor MAY remain only when required to retain a matching
descendant's structural path, `exclude_kinds` SHALL win for a node named in both
filters, and every node actually removed by filtering SHALL increment
`omitted`. Descendants removed by `max_depth` and nodes removed by final answer
budgeting SHALL also contribute to the same `omitted` total.

#### Scenario: MJS file overview is requested
- **WHEN** the client requests an overview of `external/codexUI/scripts/generate-pwa-icons.mjs`
- **THEN** the TypeScript adapter returns the file's functions and requested descendants as compact name/kind/children nodes without scanning unrelated files

#### Scenario: Default overview is requested
- **WHEN** neither kind filter is supplied
- **THEN** no supported symbol kind is silently hidden

#### Scenario: Kind filter matches only nested descendants
- **WHEN** a requested kind occurs below an ancestor whose kind does not match the filter
- **THEN** the matching descendant remains reachable through the minimum ancestor path and every unrelated removed node contributes to `omitted`

#### Scenario: Result exceeds the answer limit
- **WHEN** the final serialized overview exceeds `max_answer_chars`
- **THEN** trailing nodes or complete remaining subtrees are removed in stable preorder, orphan children and childless nonmatching structural ancestors are never produced, and `omitted` counts every removed node

## ADDED Requirements

### Requirement: Cold TypeScript semantic reads are independent of prior tool order

For a trusted TypeScript file in the configured program, the system SHALL NOT
require an earlier overview, diagnostics, global-symbol, or unrelated semantic
call in order to resolve a cross-file declaration or complete reference set.
The system MAY use a bounded internal LSP preparation request to open a trusted
workspace owner, but the public declaration/reference result SHALL still come
from the declared authoritative LSP operation, pass the same two-response
stability check, and retain response-owned freshness witnesses. This internal
preparation SHALL retain the exact trusted owner bytes as an internal freshness
witness even when that owner is not a public result target, and SHALL NOT parse
imports, open an untrusted or read-only-external hint, add a watcher, or alter
the public tool schema.

#### Scenario: Declaration is the first semantic call after adapter start

- **WHEN** a configured-program TypeScript file imports a symbol from another
  trusted workspace file and `find_declaration` is the first semantic call
- **THEN** the tool returns the owner declaration rather than the local import
  alias without requiring the client to warm the owner through another tool

#### Scenario: References are the first semantic call after adapter start

- **WHEN** `find_referencing_symbols` is the first semantic call for a
  configured-program TypeScript symbol with references in other trusted files
- **THEN** the tool returns the complete current cross-file reference set rather
  than a cold-project subset
