# Semantic Navigation Specification

## Purpose

Define the agent-facing semantic query tools, compact typed success envelopes,
rich typed errors, exact position semantics, capability gates, and cross-root
read-only navigation behavior.
## Requirements
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
- **WHEN** the complete result would fit an internal compact fragment but the final MCP text would exceed `max_answer_chars`
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
- **WHEN** a ready supported semantic query finds no result and no result was removed by a limit
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

### Requirement: Path-scoped symbol lookup preserves name paths
`find_symbol` SHALL match Serena-style symbol name paths within an explicitly
selected file or directory, SHALL support optional body or hover-like info, and
SHALL reject an ambiguous single-symbol operation. A trusted file omitted by
native project configuration MAY be served path-scoped only when the adapter's
engine owns an inferred or transient project for that file; this MUST NOT imply
configured-program global readiness. Directory scope SHALL be bounded by the
current lexical inventory. Global `include_body` and `include_info` SHALL be
populated only from candidate documents revalidated in the same snapshot;
unsupported parameter combinations SHALL return a typed failure and MUST NOT
be silently ignored.

When a language server reports an identifier-only range for a variable or
constant, the owning language adapter SHALL recover a unique complete
assignment-statement range from the same verified snapshot before advertising
it as a complete body. The server's identifier range SHALL remain an internal
selection anchor but SHALL not be repeated in compact success. Recovery MUST be
syntax-aware, language-specific, and fail without a successful incomplete body
when no unique enclosing assignment exists.

Each selected file group SHALL contain `symbols`; every record SHALL contain
only `name_path`, lowercase `kind`, compact `range`, and requested `body` or
`info`. Query inputs and candidate-program metadata MUST NOT be echoed.

#### Scenario: Function body is requested
- **WHEN** `find_symbol` selects one exact function with `include_body=true`
- **THEN** the response includes its complete body and compact body range, with the current whole-file hash once on the file group

#### Scenario: Python module constant body is requested
- **WHEN** the repaired Pyright adapter recovers a uniquely enclosing module-level assignment and the caller sets `include_body=true`
- **THEN** one compact symbol record contains the complete assignment body/range and the file group contains one current hash

#### Scenario: Python assignment is in module-executed control flow
- **WHEN** Pyright reports an identifier-only root symbol for a unique assignment inside a module-level `if`, `try`, `with`, loop, or `match` suite
- **THEN** recovery returns that complete assignment while excluding assignments owned by nested function or class scopes

#### Scenario: TypeScript or JavaScript variable body is requested
- **WHEN** the pinned server reports a top-level variable range that omits `export`/`declare` or `const`/`let`/`var` and the caller sets `include_body=true`
- **THEN** server-owned syntax ancestry recovers the complete variable statement, including an existing terminal semicolon, while the internal selection anchor remains on the requested binding

#### Scenario: Recovered assignment is edited
- **WHEN** guarded editing selects a recoverable Python or TypeScript/JavaScript assignment
- **THEN** it uses the same complete recovered range and fail-closed filter as body lookup, so declaration syntax cannot be duplicated or partially retained

#### Scenario: Assignment recovery is ambiguous
- **WHEN** an identifier-only variable range cannot be mapped to one unique supported assignment statement in the exact snapshot
- **THEN** `include_body=true` returns the repaired typed failure and does not label identifier text as a complete body

#### Scenario: Assignment name and selection disagree
- **WHEN** a server symbol name matches an assignment but its selection range is not contained by that assignment target
- **THEN** recovery fails closed and neither body lookup nor guarded editing may use the name-only candidate

#### Scenario: Pattern matches multiple edit candidates
- **WHEN** a caller needs one symbol but the name path matches more than one
- **THEN** the result identifies the ambiguity and does not choose one implicitly

### Requirement: Global symbol lookup uses workspace-symbol candidates
Global `find_symbol` SHALL query `workspace/symbol` using the final name-path
segment, filter exact names by default, request document symbols only for
candidate files, and rebuild or verify complete name paths. It MUST NOT fall
back to a per-file full-workspace document-symbol walk. Its semantic scope
SHALL be the native configured program reported by runtime status, not the full
Git trust inventory, and that operational projection MUST NOT be repeated in a
successful symbol response.

