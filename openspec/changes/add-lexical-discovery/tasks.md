## 1. Prerequisite and Dependency Admission

- [ ] 1.1 Verify `strengthen-call-freshness` is accepted, synced, and archived; record the schema-3 source/build/lock baseline and stop if its per-call preflight/postflight owner is not the stable production path.
- [ ] 1.2 Select the Linux ripgrep release asset and record exact version, platform, SHA-256, license, provenance URL, lock format, and bootstrap failure behavior in a repository-owned lock manifest.
- [ ] 1.3 Add the ripgrep manifest to dependency-lock digest/build identity and bootstrap the verified executable atomically to `deps/<lock_digest>/bin/rg`, with download proxy allowed only during bootstrap and no ambient runtime resolution.
- [ ] 1.4 Add checksum mismatch, interrupted install, wrong platform, executable version, clean environment, poisoned `PATH`, user config, and poisoned loopback-proxy tests before exposing a tool.

## 2. Full-File Trust Catalog

- [ ] 2.1 Refactor the existing Git census so one NUL-safe cached/untracked/non-ignored candidate stream feeds a full-file regular-path catalog and the existing supported-language source projection without enumerating ignored contents twice.
- [ ] 2.2 Apply lexical normalization, component `lstat`/no-symlink, resolved-root, regular-file, tracked-deleted, and race guards to every full-file candidate; preserve hidden tracked files and arbitrary safe extensions.
- [ ] 2.3 Implement explicitly scoped, bounded, no-symlink catalog construction for the exact read-only transformers root and reject empty lexical scope there.
- [ ] 2.4 Integrate projection-specific read sets with the accepted per-call freshness owner: semantic source/config bytes for semantic calls, catalog membership for `find_paths`, and complete scoped text identities plus response-line witnesses for `search_text`.
- [ ] 2.5 Add unit/property fixtures for ignored trees, filenames with spaces/newlines/leading dashes/non-ASCII, hidden files, deletes/renames, symlink components, source-vs-full catalog digests, and no semantic/edit authority expansion.

## 3. Compact Path Discovery

- [ ] 3.1 Implement `find_paths` over the in-memory current catalog with authorized `relative_path`, basename-only case-sensitive glob validation, deterministic POSIX sorting, `max_results`, and no executor or subprocess use.
- [ ] 3.2 Implement its exact compact success/error DTO and canonical final MCP-text budgeting, with whole-path truncation and truthful unified `omitted` counts in text and structured content.
- [ ] 3.3 Add boundary tests for glob syntax, separators/traversal, scopes, result and answer limits, empty results, non-Git explicit scope, and catalog changes during the accepted read transaction.

## 4. Isolated Text Search

- [ ] 4.1 Add one single-worker/four-queued lexical executor per workspace, typed admission/cancellation states, runtime sealing, and status snapshots without reusing or blocking the semantic LSP executor.
- [ ] 4.2 Implement the fixed ripgrep wrapper with `--json`, `--no-config`, literal-by-default/Rust-regex modes, deterministic single-thread/path order, `--`, explicit catalog file batches, streaming full-batch match counting, and no recursive workspace operand or caller flags.
- [ ] 4.3 Parse only eligible UTF-8 text records, normalize and post-filter every returned path against the frozen catalog token, convert byte submatch offsets to 0-based Unicode-code-point ranges, and build deterministic clipped line/context records.
- [ ] 4.4 Implement `search_text` validation (including NUL/CR/LF rejection) and compact rendering for `context_lines`, `max_matches`, `max_line_chars`, final `max_answer_chars`, whole-match truncation, exact total-minus-returned `omitted`, and text/structured-content equality; timeout returns no partial success.
- [ ] 4.5 Enforce the fixed 15-second deadline and exact process-group termination/drain on timeout, cancellation, disconnect, and runtime stop; retain pending cleanup and prevent `stopped` publication until ownership settles.
- [ ] 4.6 Route both lexical tools through the stable per-call freshness owner; prove one complete replay after create/change/delete/config/ignore/symlink races and retryable `NOT_READY` without mixed paths/snippets after a second race.

## 5. Tool, Instructions, Status, and Schema Integration

- [ ] 5.1 Register exactly `find_paths` and `search_text` in daemon schemas and connector `READ_ONLY_TOOLS`, preserving typed envelopes and forbidding editing/process replay paths.
- [ ] 5.2 Define one concise initialize-instructions constant and attach it to both the inner daemon `FastMCP` and client-visible stdio proxy `Server`; add protocol tests proving byte-equivalent guidance and no hook/public instructions tool.
- [ ] 5.3 Extend bounded runtime status with full-file catalog count/digest, lexical executor queue/running state, and pinned ripgrep path/version without patterns, source, raw argv, secrets, or complete catalog listings.
- [ ] 5.4 Advance compatibility/public tool schema to version 4, include tool/instructions identity in build identity, and verify schema-3 and schema-4 daemon slots coexist without cross-attachment or forced lease termination.
- [ ] 5.5 Update direct dependency ownership, runtime manifests, source census/provenance, README/tool guidance, compatibility, client registration, and roadmap without changing canonical Serena.

## 6. Tests and Real-Repository Acceptance

- [ ] 6.1 Add deterministic unit/fault tests for literal and Rust regex behavior, unsupported PCRE features, NUL/CR/LF patterns, Unicode/non-BMP/CRLF lines, binary/invalid UTF-8 match and context exclusion, long-match clipping, context 0..2, exact omitted counts across batch boundaries, queue saturation, deadline-without-partial-success, and cleanup races.
- [ ] 6.2 Add black-box MCP tests proving complete actual `CallToolResult.content[0].text` budgets, structured-content equality, stable ordering/omission, typed trust/readiness failures, and no semantic-reference contamination.
- [ ] 6.3 Run real-daemon `find_paths` and `search_text` scenarios over source/tests/config/docs and dynamic-import spellings in `/data/CoordExp`, `/data/CoordExp/external/codexUI`, and `/data/ms-swift`.
- [ ] 6.4 Activate the conda-`ms` transformers package, require an explicit read-only lexical scope, verify targeted freshness and no enclosing site-packages scan/edit authorization, then release it cleanly.
- [ ] 6.5 Run fresh Codex, Claude Code, and CC Agent clients in clean and poisoned-proxy/PATH/config environments; verify instructions, explicit cross-root activation, multi-agent same-root reuse, timeout cleanup, and zero orphan ripgrep/LSP processes.
- [ ] 6.6 Pass full pytest, Ruff, Ty, bootstrap, direct-dependency/source-ownership/provenance/census/copied-hash gates and strict OpenSpec; report production LOC only as information.
- [ ] 6.7 Stop and return to design review if implementation requires recursive ripgrep authority, ambient tools/config, a content index/watcher, raw shell/LSP access, a third language, semantic/lexical result merging, new edit authority, or Serena agent/mode/project-server subsystems.
- [ ] 6.8 Obtain independent correctness and runtime-evidence review, disposition every blocker, re-run affected gates, then sync and archive before `improve-warm-runtime-reuse` implementation begins.
