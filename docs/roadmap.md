# Serena Light Roadmap

This document is a non-normative execution map. Behavioral requirements and
acceptance criteria are owned by OpenSpec. If this roadmap disagrees with an
active OpenSpec change, OpenSpec wins.

## Current state

Archived change `2026-07-29-build-serena-light-v1` was the sole repair owner. Its 2026-07-28 PASS was
superseded by HOLD. The 2026-07-29 Sol-xhigh and Opus-max audits found new
release-blocking correctness and evidence gaps. Those gaps were repaired, v1
reached `PASS — V1 ARCHIVED`, and both follow-up ablation changes have since
been accepted and archived: `fix-position-and-coverage-contract`
(Phase C), then `compact-success-schema` (Phase D). Serena Light remains
registered in parallel under `serena-light`; canonical `serena` is unchanged.
Guarded editing (`replace_symbol_body`) is restored after reacceptance.

`strengthen-call-freshness` (Phase E below) is accepted and archived at build
`7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80`.
The unimplemented `add-lexical-discovery` change was retired at 0/31 tasks:
host `rg`/`find` remains the lexical-discovery owner and Git history preserves
the removed planning artifacts. The unimplemented
`improve-warm-runtime-reuse` change was likewise retired at 0/26 tasks because
its prerequisite was removed and repeated measured cold-start friction has not
established a need for its warm pool, RSS accounting, or prewarm scheduler.

The archived `tighten-agent-interaction` revision advanced the public schema to
4 for compact overview/reference/diagnostics presentation and source-owned MCP
initialize guidance. The archived `tighten-query-recovery` source-only revision
keeps schema 4 and build-slot isolation while shortening repeated initialization
metadata and adding two closed, error-only Agent recovery actions. Its final
build `78de9eae...` passed the 880-test fixed point, static/provenance/bootstrap
and rollover gates, plus fresh Codex/Luna-medium and Claude Code/Haiku clients.
It does not introduce lexical MCP tools, diagnostics hooks, RTK coupling,
workspace or symbol guessing, or successful-response changes.

## Phase A: contain and repair v1

1. Contain guarded editing and isolate loopback traffic from ambient proxies.
2. Repair synchronous freshness, lexical trust, symlink safety, and edit commit
   state semantics.
3. Repair typed envelopes, document lifecycle, positions, symbol lookup,
   per-language isolation, bounded status, logging, dependency ownership, and
   provenance.
4. Install service-owned CPython and introduce reproducible build identities,
   versioned daemon slots, nonce-authorized startup, coexistence, and retirement.

Gate: all tasks 11–14 in the archived change pass at their declared evidence
layer. Unit/in-process, real connector plus deterministic LSP, real daemon fault,
and real LSP/repository evidence must be named separately; only the stdio gate
currently combines the production connector executable with the shared daemon.
No canonical-name change is permitted. The current candidate also reconciles
changed open documents before generation success, retains and reports failed
runtime-stop ownership, translates LSP failures at the service boundary, and
snapshot-isolates mutable external acceptance. Current-build client acceptance
and independent reaudit are complete. Stable specs are synced and the change is
archived. See [the final acceptance record](../openspec/changes/archive/2026-07-29-build-serena-light-v1/final-acceptance.md).

## Phase B: reaccept and release v1

Run fresh Codex, Claude Code, and CC Agent acceptance across `/data/CoordExp`,
`/data/CoordExp/external/codexUI`, `/data/ms-swift`, and the pinned transformers
package while retaining the model clients' required external-network proxy.
Run clean and poisoned environments directly through the real stdio connector
to test the localhost boundary. Restore `replace_symbol_body` only after its
complete fault matrix passes. Then obtain
independent `sol-xhigh` static-correctness and `opus-max` runtime/evidence
audits, disposition every finding, mark v1 PASS, sync stable specs, and archive.