`find_symbol` SHALL expose `max_matches` for file, directory, and global scopes,
default it to 20, accept values from 1 through 100, and apply it after exact or
substring filtering, deterministic deduplication, and sorting but before file
grouping and answer-budget pruning. Any adapter candidate fan-out bound SHALL
remain internal. Results omitted by `max_matches` and by the final answer budget
SHALL both contribute to the single `data.omitted` count.

#### Scenario: Exact transformers class is requested
- **WHEN** the client binds the trusted transformers package workspace and globally searches for `Qwen2VLForConditionalGeneration`
- **THEN** Pyright candidates are exact-name filtered and the verified class is returned in one compact file group

#### Scenario: Fuzzy workspace results contain unrelated names
- **WHEN** Pyright returns variables whose names merely contain the requested text
- **THEN** default exact matching removes them before limiting and grouping the final result

#### Scenario: Substring matching exceeds the default match limit
- **WHEN** `substring_matching=true` produces more than 20 deduplicated matches and the caller does not override `max_matches`
- **THEN** the first 20 in stable order are eligible for the answer budget and `omitted` includes all remaining matches

#### Scenario: Public match limit is invalid
- **WHEN** `max_matches` is outside 1 through 100
- **THEN** the tool returns `INVALID_INPUT` before semantic dispatch

#### Scenario: Workspace-symbol recall gate fails
- **WHEN** an adapter cannot reliably recall acceptance symbols through `workspace/symbol`
- **THEN** global lookup for that adapter is disabled with `UNSUPPORTED` rather than using an O(files) fallback

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

### Requirement: Definition and implementation tools preserve Serena semantics
The system SHALL expose the stable Serena-compatible `find_declaration` name as
definition resolution through `textDocument/definition`. It SHALL gate that tool
on `definitionProvider`, SHALL gate `find_implementations` on
`implementationProvider`, and SHALL report both raw providers and derived tool
availability through runtime status or rich failures before dispatch.
`find_declaration` SHALL locate the source occurrence from `relative_path` and a
Python MULTILINE/DOTALL regex containing exactly one capture group, optionally
restricted to one containing symbol body.

Successful declaration and implementation responses SHALL group results by
file under `targets`. Each target SHALL contain compact `range` (or `raw_range`
plus `position_basis` for an external target lacking an exact response-owned
snapshot) and only the available `name_path`, lowercase `kind`, requested
`body`, or requested `info`; file path, optional language/external identity,
and body hash SHALL not repeat per target. Both tools SHALL obey the common
client-visible answer budget.

When declaration info or implementation info/kind filtering is requested for a
verified workspace target, the runtime SHALL obtain target document-symbol
evidence inside the same response-owned snapshot/generation transaction.
`include_info` SHALL retain available bounded semantic detail without exposing
selection ranges or raw server payloads. An implementation kind filter SHALL
use only verified target kind evidence; a target of unknown kind SHALL fail a
positive include predicate, SHALL survive an exclude predicate, and every
filter removal SHALL contribute to `omitted` together with budget removals.

#### Scenario: Python definition is requested
- **WHEN** Pyright advertises definition support and the selected occurrence has a definition
- **THEN** `find_declaration` dispatches `textDocument/definition` and returns its normalized target in a compact file group

#### Scenario: Python implementation is requested
- **WHEN** Pyright does not advertise implementation support
- **THEN** `find_implementations` returns `UNSUPPORTED` and the Pyright capability matrix in the rich error

#### Scenario: TypeScript implementation is requested
- **WHEN** the TypeScript server advertises implementation support
- **THEN** `find_implementations` dispatches the protocol request and returns compact grouped targets

#### Scenario: Implementation kind filter is requested
- **WHEN** a verified target document symbol supplies its kind and the caller provides integer include or exclude kinds
- **THEN** the filter uses that response-owned kind and `omitted` counts every filtered target plus later budget removal

#### Scenario: Target semantic info is requested
- **WHEN** a verified target document symbol supplies bounded detail and the caller sets `include_info=true`
- **THEN** the compact target retains that semantic detail without selection ranges or raw server data

#### Scenario: TypeScript definition is requested
- **WHEN** the TypeScript server advertises definition support but not declaration support
- **THEN** `find_declaration` dispatches `textDocument/definition` and returns compact grouped targets rather than `UNSUPPORTED`

#### Scenario: Supported definition has no target
- **WHEN** the active adapter supports definitions but resolves no location for the selected occurrence
- **THEN** `find_declaration` returns `SYMBOL_NOT_FOUND` rather than `UNSUPPORTED` or an ambiguous empty success

