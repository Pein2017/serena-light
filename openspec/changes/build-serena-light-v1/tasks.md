## 1. Freeze Dependencies and Clear Admission Gates

- [x] 1.1 Create the Python 3.12 `pyproject.toml`, `src/serena_light` package, pytest layout, local lint/typecheck commands, and console-entry placeholders without adding product behavior.
- [x] 1.2 Add repository-owned Python and Node lock files that pin the MCP SDK, `lsprotocol`, Pyright, `typescript-language-server`, TypeScript 5.9, pytest, and developer tooling; record any deliberate departure from the observed MCP 1.27.1, Pyright 1.1.403, typescript-language-server 5.1.3, and TypeScript 5.9.3 baseline.
- [x] 1.3 Implement one bootstrap/check command that materializes dependencies below `/data/CoordExp/.codex/runtime/serena-light/deps/<lock-digest>` and reports exact executable paths and versions; fail if an engine resolves through `/root/.nvm`, user pip state, or ambient `PATH`.
- [x] 1.4 Add `THIRD_PARTY_NOTICES.md` and a machine-readable copied-source manifest keyed to Serena commit `9a9d07e83d8c`, source path, license, copied hash, and local owner.
- [x] 1.5 Produce a symbol-level copy/keep/delete census for the proposed Serena/SolidLSP closure and a locally runnable source-budget command; report production LOC as audit information and stop if the closure requires `agent.py`, modes, memories, UI, or `project_server.py`.
- [x] 1.6 Preserve the standalone readiness probes under `tests/admission` and run five clean starts each for the transformers Pyright root, `/data/CoordExp`, `/data/ms-swift`, and the `cc-plugin-codex` TypeScript root; require the declared acceptance symbol and current-generation global readiness within 30 seconds on every run.
- [x] 1.7 Add a TypeScript fixture whose tsconfig includes an ignored supported-language subtree; compare the effective tsserver program with the Git JS/TS inventory and stop for an OpenSpec revision if exact inclusion/exclusion cannot be achieved without changing project semantics.
- [x] 1.8 Probe Pyright and TypeScript initialization for the selected `positionEncoding`, recording UTF-16 when absent, and preserve the initialize transcripts as bounded test fixtures.
- [x] 1.9 Write an admission report containing lock hashes, engine paths, source census, five-run readiness results, TypeScript scope equivalence, and position-encoding results; do not begin section 2 unless every gate passes.
- [x] 1.10 Extend the admission probes to attribute configured-program files by normalized path for Pyright and TypeScript, record the selected native config and engine-owned project kind, and reject count-only equivalence claims for `/data/CoordExp`, `cc-plugin-codex`, `/data/ms-swift`, and transformers.
- [x] 1.11 Classify `trusted_not_in_configured_program` and `configured_program_outside_trust` separately with reasons; prove native exclusions are permitted, the ignored-subtree fixture produces typed `SCOPE_INCOMPATIBLE`, symlink/tracked-deleted paths never enter either accepted set, and no overlay config is generated.
- [x] 1.12 Re-run five clean configured-program readiness starts per real profile within 30 seconds, and add path-scoped tests showing a trusted source omitted by native config is either served by an engine-owned inferred/transient project without expanding global scope or fails explicitly without writing.
- [x] 1.13 Write a superseding scope-admission report that references the historical failed report, contains file-level projection evidence and difference reasons, and records a PASS only when real native programs contain no supported-language path outside trust; Section 2 remains blocked until this task passes.

## 2. Build the Owned LSP Core and Execution Boundary

