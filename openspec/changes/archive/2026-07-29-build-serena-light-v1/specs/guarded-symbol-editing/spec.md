## ADDED Requirements

### Requirement: Symbol-body replacement is the only v1 editing operation
The system SHALL expose `replace_symbol_body` for complete, unambiguous symbols
and SHALL NOT expose line insertion, line deletion, rename, formatting, code
actions, or other editing tools in v1.

#### Scenario: Client enumerates tools
- **WHEN** an MCP client lists serena-light tools
- **THEN** `replace_symbol_body` is absent while the explicit editing-containment
  gate is active and is restored after the guarded-edit reacceptance gate;
  excluded editing operations remain absent even if a later independent-audit
  HOLD still blocks release

#### Scenario: Stale client invokes the tool while containment is active
- **WHEN** a client negotiated the old declaration before repair containment and
  invokes it while the repair gate still withholds editing
- **THEN** the call returns `UNSUPPORTED` with reason
  `temporarily_disabled_pending_reacceptance` and writes nothing

### Requirement: Editing requires an authorized Git workspace
Before resolving or writing content, `replace_symbol_body` SHALL verify that the
resolved target is inside the bound Git workspace below `/data`, is not a
symlink escape, is present in the current Git trust inventory, and is not a
read-only external root. Edit authorization SHALL NOT depend on membership in
the native configured semantic program; if the engine cannot safely resolve a
trusted target path, the edit SHALL fail before writing.
Authorization SHALL use lexical inventory membership and SHALL walk the target
under the workspace lock with directory file descriptors, `lstat`, and
`O_NOFOLLOW`; it MUST NOT authorize an inventory path through its later symlink
resolution.

#### Scenario: Target is in the bound source repository
- **WHEN** the resolved file is inside the active editable Git workspace
- **THEN** edit processing may continue to symbol and hash validation

#### Scenario: Target is in transformers
- **WHEN** the resolved file is in the trusted transformers query root
- **THEN** the tool returns `READ_ONLY_ROOT` before creating a temporary file

#### Scenario: Target belongs to another bound workspace
- **WHEN** a session bound to workspace A submits a relative path that resolves only in workspace B
- **THEN** the edit is rejected as outside the active edit root

#### Scenario: Tracked path is substituted by an in-root symlink
- **WHEN** an inventoried source path is replaced after activation by a symlink
  to an ignored file under the same Git root
- **THEN** editing fails before opening a temporary file and the ignored target
  remains unchanged

### Requirement: Editing requires one current symbol and expected hash
The tool SHALL re-resolve exactly one symbol under the workspace lock, SHALL
read the current file, and SHALL compare its whole-file SHA-256 with the
required `expected_hash` before writing. It SHALL convert the symbol's LSP range
through the negotiated position encoding against that exact raw file snapshot.

#### Scenario: Hash matches the current file
- **WHEN** one symbol is resolved and `expected_hash` equals the current whole-file hash
- **THEN** the tool may construct the replacement

#### Scenario: File changed after retrieval
- **WHEN** the current whole-file hash differs from `expected_hash`
- **THEN** the tool returns `STALE_HASH` with the current hash and writes nothing

#### Scenario: Symbol location changed ambiguously
- **WHEN** re-resolution finds zero or multiple matching symbols
- **THEN** the tool returns a typed missing or ambiguous error and writes nothing

#### Scenario: Non-BMP text precedes the symbol
- **WHEN** the adapter reports UTF-16 positions and an astral Unicode character occurs before the selected symbol
- **THEN** the converted replacement range selects exactly the intended symbol body

### Requirement: File replacement is atomic and preserves file contract
The tool SHALL write the new complete file to a temporary file in the target
directory, preserve the original file mode and text encoding/newline contract,
including BOM and CRLF/LF form, flush the content, and install it with an atomic
`os.replace`.

#### Scenario: Process fails before atomic replace
- **WHEN** the process fails while writing or flushing the temporary file
- **THEN** the original target file remains unchanged

#### Scenario: Atomic replace succeeds
- **WHEN** the prepared file is atomically installed
- **THEN** readers observe either the complete old file or the complete new file and never a partial file

#### Scenario: UTF-8 BOM and CRLF are present
- **WHEN** a symbol body is replaced in a file containing a UTF-8 BOM and CRLF newlines
- **THEN** the BOM, untouched bytes, newline form, encoding, and mode remain unchanged

### Requirement: Successful edits report old and new state
After atomic replacement, the tool SHALL notify the owning language adapter and
SHALL return the old hash, new hash, resolved symbol identity, file generation,
and notification state. It SHALL NOT run diagnostics automatically.

#### Scenario: Replacement and notification succeed
- **WHEN** the file is replaced and the language server accepts the change notification
- **THEN** the response reports `ok=true`, both hashes, and the new adapter generation

#### Scenario: Caller wants validation
- **WHEN** an edit succeeds
- **THEN** diagnostics remain a separate explicit tool call against the new generation

### Requirement: Editing is never automatically replayed
The connector and daemon MUST NOT retry `replace_symbol_body` after a language
server crash, daemon identity change, HTTP or MCP session loss, timeout, or
uncertain response. Generic HTTP retry and resumability middleware MUST be
disabled for editing calls.

Executor state SHALL distinguish `queued`, `running`, `installed`, and `done`.
A proven cancellation while queued returns `TIMED_OUT`; a timeout after start or
with unknown state returns `UNCERTAIN`. Every failure after `os.replace`,
including fsync, notification, or response loss, is `UNCERTAIN` and reports the
current hash when safely observable.

#### Scenario: Adapter fails after file replacement
- **WHEN** the file was atomically replaced but adapter notification fails while a response can still be returned
- **THEN** the tool returns `UNCERTAIN` with the current file hash and does not replay the edit

#### Scenario: Connection breaks after file replacement
- **WHEN** the client loses the response after the file may have changed
- **THEN** recovery requires re-reading the symbol or file hash before issuing another edit

#### Scenario: Connector reconnects after daemon loss
- **WHEN** the connector establishes a new daemon session after an edit response is lost
- **THEN** it restores the binding but does not resend the edit and reports `UNCERTAIN` to the stdio client

#### Scenario: Caller retries with the old hash
- **WHEN** the original edit already changed the file and the caller repeats its old `expected_hash`
- **THEN** the retry returns `STALE_HASH` and cannot apply the body twice

### Requirement: External changes remain conflict-visible
The workspace freshness mechanism SHALL update file generations for changes
made outside serena-light and SHALL cause a previously retrieved hash to fail
before an edit.

#### Scenario: Another agent changes the file
- **WHEN** a second process modifies the target after the first agent retrieves the symbol
- **THEN** the first agent's edit is rejected by `expected_hash` even if the language-server notification is delayed

#### Scenario: File is deleted externally
- **WHEN** the target file is removed before the edit lock validates it
- **THEN** the tool returns a typed missing-file error and does not recreate the file