**Progress as of 2026-07-29:** exact-head audits at `483e7a4` confirmed all
earlier cleanup, authority, stdio, and client-evidence blockers closed. Opus-max
passed the runtime/evidence repair; Sol-xhigh first reproduced a stat-only
same-size rewrite miss, then showed that one streaming pass could still miss a
concurrent rewrite to an already-read region. Build
`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`
requires two matching guarded byte passes for every trusted Git source and
native config, fails closed before scan commit on unstable reads, covers the
caller-targeted transformers non-Git branch, and documents the operation
linearization boundary. The deterministic default suite passes 543 tests and
explicitly skips 22 snapshot-gated external tests; the snapshot-bound complete
suite passes 564 tests with only the opt-in performance case skipped.
Fixed-snapshot TypeScript acceptance/integration/admission passes 23, Python
production acceptance passes 5 with one intentional performance skip, real
Pyright integration passes 2, and three isolated transformers performance runs
remain below 40 seconds. Fresh Codex/Terra, native Claude Code/Sonnet, and CC
Agent/Sonnet pass this exact build across all four roots plus isolated guarded
edit/restore. Sol-xhigh and Opus-max both returned PASS on exact clean commit
`c2dffca`; stable-spec sync and archive are complete, and only the authorized
push remains pending.

Gate: every blocker is cleared and task 15 is complete. The authorized GitHub
push follows the archive. No canonical Serena switch is part of this roadmap.

## Phase C: repair post-archive semantic correctness

Implement and accept `fix-position-and-coverage-contract`. Standardize all
public navigation and diagnostic positions as 0-based decoded-text
line/Unicode-column values, recover complete assignment bodies instead of
identifier-only constants, and disclose bounded configured-program coverage on
semantic reference success. Keep semantic references pure LSP results and keep
the existing verbose success shape during this phase.

Gate: real connector/MCP acceptance across Unicode/CRLF/BOM, Python and
TypeScript, excluded native-program files, multi-session/multi-root reuse, and
guarded-edit regression; then Sol-xhigh and Opus-max review, stable-spec sync,
and archive.

**Progress as of 2026-07-30:** predecessor build
`b3b9952e7abcbca7554c8572499c5541888f6ecf3661fe8787dbde629a258f33`
passed the snapshot-bound 714-test suite, static/bootstrap/provenance gates,
clean and poisoned connector coverage, and fresh Codex, native Claude Code,
and CC Agent inspection, including a real TypeScript guarded edit/restore. The
repaired candidate adds exact runtime/capability-owned semantic replay,
process-owned diagnostic/document state with late-publication recovery, a
complete workspace-plus-external target cap, and shared read/edit assignment
recovery after the Sol-xhigh and Opus-max HOLD audits. The final b3b Serena
Light correctness-ablation arms score 99.5 (Sol/high) and 99.25 (Opus/high);
both independently derived the correct `YES / YES / NO / NO` verdicts, and Sol
ranks first by a small efficiency margin. This does not
establish a response-efficiency winner. The frozen-tree audit first exposed an
owner-after-`didChange` handoff race. Successor `481c45e...` installed the
owner first, but its exact-build Sol-xhigh and Opus-max audits then established
that the locked TypeScript server publishes no document version and reproduced
a stale old publication crossing into the new owner. Candidate build
`22c80421...` used a close/reopen epoch for unversioned diagnostics, but exact
Sol-xhigh review found that notification delivery was not a server-processing
barrier and Opus-max reproduced false `CLEAN` in six of six real product-seam
trials. Sol-xhigh also found that assignment recovery trusted a candidate name
without proving that the selected snapshot text named that binding. Build
`e26ccf65...` then disowned tracking, sent `didClose`, awaited a bounded
same-connection response, and only then installed the new owner before
exact-full-text `didOpen`; it also required the exact selected text to equal the
candidate name. Its full fixed-snapshot and three-client receipts passed, but
final Sol-xhigh and Opus-max audits found a remaining exact-retry gap: after the
first barrier timed out, the URI no longer looked open and retry skipped the
barrier, permitting a late close `CLEAN` to consume the new generation.

