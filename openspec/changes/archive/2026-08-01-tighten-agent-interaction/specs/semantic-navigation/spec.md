## MODIFIED Requirements

### Requirement: Navigation tools return stable typed JSON
The system SHALL expose `get_symbols_overview`, `find_symbol`,
`find_referencing_symbols`, `find_declaration`, and `find_implementations` with
stable JSON success and error envelopes. Navigation success SHALL contain
exactly `ok=true` and `data`. `data` SHALL contain the active physical workspace
root once as `workspace`, deterministically ordered file groups as `files`, and
one non-negative integer `omitted`. A file group SHALL contain `path` once and
one tool-specific records array; it SHALL contain whole-file `sha256` once only
when a returned record includes source body text. It SHALL contain `language`
only when the response combines language families or the path does not
unambiguously identify the family. Trusted external/read-only identity SHALL
appear once per file group.

Navigation success MUST NOT include top-level or per-record adapter phase,
runtime generations, configured-program detail, query echoes, URI, repeated
path/hash, text offset, or byte offset. Public ranges SHALL use
`[[start_line,start_column],[end_line,end_column]]`, with 0-based decoded-text
lines and 0-based Unicode code-point columns from the exact verified snapshot.
Known symbol kinds SHALL be stable lowercase strings and unknown numeric kinds
SHALL be `unknown:<integer>`.

Deterministic `INVALID_INPUT`, `INVALID_PATH`, and `SYMBOL_NOT_FOUND` errors
SHALL contain `ok=false`, a typed code and concise message, the bound workspace
when available, and only bounded details required to correct the query; they
MUST NOT repeat adapter phase, runtime generations, configured-program detail,
or engine identity. `AMBIGUOUS_SYMBOL` SHALL retain a bounded deterministic
candidate set. Operational `NOT_READY`, `BUSY`, `COOLDOWN`, `TIMED_OUT`,
`SCOPE_INCOMPATIBLE`, and `UNCERTAIN` failures SHALL retain the rich adapter,
generation, phase, retry, and diagnostic metadata required to recover.

A reference, declaration, or implementation record backed by an exact
response-owned snapshot SHALL use compact `range`. A read-only external target
that lacks an exact response-owned snapshot for that response SHALL instead
carry `raw_range` in the same two-point array shape plus a `position_basis`
string naming the raw LSP coordinate system; the system MUST NOT populate both
fields on one record and MUST NOT relabel a raw-basis location as decoded-text.
Trusted external identity SHALL be the file group's own absolute `path` plus an
optional `read_only` boolean set to `true` once per group; the system MUST NOT
introduce a second non-forgeable external identifier or an alternate trust
owner.

#### Scenario: Navigation succeeds
- **WHEN** a supported semantic query completes
- **THEN** the response has `ok=true`, compact `data.workspace`, `data.files`, and `data.omitted`, with no repeated operational metadata

#### Scenario: Deterministic query fails
- **WHEN** a query has invalid input/path or a current ready snapshot contains no matching symbol
- **THEN** the response keeps the typed correction evidence without adapter, engine, configuration, or generation repetition

#### Scenario: Navigation cannot run operationally
- **WHEN** a request is unsupported, not ready, busy, timed out, uncertain, in cooldown, or incompatible with configured scope
- **THEN** the response has `ok=false` and the existing rich error code, phase, retry, and authority metadata rather than a successful empty payload

#### Scenario: UTF-16 adapter reports a range after an astral character
- **WHEN** a semantic result follows a non-BMP character in a UTF-8 file with CRLF line endings and an optional BOM
- **THEN** its compact range uses 0-based decoded-text line and Unicode code-point columns without exposing redundant LSP, text, or byte offsets

#### Scenario: External target lacks an exact response-owned snapshot
- **WHEN** a trusted read-only external declaration, implementation, or reference target cannot be bound to an exact response-owned snapshot
- **THEN** its record carries `raw_range` and `position_basis` instead of `range`, the file group's `path` plus `read_only=true` remain its only identity, and the location is never presented as decoded-text

#### Scenario: Workspace reference mapping cannot be verified
- **WHEN** a workspace reference has a response-owned snapshot but its URI or LSP range cannot be mapped to that snapshot
- **THEN** the tool returns rich retryable `NOT_READY` and never emits raw-basis coordinates for the workspace target

#### Scenario: Source body is returned
- **WHEN** one or more returned records from the same file contain body text
- **THEN** the current whole-file SHA-256 appears once on that file group and not on each record

#### Scenario: A semantic target changes after the first response
- **WHEN** a declaration, implementation, or reference target changes before Serena Light can bind the target snapshot to the reporting adapter
- **THEN** the service performs at most one bounded replay against the bound target snapshots and returns retryable `NOT_READY` unless the location set and owning generations remain stable