- [x] 2.1 Implement `lsprotocol` JSON conversion and typed request/response models for initialize, document lifecycle, document/workspace symbols, hover, definition, implementation, references, diagnostics publication, configuration, cancellation, shutdown, and exit; do not add a `textDocument/declaration` request path for `find_declaration`.
- [x] 2.2 Copy and simplify the synchronous SolidLSP framing/request lifecycle with per-request timeouts, deterministic shutdown, and `ContentModified` retry only for read requests; add provenance entries for every copied mechanism.
- [x] 2.3 Implement one bounded single-worker LSP executor and queue per workspace; prove with a blocking fake LSP that the daemon event loop, another root, status, and heartbeats remain responsive.
- [x] 2.4 Define cancellation ownership: remove queued-not-started work, allow started synchronous work to reach its bounded terminal state, discard cancelled results, and prove no cancelled request retains the workspace lock.
- [x] 2.5 Copy the Linux parent-death launcher without weakening its persistent daemon-lifetime spawner thread, `start_new_session=True`, or `exec` prefix invariants; keep the ordinary LSP executor separate from the spawner.
- [x] 2.6 Port both upstream parent-death regressions and add a real daemon-path test proving a normally spawned server survives its caller's return while daemon SIGKILL leaves no descendant.
- [x] 2.7 Implement terminate-then-kill process-group cleanup and deterministic adapter/executor shutdown; verify Pyright, typescript-language-server, and both tsserver children exit.
- [x] 2.8 Implement one position mapper for negotiated UTF-8/UTF-16/UTF-32 LSP code units, decoded-text offsets, and raw byte offsets against an immutable file snapshot.
- [x] 2.9 Test the position mapper with Python and MJS fixtures containing astral Unicode before and inside symbols, UTF-8 BOM, CRLF and LF, multi-byte text, and end-of-line positions.
- [x] 2.10 Implement document versions, source/index/diagnostic generations, and the published-diagnostics store without copying pull diagnostics or Serena's asynchronous request stack.
- [x] 2.11 Implement shared symbol/document normalization with adapter-owned containment recovery and no Python-specific reference hack in the common layer.
- [x] 2.12 Run the local source-budget/import-boundary command and prove the core has no Serena agent, mode, memory, UI, project-server, JetBrains, or unused-language imports.

## 3. Implement Workspace Identity, Trust, and Source Scope

- [x] 3.1 Implement absolute-path validation, resolved Git top-level identity, exact allowlisted non-Git identity, and deterministic same-root working-subdirectory metadata.
- [x] 3.2 Implement acquire-then-swap cross-root activation so validation or acquisition failure preserves the old lease and binding; test same-root reuse and failed cross-root rollback.
- [x] 3.3 Resolve the pinned `ms` interpreter's stdlib, purelib, and platlib roots at startup and implement `read_only_external` semantic locations without adding them to the active inventory.
- [x] 3.4 Implement edit authorization for resolved Git workspaces below `/data`, read-only rejection for conda environment paths, and pre-I/O symlink-escape rejection.
- [x] 3.5 Implement `OUT_OF_WORKSPACE` for trusted path operands outside the active inventory, including the current identity and an absolute activation hint; test nested repositories and ignored linked worktrees below `/data/CoordExp`.
- [x] 3.6 Implement the Git cached-plus-untracked trust inventory, filter tracked-but-deleted, non-regular, and symlink-escaped paths, and build a supported-language prefix tree without enumerating ignored subtrees.
- [x] 3.7 Discover native `pyrightconfig.json`, `tsconfig.json`, or `jsconfig.json` semantics without overlays; attribute configured-program paths, classify both inventory differences with reasons, and return `SCOPE_INCOMPATIBLE` when the configured program contains supported-language files outside trust.
- [x] 3.8 Implement bounded, no-symlink discovery for the exact trusted transformers root and targeted stat freshness that never walks all of site-packages.
- [x] 3.9 Implement separate trust-inventory, configured-program, and path-scoped document generations plus `didChangeWatchedFiles`; port focused Serena external-file-change scenarios without expanding native program membership.
- [x] 3.10 Implement a bounded generation verification barrier that prevents configured-program global readiness until changed program state is observable while invalidating only path-scoped readiness for omitted trusted files; return `NOT_READY` rather than stale empty success.

## 4. Implement Fixed Adapters and Readiness