Build
`ecc4689b781c2de8c4bf03788a4dc17388c28e402220b99519294f31010dc358`
keeps process-tokened undrained-close state independent of open ownership,
retains it across timeout/response failure, and clears it only after a
successful same-connection barrier or with a dead process. It also drains all
recorded closes before any later open and bounds LRU/watched/create markers.
The adapter suite passes 52 tests, the unit suite passes 589 tests, six real
pinned-TSLS timeout-retry trials return TS2322 `FINDINGS`, and a focused
Sol/high checker found no causal or boundedness defect. The full fixed-snapshot
suite passes 742 tests with one intentional performance skip. Exact-current
Codex, hooks-isolated native Claude Code, and CC Agent clients pass both hash
edit/restore and the four-root semantic matrix. Its final Sol-xhigh lane passed,
but Opus-max reproduced a production watcher timeout/recreate chain that left a
close marker overlapping local-open ownership; cached diagnostics reuse then
accepted a close-empty publication as sticky false `CLEAN`.

The current repair build
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`
skips watcher-created temporary lifecycle for an already-owned URI, drains any
surviving marker before cached diagnostics owner retention, and fails retryably
while retaining the marker when that barrier does not complete. The exact
two-file chain, focused unit suites, and the 12-test real pinned-engine file
pass. The four-snapshot suite passes 747 tests with one intentional performance
skip, and fresh Codex, native Claude Code, and CC Agent clients pass the
affected diagnostics surface. Final Sol-xhigh and Opus-max audits both PASS;
stable specs are synchronized and Phase C is archived. A pre-existing loud
untyped transport/protocol-error path is recorded as non-blocking follow-up.

## Phase D: compact agent-facing navigation success

Phase C is archived, so implement `compact-success-schema`. Group
results by file, remove repeated success-only runtime metadata, use compact
ranges and kinds, add bounded `max_matches`, and enforce `max_answer_chars` on
the actual MCP text content. Preserve rich errors and leave status,
diagnostics, editing, workspace lifecycle, and control-plane contracts intact.

Gate: deterministic client-visible character reductions on fixed fixtures with
identical semantic evidence, fresh Codex/Claude Code/CC Agent acceptance, and a
locked four-arm Serena-versus-Serena-Light ablation where each arm exposes only
its assigned MCP while retaining recorded shell fallback. Finish with
Sol-xhigh and Opus-max review, stable-spec sync, and archive.

**Progress as of 2026-07-30:** fixed-fixture character gates and the locked
four-arm ablation are complete. The first Sol-xhigh and Opus-max pass found
workspace raw-coordinate fallback, overview structural pruning, stable-prefix
minimum, multi-adapter authority, public ambiguity-budget, and response-owned
implementation metadata defects. Build
`92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
repairs those contracts, passes 143 combined focused repair tests, the 821-test
snapshot-bound suite, Ruff, Ty, bootstrap, provenance/census, strict OpenSpec,
and the opt-in transformers performance case. Fresh Codex, native Claude Code,
and CC Agent clients pass the repaired schema and release cleanly. The final
Sol-xhigh static and Opus-max runtime/evidence audits both pass with no P0/P1
findings. Stable specs are synchronized and Phase D is archived as
`2026-07-30-compact-success-schema`.

## Phase E: strengthen call freshness and keep lexical discovery host-owned

Phase D is archived. `strengthen-call-freshness` was implemented and accepted.
The later lexical MCP proposal was retired before implementation because host
`rg`/`find` already provides the Agent-facing file/text discovery path without
duplicating tool schemas or daemon lifecycle. The speculative warm-runtime
proposal was also retired before implementation. Any future performance work
starts from new measurements and a new proposal rather than reviving its old
lexical dependency or implementation plan.

1. `strengthen-call-freshness` gives every content-bearing read its own FIFO
   per-call freshness admission ticket instead of letting a later caller
   accept an already-running scan. Every call gets a guarded preflight;
   source-derived successes and failures additionally retain response-owned
   byte witnesses and run a real postflight, while invalid/trust/adapter-owned
   conditions keep one preflight. A changed postflight replays the complete
   read once, and a second race returns typed retryable `NOT_READY`
   with reason `workspace_changed_during_read`. The explicitly trusted
   non-Git conda `ms` transformers root keeps targeted stat-plus-byte
   pre/post validation for scoped/indexed reads and adds a bounded
   no-symlink full-root pre/post scan for global, directory, or
   not-yet-indexed reads. Editing stays outside this replay boundary. This
   change adds no authoritative background watcher, persistent content
   index, or cooperative external-writer lock.
