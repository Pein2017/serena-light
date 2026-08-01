## MODIFIED Requirements

### Requirement: File diagnostics distinguish findings, clean, timeout, and readiness
`get_diagnostics_for_file` SHALL wait for the requested document generation
using published diagnostics. A current publication SHALL return compact
`ok=true` data containing the active workspace once, one file group with the
requested path and a `diagnostics` array, and one non-negative `omitted` count.
A non-empty array is the findings state and an empty array is the clean state.
Timeout, stale, and not-ready states SHALL be typed `ok=false` operational
errors; the system MUST NOT represent them as empty diagnostics success.

#### Scenario: Current generation has no diagnostics
- **WHEN** the adapter publishes an empty diagnostic set for the requested current generation
- **THEN** the tool returns the requested file with `diagnostics=[]` and `omitted=0` without generation or engine repetition

#### Scenario: Publication contains findings
- **WHEN** the adapter publishes one or more diagnostics for the requested current generation
- **THEN** the tool returns those current findings in the requested file group without a redundant state field

#### Scenario: Publication does not arrive
- **WHEN** the bounded diagnostics wait expires without the requested generation
- **THEN** the tool returns rich typed `TIMED_OUT` and not clean success

#### Scenario: Adapter is still cold
- **WHEN** diagnostics are requested before the target document can be opened
- **THEN** the tool returns rich typed `NOT_READY` with phase and retry metadata

### Requirement: Diagnostic severity and grouping are bounded
Diagnostics SHALL default to LSP severities Error and Warning and SHALL allow a
caller-supplied severity threshold. Each diagnostic record SHALL contain
lowercase `severity`, compact decoded-text `range`, `message`, optional
containing `symbol`, and optional `source` and `code`. A finding that cannot be
mapped to a containing symbol SHALL omit `symbol` rather than create a repeated
group wrapper. File identity SHALL appear once in the containing file group.

Successful diagnostics MUST NOT contain a whole-file hash, diagnostic or
runtime generation, adapter phase, configured-program detail, engine version,
interpreter path, URI, text offset, or byte offset. Diagnostic and symbol ranges
SHALL be converted through the adapter's negotiated position encoding against
the exact current file snapshot, using 0-based decoded-text lines and 0-based
Unicode code-point columns. Freshness and publication correlation remain
admission invariants even though their proof is not repeated in success.

#### Scenario: Hint-only diagnostics are published
- **WHEN** the adapter publishes only Information or Hint findings and the caller uses the default threshold
- **THEN** the response is compact clean success for the requested file without generation metadata

#### Scenario: File-level parse error is published
- **WHEN** a diagnostic cannot be mapped to a symbol
- **THEN** it remains in the file's diagnostics array without a `symbol` field

#### Scenario: Non-BMP text precedes a diagnostic
- **WHEN** a UTF-16-reporting adapter publishes a range after an astral Unicode character in a CRLF file
- **THEN** the compact public range counts decoded Unicode code points against the exact snapshot without exposing redundant offsets

#### Scenario: Unchanged diagnostics follow semantic navigation
- **WHEN** a document is already open on the exact same snapshot and diagnostics are requested one or more times after a symbol query
- **THEN** the adapter reuses the current document generation without sending a same-text `didChange`, retains the asynchronous publication correlation through document-symbol readiness, and never promotes a stale prior generation to clean

#### Scenario: Freshness changes an open diagnostics target
- **WHEN** freshness sends full changed text for an open document before the next diagnostics request
- **THEN** that request reuses the freshness-owned changed generation without sending a duplicate document notification, and an older publication remains stale until the new generation publishes

#### Scenario: Versioned publication arrives during changed-document notification
- **WHEN** an engine declares versioned diagnostic publications and a matching versioned publication arrives while Serena Light is sending `didChange` for a new document generation
- **THEN** the new target already owns publication correlation, the publication is retained for that generation, Serena Light does not reinstall a consumed owner after notification returns, and a publication without integer version evidence is rejected
- **AND WHEN** sending the notification fails
- **THEN** Serena Light removes only the matching undelivered target and does not leave a phantom or delete a newer owner

#### Scenario: Unversioned engine changes an open diagnostics target
- **WHEN** an engine declares that diagnostic publications contain no document version and the exact text changes for an open document
- **THEN** Serena Light forgets the old publication owner before `didClose`, waits for a bounded same-connection request/response barrier that consumes any synchronous or delayed close publication without an owner, and only then installs the new owner before `didOpen` sends the exact full text
- **AND WHEN** the barrier times out or the close, barrier, or reopen transport fails
- **THEN** Serena Light leaves no phantom target or reopen for an undelivered generation, and a later retry cannot treat an old or close-generated publication as current

