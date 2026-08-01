## Context

Serena Light's Git trust inventory intentionally filters to Python and
JavaScript/TypeScript source extensions because it owns semantic adapters only
for those families. Reusing that projection for lexical discovery would omit
README files, TOML/YAML/JSON configuration, tests with unfamiliar suffixes, and
other agent-relevant text. Conversely, using ripgrep's recursive discovery or
ignore behavior as an authority would create a second trust policy and could
admit ignored data, hidden-path drift, or symlink escapes.

The service already owns per-session workspace binding, request-driven guarded
freshness, compact final MCP budgeting, typed failures, versioned build slots,
and a fixed read-only transformers exception. This design adds the smallest
lexical layer that composes with those owners. It assumes both
`strengthen-call-freshness` and `tighten-agent-interaction` have been accepted
and archived first.

## Goals / Non-Goals

**Goals:**

- Let agents enumerate trusted regular files and search literal or ordinary
  Rust-regex text without shell fallback.
- Cover source, tests, configuration, documentation, and string/dynamic-import
  spellings while preserving one trust authority.
- Return compact deterministic results with exact 0-based decoded-text
  positions and a true final MCP-text limit.
- Isolate bounded lexical subprocess work from the semantic LSP executor.
- Own and verify the ripgrep binary through Serena Light's dependency/build
  lifecycle.
- Teach all supported MCP clients the few routing facts they need at initialize.

**Non-Goals:**

- A raw shell, arbitrary ripgrep flags, PCRE2, a specialized dynamic-import
  parser, semantic/lexical reference merging, or a raw LSP tunnel.
- File content retrieval, replacement, line editing, a new language adapter, or
  changing site-packages edit policy.
- A watcher, resident text index, persistent cache, hook, public instructions
  tool, configurable package surface, or ambient `PATH` discovery.

## Decisions

### 1. Add a full-file catalog beside, not inside, semantic inventory

For Git workspaces one Git census remains authoritative:
`git ls-files --cached --others --exclude-standard -z`. Each candidate is
lexically normalized, checked component-by-component without following
symlinks, and admitted only when it is a regular file still inside the resolved
root. Tracked-but-deleted entries disappear. Ignored directory contents are not
enumerated. The resulting `FullFileCatalog` keeps every admitted extension,
including hidden tracked files.

The existing semantic source inventory is derived as its supported-extension
projection and configured programs remain adapter-owned projections. The three
counts/digests are reported separately. This avoids changing language
attribution merely because lexical tools can see a Markdown or TOML file.

Freshness observes these projections by read set rather than hashing every
catalog file for every semantic call. Semantic reads retain source/config byte
identity; `find_paths` needs exact catalog membership; and `search_text` guards
the complete explicit text scope plus internal response-line witnesses before
and after its subprocess. Thus documentation does not inflate unrelated LSP
preflights, while a lexical call cannot use semantic-only freshness.

The exact non-Git transformers root retains a bounded no-symlink walk, but a
lexical tool must receive a non-empty `relative_path` (`.` is an explicit whole
root choice). There is no implicit site-packages-wide search.

### 2. Keep `find_paths` an in-memory deterministic operation

`find_paths` takes `relative_path`, `name_glob`, `max_results`, and
`max_answer_chars`. `name_glob` is a case-sensitive basename glob supporting
`*`, `?`, and bracket classes; separators and parent traversal are invalid, so
directory selection has one owner in `relative_path`. The tool filters the
current catalog, sorts root-relative POSIX paths, applies `max_results`, then
removes trailing whole paths until the canonical final MCP JSON fits.

This does not need a thread or subprocess. Adding a general path-regex language
or directory records would expand the contract without a current consumer.

### 3. Give `search_text` a closed, compact query contract

The public inputs are:

```text
search_text(
  pattern,
  relative_path="",
  regex=false,
  context_lines=0,
  max_matches=50,
  max_line_chars=160,
  max_answer_chars=12000,
)
```

Literal search is case-sensitive and passes the pattern as fixed text. Regex
mode accepts only the pinned ripgrep/Rust regex engine, including its explicit
inline case semantics; invalid syntax, lookaround, backreferences, NUL, CR, or
LF return `INVALID_INPUT`, and no caller flags reach the subprocess. Rejecting
line breaks keeps every public match range single-line. Searchable content is a
current trusted regular file that ripgrep reports as UTF-8 text rather than
binary/byte-only data.

Results use the existing compact success shape: workspace once, deterministic
file groups, matches, and one `omitted` count. Each match has a compact 0-based
Unicode-code-point `range`, one clipped decoded line `text`, optional
`text_start_column` when clipping removed a prefix, and optional bounded
`context` entries when requested. Clipping never changes the absolute range and
may abbreviate a very long matched span; it is presentation, not a replacement
body. Context is zero through two adjacent lines on each side, clipped by the
same character limit.