- [x] 4.1 Define the adapter interface with extension routing, raw LSP providers, derived tool availability, engine metadata, selected position encoding, readiness generations, crash state, and independent lazy-start ownership.
- [x] 4.2 Implement the Pyright adapter with `workspace.configuration=true`, a responder for `python`, `python.analysis`, and `pyright`, the fixed conda `ms` interpreter, native pyrightconfig semantics, and file-level configured-program attribution.
- [x] 4.3 Add a real Pyright `find_declaration` test from `/data/ms-swift` `GenerationConfig` into the exact conda transformers location and a second installed-package definition test; both external results must be read-only.
- [x] 4.4 Implement the TypeScript adapter for JS/JSX/MJS/CJS/TS/TSX/MTS/CTS with the pinned server-owned TypeScript 5.9 engine, native tsconfig/jsconfig semantics, inferred-project path support when engine-owned, and `SCOPE_INCOMPATIBLE` enforcement.
- [x] 4.5 Add real `cc-plugin-codex/runtime` MJS overview, cross-file `find_declaration`, reference, and implementation tests; status must report the pinned engine, selected encoding/config, trust inventory, configured program, and explained differences.
- [x] 4.6 Assert raw provider facts separately from derived tools: both adapters support `find_declaration` through `definitionProvider`; only TypeScript supports `find_implementations`; a supported definition miss returns `SYMBOL_NOT_FOUND`.
- [x] 4.7 Implement phases `cold`, `starting`, `document_ready`, `global_warming`, `ready`, `degraded`, `cooldown`, and `stopping` with trust/program/document/index generations and timestamped transitions.
- [x] 4.8 Implement controlled document warm-up, the admission-proven configured-program workspace-symbol sentinel, and the 30-second lock-free readiness wait; cold or stale configured-program generations return typed `NOT_READY` with retry metadata.
- [x] 4.9 Implement read-only restart-and-retry once, no edit retry, per-adapter crash windows/circuit breakers, and proof that one failed adapter or root does not stop another.

## 5. Implement the Shared Daemon and Stdio Connector

- [x] 5.1 Create the runtime directory symlink-safely at mode `0700`; atomically write mode-`0600` bearer and discovery metadata; reject stale, malformed, wrong-owner, or over-permissive metadata.
- [x] 5.2 Implement loopback-only Streamable HTTP, bearer authentication before workspace work, daemon identity/version health checks, and startup serialization.
- [x] 5.3 Start the daemon detached from the winning connector and prove the starter connector may exit while the daemon and another lease remain healthy.
- [x] 5.4 Implement daemon-issued lease UUIDs, independent 15-second heartbeats, 60-second expiry, normal release, ten-minute last-lease grace, and last-holder-only immediate shutdown.
- [x] 5.5 Implement the workspace registry and lease-to-binding map with no mutable daemon-global workspace pointer and no MCP HTTP session as lifetime authority.
- [x] 5.6 Implement the stdio MCP connector, inherited-cwd auto-activation, authenticated proxying, graceful release, daemon reuse, and no embedded language server.
- [x] 5.7 Implement daemon/session-loss recovery: create a new HTTP session and lease, restore the last validated binding, retry an interrupted read-only request at most once, and return `UNCERTAIN` without replaying an in-flight edit.
- [x] 5.8 Test simultaneous connect-or-start, stale discovery recovery, invalid bearer rejection, connector crash expiry, expired-lease `LEASE_EXPIRED`, and one connector exiting while another retains the runtime.
- [x] 5.9 Run a blocking request longer than 60 seconds and prove heartbeats, status, another root, and the daemon event loop remain responsive without unbounded worker growth.
- [x] 5.10 SIGKILL the daemon during idle, read, and edit phases; assert zero owned descendants, correct connector rebind behavior, and no edit replay.

## 6. Implement Typed Status and Semantic Navigation

