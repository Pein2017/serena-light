## 1. Prerequisite and Repaired Baseline

- [x] 1.1 Verify `fix-position-and-coverage-contract` is fully implemented, independently accepted, synced to stable specs, and archived; stop this change immediately if any coordinate, complete-body, or reference-coverage blocker remains.
- [x] 1.2 Record the repaired source commit, dependency-lock digest, public schema/build identity, engine versions, client versions, clean/poisoned-proxy environment, and production LOC as information only.
- [x] 1.3 Create fixed Python and TypeScript fixtures for exact symbol without body, exact symbol with body, global symbol, large nested overview, multi-file references with uncovered configured-program files, declaration, implementation, external read-only target, and true empty result.
- [x] 1.4 Through the real daemon and connector, preserve each baseline `CallToolResult.content[0].text`, `structuredContent`, semantic entity set, ranges, hashes, coverage, character count, client-visible token count, calls, and wall time; do not use internal envelope JSON as the baseline.
- [x] 1.5 Add schema snapshots proving current success duplicates workspace/path/adapter/generation/hash metadata and a regression test reproducing the final-response overflow when an inner fragment alone is budgeted.

## 2. Compact DTOs, Ordering, and Kind Mapping

- [x] 2.1 Add Serena-Light-owned navigation success DTOs for the fixed `ok/data/workspace/files/omitted` shape and tool-specific overview symbols, symbol matches, references, and declaration/implementation targets.
- [x] 2.2 Implement stable normalized-path/range/name/container/kind ordering and exact deduplication before grouping; add permutation tests showing identical JSON for adapter results delivered in different orders.
- [x] 2.3 Implement the fixed lowercase LSP kind mapping plus `unknown:<integer>` fallback and validate overview kind filters before semantic dispatch.
- [x] 2.4 Implement file grouping with path once, conditional language/external/read-only identity, hash once only for body-bearing files, and no empty groups; add golden JSON tests for single-file, multi-file, multi-language, and external results.
- [x] 2.5 Add structural tests forbidding adapter phase, generations, configured-program detail, query echoes, URI, repeated path/hash, text offsets, byte offsets, selection ranges, detail, and boolean child flags in navigation success.

## 3. Exact MCP Rendering and Public Limits

- [x] 3.1 Add one canonical UTF-8-preserving compact JSON renderer with fixed field order and no insignificant whitespace, and construct explicit MCP `CallToolResult` values whose sole text block is that exact string and whose `structuredContent` is the same JSON value.
- [x] 3.2 Validate `max_answer_chars` uniformly at 512 through 50,000 with default 12,000; add the parameter to `find_declaration` and ensure invalid values fail before LSP dispatch.
- [x] 3.3 Add `find_symbol.max_matches` to the public schema with default 20 and accepted range 1 through 100; apply it after semantic filter/dedup/sort for file, directory, and global scopes and make adapter candidate fan-out internal.
- [x] 3.4 Implement whole-record final-budget pruning after match limiting, with one summed `omitted` count, stable removal order, removal of empty groups, and exact reserialization after every retained-set change.
- [x] 3.5 Implement overview subtree pruning in stable preorder without orphaning children and count every removed node; test nested trees at exact boundary values.
- [x] 3.6 Implement the bounded `INVALID_INPUT` response with `field=max_answer_chars` and measured `minimum_required_chars` when no matching indivisible record fits; prove bodies, info, snippets, paths, and JSON tokens are never sliced.
- [x] 3.7 Add black-box assertions that the real connector-observed `content[0].text` is byte-for-byte the canonical renderer output, its character length is within the requested success bound, and `structuredContent` parses to the identical value under the pinned MCP SDK.

## 4. Convert Navigation Tools One at a Time

