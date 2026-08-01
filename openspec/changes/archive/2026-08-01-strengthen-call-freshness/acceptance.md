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
authoritative adapter requests, binds response-owned targets, and rejects
changed adapter or target identities. A cold TypeScript definition/reference
may first issue one optional bounded `typeDefinition` preparation hint. The new
outer filesystem transaction must remain bounded to two complete attempts and
must not turn this inner stabilization or adapter-process retry into an
unbounded nested loop.

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

## Bounded retry composition evidence

The retry census after `a668f87` found three independent finite owners rather
than one nested retry loop:

- the outer fresh-read transaction runs at most two complete attempts;
- semantic target stabilization issues exactly two authoritative native
  requests per outer attempt, plus at most one optional TypeScript preparation
  hint;
- the adapter runtime executes read-only work at most twice after transport or
  process loss, while edit work executes once.

Existing tests already prove two outer attempts multiplied by two semantic
requests (`calls == 4`), typed trust and readiness failures returning after one
preflight, queue saturation remaining pending until a later call, diagnostics
cancellation ownership, cooldown/timeout preservation, and edit non-replay.
`22be317` added the missing hard-ceiling test: two consecutive read-only
transport losses raise the second failure after exactly two client starts and
two recorded crashes. `08c440b` strengthened the COLD, COOLDOWN, and
UNSUPPORTED reference cases to prove one preflight and zero reference requests
per typed failure.

Both additions were falsified against temporary production weakenings: raising
the adapter retry allowance to three made the transport test fail, and forcing
an `ErrorEnvelope` through read postflight made the typed-failure scan count
fail. Production sources were restored before acceptance. Lead verification of
the three owning unit files was `174 passed`; Ruff, Ty, and diff checks passed.

No unit test composes the real adapter transport retry inside the outer
workspace transaction because the workspace tests use protocol fakes and the
adapter tests own the real transport runtime. Constructing retry behavior in a
fake would duplicate the implementation. The owners are independently bounded
and meet through one opaque future; a real-daemon fault case remains part of
the later acceptance stage rather than a new parallel retry model.

## Deterministic Git race and membership evidence

The task-4.2 census traces rename handling to the same `created`/`deleted` set
difference already exercised by the stable membership tests; there is no
rename-specific production branch. Existing tests also cover native-config
restart, source symlink rejection, and same-size/inode/timestamp byte changes
at both scan and final-witness boundaries.

`96ff397` closed the two remaining distinct gaps:

- a pure Git ignore-rule transition now proves tracked-to-ignored and
  ignored-to-inventory membership changes without deleting or creating the
  file bytes; both changes advance family attribution;
- a Git file-scoped `find_symbol(include_body=True)` race now proves that the
  returned body, range, and hash all belong to the settled second attempt.

The membership test was falsified by temporarily removing
`--exclude-standard` from Git inventory discovery, and the raced-body test by
temporarily bypassing read postflight. Each became red for the intended reason,
and both production files were restored byte-for-byte. Together with the
existing one/two-race navigation and diagnostic-clean tests, semantic target
stabilization tests, response-witness restoration test, and exact scan counts,
this closes deterministic tasks 4.2 and 4.3. Lead verification of the three
owning files was `127 passed`; Ruff, Ty, and diff checks passed.

Real `git mv` behavior with live open documents remains a real-daemon
acceptance concern, not a separate unit mechanism: the coordinator intentionally
models it as one deletion plus one creation.

## Real daemon/connector race evidence

`a98a997` adds a real `uvicorn` loopback HTTP daemon (`create_daemon_app`)
exercised through the production `Connector`, `DaemonSession` protocol,
`WorkspaceDaemonService`, and `WorkspaceRuntime` operating on a real Git
workspace root, plus a spawned writer process connected only by a duplex
pipe. Only the language server itself is a deterministic fake: adapter/LSP
replies are scripted, not real Pyright or tsserver processes. Every
freshness, ordering, and publication decision asserted here is still made by
production code against that fake, not by a test double standing in for the
production boundary. Each race uses an explicit answer-produced barrier: an
attempt first owns its document snapshot and fake-adapter result, the foreign
process completes and acknowledges the rewrite, and only then may read
postflight continue. The harness therefore does not infer ordering from
sleeps.

Five real connector/runtime-boundary cases cover:

- a Python symbol-body race that replays to one settled body, range, and hash;
- a TypeScript astral-Unicode rewrite whose settled source position is mapped
  from the final snapshot;