- [x] 6.1 Define stable JSON envelopes and schemas for `INVALID_PATH`, `UNTRUSTED_ROOT`, `INVALID_INPUT`, `OUT_OF_WORKSPACE`, `SCOPE_INCOMPATIBLE`, `NOT_READY`, `UNSUPPORTED`, `SYMBOL_NOT_FOUND`, `AMBIGUOUS_SYMBOL`, `STALE_HASH`, `READ_ONLY_ROOT`, `LEASE_EXPIRED`, `BUSY`, `TIMED_OUT`, `COOLDOWN`, and `UNCERTAIN`.
- [x] 6.2 Implement `activate_workspace`, `release_workspace`, and `get_runtime_status` with daemon identity, lease/binding, warm grace, raw and derived capabilities, engine/interpreter, selected native config, trust/program projections and differences, selected encoding, generations, executor queue, crash, and cooldown fields but no secrets.
- [x] 6.3 Implement bounded `get_symbols_overview` from one document-symbol tree with depth, truncation, and position-conversion tests for Python and MJS.
- [x] 6.4 Implement path-scoped `find_symbol` with Serena-style name paths, exact/substring matching, body/info options, whole-file hash metadata, and typed ambiguity handling.
- [x] 6.5 Implement global `find_symbol` over current-generation native configured-program workspace-symbol candidates, exact-name filtering, candidate-file document-symbol verification, name-path reconstruction, multi-adapter merge, bounded output, explicit advertised scope, and no O(files) or external-library fallback.
- [x] 6.6 Implement `find_referencing_symbols` with containing-symbol mapping, bounded snippets, file-level fallback, adapter-owned recovery, and read-only external-location normalization.
- [x] 6.7 Implement Serena-compatible `find_declaration(relative_path, regex, containing_symbol_name_path, include_body, include_info)` exclusively through `textDocument/definition`; enforce one capture group and unique occurrence, convert the capture position through the shared mapper, and add TypeScript and cross-library Python regressions that fail if raw `declarationProvider` is used as the gate.
- [x] 6.8 Implement capability-gated `find_implementations(name_path, relative_path, include_info, include_kinds, exclude_kinds, max_answer_chars)`, returning `UNSUPPORTED` with raw and derived matrices for Pyright and normalized locations for TypeScript.
- [x] 6.9 Test every path-taking semantic tool against an active nested repository, another `/data` workspace, a linked worktree, an allowed external result, and an untrusted external result.

## 7. Implement Diagnostics and Minimal Debugging

- [x] 7.1 Implement `get_diagnostics_for_file` over published current generations with distinct `findings`, `clean`, `not_ready`, and `timed_out` states and default Error/Warning filtering.
- [x] 7.2 Implement deterministic diagnostic truncation and grouping by containing symbol or `<file>` through the shared position mapper.
- [x] 7.3 Implement `get_diagnostics_for_symbol` by resolving one symbol and filtering the same current file generation without a project-wide diagnostic pass.
- [x] 7.4 Mark every TypeScript diagnostic result `authority=advisory`; retain the known `runtime/args.mjs` TypeScript 5.9 versus repository TypeScript 7 divergence fixture and keep native typecheck authoritative.
- [x] 7.5 Include Pyright version, conda interpreter, and external-root classification in Python diagnostics and prove current-generation transformers import resolution.
- [x] 7.6 Test empty publications, missing publications, stale generations, timeouts, non-BMP/CRLF ranges, and truncation without ever representing not-ready or timeout as clean.
- [x] 7.7 Implement concise stderr reporting and bounded rotating debug logs with rotation, secret-redaction, and no dashboard, telemetry, memories, or call-audit payloads.

## 8. Implement Guarded Symbol-Body Replacement

- [x] 8.1 Implement `replace_symbol_body(name_path, relative_path, body, expected_hash)` with edit-root authorization, current-file reread, exact symbol re-resolution, shared position conversion, and whole-file SHA-256 verification under the workspace lock.
- [x] 8.2 Implement same-directory temporary writes that preserve mode, encoding, BOM, and CRLF/LF form, flush content, and install with atomic `os.replace`.
- [x] 8.3 Inject pre-replace write/flush failures and prove the original file remains byte-for-byte intact and temporary artifacts are cleaned.
- [x] 8.4 Notify the owning adapter after replacement and return old/new hashes, symbol identity, file generation, and notification state without automatically running diagnostics.
- [x] 8.5 Implement `UNCERTAIN` for post-replace notification or transport loss and disable edit retry in connector, HTTP, MCP-session, daemon, and adapter layers.
- [x] 8.6 Test non-BMP text before/inside the symbol, BOM/CRLF preservation, stale hash, concurrent modification, external deletion, ambiguity, symlink escape, out-of-workspace input, and conda site-packages rejection.
- [x] 8.7 Test a lost-response retry with the original hash and a daemon restart after possible replacement; both paths must avoid duplicate application and require a current reread.

