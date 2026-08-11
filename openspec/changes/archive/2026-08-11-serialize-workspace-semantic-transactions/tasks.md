## 1. Contract-first regression

- [x] 1.1 Add a deterministic same-workspace concurrency test in `tests/unit/test_workspace_runtime_semantics.py` that starts multiple semantic reads together, pauses the first after it captures document generation, and asserts FIFO start order plus no sibling-induced `NOT_READY`; run the exact test against the current implementation and record the expected red failure.
- [x] 1.2 Add a deterministic read/edit ordering test in `tests/unit/test_workspace_runtime_semantics.py` that proves the edit preflight cannot run inside another read's preflight-to-postflight interval and that the stale expected hash still fails without writing; run it red before production edits.

## 2. Bounded workspace transaction owner

- [x] 2.1 Add one separately owned bounded single-worker transaction executor to `WorkspaceRuntime`, without changing the existing LSP executor algorithm or adding a dependency.
- [x] 2.2 Route `_fresh_read_envelope`, public `ensure_fresh`, and the complete guarded-edit preflight/commit path through that transaction owner while preventing recursive same-worker admission.
- [x] 2.3 Stop transaction admission before adapter cleanup, cancel queued entries, settle a running entry, and close both owned executors without orphan threads; add repeatable lifecycle coverage.
- [x] 2.4 Run the focused tests from 1.1 and 1.2 and the existing executor/runtime lifecycle suites green before continuing.

## 3. Saturation, isolation, and freshness invariants

- [x] 3.1 Add and run a queue-saturation test proving the fixed bound returns typed `BUSY` through `DaemonService` and rejected work never starts later.
- [x] 3.2 Add and run a queued-cancellation/shutdown test proving queued reads and edits never execute, while already-running work settles before runtime ownership is released.
- [x] 3.3 Add and run a two-runtime test proving a blocked semantic transaction on one root does not block semantic work, status, or lease heartbeats for another root.
- [x] 3.4 Re-run the existing external-write, response-witness, replay, stale-hash, timeout, and `UNCERTAIN` tests; any stale success, edit replay, or weakened failure authority is a release blocker.

## 4. Real connector acceptance and documentation

- [x] 4.1 Add a real Python daemon/connector acceptance that batches at least nine same-workspace reference/declaration calls on a stable repository and asserts no sibling-generation `NOT_READY`, complete results, and no transport failure.
- [x] 4.2 Run a companion acceptance with an actual concurrent external source write and assert the read replays to current bytes or returns the existing typed `NOT_READY`, never stale success.
- [x] 4.3 Update compatibility prose only where concurrency wording is made false by FIFO semantic transactions; the agent-facing README and client-registration freshness guidance remained true and required no task-owned edit.

## 5. Verification and release gate

- [x] 5.1 Run targeted pytest for every changed test file, then the full pytest suite, Ruff, Ty, bootstrap/source-ownership/provenance checks, and `openspec validate serialize-workspace-semantic-transactions --strict` using the repository's documented commands.
- [x] 5.2 Run a fresh-client smoke against `/data/CoordExp/serena-light` and one second Git workspace, confirm build-identity rollover, same-root shared runtime, cross-root isolation, zero new orphan processes, and unchanged canonical Serena.
- [x] 5.3 Review the final diff against proposal/spec/design, ensure no digest/schema/feature scope leaked in, mark every task with evidence, and proceed to the separately requested OpenSpec archive and Git finalization only after verification.

## Verification evidence

- Pre-implementation red: the two focused same-workspace tests both failed because the sibling read/edit entered before the first transaction released.
- Focused runtime regression: `138 passed` across `test_workspace_runtime.py` and `test_workspace_runtime_semantics.py`.
- Real connector regression: a nine-call same-root reference burst returned nine successes; a capacity-one public transaction queue returned typed `BUSY` and the rejected call never reached the language server.
- Real write races: three connector cases passed, covering settled replay and repeated-race `NOT_READY` without stale payload.
- Full local suite: `894 passed, 35 skipped` (snapshot/performance-gated), followed by live-snapshot Python acceptance on CoordExp, ms-swift, and transformers: `7 passed, 3 performance skips`.
- Static/runtime gates: Ruff and Ty passed; bootstrap check passed; source/provenance census passed with 9 copied hashes, bidirectional manifest agreement, no forbidden imports, and `maximum_production_lines=null`; strict OpenSpec validation passed.
- Fresh production client loaded build `d318ddf8010c76805f3d4a3944eb179b192dd4be4715a7b6c0dc151a734ac414`, bound `/data/CoordExp`, activated `/data/CoordExp/.worktrees/research-probes`, returned one real overview file, released immediately, and left no Serena Light daemon/LSP process behind.

Admission stop rule: stop implementation and report rather than weakening freshness or edit safety if complete transaction ordering requires removing preflight/postflight validation, replaying edits, hiding typed failures, changing public tools, or coupling different workspace identities.