- two consecutive Python races that return retryable `NOT_READY` without any
  attempted source body in the serialized error;
- a reference-target rewrite that returns only the settled container, snippet,
  and range; and
- diagnostics changing from clean to findings during the first attempt, with
  only the final findings state published.

The writer exit code and cleanup are asserted. The cases were falsified by
temporarily suppressing the production postflight change result; all five
failed for their intended stale-result reason before the production source was
restored byte-for-byte. Lead verification was `19 passed` for the full
connector-contract acceptance file, plus Ruff, Ruff format, Ty, source-diff,
and `git diff --check` success.

The requested ms-swift root was corrected from the nonexistent
`/data/CoordExp/ms-swift` to the live Git root `/data/ms-swift`; no symlink or
repository relocation was introduced.

## Real-root and latency evidence

The external-root matrix was pinned immediately before execution:

```text
SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT=git:deff2f5d117dbe9f9c47e7cd8d5fe3407f1469f7:2b2860a444149de1c69f6ad70dc7156d672f5486e0bb2cb9cbb6a3755fe45a90
SERENA_LIGHT_COORDEXP_SNAPSHOT=git:0490f73b56b352826de5f4b3e697575037582718:107c365432ff36acd2c2af7309487c4dc013108935563951e42bd3e9b0140310
SERENA_LIGHT_MS_SWIFT_SNAPSHOT=git:f2797138dba0e224cfff735cd89a528a08d8732a:45696b3ae91193e921ccb9b1dbd5b33c27b7462d4b6281801d22f90d825de19a
SERENA_LIGHT_TRANSFORMERS_SNAPSHOT=transformers:4.57.1:4880a9c5bf65f2bb124b7739c74991c1bc2aaf7755133b7fa77ce1e017745dcf
```

`900ea93` adds observation-only navigation and diagnostics timing on the live
`/data/CoordExp` and `/data/ms-swift` Git roots. Each operation has exactly two
samples; the underlying test code labels them via the standard nearest-rank
formula (index `ceil(rank/100 * n)`), which sorts the two values before
ranking and for `n=2` reduces exactly to the sample minimum (its "p50") and
sample maximum (its "p95"). This is a two-sample nearest-rank minimum/maximum
observation, not a statistical percentile: it carries no distributional
meaning, must not be used for downstream sizing or regression inference, and
which of the two calls ran first is not recorded. The table below reuses that
same minimum/maximum framing, asserts only the expected symbol and path for
global and scoped lookup, and has no latency pass threshold. The recorded
seconds were:

| root | operation | minimum | maximum |
|---|---|---:|---:|
| `/data/CoordExp` | global symbol | 12.53 | 33.95 |
| `/data/CoordExp` | scoped symbol | 10.96 | 11.60 |
| `/data/CoordExp` | overview | 11.57 | 11.74 |
| `/data/CoordExp` | diagnostics | 11.00 | 11.04 |
| `/data/ms-swift` | global symbol | 1.49 | 15.21 |
| `/data/ms-swift` | scoped symbol | 0.46 | 0.87 |
| `/data/ms-swift` | overview | 0.47 | 0.55 |
| `/data/ms-swift` | diagnostics | 0.47 | 0.47 |

The larger CoordExp numbers reflect the deliberately authoritative per-call
Git scan and are evidence for the later, separately owned warm-runtime change;
they are not a failure of this correctness change. The worker's complete
snapshot-bound Python file passed `8 tests` in 331 seconds. Lead review then
strengthened the symbol assertions, caught and corrected a test-only scoped
envelope path mistake, and independently reran both timing cases: `2 passed`
in 158.78 seconds. Ruff and Ty passed.

### Superseded host-client evidence

Fresh Serena Light-only CC/Claude sessions selected the then-current production build
`1a940728c705c5b1b2f460ec1950884727c91d4ddcebfb98a025171c88cff5cd`
and shared daemon `cfb31059-c2d5-48b1-8e5d-afb0e020aa0b`. The Sonnet session
queried `PipelinePlanner` and current diagnostics in `/data/CoordExp`, switched
to `/data/CoordExp/cc-plugin-codex`, found TypeScript-language-server symbols,
and repeated identical current diagnostics after same-root reactivation. The
Opus session resolved `GenerationConfig` from `/data/ms-swift` into the
read-only transformers package, switched to the transformers root, and repeated
a scoped `Qwen2VLForConditionalGeneration/forward` read after same-root
reactivation. Typed ambiguity and unavailable external `include_info` were
reported explicitly rather than silently degraded. Both sessions released
their bindings with `runtime_stop_pending=false` and observed no stale result.

