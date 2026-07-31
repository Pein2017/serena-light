## ADDED Requirements

### Requirement: Path discovery enumerates the current trusted file catalog
The system SHALL expose `find_paths` with inputs `relative_path=""`,
`name_glob="*"`, `max_results=100`, and `max_answer_chars=12000`.
`relative_path` SHALL select an authorized directory in the active workspace.
`name_glob` SHALL be a case-sensitive basename glob supporting `*`, `?`, and
bracket classes; it MUST reject path separators, parent traversal, an invalid
bracket expression, or an empty value. `max_results` SHALL accept 1 through
1,000 inclusive, and `max_answer_chars` SHALL accept 512 through 50,000
inclusive.

Success SHALL contain exactly `ok=true` and `data`. `data` SHALL contain the
active physical `workspace` once, a sorted array of root-relative POSIX `paths`,
and one non-negative integer `omitted`. Paths SHALL identify regular files from
the exact current full-file trust catalog; directories, tracked-but-deleted
entries, ignored files, symlinks, and escapes MUST NOT appear. The system SHALL
apply `max_results` before final answer budgeting, remove only trailing whole
paths, and count every removed eligible path in `omitted`.

#### Scenario: Agent enumerates Python tests
- **WHEN** the active Git workspace contains trusted nested tests and the caller
  uses `relative_path="tests"` and `name_glob="test_*.py"`
- **THEN** `find_paths` returns matching regular files in deterministic
  root-relative path order without starting ripgrep or a language adapter

#### Scenario: Configuration and documentation are visible
- **WHEN** tracked or eligible untracked TOML, YAML, JSON, Markdown, and hidden
  configuration files satisfy the requested basename glob
- **THEN** they can appear even though they are absent from the semantic source
  inventory

#### Scenario: Ignored or symlinked path would match
- **WHEN** a matching file is ignored, tracked-but-deleted, a symlink, below a
  symlinked component, or outside the resolved workspace
- **THEN** it is absent and cannot be authorized by the lexical tool

#### Scenario: Transformers scope is omitted
- **WHEN** the active workspace is the explicitly trusted non-Git transformers
  root and `relative_path` is empty
- **THEN** the tool returns `INVALID_INPUT` requiring an explicit relative scope
  and does not walk the whole package

#### Scenario: Path result reaches the public limit
- **WHEN** eligible paths exceed `max_results` or the canonical final MCP text
  would exceed `max_answer_chars`
- **THEN** the tool returns the stable prefix of whole paths and one truthful
  `omitted` total within the requested final-text bound

### Requirement: Text search is closed, compact, and deterministic
The system SHALL expose `search_text` with required `pattern` and optional
`relative_path=""`, `regex=false`, `context_lines=0`, `max_matches=50`,
`max_line_chars=160`, and `max_answer_chars=12000`. Literal mode SHALL be
case-sensitive and interpret the entire non-empty pattern as fixed text. Regex
mode SHALL use only the pinned ripgrep Rust regex engine, including explicit
inline case semantics, and MUST reject invalid syntax, lookaround,
backreferences, embedded PCRE2 requests, NUL, CR, LF, or an empty pattern before
subprocess dispatch.

`context_lines` SHALL accept 0 through 2 inclusive, `max_matches` 1 through 500,
`max_line_chars` 40 through 500, and `max_answer_chars` 512 through 50,000.
Search SHALL cover only current trusted regular files under the authorized
scope whose matched records are valid UTF-8 text rather than binary or
byte-only data.

Success SHALL contain exactly `ok=true` and `data`. `data` SHALL contain the
physical `workspace` once, deterministic file groups in `files`, and one
non-negative integer `omitted`. Each file group SHALL contain root-relative
`path` once and a non-empty `matches` array. Each match SHALL contain a compact
`range` of `[[start_line,start_column],[end_line,end_column]]` using 0-based
decoded-text lines and Unicode code-point columns, plus a decoded `text` clipped
to `max_line_chars`. A clipped prefix SHALL be disclosed by
`text_start_column`; requested context SHALL be a bounded `context` array whose
records contain absolute 0-based `line`, clipped `text`, and
`text_start_column` only when nonzero. Presentation clipping MUST NOT alter the
absolute match range. An adjacent context record that is byte-only or invalid
UTF-8 SHALL be omitted without removing the valid match or changing match-level
`omitted`.

The system SHALL sort matches by path, range, then text; apply `max_matches`;
then remove trailing whole matches and empty groups until the canonical JSON in
the actual MCP `CallToolResult.content[0].text` fits `max_answer_chars`.
`structuredContent` SHALL represent the same JSON value. `omitted` SHALL count
all matches removed by the match limit and final budget. The system MUST NOT cut
a path, range, JSON token, match record, or context record mid-value.
The wrapper SHALL stream every explicit trusted-file batch to completion within
the owned deadline, count every eligible match while retaining only a bounded
stable prefix, and compute exact `omitted` as total eligible matches minus
returned matches. A deadline SHALL return `TIMED_OUT` and MUST NOT return a
partial success with an inexact total.

#### Scenario: Literal discovers a dynamic import spelling
- **WHEN** a trusted Python, JavaScript, configuration, test, or documentation
  file contains the literal import spelling
- **THEN** `search_text` returns the exact file and decoded-text range without
  claiming semantic-reference authority

#### Scenario: Rust regex is requested
- **WHEN** `regex=true` and the pattern is valid for the pinned Rust regex engine
- **THEN** the tool returns deterministic matches without enabling PCRE2 or
  accepting caller-supplied ripgrep flags