2. Host `rg`/`find` owns unknown-file, text, configuration, documentation, and
   dynamic-string discovery; Serena Light takes over for overview, symbol,
   reference, implementation, and diagnostics queries. No lexical MCP tool,
   RTK wrapper, content index, or watcher is planned.

Gate: the accepted freshness change retains its existing evidence. Any future
performance proposal requires a new user decision backed by measured friction,
a coherent standalone OpenSpec plan, proportionate acceptance, and independent
correctness/runtime review.

**Progress as of 2026-08-01:** `strengthen-call-freshness` is accepted and
archived at build
`7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80`. The
deterministic race-test suite and a real daemon/connector race harness
(production `Connector` against a spawned writer process over loopback) both
pass. A real shared-daemon acceptance run proved three simultaneous clients
across two roots, same-root reactivation, a partial release, a
poisoned-proxy environment, and zero newly created test-owned LSP orphans.
Prior CC Sonnet/Opus host receipts are retained only as superseded-build
evidence after the TypeScript authority root moved to
`/data/CoordExp/external/codexUI`. Fresh Sol-xhigh and Opus-max sessions on the
final build each passed all four roots, same-root reactivation, cold TypeScript
declaration, cross-library resolution, and immediate zero-holder release. The
four-snapshot suite passes 875 tests with only
the 3 explicit performance cases skipped in 439.52 seconds; those 3
observation-only cases pass separately in 190.37 seconds, for 878 passing cases
in total. This
includes cold first-call TypeScript declaration/reference coverage.
Recorded per-call latency is observation-only (2 samples per call, sample
minimum/maximum, no threshold or percentile interpretation):
`/data/CoordExp` global 11.32/33.27s,
scoped 10.87/11.22s, overview 10.74/11.31s, diagnostics 10.29/10.93s;
`/data/ms-swift` global 1.44/13.16s, scoped 0.45/0.89s, overview
0.57/0.95s, diagnostics 0.85/0.90s. These numbers remain historical
observations: they do not establish repeated user-facing cold-start friction
or authorize a warm pool. Tasks
5.2--5.5 pass; task 5.6 required no design reset; Sol-xhigh and Opus-max both
passed the final independent review after all findings were dispositioned.
The change is **PASS**, synced, and archived.
`add-lexical-discovery` was subsequently retired at 0/31 tasks in favor of the
host `rg`/`find` route. `improve-warm-runtime-reuse` was subsequently retired at
0/26 tasks because its prerequisite and measured-need case were absent. See
[`openspec/changes/archive/2026-08-01-strengthen-call-freshness`](../openspec/changes/archive/2026-08-01-strengthen-call-freshness)
for the owning tasks and acceptance evidence.

Before lexical work begins, the active schema-4 interaction revision must keep
the routing boundary intact: initialize guidance sends lexical file/text work
to host shell/file tools, overview begins at depth 0, references exclude the
declaration and keep snippets opt-in, and diagnostics stay explicit. It adds no
lexical tools, diagnostics hooks, or RTK integration. The deferred lexical
change is therefore re-based as the next public schema rollover (5), not a
second owner of schema 4.

## Phase F: agent-facing position queries

After v1 archive, create the independent OpenSpec change
`add-agent-lsp-query`. Add a closed-enum `lsp_query` beginning with `hover`,
using the shared position mapper and capability gating. Extend the same tool to
incoming/outgoing calls only after real Pyright and TypeScript probes establish
stable call-hierarchy behavior. Do not expose arbitrary LSP RPC.

## Phase G: client diagnostics adapters

After v1 archive, create the independent OpenSpec change
`add-client-diagnostics-adapters`. Add connector-internal
`notify_file_changed(relative_path)` and reliable Claude Code/CC Agent post-edit
adapters. Add a Codex adapter only if a reliable path-bearing post-edit surface
is proven. Do not add polling sidecars or per-session language servers.

## Deferred

- Typed retry envelopes for `LspTransportClosed` and `LspProtocolError` at the
  public runtime boundary; current behavior fails loudly and never returns a
  false semantic success.
- A third language and a generic `LanguageServerSpec` abstraction.
- UI, JetBrains integration, memories, modes, broad editing, ambient PATH, and
  automatic upstream synchronization.
- Installing the Claude LSP plugin or replacing the shared daemon with one LSP
  process per session.