The fixed-snapshot TypeScript real acceptance separately passed `6 tests` in
10.87 seconds, covering `/data/CoordExp/cc-plugin-codex`. The implementation
and TypeScript authority root subsequently changed, so these receipts remain
historical evidence only: they do not close task 5.2 or the final-build host
matrix. Current acceptance uses `/data/CoordExp/external/codexUI` and must not
inspect or depend on the retired acceptance root.

## Shared-client and lifecycle evidence

`1eda55d` adds a real locked-service connector test with three independent
stdio clients, two temporary Git roots, and one isolated build slot. It proves
that two clients share one root and daemon, a second root coexists, one lease
can switch cross-root and reactivate the same root without replacing the daemon,
and releasing one or two leases cannot retire a daemon still held by another
client. With zero holders, the exact PID+create-time daemon and all four
observed locked-runtime language-server descendants retire, and its discovery
and bearer files disappear. One client inherits a fully poisoned proxy
environment while test-side loopback HTTP uses `trust_env=False`.

Lead verification of that test was `1 passed` in 14.30 seconds; the worker also
ran it three times and in combination with rollover and stdio proxy acceptance
for `6 passed` in 52.49 seconds. Existing supporting evidence passed as well:
real versioned rollover `1 passed`, stdio clean/poisoned proxy `4 passed`, lease
lifecycle `17 passed`, and parent-death cleanup `5 passed`. The test records and
cleans only identities it created; it performs no name-based or broad process
kill.

A read-only process census also identified 14 pre-build-slot flat-layout
legacy daemons that predate current lifecycle ownership, have no established
connections, and cannot be discovered by current connectors. They are an
explicit pre-existing baseline, not newly created task-5.3 orphans. At the
time of that census, build-slot daemons `92b2618e...` and `1a940728...` had
real holders and remained untouched. Any manual cleanup of the legacy set needs
separate authorization and a fresh exact PID+create-time/connection check.

The generic multi-client, multi-root, proxy, rollover, release, and zero-new-
orphan portions of task 5.3 have real process evidence. The final-build
Sol-xhigh and Opus-max receipts recorded below close the four-root host matrix;
the superseded pre-audit CC/Claude receipt remains historical context only.

## Documentation and complete gate evidence

