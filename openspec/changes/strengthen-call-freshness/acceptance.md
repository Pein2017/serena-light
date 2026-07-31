# Implementation Evidence

## Implementation start

- Change owner: `strengthen-call-freshness` is the only active owner of
  call-freshness behavior. `add-lexical-discovery` and
  `improve-warm-runtime-reuse` remain planning-only dependencies and MUST NOT
  enter production until this change is accepted and archived.
- Planning baseline: `572526f0d68959ed6586cabc82f6745119183f78`.
- First accepted implementation commit: `08a4041b7be1e77206c76858044084c14c5fc760`.
- Implementation-start production tree: unchanged from `f7dbf12e0f337e8e24270960d4756fd87ed393cf`;
  the planning baseline added only the three OpenSpec change directories.
- Public tool schema: `3`; build-identity algorithm: `3`.
- Last accepted pre-change build identity recorded by compatibility evidence:
  `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`.
  A new identity will be computed from the accepted implementation rather than
  reusing this value.

## Pre-change read ownership trace

`src/serena_light/connector.py::READ_ONLY_TOOLS` exposes eight read-only calls.
`get_runtime_status` is bounded control-plane status and is not content-bearing.
The remaining seven public names are content-bearing:

| Tool | Pre-change runtime owner | Required new owner |
|---|---|---|
| `get_symbols_overview` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction |
| `find_symbol` | `WorkspaceRuntime._tool_envelope`; file, directory, and global branches inside the operation | path- or root-scoped fresh-read transaction |
| `find_referencing_symbols` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction |
| `find_declaration` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction |
| `find_implementations` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction |
| `get_diagnostics_for_file` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction, including `clean` |
| `get_diagnostics_for_symbol` | `WorkspaceRuntime._semantic_envelope` | path-scoped fresh-read transaction, including `clean` |

Before `08a4041`, `FreshnessCoordinator.ensure_fresh` admitted one `_SharedScan`:
a later caller joined and accepted the result of an already-running scan.
`08a4041` replaced that owner with monotonic FIFO arrival tickets and one scan
per caller. The same coordinator still owns Git inventory rebuild,
create/change/delete/config/symlink reconciliation, generation advancement,
watched-file delivery, and pending-reconcile retry.

Before the fresh-read refactor, `WorkspaceRuntime._tool_envelope` owned one
preflight plus exception-to-envelope mapping. `_semantic_envelope` added path
routing, while global `find_symbol` and `replace_symbol_body` both called the
same `_tool_envelope`. The refactor must therefore preserve one explicit edit
preflight when it removes freshness from generic error mapping; editing must
never enter read replay.

Diagnostics publication is loaded through
`WorkspaceRuntime.load_diagnostics` and normalized by
`src/serena_light/tools/diagnostics.py`. A successful `clean` state is
source-derived authority and therefore needs the same postflight as findings;
stale, not-ready, timeout, cooldown, and other typed errors do not become
success through replay.

Semantic references, declarations, and implementations already have a bounded
target-stabilization owner in
`WorkspaceRuntime._stabilize_semantic_locations`: it issues exactly two
adapter requests, binds response-owned targets, and rejects changed adapter or
target identities. The new outer filesystem transaction must remain bounded to
two complete attempts and must not turn this inner two-request stabilization or
adapter-process retry into an unbounded nested loop.

Before `a668f87`, the allowlisted read-only transformers root received one
targeted stat from `WorkspaceRuntime._route`, but it had no matching
postflight and global reads had no honest full-root freshness owner.

## Accepted fresh-read implementation slices

- `8b517430af2c702f240493284e6f5e20d2c36693` moved all seven public
  content-bearing navigation and diagnostics paths through one bounded Git
  fresh-read transaction. Each successful attempt owns a preflight, exact
  response-byte witnesses, and a real postflight; one changed attempt is
  discarded and replayed, while a second change returns retryable `NOT_READY`
  with reason `workspace_changed_during_read`. Editing remains outside this
  boundary.
- `a668f87` extended the same transaction to the allowlisted non-Git
  transformers root. An indexed file uses targeted preflight and postflight;
  response-owned witness paths join the postflight set. Global, directory, and
  not-yet-indexed path queries use the existing bounded no-symlink root
  inventory because targeted stats cannot prove membership. Targeted and root
  scans share the same FIFO admission queue and publish the latest completed
  scan, including a clean postflight.
- Same-root reactivation of a Git workspace retains its immediate ticketed
  refresh. Reactivating the non-Git transformers root remains a control-plane
  no-op: it returns no source-derived content, and the next content-bearing
  file or global query performs the authoritative scoped preflight. This avoids
  adding a full-package digest walk to reactivation without weakening any tool
  success.

## Non-Git deterministic evidence

The accepted tests prove exact targeted pre/post observations without a root
walk, one-race replay to settled body/range/hash, two-race payload suppression,
same-stat B-to-A witness rejection, bounded global root scans, create/change/
delete/symlink membership reconciliation, directory membership discovery,
missing-target fail-closed behavior, and edit non-replay. The settled-body test
was falsified by temporarily bypassing targeted postflight and correctly failed
on the first-attempt body before the production file was restored byte-for-byte.

Lead verification after `a668f87`:

- focused runtime and bounded-freshness tests: `67 passed`;
- full `tests` suite: `819 passed, 31 skipped` in 185.83 seconds;
- Ruff and Ty on all three changed files: pass;
- `git diff --check`: pass.

The skipped tests require recorded external snapshots or opt-in performance
inputs and remain part of the later real-root acceptance tasks; they are not
counted as completed evidence here.