#### Scenario: Unsupported regex feature is requested
- **WHEN** a pattern contains lookaround, a backreference, or syntax rejected by
  the pinned Rust regex engine
- **THEN** the tool returns rich typed `INVALID_INPUT` before enqueueing or
  starting ripgrep

#### Scenario: Non-BMP text precedes the match
- **WHEN** a UTF-8 line contains an astral Unicode character before a match
- **THEN** the public columns count decoded Unicode code points and the clipped
  text plus `text_start_column` map to that same line snapshot

#### Scenario: Context is not requested
- **WHEN** `context_lines=0`
- **THEN** match records omit `context` rather than returning empty or repeated
  neighboring-line metadata

#### Scenario: Adjacent context is not valid UTF-8
- **WHEN** a valid UTF-8 match has an adjacent requested context record that
  ripgrep reports only as bytes or that cannot be decoded as UTF-8
- **THEN** that context record is omitted while the match remains valid and the
  match-level `omitted` total is unchanged

#### Scenario: Pattern contains a line break
- **WHEN** a literal or regex pattern contains CR or LF
- **THEN** the tool returns `INVALID_INPUT` before subprocess dispatch so every
  public match remains single-line

#### Scenario: Long line is clipped around the match
- **WHEN** a matching decoded line exceeds `max_line_chars`
- **THEN** the result returns a deterministic clipped window, discloses its
  nonzero `text_start_column` when applicable, and preserves the absolute full
  match range even if the displayed text abbreviates a very long match

#### Scenario: Text result reaches the public limit
- **WHEN** matches exceed `max_matches` or the complete MCP success would exceed
  `max_answer_chars`
- **THEN** only trailing whole matches are removed, `omitted` is truthful, and
  both MCP text and structured content contain the same bounded value

#### Scenario: Match limit omits many later matches
- **WHEN** eligible matches beyond `max_matches` span later explicit-file batches
- **THEN** the wrapper consumes and counts those batches within the deadline and
  reports the exact total removed rather than a one-lookahead lower bound

#### Scenario: No trusted text matches
- **WHEN** the admitted scope is current and no eligible UTF-8 text file matches
- **THEN** the tool returns `files=[]` and `omitted=0` rather than a readiness or
  trust failure disguised as empty success

### Requirement: Lexical results obey trust and call freshness
Both lexical tools SHALL authorize scope and candidates from one exact
full-file catalog generation. They SHALL use the workspace runtime's per-call
freshness admission and final validation. `search_text` SHALL post-filter every
ripgrep path against the frozen catalog token and SHALL return success only when
its files, decoded lines, ranges, and context belong to one validated
transaction. If a relevant create, change, delete, rename, ignore/config change,
or symlink substitution races the operation, the system SHALL discard the
result and replay the complete lexical read at most once; a second race SHALL
return retryable `NOT_READY`.

#### Scenario: Another agent changes a searched file
- **WHEN** another process completes a write after search preflight and before
  final guarded validation
- **THEN** the original paths/snippets are discarded and the complete search is
  replayed once against the reconciled catalog and bytes

#### Scenario: File is replaced by a symlink during search
- **WHEN** an admitted regular file or ancestor becomes a symlink before final
  validation
- **THEN** no result from that path is returned and the call replays once or
  returns retryable `NOT_READY`

#### Scenario: Catalog changes during both attempts
- **WHEN** the lexical catalog or searched bytes change during both the initial
  attempt and its replay
- **THEN** the tool returns retryable `NOT_READY` with no mixed-generation
  success payload

### Requirement: Lexical failures are typed and isolated
Invalid pattern/scope/limit inputs SHALL return `INVALID_INPUT`; an unauthorized
path SHALL return the existing trust error; a full lexical queue SHALL return
`BUSY`; and expiration of the owned search deadline SHALL return `TIMED_OUT`.
The lexical worker and ripgrep process tree MUST NOT block semantic LSP work,
lease heartbeats, runtime status, or work for another workspace. Timed-out,
cancelled, disconnected, or shutdown searches MUST NOT continue consuming an
owned process or later publish a result.

#### Scenario: Lexical queue is saturated
- **WHEN** one search is running and the fixed queued-search capacity is full
- **THEN** another search returns `BUSY` without entering the semantic executor
  or starting an unowned subprocess

#### Scenario: Ripgrep exceeds its deadline
- **WHEN** the owned ripgrep process does not settle within the fixed search
  deadline
- **THEN** Serena Light terminates and drains the exact process group and returns
  `TIMED_OUT`

#### Scenario: Search and semantic query overlap
- **WHEN** a long lexical search and a ready semantic query target the same
  workspace
- **THEN** the lexical executor does not occupy the semantic LSP worker and the
  semantic query remains independently admissible

#### Scenario: Runtime stops during search
- **WHEN** workspace retirement seals lexical admission while searches are
  queued or running
- **THEN** queued searches are cancelled, the running process tree is settled,
  and runtime `stopped` is not published while lexical cleanup remains pending

### Requirement: Lexical discovery remains non-semantic and read-only
Lexical matches SHALL be presented only as text occurrences. They MUST NOT be
merged into semantic references, declarations, implementations, diagnostics,
or symbol bodies and MUST NOT be used to authorize editing. The tools SHALL not
write workspace or site-packages content.

#### Scenario: String occurrence resembles a reference
- **WHEN** `search_text` finds a symbol name in a comment, string, or unrelated
  language file
- **THEN** it remains a lexical match and does not alter the result of
  `find_referencing_symbols`

#### Scenario: Site-packages text matches
- **WHEN** an explicitly scoped transformers search returns text
- **THEN** the result remains read-only and cannot become an edit target
