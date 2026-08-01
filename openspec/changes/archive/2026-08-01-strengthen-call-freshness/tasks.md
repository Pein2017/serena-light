## 1. Admission and Transaction Baseline

- [x] 1.1 Confirm `strengthen-call-freshness` is the only active owner of call-freshness behavior, record the implementation-start commit/build/schema identities, and verify that no production-code changes from either dependent change are present.
- [x] 1.2 Trace and record every current content-bearing read entrypoint, the shared in-flight scan state, read/edit envelope split, diagnostics `clean` path, adapter-process retry, semantic target replay, and targeted transformers freshness path before changing ownership.
- [x] 1.3 Add deterministic test barriers around call arrival, guarded scan start/finish, operation completion, and postflight so race tests do not depend on sleeps.

## 2. Per-Call Freshness Admission

- [x] 2.1 Add monotonic arrival tickets and a bounded FIFO scan owner to `FreshnessCoordinator`; make every content-bearing read run a guarded scan that starts after its own ticket is issued.
- [x] 2.2 Remove the contract that a later caller can accept an already-running scan, while preserving a single filesystem scan at a time, retained failed reconciliation batches, same-stat full-byte detection, and same-root activation refresh.
- [x] 2.3 Preserve Git create/change/delete/config/symlink attribution; keep targeted transformers scans for path-scoped reads and add bounded full-root scans only for existing global transformers reads.

## 3. Fresh Read Boundary

- [x] 3.1 Introduce one workspace-owned `run_fresh_read` transaction that performs per-call preflight, records the accepted identity/generations, executes the read while retaining internal response-owned byte witnesses, and performs a real guarded postflight.
- [x] 3.2 On a changed postflight, discard the complete source-derived success or error and replay the complete read once; on a second race, return rich retryable `NOT_READY` with reason `workspace_changed_during_read` and bounded attempt/generation evidence, without either attempt's payload, candidates, or raw ranges.
- [x] 3.3 Route every semantic navigation and file/symbol diagnostics success path, including diagnostic `clean`, plus source-derived missing/ambiguity/range/body errors through `run_fresh_read`; keep invalid/trust/adapter-condition errors typed, finite, and single-preflight.
- [x] 3.4 Keep `replace_symbol_body` and all future editing outside the read-replay owner; verify queued/running/installed commit states, timeout mapping, lost-response handling, and `UNCERTAIN` are unchanged and no edit callable can be replayed.
- [x] 3.5 Audit the combined freshness replay, adapter-process retry, and semantic target-stabilization loops and add assertions proving each bound terminates without an accidental nested unbounded loop.

## 4. Deterministic Correctness Tests

- [x] 4.1 Replace the old shared-scan unit expectation with a barrier test where call B arrives after scan A starts, waits, then runs a distinct scan begun after B's arrival.
- [x] 4.2 Cover stable file create, ordinary change, delete, rename, native-config change, ignored/tracked membership change, symlink substitution, and same-size/inode/timestamp byte rewrite between arrival, preflight, operation, and postflight.
- [x] 4.3 Cover one raced navigation/diagnostics read followed by successful replay and two consecutive races followed by retryable `NOT_READY`, asserting that no first-attempt body, range, reference, diagnostic, `clean` state, source-derived error candidate, or external raw location escapes.
- [x] 4.4 Cover a write after final guarded validation and prove the linearized result may return while the next call's own preflight observes the new bytes.
- [x] 4.5 Cover a same-tick write to bytes B followed by restoration of bytes A before postflight; prove an operation-owned B snapshot fails final A witness comparison and cannot escape.
- [x] 4.6 Cover adapter crash/retry, target-snapshot replay, client cancellation, queue saturation, cooldown, and freshness failure combinations with finite exact invocation counts.
- [x] 4.7 Cover explicit transformers file queries using targeted pre/post validation, global transformers queries using bounded full-root pre/post validation, and cross-root trusted-external targets using bounded guarded byte witnesses around the authoritative response and final return.
- [x] 4.8 Remove cold TypeScript semantic test-order dependence: use only a bounded trusted-workspace LSP owner hint, retain the hinted owner's bytes as an internal freshness witness without adding it to public targets, preserve authoritative two-response definition/reference results, and cover declaration and references as isolated first calls against the real configured-program root.
- [x] 4.9 Cover source snapshot acquisition failure before a witness exists, including one-race replay and bounded repeated-race failure, and make optional TypeScript owner hints fall back on any typed LSP response error.

## 5. Real-Daemon Acceptance and Documentation

- [x] 5.1 Run a real daemon/connector race harness with a separate writer process and explicit barriers against Python and TypeScript bodies, references, Unicode ranges, diagnostic `clean`/findings, and trusted-external raw-location races.
- [x] 5.2 Run targeted fresh-client smokes on the final build in `/data/CoordExp`, `/data/CoordExp/external/codexUI`, `/data/ms-swift`, and the conda-`ms` transformers package, including explicit cross-root `activate_workspace` and same-root reactivation; retain latency only as a two-sample minimum/maximum observation with no pass threshold or statistical interpretation.
- [x] 5.3 Verify multiple Codex/Claude/CC sessions sharing one root, different roots concurrently, clean and poisoned-proxy environments, daemon rollover, lease release, and zero new orphan language-server processes.
- [x] 5.4 Update compatibility, README/freshness documentation, roadmap state, and acceptance evidence with the repaired final linearization boundary and final environment/build identities; do not advertise watcher or delivery-time guarantees.
- [x] 5.5 Re-pass full pytest, Ruff, Ty, bootstrap, source ownership/direct-dependency/provenance/census checks, copied-source hashes, and strict OpenSpec validation on the repaired tree; report production LOC with `maximum_production_lines=null`.
- [x] 5.6 Stop and return to design review if correctness requires an authoritative background watcher, persistent content index, cooperative external-writer lock, filesystem snapshot, edit replay, public generation metadata, or Serena agent/mode/project-server subsystems.
- [x] 5.7 Obtain independent correctness and runtime-evidence review, disposition every blocker, re-run affected gates, then sync and archive this change before any `add-lexical-discovery` implementation begins.