## 9. Run Real-Repository and Failure Acceptance

- [x] 9.1 Run the complete local unit/integration suite plus OpenSpec strict validation from a clean dependency bootstrap; record exact commands, lock digest, engine versions, and any skipped platform case.
- [x] 9.2 Run `/data/CoordExp` Python acceptance with configured-program global symbol recall, explained Git/program differences, ignored-data trust pruning, generation freshness, peak RSS below 8 GB, and global readiness within 30 seconds.
- [x] 9.3 Run `/data/CoordExp/cc-plugin-codex` configured-program MJS overview, find, definition, reference, implementation, Unicode range, path-scoped omitted-file behavior, and advisory-diagnostics acceptance; compare diagnostics with `npm run typecheck`.
- [x] 9.4 Run `/data/ms-swift` Python definition and diagnostics acceptance through the conda `ms` interpreter, then activate the exact transformers root, verify its attributed semantic projection and global symbol, and reject every edit there.
- [x] 9.5 Run two-session same-root reuse, two-root concurrent isolation, acquire-then-swap activation, failed-switch rollback, lease expiry, non-last immediate release, ten-minute grace, and last-holder immediate shutdown.
- [x] 9.6 Run external create/change/delete generation barriers and prove a global query either observes the new generation or returns `NOT_READY`, never stale empty success.
- [x] 9.7 Run adapter crash, circuit-breaker, connector crash, daemon SIGKILL, HTTP-session loss, cancellation, queue saturation, and parent-death/no-orphan fault injection with edit no-replay assertions.
- [x] 9.8 Re-run the production source budget, forbidden-import scan, dependency-path smoke, and provenance audit; report LOC without a numeric stop threshold, and stop if copied code lacks MIT provenance or excluded Serena subsystems re-enter the closure.

## 10. Parallel Client Migration and Final Gate

- [x] 10.1 Add Codex, Claude Code, and CC Agent parallel-registration snippets under the distinct `serena-light` name plus stop/restart and rollback instructions; do not modify the live canonical `serena` registration.
- [x] 10.2 In fresh sessions for all three client types, exercise cwd auto-activation, explicit cross-root activation, status, Python and MJS navigation, diagnostics, hash-guarded editing, daemon reuse, and connector exit cleanup without relying on fallback calls to canonical Serena.
- [x] 10.3 Produce a machine-readable old/new compatibility inventory covering tool names, required arguments, result shapes, SessionStart hooks, instruction consumers, and excluded Serena tools; classify every delta as supported, deliberately dropped, or separately shimmed.
- [x] 10.4 Produce the final acceptance report with gate results, resource/process evidence, residual risks, compatibility inventory, and rollback value; unresolved compatibility deltas SHALL block only a later canonical MCP-name switch.
- [x] 10.5 Re-run `openspec validate build-serena-light-v1 --strict` and the complete local suite, then request separate user approval before any canonical MCP registration change; do not perform that switch in this change.

## 11. Contain Editing and Fix Local Transport Ownership

- [x] 11.1 Remove `replace_symbol_body` from new MCP tool advertisements while preserving its implementation/tests; stale invocations return `UNSUPPORTED` with `temporarily_disabled_pending_reacceptance` and never write.
- [x] 11.2 Set connector and health-check HTTP clients to ignore environment proxies, use explicit no-proxy openers in local acceptance/fault tests, and prove poisoned proxy variables cannot intercept loopback.
- [x] 11.3 Strip all case-variants of `*_PROXY` from daemon and LSP child environments without mutating global proxy or `NO_PROXY`; retain ambient proxy only for external bootstrap downloads.
- [x] 11.4 Make every `DaemonProcess.start` failure path reclaim the driver and child created by that attempt; add no-orphan regression coverage.
- [x] 11.5 Remove only the stale `/data/serena-light-acceptance-codex` trust entry from shared Serena Light configuration and verify canonical Serena plus unrelated projects are unchanged.

