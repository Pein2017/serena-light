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
candidate set. Structured symbol candidates SHALL keep only their qualified
name path, stable kind, compact range, and relative path when one is needed to
identify the file; they MUST NOT repeat the unqualified name, source body,
detail, selection range, text offset, or byte offset. Operational `NOT_READY`,
`BUSY`, `COOLDOWN`, `TIMED_OUT`,
`SCOPE_INCOMPATIBLE`, and `UNCERTAIN` failures SHALL retain the rich adapter,
generation, phase, retry, and diagnostic metadata required to recover.
`SCOPE_INCOMPATIBLE` SHALL always be non-retryable and, whenever it is backed
by a scope projection, SHALL include the language family, project kind when
known, the selected native config path when present, and bounded
`configured_program_outside_trust` items (path, reason, total, digest,
omitted_count) reused from that projection; it MUST NOT include an engine name,
version, executable, or interpreter field, since `status`/`get_runtime_status`
remains the sole owner of engine/interpreter identity. When no projection backs
the failure, it SHALL keep its existing concise reason and bounded paths
without fabricating those fields.

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

#### Scenario: Symbol ambiguity exposes correction candidates
- **WHEN** more than one current symbol matches a query
- **THEN** each structured candidate keeps its qualified name path, stable kind, and compact range without duplicate names, body, detail, selection range, or internal offsets

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

#### Scenario: Scope-incompatible failure carries actionable projection evidence
- **WHEN** a bound query fails `SCOPE_INCOMPATIBLE` because a native configured program contains paths outside trust
- **THEN** the error details contain the language family, project kind, the selected native config path when present, and bounded `configured_program_outside_trust` items with path, reason, total, digest, and omitted_count, with no retry metadata and no engine or interpreter field

#### Scenario: Scope-incompatible failure without a backing projection stays concise
- **WHEN** `SCOPE_INCOMPATIBLE` is returned for a family with no trusted source paths, a failed adapter construction, or a multi-family unavailable directory/global scope
- **THEN** the error details keep the existing concise reason and bounded paths and do not invent a language, project kind, or selected config value