#### Scenario: Semantic replay crosses a language-server process or capability identity
- **WHEN** the adapter runtime token, raw or derived capabilities, position encoding, or owning generations change before either response, target binding, or final rendering
- **THEN** the service returns retryable `NOT_READY` and never reuses the source or target snapshot across the replacement process

#### Scenario: Semantic target set exceeds the response-owned bound
- **WHEN** the first response contains more than 64 unique workspace and external target URIs
- **THEN** the service returns non-retryable `UNSUPPORTED` with bounded deterministic target evidence before reading or opening any target snapshot

### Requirement: Navigation success obeys the client-visible answer budget
Every navigation tool that accepts `max_answer_chars` SHALL default it to
12,000 and accept values from 512 through 50,000 inclusive. `find_declaration`
SHALL expose the same input. The bound SHALL apply to the length of the complete
canonical minified JSON string placed in the actual MCP
`CallToolResult.content[0].text` after every field, file group, coverage object,
and `omitted` count is present. The same JSON value SHALL be available in
`structuredContent`.

Before grouping, the system SHALL deduplicate and deterministically order atomic
results. It SHALL apply `max_matches` where present, then remove trailing whole
records or overview subtrees and reserialize until the success text fits. It
MUST NOT cut a path, name, range, body, info, snippet, message, or JSON token.
`omitted` SHALL equal the number of atomic results removed by public match or
answer limits plus any upstream semantic bound reported to the presentation
layer. Caller-selected overview depth, explicit kind filters, and default
descendant-noise filtering MUST NOT contribute to `omitted`.

If at least one semantic match exists but the first eligible record in stable
order cannot fit, the tool SHALL return a bounded `INVALID_INPUT` error
containing `field=max_answer_chars` and `minimum_required_chars`, together with
the workspace and only the minimum original semantic authority needed to make
that measurement, rather than an empty success. For a multi-adapter global
result, the available authority SHALL be a bounded deterministic list of
adapter/generation pairs in error details rather than a fabricated single
top-level adapter. Candidate-bearing semantic errors SHALL bound their evidence
using the caller's public answer budget even when a larger private internal
limit is used to avoid prematurely pruning compact success.

#### Scenario: FastMCP would expand an inner bounded value
- **WHEN** the complete result would fit an internal compact fragment but default MCP serialization would exceed `max_answer_chars`
- **THEN** the connector-visible `content[0].text` is the canonical minified JSON, fits the bound after whole-result pruning, and `structuredContent` represents the same value

#### Scenario: Exact body is larger than the answer bound
- **WHEN** one exact requested symbol body is the first eligible stable record and its complete success cannot fit
- **THEN** the tool returns `INVALID_INPUT` with the measured minimum required characters and never returns a partial body or misleading empty success

#### Scenario: Later record is smaller than the stable prefix
- **WHEN** a large first record cannot fit but a later record would fit in isolation
- **THEN** the tool returns the characters required for the reachable first-record prefix and does not advertise the unreachable later-record minimum

#### Scenario: Ambiguity contains many candidates
- **WHEN** a path-scoped query is ambiguous and its candidate evidence exceeds the caller's public answer budget
- **THEN** the rich error returns a deterministic bounded candidate prefix with truthful truncated and omitted counts rather than using the private success budget

#### Scenario: Global minimum error spans language families
- **WHEN** no stable compact record fits and the original global result contains multiple adapter/generation owners
- **THEN** the rich error retains a bounded deterministic list of those owners without adding operational metadata to success

#### Scenario: Query genuinely has no semantic result
- **WHEN** a ready supported semantic query finds no result and no result was removed by a result limit
- **THEN** it returns `files=[]` and `omitted=0`

### Requirement: Symbol overviews are document-scoped
`get_symbols_overview` SHALL use the owning adapter's document-symbol tree for
one authorized source file, SHALL default `max_depth` to 0, and SHALL support an
explicit bounded descendant depth. Its single file group SHALL contain
`symbols`; each overview node SHALL contain only `name`, lowercase `kind`, and
`children` when non-empty. It MUST NOT return node ranges, selection ranges,
detail, name paths, hashes, adapter/generation data, or boolean child flags.

At depth 0 the default SHALL retain every supported root symbol kind. At an
explicit depth greater than 0, descendant `variable` and `constant` nodes SHALL
be suppressed unless `include_kinds` explicitly requests those kinds. Optional
`include_kinds` and `exclude_kinds` SHALL accept stable lowercase kind strings
and SHALL otherwise narrow the result. Filtering SHALL be post-order: a
non-matching ancestor MAY remain only when required to retain a matching
descendant's structural path, and `exclude_kinds` SHALL win for a node named in
both filters. Nodes outside selected depth, nodes removed by explicit/default
kind selection, and structural ancestors removed as a consequence MUST NOT
increment `omitted`; nodes or subtrees removed by the final answer budget and
any upstream semantic cap SHALL increment it.