#### Scenario: Watcher retry recreates a locally open unversioned document
- **WHEN** a prior watcher reconciliation times out while draining another URI, the requested URI remains locally open, and a later watcher batch reports that URI as created
- **THEN** Serena Light does not send a temporary `didOpen`/`didClose` pair for the already-owned URI and does not create an undrained-close marker that overlaps local-open ownership
- **AND WHEN** diagnostics reuse an unchanged cached snapshot while any current-process close marker exists
- **THEN** Serena Light drains the marker before retaining the diagnostics owner, preserves the marker and returns a retryable typed failure if the barrier fails, and never converts a close-empty publication into current clean success

#### Scenario: Language-server transport restarts before diagnostics
- **WHEN** the process that owned an open document and its cached diagnostics exits before the next diagnostics request
- **THEN** the replacement process receives a fresh `didOpen`, old document/publication ownership is discarded, and only a publication correlated to the reopened target can become clean or findings

#### Scenario: Publication wins the timeout cancellation race
- **WHEN** a matching diagnostics publication is accepted between the caller's last sample and cancellation of an unanswered owner
- **THEN** the accepted publication is re-sampled and returned rather than being misreported as timed out

#### Scenario: Result exceeds the bound
- **WHEN** diagnostics exceed the maximum serialized size
- **THEN** whole trailing findings are removed deterministically and `omitted` reports their exact count

### Requirement: TypeScript diagnostics disclose advisory authority
Every successful TypeScript diagnostic file group SHALL contain
`authority=advisory` once and MUST NOT present an LSP finding as a failed
repository-native check. Pinned TypeScript engine version and discoverable
repository-native typecheck/CI authority SHALL remain available in runtime
status and rich operational errors rather than repeating in every successful
finding.

#### Scenario: TypeScript 5.9 and repository TypeScript 7 disagree
- **WHEN** the LSP reports an Error that the repository-native TypeScript 7 check does not report
- **THEN** the compact file group retains `authority=advisory` and does not present the finding as a failed repository check

#### Scenario: Agent needs authoritative validation
- **WHEN** a caller asks status how TypeScript correctness is decided
- **THEN** status identifies the pinned engine and repository-native typecheck command as external authority when one is discoverable

#### Scenario: TypeScript diagnostics fail operationally
- **WHEN** the TypeScript diagnostic call is not ready, times out, or has incompatible scope
- **THEN** its rich typed error retains the engine and repository-authority facts needed to diagnose or validate independently

### Requirement: Python diagnostics disclose interpreter and engine
Successful Python diagnostics SHALL omit Pyright version and interpreter path.
`get_runtime_status` and rich Python diagnostic operational errors SHALL retain
the pinned Pyright version, selected `ms` interpreter, and trusted external
package context used for import resolution.

#### Scenario: Python diagnostic references an external import
- **WHEN** Pyright reports or clears an import diagnostic for transformers
- **THEN** compact success returns the finding or clean state while runtime status remains the owner of interpreter, engine, and trusted-external context

#### Scenario: Python diagnostics fail operationally
- **WHEN** a Python diagnostic cannot run because its engine, interpreter, or external-root setup is not ready
- **THEN** the rich typed error retains the setup facts needed to recover

## ADDED Requirements

### Requirement: Diagnostics obey the client-visible answer budget
Both public diagnostics tools SHALL default `max_answer_chars` to 12,000 and
accept values from 512 through 50,000 inclusive. The bound SHALL apply to the
complete canonical minified JSON string placed in actual MCP
`CallToolResult.content[0].text` after workspace, file group, TypeScript
authority, and `omitted` are present. `structuredContent` SHALL represent the
same JSON value.

Findings SHALL be deterministically ordered before whole trailing records are
removed and the result is reserialized. The service MUST NOT cut a diagnostic
message, range, source, code, symbol, path, or JSON token. If current diagnostic
authority exists but the irreducible clean envelope or first finding cannot fit,
the tool SHALL return bounded typed `INVALID_INPUT` with
`field=max_answer_chars` and truthful `minimum_required_chars` rather than an
oversized or misleading success.

#### Scenario: Existing inner diagnostics budget fits but final envelope does not
- **WHEN** an inner diagnostics value fits the caller's bound but adding outer workspace/file/authority metadata would exceed it
- **THEN** the connector-visible canonical text is pruned and remeasured after all fields are present and never exceeds the bound

#### Scenario: One diagnostic cannot fit
- **WHEN** the first stable diagnostic record makes complete success exceed the bound
- **THEN** the tool returns bounded `INVALID_INPUT` with the minimum characters required for that stable prefix and does not return partial message text

#### Scenario: Clean diagnostics use the minimum bound
- **WHEN** a current clean Python or TypeScript result is requested with `max_answer_chars=512`
- **THEN** the complete connector-visible compact success fits and `structuredContent` represents the same value
