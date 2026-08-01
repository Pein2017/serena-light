## Why

Serena Light can answer semantic Python and JavaScript/TypeScript questions but
cannot enumerate ordinary trusted files or search configuration, tests,
documentation, string references, and dynamic-import spellings. Agents must
fall back to shell for these common discovery steps, which makes the lighter
server less complete even when semantic navigation itself is accurate.

## What Changes

- Add two compact agent-facing read-only tools: `find_paths` for bounded path
  discovery and `search_text` for bounded literal or Rust-regex text search.
- Build a full-file trust catalog from the same Git authority and symlink guards
  as the source inventory, while keeping full-file trust, semantic source trust,
  and configured-program attribution as distinct projections.
- Search tracked and eligible untracked regular text files across source,
  tests, configuration, and documentation. Preserve the explicitly activated,
  read-only transformers exception, but require a non-empty explicit scope for
  lexical operations on that non-Git root.
- Pin a Serena-Light-owned ripgrep executable through the dependency lock and
  build identity. Never use ambient `rg`, user config, ignore files as a second
  authority, PCRE2, lookaround, or backreferences.
- Run text search through a separate bounded per-workspace lexical executor and
  owned process lifecycle. `find_paths` remains an in-memory catalog operation;
  lexical saturation, timeout, invalid pattern, and shutdown return typed
  failures without blocking the semantic LSP executor.
- Apply the strengthened call freshness contract before and after lexical
  results, post-filter every result against current trust, and never return
  mixed-version paths, line text, or context.
- Default to literal, case-sensitive search, 160 decoded characters per matched
  line, no context, bounded matches, and a final compact MCP-text budget;
  callers may request Rust regex and zero through two context lines.
- Provide one short, shared initialize-instructions text at both MCP initialize
  boundaries so Codex, Claude Code, and CC Agents know to activate a workspace,
  prefer semantic tools for symbols, and use lexical tools for files/text.
- Bump the public tool/schema version and deploy by build-identity rollover.

## Capabilities

### New Capabilities

- `lexical-discovery`: Trusted path enumeration, bounded text search, compact
  results, deterministic limits, and typed lexical failures.

### Modified Capabilities

- `workspace-runtime`: Add the distinct full-file trust catalog, pinned lexical
  runtime ownership, isolated lexical execution, initialize instructions, and
  schema/build rollover behavior needed by the two tools.
- `diagnostics-status`: Report bounded full-file catalog and lexical executor/
  ripgrep identity as operational status, without adding that metadata to
  successful discovery results.

## Impact

This change affects trust inventory construction, freshness projections,
daemon and connector tool registration, MCP initialization, dependency locks,
build identity, runtime status, lifecycle cleanup, compatibility metadata, and
real-repository acceptance. The connector read-only allowlist gains exactly
`find_paths` and `search_text`; public schema advances from version 4 to 5, so
fresh clients must reconnect to the matching build slot.

The feature does not add a language, dynamic-import parser, semantic/lexical
result merger, arbitrary shell or raw LSP tunnel, file reading tool, watcher,
hook, UI, memory, broad logging, or edit operation. It does not change the rule
that shell `cd` alone cannot rebind a session: agents continue to call
`activate_workspace("/absolute/path")` when switching repositories.

Admission requires archived acceptance of both `strengthen-call-freshness` and
`tighten-agent-interaction`, pinned ripgrep version/checksum and dependency
ownership evidence, deterministic unit and fault tests, strict
OpenSpec/provenance gates, and targeted real-daemon smokes in `/data/CoordExp`,
`/data/CoordExp/external/codexUI`,
`/data/ms-swift`, and the conda-`ms` transformers package with an
explicit read-only scope. Production LOC remains informational.
