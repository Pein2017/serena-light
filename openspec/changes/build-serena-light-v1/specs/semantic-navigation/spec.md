## ADDED Requirements

### Requirement: Navigation tools return stable typed JSON
The system SHALL expose `get_symbols_overview`, `find_symbol`,
`find_referencing_symbols`, `find_declaration`, and `find_implementations` with
stable JSON success and error envelopes. Results SHALL include workspace,
adapter, and generation metadata.
Every global and document result range SHALL be derived from the exact verified
document snapshot through the shared `PositionMapper`, with consistent LSP,
decoded-text, and byte semantics.

#### Scenario: Navigation succeeds
- **WHEN** a supported semantic query completes
- **THEN** the response has `ok=true`, typed result data, and the workspace and adapter generation used

#### Scenario: Navigation cannot run
- **WHEN** a request is invalid, untrusted, out of workspace, unsupported, not ready, busy, timed out, or in cooldown
- **THEN** the response has `ok=false` and a stable error code rather than a successful empty payload

### Requirement: Symbol overviews are document-scoped
`get_symbols_overview` SHALL use the owning adapter's document-symbol tree for
one authorized source file and SHALL support a bounded descendant depth.

#### Scenario: MJS file overview is requested
- **WHEN** the client requests an overview of `cc-plugin-codex/runtime/args.mjs`
- **THEN** the TypeScript adapter returns the file's functions and requested descendants without scanning unrelated files

#### Scenario: Result exceeds the answer limit
- **WHEN** the serialized overview exceeds its configured result bound
- **THEN** the response reports truncation and enough metadata to request a narrower scope

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

#### Scenario: Function body is requested
- **WHEN** `find_symbol` selects one exact function with `include_body=true`
- **THEN** the response includes its complete language-server-normalized body range, content, and current whole-file hash

#### Scenario: Pattern matches multiple edit candidates
- **WHEN** a caller needs one symbol but the name path matches more than one
- **THEN** the result identifies the ambiguity and does not choose one implicitly

### Requirement: Global symbol lookup uses workspace-symbol candidates
Global `find_symbol` SHALL query `workspace/symbol` using the final name-path
segment, filter exact names by default, request document symbols only for
candidate files, and rebuild or verify complete name paths. It MUST NOT fall
back to a per-file full-workspace document-symbol walk. Its advertised scope
SHALL be the native configured semantic program reported by runtime status, not
the full Git trust inventory.

#### Scenario: Exact transformers class is requested
- **WHEN** the client binds the trusted transformers package workspace and globally searches for `Qwen2VLForConditionalGeneration`
- **THEN** Pyright candidates are exact-name filtered and the verified class location is returned from its candidate file

#### Scenario: Fuzzy workspace results contain unrelated names
- **WHEN** Pyright returns variables whose names merely contain the requested text
- **THEN** default exact matching removes them before the final result

#### Scenario: Substring matching is requested
- **WHEN** the client sets `substring_matching=true`
- **THEN** bounded substring candidates may be returned with truncation metadata

#### Scenario: Workspace-symbol recall gate fails
- **WHEN** an adapter cannot reliably recall acceptance symbols through `workspace/symbol`
- **THEN** global lookup for that adapter is disabled with `UNSUPPORTED` rather than using an O(files) fallback

### Requirement: Reference results identify containing symbols
`find_referencing_symbols` SHALL request references from the owning adapter and
map each reference to a containing symbol and bounded code snippet when one can
be determined. Language-specific recovery logic SHALL remain inside the
adapter.

#### Scenario: Reference occurs inside a function
- **WHEN** a reference location falls inside a document symbol
- **THEN** the result includes the containing symbol name path, location, and a bounded source snippet

#### Scenario: Reference has no containing symbol
- **WHEN** a valid reference occurs at module scope or cannot be mapped safely
- **THEN** the reference is returned under a typed file-level container rather than discarded

### Requirement: Definition and implementation tools preserve Serena semantics
The system SHALL expose the stable Serena-compatible `find_declaration` name as
definition resolution through `textDocument/definition`. It SHALL gate that tool
on `definitionProvider`, SHALL gate `find_implementations` on
`implementationProvider`, and SHALL report both raw providers and derived tool
availability before dispatch. `find_declaration` SHALL locate the source
occurrence from `relative_path` and a Python MULTILINE/DOTALL regex containing
exactly one capture group, optionally restricted to one containing symbol body.

#### Scenario: Python definition is requested
- **WHEN** Pyright advertises definition support and the selected occurrence has a definition
- **THEN** `find_declaration` dispatches `textDocument/definition` and returns its normalized locations

#### Scenario: Python implementation is requested
- **WHEN** Pyright does not advertise implementation support
- **THEN** `find_implementations` returns `UNSUPPORTED` and the Pyright capability matrix

#### Scenario: TypeScript implementation is requested
- **WHEN** the TypeScript server advertises implementation support
- **THEN** `find_implementations` dispatches the protocol request and returns normalized locations

#### Scenario: TypeScript definition is requested
- **WHEN** the TypeScript server advertises definition support but not declaration support
- **THEN** `find_declaration` dispatches `textDocument/definition` and returns normalized locations rather than `UNSUPPORTED`

#### Scenario: Supported definition has no target
- **WHEN** the active adapter supports definitions but resolves no location for the selected occurrence
- **THEN** `find_declaration` returns `SYMBOL_NOT_FOUND` rather than `UNSUPPORTED` or an ambiguous empty success

#### Scenario: Declaration locator is invalid
- **WHEN** the regex has zero or multiple capture groups
- **THEN** `find_declaration` returns `INVALID_INPUT` before dispatching an LSP request

#### Scenario: Declaration locator is ambiguous
- **WHEN** the capture group matches more than once in the selected file or containing symbol body
- **THEN** `find_declaration` returns `AMBIGUOUS_SYMBOL` without choosing an occurrence

### Requirement: External Python definitions remain navigable and read-only
The Pyright adapter SHALL use the conda `ms` interpreter through the
`workspace/configuration` protocol and SHALL return trusted definitions inside
the pinned environment's standard-library and site-packages trees as
`read_only_external` locations.

#### Scenario: ms-swift imports transformers
- **WHEN** a caller requests the definition of `GenerationConfig` from an `ms-swift` source file
- **THEN** `find_declaration` points into the installed transformers package selected by the `ms` interpreter

#### Scenario: External definition becomes an edit input
- **WHEN** a caller passes the returned transformers location to an editing tool
- **THEN** navigation remains allowed but editing returns `READ_ONLY_ROOT`

#### Scenario: ms-swift imports another installed package
- **WHEN** `find_declaration` resolves a source occurrence into another package under the pinned `ms` environment
- **THEN** the external location remains visible with read-only metadata and cannot expand the active workspace inventory

### Requirement: Multi-adapter global results retain language ownership
When a global workspace query covers both supported language families, the
system SHALL fan out to the required ready adapters, merge bounded results, and
retain adapter/language identity. It MUST NOT infer references, declarations,
or implementations across adapter boundaries.
Scope compatibility SHALL be tracked per language family. An incompatible
family SHALL return `SCOPE_INCOMPATIBLE` for its operations without preventing a
healthy family in the same workspace from serving its operations.

#### Scenario: Python and TypeScript define the same name
- **WHEN** both adapters return an exact global symbol with the same name
- **THEN** both results are returned with distinct language, adapter, and file metadata

#### Scenario: Caller requests cross-language references
- **WHEN** a symbol name exists in both languages but no language server owns a cross-language edge
- **THEN** the system returns only references reported by the selected symbol's adapter