#### Scenario: Declaration locator is invalid
- **WHEN** the regex has zero or multiple capture groups
- **THEN** `find_declaration` returns `INVALID_INPUT` before dispatching an LSP request and identifies the regex capture-count reason

#### Scenario: Agent discovers the declaration locator contract
- **WHEN** a client lists the public tools
- **THEN** every tool has a non-empty agent-facing description and the `find_declaration.regex` schema states that exactly one capture group selects the queried symbol

#### Scenario: Language server returns an ordinary protocol error
- **WHEN** a semantic read receives an LSP response/protocol failure or an exhausted transport/process loss, including during cold global warm-up
- **THEN** the service boundary returns a bounded non-leaking `UNSUPPORTED` envelope; the same failure during guarded editing returns `UNCERTAIN` and the edit is never replayed

#### Scenario: Declaration locator is ambiguous
- **WHEN** the capture group matches more than once in the selected file or containing symbol body
- **THEN** `find_declaration` returns `AMBIGUOUS_SYMBOL` without choosing an occurrence

### Requirement: External Python definitions remain navigable and read-only
The Pyright adapter SHALL use the conda `ms` interpreter through the
`workspace/configuration` protocol and SHALL return trusted definitions inside
the pinned environment's standard-library and site-packages trees. Compact
public identity SHALL be the authoritative absolute path plus `read_only=true`;
the internal external-root classification MUST NOT create a second public trust
identifier.

#### Scenario: ms-swift imports transformers
- **WHEN** a caller requests the definition of `GenerationConfig` from an `ms-swift` source file
- **THEN** `find_declaration` points into the installed transformers package selected by the `ms` interpreter and marks its file group read-only

#### Scenario: External definition becomes an edit input
- **WHEN** a caller passes the returned transformers path to an editing tool
- **THEN** navigation remains allowed but editing returns `READ_ONLY_ROOT`

#### Scenario: ms-swift imports another installed package
- **WHEN** `find_declaration` resolves a source occurrence into another package under the pinned `ms` environment
- **THEN** the external path remains visible with read-only metadata and cannot expand the active workspace inventory

### Requirement: Multi-adapter global results retain language ownership
When a global workspace query covers both supported language families, the
system SHALL fan out to the required ready adapters, merge bounded results, and
retain language identity once on each file group. It MUST NOT repeat adapter
phase/generation metadata in successful records and MUST NOT infer references,
declarations, or implementations across adapter boundaries. Scope compatibility
SHALL be tracked per language family. An incompatible family SHALL return
`SCOPE_INCOMPATIBLE` for its operations without preventing a healthy family in
the same workspace from serving its operations. Detailed adapter identity and
phase SHALL remain available through runtime status and rich failures.

#### Scenario: Python and TypeScript define the same name
- **WHEN** both adapters return an exact global symbol with the same name
- **THEN** both results are returned in distinct file groups with one language identifier per group and no per-symbol adapter metadata

#### Scenario: Caller requests cross-language references
- **WHEN** a symbol name exists in both languages but no language server owns a cross-language edge
- **THEN** the system returns only references reported by the selected symbol's adapter

### Requirement: Compact navigation demonstrates material payload reduction
Acceptance SHALL capture correctness-repaired, pre-compaction client-visible
MCP text for fixed exact-symbol, body, global-symbol, large-overview, and
multi-file-reference fixtures. With identical semantic results and answer
bounds, compact exact-symbol success without body SHALL be no more than 50% of
its baseline character count, global-symbol and multi-file-reference success
SHALL each be no more than 40%, and large-overview success SHALL be no more than
25%. For a body query, the serialized characters outside the unchanged body
string SHALL be no more than 50% of baseline body-external characters.
Character counts SHALL use the actual connector-observed
`CallToolResult.content[0].text`; model-token counts, calls, and wall time SHALL
also be reported but SHALL not replace the deterministic character gate.

#### Scenario: Compact fixtures are accepted
- **WHEN** the five fixed fixtures return the same semantic entities, bodies, ranges, hashes, and reference coverage as the correctness-repaired baseline
- **THEN** every deterministic character-ratio gate passes and the report includes client-visible tokens, call counts, and wall time

#### Scenario: A smaller response loses semantic evidence
- **WHEN** a payload meets its character ratio only by dropping a result within the same public limits, truncating body text, changing range semantics, or omitting reference coverage
- **THEN** acceptance fails regardless of the measured token reduction

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