#### Scenario: MJS file overview is requested
- **WHEN** the client requests an overview of `external/codexUI/scripts/generate-pwa-icons.mjs`
- **THEN** the TypeScript adapter returns the file's functions and requested descendants as compact name/kind/children nodes without scanning unrelated files

#### Scenario: Default overview is requested
- **WHEN** neither depth nor kind filter is supplied
- **THEN** every root symbol is returned, no descendants are returned, and `omitted` does not claim that caller-unrequested descendants were truncated

#### Scenario: Caller requests structural descendants
- **WHEN** `max_depth` is greater than 0 and no kind filter is supplied
- **THEN** structural descendants remain visible while descendant variables/constants are omitted as default selection rather than truncation

#### Scenario: Caller explicitly requests descendant variables
- **WHEN** `include_kinds` contains `variable` or `constant` with sufficient `max_depth`
- **THEN** matching descendants remain reachable through the minimum ancestor path and explicit/default filtering does not inflate `omitted`

#### Scenario: Kind filter matches only nested descendants
- **WHEN** a requested kind occurs below an ancestor whose kind does not match the filter
- **THEN** the matching descendant remains reachable through the minimum ancestor path and unrelated filtered nodes do not contribute to `omitted`

#### Scenario: Result exceeds the answer limit
- **WHEN** the final serialized overview exceeds `max_answer_chars`
- **THEN** trailing nodes or complete remaining subtrees are removed in stable preorder, orphan children and childless nonmatching structural ancestors are never produced, and `omitted` counts exactly the budget-removed nodes plus any upstream semantic cap

### Requirement: Reference results identify containing symbols
`find_referencing_symbols` SHALL request references from the owning adapter with
declaration inclusion disabled and SHALL map each remaining reference to a
containing symbol when one can be determined. Language-specific recovery logic
SHALL remain inside the adapter. Each file group SHALL contain `references`;
each record SHALL contain compact `range` (or `raw_range` plus
`position_basis` for an external target lacking an exact response-owned
snapshot), optional containing `symbol`, and optional `snippet`, while path and
trusted-external/read-only identity appear once on the file group. Snippets
SHALL remain absent by default and SHALL appear only when the caller gives the
existing `max_snippet_chars` a value greater than zero.

A successful result SHALL retain one bounded `data.coverage` object from the
same freshness and configured-program generation used for dispatch. If the
configured program covers the entire trusted supported-language inventory, the
object SHALL contain exactly `complete=true`. Otherwise it SHALL contain
`complete=false`, `uncovered_files` as the total uncovered count, a
deterministically sorted bounded `sample` of uncovered paths and reasons, and
`omitted` as the number not shown. Full configured-program/trust counts,
digests, and projection evidence SHALL remain available in runtime status and
rich scope errors. The service MUST NOT supplement semantic references with
lexical matches or claim that trusted files outside the configured program were
searched.

#### Scenario: Reference occurs inside a function
- **WHEN** a non-declaration reference location falls inside a document symbol
- **THEN** one compact reference record includes the containing symbol and compact range, while file identity and coverage are not repeated

#### Scenario: Caller opts into a snippet
- **WHEN** `max_snippet_chars` is greater than zero and a bounded snippet can be mapped to the exact snapshot
- **THEN** the compact reference record includes that snippet without repeating path or runtime authority

#### Scenario: Reference has no containing symbol
- **WHEN** a valid non-declaration reference occurs at module scope or cannot be mapped safely
- **THEN** the record uses the typed file-level container rather than being discarded

#### Scenario: Declaration is returned by the language server
- **WHEN** the underlying server would include the queried declaration in a reference response
- **THEN** Serena Light excludes it and leaves declaration retrieval to `find_declaration`

#### Scenario: Configured program covers every trusted language file
- **WHEN** a semantic reference query completes with full configured-program coverage
- **THEN** the single coverage object is exactly `{"complete":true}`

#### Scenario: Configured program excludes trusted tests
- **WHEN** a semantic reference query completes with no references and the native project excludes trusted supported-language test files
- **THEN** `files=[]`, `omitted=0`, and the single incomplete coverage object reports the total, bounded sample, and omitted count without implying repository-wide absence

#### Scenario: Reference adapter is not semantically ready
- **WHEN** the owning adapter is cold, incompatible, timed out, in cooldown, or lacks reference capability
- **THEN** the tool returns its rich typed failure rather than an empty reference success or lexical fallback

#### Scenario: Coverage sample exceeds its bound
- **WHEN** the trusted supported-language inventory contains more uncovered paths than the fixed coverage sample limit
- **THEN** coverage retains the deterministic prefix and accurate total/omitted counts without repeating full projection digests or counts