The earlier documentation pass at `24ae46c` described candidate build
`1a940728c705c5b1b2f460ec1950884727c91d4ddcebfb98a025171c88cff5cd`.
The public schema remains `3`, the dependency lock digest remains
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`,
and canonical Serena remains unchanged. The agent-facing text states the FIFO
admission and final guarded byte-observation boundary, explicitly denies a
background-watcher or response-delivery-time guarantee, records the Git versus
non-Git validation split, and keeps editing outside replay. The live ms-swift
path is `/data/ms-swift`.

Those superseded-candidate pre-review gates passed:

- default full suite: `828 passed, 33 skipped` in 204.89 seconds; every skip
  was an explicit external-snapshot or performance gate;
- four-snapshot full suite: `858 passed, 3 skipped` in 401.01 seconds; only the
  three explicit performance observations were skipped, and the two new
  latency cases had already passed separately;
- observation-only latency cases with strengthened semantic assertions:
  `2 passed, 6 deselected` in 158.78 seconds;
- Ruff on `src`, `tests`, and `scripts`: pass;
- Ty on the repository: pass;
- locked service-runtime bootstrap check: pass with CPython 3.12.12, Node
  22.22.0, Pyright 1.1.403, TypeScript 5.9.3, and
  typescript-language-server 5.1.3 under the service-owned runtime;
- source ownership, direct dependency, forbidden import, census/manifest, and
  copied-source provenance: pass, including all 9 copied hashes against Serena
  commit `9a9d07e83d8c1cba3458992707f440c624446c6d`;
- production LOC: 18,569, informational only with
  `maximum_production_lines=null`;
- strict OpenSpec validation and compatibility JSON/public-contract tests:
  pass.

The suite emits one pre-existing Starlette/httpx deprecation warning; it does
not affect correctness or the public contract.

## Current candidate gate evidence

The active source-only rollover is
`7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80`.
It replaces the retired TypeScript acceptance root with the pinned snapshot of
`/data/CoordExp/external/codexUI` and removes cold first-call test-order
dependence for TypeScript declaration/reference queries. The internal bounded
`textDocument/typeDefinition` owner hint may only open a trusted workspace
owner; the two authoritative definition/reference responses still own the
public result, and malformed, unsupported, external, or untrusted hints cannot
enter it.

Current pre-review gates pass:

- four-snapshot full suite: `875 passed, 3 skipped` with `7 warnings` in
  439.52 seconds; the only skips were the three explicit opt-in performance
  cases, which passed separately as `3 passed, 875 deselected` with `4
  warnings` in 190.37 seconds, for 878 passing cases in total;
- five-run readiness/admission probe: `20/20` pass across `coordexp`,
  `codexui`, `ms-swift`, and `transformers`; every run reports stable inventory
  and `cleanup_ok=true`, and the rendered Section-1 report is `PASS`;
- explicit TypeScript scope probe: overall `PASS`; the codexUI native program
  remains inside lexical trust, while the ignored/symlink fixture is rejected
  with the expected typed `SCOPE_INCOMPATIBLE` evidence and cleans up;
- Ruff on `src`, `tests`, and `scripts`: pass;
- Ty on the repository: pass;
- locked service-runtime bootstrap materialize/check: pass with service-owned
  CPython 3.12.12, Node 22.22.0, Pyright 1.1.403, TypeScript 5.9.3, and
  typescript-language-server 5.1.3;
- source ownership, direct dependency, forbidden import, census/manifest, and
  copied-source provenance: pass, including all 9 copied hashes against Serena
  commit `9a9d07e83d8c1cba3458992707f440c624446c6d`;
- production LOC: 18,868, informational only with
  `maximum_production_lines=null`;
- strict OpenSpec validation and compatibility JSON/public-contract tests:
  pass.

The warnings are the existing Starlette/httpx deprecation and pytest xUnit2
`record_property` notices.

### Superseded pre-audit CC host receipt

A fresh CC Agent running Sonnet-high selected pre-audit build
`442987ed9cc4520743d4a79c880a6a19231d86a8628ae482108277ce00af38a1`
and daemon `f38fc80c-1d1e-497c-9f0e-7fb6a3bcb66b`. One lease activated
`/data/CoordExp`, `/data/CoordExp/external/codexUI`, `/data/ms-swift`, and the
read-only conda-`ms` transformers root; both CoordExp and transformers
same-root reactivations returned identical current symbols. The cold codexUI
declaration resolved `normalizeCodexApiError` to `src/api/codexErrors.ts` and
reported 11 references. The ms-swift declaration resolved
`GenerationConfig` into the read-only transformers package. Final immediate
release reported zero holders, `runtime_stopped=true`, and
`runtime_stop_pending=false`. No file was edited, canonical Serena was not
called, and `/data/CoordExp/cc-plugin-codex` was not inspected.

The receipt predates the final audit repairs and therefore does not close the
current build.

### Final-build dual-audit and host receipts

Fresh, independent Sol-xhigh and Opus-max sessions both selected build
`7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80`
before querying. Each used Serena Light only, activated `/data/CoordExp`,
`/data/CoordExp/external/codexUI`, `/data/ms-swift`, and the read-only conda-`ms`
transformers root, repeated same-root activation, resolved the cold first-call
TypeScript declaration from `src/api/codexGateway.ts` into
`src/api/codexErrors.ts`, resolved an ms-swift import into transformers, and
ended with `active_holders=0`, `runtime_stopped=true`, and
`runtime_stop_pending=false`.

Sol-xhigh closed the prior source-`OSError` postflight/replay blocker and the
optional TypeScript hint error, then returned **PASS** with no P0/P1/P2. It
recorded one deferred P3: `_source_exception_path` accepts a bytes-valued
`OSError.filename` although `Path(bytes)` is invalid; supported production
operands are strings/`Path`, so this branch does not affect the accepted build.
Opus-max independently verified both runtime repairs, the full recorded
evidence, exact process ownership, corrected minimum/maximum labels, and both
OpenSpec replacement deltas, and returned runtime **PASS** with no P0/P1. Its
archive-gate concern was closed by retaining all stable scenario names and by
the final sync/archive execution. The agent-facing wording and cold-TypeScript
request census were corrected before archive.

The final verdict is **PASS**. Canonical Serena remains unchanged; no reviewer
edited a source library or inspected `/data/CoordExp/cc-plugin-codex` content.

No correctness result required an authoritative background watcher, persistent
content index, cooperative external-writer lock, filesystem snapshot, edit
replay, public generation metadata, or Serena agent/mode/project-server
subsystem. Task 5.6 therefore passes without invoking its design-review stop;
lexical discovery and warm-runtime reuse remain later independent changes.