## 12. Repair Freshness and Guarded Editing

- [x] 12.1 Implement a workspace-owned `FreshnessCoordinator` invoked before every semantic/edit operation and same-root reactivation, with concurrent callers sharing one in-flight scan and no stale-success time cache.
- [x] 12.2 For Git roots compare lexical inventory create/change/delete, symlink state, and native configs; update path/document generations for content changes and reattribute only affected language families for membership/config changes.
- [x] 12.3 Send `didChangeWatchedFiles` to running adapters and controlled open/close for new files; retain targeted-stat freshness for the read-only transformers non-Git root.
- [x] 12.4 Change edit authorization to lexical inventory membership and add dir-fd, `lstat`, and `O_NOFOLLOW` component checks under the workspace lock.
- [x] 12.5 Add executor edit states `queued`, `running`, `installed`, and `done`; proven queued cancellation returns `TIMED_OUT`, while started or unknown state returns `UNCERTAIN` and is never replayed.
- [x] 12.6 Treat every fsync, notification, or transport failure after `os.replace` as `UNCERTAIN` and include the safely observed current hash when possible.
- [x] 12.7 Pass real daemon/connector tests for in-root ignored-file symlink substitution, queued/running timeout, post-replace fsync failure, and lost response.

## 13. Repair Semantic Contracts and Runtime Truth

- [x] 13.1 Wire the existing envelope converters at one runtime/service boundary, catch `TimeoutError` before `OSError`, and preserve all declared typed failure codes.
- [x] 13.2 Implement one `didOpen` per URI, subsequent `didChange`, and `didClose` on adapter stop or LRU eviction with at most 128 open documents per adapter.
- [x] 13.3 Convert global symbol ranges from exact candidate snapshots through the shared `PositionMapper` and test Unicode/CRLF source, decoded-text, and byte offsets.
- [x] 13.4 Complete inventory-bounded directory `find_symbol`; populate global body/info from verified candidate documents and reject unsupported parameter combinations explicitly.
- [x] 13.5 Attribute compatibility independently per language family, retain incompatible families in status, serve healthy families, and permit all-unavailable workspace binding for status/refresh.
- [x] 13.6 Bound each projection difference list to 50 entries with total/digest/omitted metadata and adapter transitions to `deque(maxlen=64)`.
- [x] 13.7 Wire `DebugLogger` only for build/daemon startup/takeover, adapter crash/cooldown, lease/grace, and cleanup summaries; prove payload/source/secret redaction.
- [x] 13.8 Declare every directly imported external production dependency and add AST checks for direct dependency ownership.
- [x] 13.9 Recompute copied-symbol hashes from pinned `/data/CoordExp/external/serena`, require bidirectional census/manifest agreement, keep LOC informational with `maximum_production_lines=null`, and verify the official commit.

## 14. Introduce Managed Python and Versioned Daemon Rollover

- [x] 14.1 Install pinned CPython with `uv python install --install-dir /data/CoordExp/.codex/runtime/serena-light/python`; ensure the service venv and daemon executable do not resolve through `/root/.local/share/uv`.
- [x] 14.2 Launch daemon/LSP children with service-owned HOME, locked executable paths, minimal environment allowlist, and no proxy variables; bootstrap may retain external-network proxy.
- [x] 14.3 Implement the specified build-identity algorithm over packaged `.py`/`.mjs` runtime source bytes, dependency lock digest, public tool/schema version, and algorithm version.
- [x] 14.4 Move discovery, bearer, startup lock, nonce, and logs under `builds/<build_identity>` and make connectors attach only to an exact build.
- [x] 14.5 Require daemon-side identity recomputation and a connector-created one-time nonce before discovery publication; remove the ordinary bypassable daemon-start surface.
- [x] 14.6 Prove two builds, multiple clients, and two workspaces coexist; retire only a zero-holder, post-grace build and never delete successor discovery.
- [x] 14.7 Inspect the legacy v1 root through authenticated holder/status and exact PID+create-time evidence, but fail closed with `atomic_retirement_unsupported` and send no signal because v1 cannot freeze lease acquisition; never inspect or terminate canonical Serena processes.

