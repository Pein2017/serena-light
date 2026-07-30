## MODIFIED Requirements

### Requirement: Navigation tools return stable typed JSON
The system SHALL expose `get_symbols_overview`, `find_symbol`,
`find_referencing_symbols`, `find_declaration`, and `find_implementations` with
stable JSON success and error envelopes. Results SHALL include workspace,
adapter, and generation metadata.
Every global and document result range SHALL be derived from the exact verified
document snapshot through the shared `PositionMapper`. Public `line` and
`column` values SHALL be respectively a 0-based decoded-text line and a 0-based
Unicode code-point column within that line, independent of the adapter's LSP
position encoding. Existing `text_offset` values SHALL be decoded-text offsets
and existing `byte_offset` values SHALL be physical UTF-8 file offsets in that
same snapshot, including any BOM. Individual tools MUST NOT apply their own
base adjustment. A location that cannot be mapped to an authorized verified
snapshot MUST be explicitly marked with one named raw coordinate basis or
returned as a typed failure; it MUST NOT mix coordinate bases.

#### Scenario: Navigation succeeds
- **WHEN** a supported semantic query completes
- **THEN** the response has `ok=true`, typed result data, the workspace and adapter generation used, and 0-based decoded-text public positions

#### Scenario: Navigation cannot run
- **WHEN** a request is invalid, untrusted, out of workspace, unsupported, not ready, busy, timed out, or in cooldown
- **THEN** the response has `ok=false` and a stable error code rather than a successful empty payload

#### Scenario: UTF-16 adapter reports a position after an astral character
- **WHEN** a semantic result follows a non-BMP character in a UTF-8 file with CRLF line endings and an optional BOM
- **THEN** its public column counts decoded Unicode code points while its text and byte offsets identify the same source boundary in the exact snapshot

#### Scenario: A raw reference cannot be mapped
- **WHEN** the service lacks an authorized verified snapshot for an adapter-reported location
- **THEN** it returns an explicitly named raw coordinate basis or a typed failure and never combines an adjusted line with an unadjusted LSP character

#### Scenario: A semantic target changes after the first response
- **WHEN** a declaration, implementation, or reference target changes before Serena Light can bind the target snapshot to the reporting adapter
- **THEN** the service performs at most one bounded replay against the bound target snapshots and returns retryable `NOT_READY` unless the location set and owning generations remain stable

#### Scenario: Semantic replay crosses a language-server process or capability identity
- **WHEN** the adapter runtime token, raw or derived capabilities, position encoding, or owning generations change before either response, target binding, or final rendering
- **THEN** the service returns retryable `NOT_READY` and never reuses the source or target snapshot across the replacement process

#### Scenario: Semantic target set exceeds the response-owned bound
- **WHEN** the first response contains more than 64 unique workspace and external target URIs
- **THEN** the service returns non-retryable `UNSUPPORTED` with bounded deterministic target evidence before reading or opening any target snapshot

#### Scenario: A read-only external target cannot be replay-bound
- **WHEN** an allowlisted external definition lacks document-owned snapshot evidence for the semantic response
- **THEN** the result exposes only an explicitly named raw LSP coordinate basis without body or info, or returns a typed failure

### Requirement: Path-scoped symbol lookup preserves name paths
`find_symbol` SHALL match Serena-style symbol name paths within an explicitly
selected file or directory, SHALL support optional body or hover-like info, and
SHALL reject an ambiguous single-symbol operation. A trusted file omitted by
native project configuration MAY be served path-scoped only when the adapter's
engine owns an inferred or transient project for that file; this MUST NOT imply
configured-program global readiness.
Directory scope SHALL be bounded by the current lexical inventory. Global
`include_body` and `include_info` SHALL be populated only from candidate
documents revalidated in the same snapshot; unsupported parameter combinations
SHALL return a typed failure and MUST NOT be silently ignored.
When a language server reports an identifier-only or identifier-start range
that omits declaration syntax for a variable or constant, the owning language
adapter SHALL recover a unique complete
assignment-statement range from the same verified snapshot before advertising
it as a complete body. The server's identifier range SHALL remain the selection
range. Recovery MUST be syntax-aware, language-specific, and fail without a
successful incomplete body when no unique enclosing assignment exists.

