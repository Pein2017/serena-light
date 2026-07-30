## MODIFIED Requirements

### Requirement: Diagnostic severity and grouping are bounded
Diagnostics SHALL default to LSP severities Error and Warning, SHALL allow a
caller-supplied severity threshold, and SHALL group findings by containing
symbol when possible or under `<file>` otherwise. Results SHALL obey an answer
size bound. Diagnostic and symbol ranges SHALL be converted through the
adapter's negotiated position encoding against the exact current file snapshot.
Public diagnostic `line` and `column` values SHALL be respectively a 0-based
decoded-text line and a 0-based Unicode code-point column. Any existing text
and byte offsets SHALL identify the same decoded and physical UTF-8 boundaries
in that snapshot. Diagnostics SHALL retain their current generation, engine,
interpreter, and authority metadata.

#### Scenario: Hint-only diagnostics are published
- **WHEN** the adapter publishes only Information or Hint findings and the caller uses the default threshold
- **THEN** the response is `clean` for the default Error/Warning view while retaining generation metadata

#### Scenario: File-level parse error is published
- **WHEN** a diagnostic cannot be mapped to a symbol
- **THEN** it is returned under the `<file>` group

#### Scenario: Non-BMP text precedes a diagnostic
- **WHEN** a UTF-16-reporting adapter publishes a range after an astral Unicode character in a CRLF file
- **THEN** the public column counts decoded Unicode code points and grouping, snippets, text offsets, and byte offsets identify the same exact finding without a base or encoding shift

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
- **THEN** Serena Light drains the marker before retaining the diagnostics owner, preserves the marker and returns a retryable typed failure if the barrier fails, and never converts a close-empty publication into current `CLEAN`

#### Scenario: Language-server transport restarts before diagnostics
- **WHEN** the process that owned an open document and its cached diagnostics exits before the next diagnostics request
- **THEN** the replacement process receives a fresh `didOpen`, old document/publication ownership is discarded, and only a publication correlated to the reopened target can become clean or findings

#### Scenario: Publication wins the timeout cancellation race
- **WHEN** a matching diagnostics publication is accepted between the caller's last sample and cancellation of an unanswered owner
- **THEN** the accepted publication is re-sampled and returned rather than being misreported as timed out

#### Scenario: Result exceeds the bound
- **WHEN** diagnostics exceed the maximum serialized size
- **THEN** the response is truncated deterministically and reports the omitted count