`max_matches` defaults to 50 and accepts 1 through 500;
`max_line_chars` accepts 40 through 500; `max_answer_chars` follows the existing
512 through 50,000 final-text contract. Matches are ordered by path, line,
start column, end column, then text. Whole matches are removed for the final
budget and every removed match contributes to `omitted`.

### 4. Invoke pinned ripgrep only over explicit trusted files

Add a Linux ripgrep lock manifest containing version, release asset, SHA-256,
license/provenance, and platform. Include that manifest in the dependency-lock
digest and install the verified executable at
`deps/<lock_digest>/bin/rg`. Bootstrap may use ambient external-network proxy;
daemon operation never downloads and never resolves ambient `rg`.

For each call the wrapper supplies fixed arguments including `--json`,
`--no-config`, deterministic path sorting/single-thread behavior, and `--`
followed by batches of explicit catalog paths. It never recursively passes the
workspace directory. Batching avoids `ARG_MAX`. The wrapper streams every batch
to completion within the fixed deadline, counts every eligible match, and
retains only the bounded stable prefix needed for rendering; this makes
`omitted` exactly `total_eligible_matches - returned_matches` without retaining
an unbounded result set. If the deadline expires, it returns `TIMED_OUT` and no
partial success. JSON records with byte-only match-line data are not eligible
success records. A byte-only adjacent context record is omitted without
invalidating the match or changing match-level `omitted`. Every parsed path is
normalized and post-filtered against the exact admitted catalog token even
though it came from an explicit argument.

### 5. Isolate search in a bounded lexical execution owner

Each workspace has one single-worker lexical executor with at most four queued
searches. `find_paths` does not consume it. Saturation returns `BUSY`. A fixed
15-second search deadline returns `TIMED_OUT`, terminates the exact ripgrep
process group, and drains it before the queue entry is complete. Runtime stop
seals admission, cancels queued work, and owns cleanup of running descendants.
The semantic executor, adapter readiness waits, heartbeats, and other roots stay
responsive.

`search_text` enters the accepted `run_fresh_read` owner. Its preflight freezes
the catalog token, the complete explicit text-scope identities, and exact
inputs; each returned line carries an internal response-owned byte witness.
Postflight either validates the scope and witnesses or causes one complete
search replay. The wrapper cannot return paths or snippets from two catalog/
content generations.

### 6. Share one initialize-instructions source across both MCP layers

A single short constant explains that Serena Light is a shared semantic and
lexical navigation service, workspace binding is session-scoped, an absolute
`activate_workspace` is required after switching repositories, semantic tools
are preferred for symbols, lexical tools are for files/config/string discovery,
and status/typed failures expose readiness. The inner daemon `FastMCP` and the
outer stdio proxy `Server` both publish that exact text because the proxy owns
the client-visible initialize exchange.

No hook or `initial_instructions` function is added. Tool descriptions remain
the local parameter-level guidance.

### 7. Treat the tool surface as public schema 5

Add both tools to the daemon and connector read-only allowlists, compatibility
metadata, public schema digest, and build identity. A schema-4 connector cannot
silently attach to the schema-5 daemon. Existing versioned rollover starts a new
slot and lets leased schema-4 daemons retire normally.

## Risks / Trade-offs

- [A full-file Git catalog is larger than the semantic inventory] → Reuse one
  NUL-delimited Git census, keep only bounded metadata/digests in status, and do
  not build a content index.
- [Explicit argv batches can still be large] → Use deterministic bounded
  batches below measured `ARG_MAX` headroom and test filenames containing
  spaces, newlines, leading dashes, and non-ASCII text.
- [A separate executor adds lifecycle state] → Keep one worker, four queue
  slots, one process owner, and reuse existing typed queue/cleanup patterns.
- [Rust regex differs from PCRE expectations] → Advertise the engine, default to
  literal, validate before enqueue, and return `INVALID_INPUT` rather than
  falling back.
- [Search can be slower under per-call guarded freshness] → Correctness is the
  admission priority; retain compact limits and measure real roots before any
  indexing proposal.
- [Downloaded binaries add supply-chain ownership] → Pin asset SHA-256 and
  provenance, include the manifest in the lock/build digest, and fail bootstrap
  closed on mismatch.

## Migration Plan

1. Require the archived freshness change and its acceptance evidence.
2. Add the ripgrep lock manifest/bootstrap verification and prove the installed
   path/version without modifying ambient tools.
3. Add the full-file catalog, compact DTO/renderer, isolated executor, and two
   read-only tools with deterministic unit/fault tests.
4. Revise the shared source-owned instructions for the new lexical routing,
   attach them to both MCP initialize boundaries, and advance compatibility/public
   schema to 5.
5. Start a new build slot and run fresh Codex, Claude Code, and CC Agent clients
   plus the four named real-root smokes in clean and poisoned-proxy environments.
6. Sync and archive before warm-runtime work begins.

Rollback removes the schema-5 client registration/build and reconnects clients
to the prior schema-4 build. It does not alter workspace files or canonical
Serena.

## Open Questions

None. Additional query flags, content retrieval, or dynamic-import semantics
require usage evidence and a separate change.