#### Scenario: Function body is requested
- **WHEN** `find_symbol` selects one exact function with `include_body=true`
- **THEN** the response includes its complete language-server-normalized body range, content, and current whole-file hash

#### Scenario: Python module constant body is requested
- **WHEN** Pyright reports only the identifier range of a uniquely enclosing module-level assignment and the caller sets `include_body=true`
- **THEN** the response body and symbol range cover the complete assignment statement while the selection range remains on the identifier

#### Scenario: Python assignment is in module-executed control flow
- **WHEN** Pyright reports an identifier-only root symbol for a unique assignment inside a module-level `if`, `try`, `with`, loop, or `match` suite
- **THEN** recovery returns that complete assignment while excluding assignments owned by nested function or class scopes

#### Scenario: TypeScript or JavaScript variable body is requested
- **WHEN** the pinned server reports a top-level variable range that omits `export`/`declare` or `const`/`let`/`var` and the caller sets `include_body=true`
- **THEN** server-owned syntax ancestry recovers the complete variable statement, including an existing terminal semicolon, while the selection range remains on the requested binding

#### Scenario: Recovered assignment is edited
- **WHEN** guarded editing selects a recoverable Python or TypeScript/JavaScript assignment
- **THEN** it uses the same complete recovered range and fail-closed filter as body lookup, so declaration syntax cannot be duplicated or partially retained

#### Scenario: Assignment recovery is ambiguous
- **WHEN** an identifier-only variable range cannot be mapped to one unique supported assignment statement in the exact snapshot
- **THEN** `include_body=true` returns a typed failure and does not label the identifier text as a complete body

#### Scenario: Assignment name and selection disagree
- **WHEN** a server symbol name matches an assignment but its selection range is not contained by that assignment target
- **THEN** recovery fails closed and neither body lookup nor guarded editing may use the name-only candidate

#### Scenario: Pattern matches multiple edit candidates
- **WHEN** a caller needs one symbol but the name path matches more than one
- **THEN** the result identifies the ambiguity and does not choose one implicitly

### Requirement: Reference results identify containing symbols
`find_referencing_symbols` SHALL request references from the owning adapter and
map each reference to a containing symbol and bounded code snippet when one can
be determined. Language-specific recovery logic SHALL remain inside the
adapter. A successful result SHALL include one bounded `coverage` object from
the same freshness and configured-program generation used for dispatch. The
object SHALL include `adapter`, `language`, `scope_kind`,
`configured_program_files`, `configured_program_digest`,
`trusted_language_files`, `trusted_language_digest`, `uncovered_files`, and a
deterministically sorted `uncovered_sample` with `total`, `items`, `digest`, and
`omitted`. The `digest` SHALL identify the full sorted uncovered-path set, not
only the returned sample. The service MUST NOT supplement semantic references with lexical
matches or claim that trusted files outside the configured program were
searched.

#### Scenario: Reference occurs inside a function
- **WHEN** a reference location falls inside a document symbol
- **THEN** the result includes the containing symbol name path, location, a bounded source snippet, and one query-level coverage object

#### Scenario: Reference has no containing symbol
- **WHEN** a valid reference occurs at module scope or cannot be mapped safely
- **THEN** the reference is returned under a typed file-level container rather than discarded

#### Scenario: Configured program excludes trusted tests
- **WHEN** a semantic reference query completes with no references and the native project excludes trusted supported-language test files
- **THEN** the empty result remains successful but coverage reports the excluded files as uncovered and does not imply repository-wide absence

#### Scenario: Reference adapter is not semantically ready
- **WHEN** the owning adapter is cold, incompatible, timed out, in cooldown, or lacks reference capability
- **THEN** the tool returns its typed failure rather than an empty reference success or lexical fallback

#### Scenario: Coverage sample exceeds its bound
- **WHEN** the trusted supported-language inventory contains more uncovered paths than the fixed coverage sample limit
- **THEN** coverage returns a deterministic prefix plus accurate `total` and `omitted` counts and a digest for the full uncovered set