- [x] 4.1 Convert `get_symbols_overview` to name/kind/non-empty-children nodes and kind filters; preserve default symbol-kind completeness and descendant-depth behavior.
- [x] 4.2 Convert file- and directory-scoped `find_symbol` to grouped compact symbol records, retaining complete recovered assignment bodies and one hash per body-bearing file.
- [x] 4.3 Convert global `find_symbol` to grouped compact records with deterministic `max_matches`, conditional per-file language identity, and no repeated configured-program/runtime metadata.
- [x] 4.4 Convert `find_referencing_symbols` to grouped compact references while retaining one complete correctness-change coverage object and typed file-level containers.
- [x] 4.5 Convert `find_declaration` and `find_implementations` to grouped compact targets, including requested info/body, one file hash when applicable, authoritative read-only external paths, and the common final-answer budget.
- [x] 4.6 Prove genuine empty results remain `files=[]/omitted=0`, while cold/not-ready, unsupported, scope-incompatible, busy, timeout, cooldown, and protocol failures retain the existing rich error envelopes and never become compact empty success.
- [x] 4.7 Run cross-tool Unicode/CRLF/BOM fixtures to prove compact ranges preserve the archived 0-based decoded-text line/Unicode-column contract after text/byte offsets disappear from navigation success.

## 5. Preserve Non-Navigation Authority and Runtime Behavior

- [x] 5.1 Add contract snapshots proving runtime status, diagnostics, guarded editing, workspace activation/release, daemon control-plane results, and all error envelopes retain their current authority, generation, retry, lifecycle, and outcome metadata.
- [x] 5.2 Re-run current-generation diagnostics and stale-hash, symlink, queued/running timeout, lost-response, and post-replace `UNCERTAIN` edit tests to ensure generic envelope changes did not leak into non-navigation paths.
- [x] 5.3 Verify two sessions sharing one workspace, two workspaces in one build daemon, deliberate client `cd`/activation switches, and per-language incompatibility isolation produce no cross-lease or cross-root result leakage.
- [x] 5.4 Run clean and poisoned-proxy daemon/connector smokes; prove loopback no-proxy behavior, stripped LSP child proxy variables, service-owned HOME/executables, and no new orphan processes are unchanged.

## 6. Compatibility, Build Identity, and Client Migration

- [x] 6.1 Update `docs/compatibility.json`, README, tool descriptions/schemas, client-registration docs, and representative examples with exact old-to-new mappings for all five navigation tools and every optional compact field.
- [x] 6.2 Increment `PUBLIC_TOOL_SCHEMA_VERSION` and verify source/lock/schema changes alter build identity; test connector-start/daemon-start identity races and one-time startup nonce behavior.
- [x] 6.3 Start old and new build daemons with independent client holders and two workspaces; verify fresh clients select only the compact-schema build, old holders continue unaffected, and zero-holder/grace retirement cannot delete successor discovery state.
- [x] 6.4 Verify fresh Codex, Claude Code, and CC Agent tool listings expose `find_symbol.max_matches`, overview kind filters, and `find_declaration.max_answer_chars` with accurate agent-facing descriptions and no `compact` flag or public adapter-candidate limit.
- [x] 6.5 Update source census/provenance/ownership manifests and copied-symbol hashes only if implementation adds or copies production mechanisms; otherwise record that the compact renderer and DTOs are newly owned Serena Light code.

## 7. Efficiency, Full Acceptance, and Archive Gate

- [x] 7.1 Run targeted and full pytest, Ruff, Ty, bootstrap, dependency/AST ownership, provenance/census, build-identity, and strict OpenSpec checks; record exact commands and results.
- [x] 7.2 Run the fixed real-MCP fixture suite and require: exact-symbol no-body characters at most 50% of repaired baseline, global symbol and multi-file references at most 40%, large overview at most 25%, and body-external characters at most 50%, with identical semantic evidence inside the same public limits.
- [x] 7.3 Report client-visible tokens, tool calls, and wall time for every fixture; investigate any regression even when deterministic character gates pass, but do not trade away correctness, body integrity, hash, range, or coverage to improve the score.
- [x] 7.4 Re-run the locked four-arm ablation on the same repository snapshots and prompts: for each model family expose exactly one of canonical Serena or Serena Light, allow shell, instruct MCP-first use, record shell fallback, and score accuracy before efficiency. Compare compact Serena Light both with its repaired pre-compact baseline and with canonical Serena.
- [x] 7.5 Stop and return to design review if the compact contract requires per-client servers, canonical Serena replacement, loss of typed structured content, removal of error/status/edit authority, lexical-semantic mixing, or a new trust/lifecycle owner; production LOC remains informational rather than a hard gate.
- [x] 7.6 Obtain final independent Sol-xhigh static-correctness and Opus-max runtime/evidence audits, disposition every blocker with lead-owned evidence, sync stable specs, archive the change, commit intentionally, and push only after all gates pass.