## 15. Reaccept and Release v1

- [x] 15.1 Pass containment: a fresh real stdio client receives the withheld tool list, explicit clean/poisoned child environments do not affect loopback, borrowed holders are preserved, and no connector child or daemon descendant is added.
- [x] 15.2 Pass real connector contract tests for file create/change/delete/config change, symlink substitution, typed errors, document lifecycle, Unicode global ranges, and per-family isolation.
- [x] 15.3 Pass both in-process and isolated real service-executable rollover tests for two derived build variants, multiple clients, two workspaces, old-build lease preservation, no pre-grace exit, and exact test-owned zero-holder retirement; record that this proves process/slot mechanics, not immutable source-snapshot packaging.
- [x] 15.4 Pass pytest, Ruff, Ty, bootstrap, dependency/provenance gates, and strict OpenSpec with LOC reported but not gated.
- [x] 15.5 Pass `/data/CoordExp`, `cc-plugin-codex`, `/data/ms-swift`, and transformers in fresh Codex, Claude Code, and CC Agent sessions while retaining the working external-network proxy; separately pass exact clean and poisoned child environments through the real stdio connector so localhost traffic never depends on the model client's proxy.
- [x] 15.6 Update compatibility, registration, README, roadmap, and acceptance evidence with recorded environment/proxy preconditions.
- [x] 15.7 Restore agent-public `replace_symbol_body`, restart fresh clients, and rerun hash edit/release only after every preceding repair gate passes.
- [x] 15.8a Disposition the exact-`8f51d9e` Sol-xhigh and Opus-max HOLD findings: reconcile changed open documents before current-generation success, retain failed runtime-stop ownership and truthful pending status, translate ordinary LSP failures at the service boundary, and correct declaration-client evidence.
- [x] 15.8b Separate deterministic default tests from explicitly snapshot-bound mutable external-repository and performance acceptance; retain native TypeScript checks as external authority and preserve the 30-second production readiness budget without inventing a larger timeout.
- [x] 15.8c Make every public MCP tool discoverable and state the one-capture-group declaration regex contract in `tools/list`; return capture-count-specific `INVALID_INPUT` details.
- [x] 15.8d Disposition the exact-`7ba6773` Sol-xhigh and Opus-max HOLD findings: synchronously seal adapter admission, invalidate all affected families before watcher delivery, translate exhausted transport/process loss, bind ignored native TypeScript authority, and isolate stdio acceptance from shared production holders.
- [x] 15.8e Disposition the exact-`6fce244` Sol-xhigh and Opus-max HOLD findings: retry failed cleanup futures through retained sealed adapters, use service-owned Node/npm for native TypeScript authority, reclaim partial isolated stdio startup, and pass exact-build fresh Codex, native Claude Code, and CC Agent clients.
- [x] 15.8f Disposition the exact-`483e7a4` and `1962b48` Sol-xhigh HOLD findings plus Opus-max P3 findings: require two matching guarded byte passes for same-stat source/config freshness, fail closed before scan commit when the passes or path identities disagree without breaking stable deletion/symlink transitions, cover the caller-targeted non-Git branch, and preserve the typed Python-liveness error before reading retry/generation details.
- [ ] 15.8 Obtain independent Sol-xhigh static-correctness and Opus-max runtime/evidence audits; disposition every finding and clear all blockers.
- [ ] 15.9 Mark v1 PASS again, sync stable specs, and archive only after tasks 11–15 are complete; do not switch canonical Serena.
