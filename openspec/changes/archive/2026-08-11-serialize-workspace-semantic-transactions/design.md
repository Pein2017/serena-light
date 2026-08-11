## Context

See `proposal.md` for the observed failure and admission reason. `WorkspaceRuntime` already owns one bounded single-worker LSP executor and a workspace operation lock, but `DaemonService` invokes each public runtime method on separate `asyncio.to_thread` workers. Freshness preflight and postflight therefore remain outside the ordered LSP section. A sibling call can open documents or advance adapter generations after another call captured its expected identity but before that call enters the LSP worker.

The stable workspace-runtime contract also permits a cold global readiness wait to be bypassed by a path query. That optimization is incompatible with complete same-workspace transaction ordering because warming can open documents and advance the same generation facts. This change deliberately chooses deterministic FIFO behavior for one root over that bypass.

The project context still mentions a historical 12k LOC stop gate; the user's later explicit decision removed that hard limit. This change follows the controlling decision: LOC remains informational and is not an admission failure.

## Goals / Non-Goals

**Goals:**

- Give one workspace exactly one owner for complete semantic read, activation-refresh, and guarded-edit transactions.
- Preserve parallel MCP submission, cross-workspace concurrency, typed saturation, fail-closed freshness, edit uncertainty, and responsive control-plane calls.
- Make shutdown cancel work that provably has not started and settle work already running before disowning adapters.

**Non-Goals:**

- Do not weaken, cache, coalesce, or otherwise change the freshness scan algorithm.
- Do not change tool schemas, success envelopes, language attribution, LSP capabilities, proxy behavior, or daemon build rollover.
- Do not add automatic retries for typed semantic failures or hide real external-write `NOT_READY` results.
- Do not copy canonical Serena's agent-wide executor; ordering remains per workspace identity.

## Decisions

### Add a separate bounded transaction executor per workspace

The runtime will own a second bounded single-worker executor dedicated to complete semantic transactions. A read entry runs freshness preflight, semantic work, response witnessing, and postflight on that worker. An edit entry runs its one preflight and the existing non-replayable edit state machine there. The entry may synchronously submit actual LSP work to the existing LSP executor because the two executors have different worker threads and ownership.

This is preferred to acquiring the existing operation lock around the outer transaction: the LSP worker also acquires that lock, so a caller holding it while waiting on LSP work would deadlock. It is preferred to a bare condition lock because the existing executor supplies fixed capacity, FIFO order, pre-start cancellation, active-state inspection, and bounded shutdown behavior.

The implementation may generalize the executor's documentation/name so the same owned queue primitive can serve both transaction and LSP work, but it will not change the queue algorithm or introduce a dependency.

### Admit reads, edits, and activation refresh through the same owner

All public content-bearing reads route through the existing `_fresh_read_envelope` seam, so that seam will submit one complete read closure. `ensure_fresh`, which is also used by same-root activation, will submit a refresh closure. Guarded editing will submit one complete edit closure and retain its existing inner adapter commit state.

Internal code already running on the transaction worker must call the underlying freshness coordinator directly rather than recursively entering the transaction executor. This avoids nested same-worker deadlock.

### Preserve error authority

Queue saturation remains `ExecutorBusyError` at the runtime/service boundary and is rendered as typed `BUSY`. A cancelled queued entry never runs. A read that starts and later times out remains a read-only timeout. An edit whose outer entry has started is never replayed; the existing `queued/running/installed/done` state remains authoritative for `TIMED_OUT` versus `UNCERTAIN`.

Sibling generation advances disappear because sibling transactions no longer overlap. Genuine process restart, external write, or generation transition inside one transaction continues to return the existing typed failure.

### Keep control plane and cross-root work outside the transaction queue

Heartbeat, binding lookup, status, and lease release do not produce source-derived semantic results and remain outside the transaction executor. Each `WorkspaceRuntime` owns its own transaction executor, so a blocked root cannot block another root.

### Preserve public parallel-tool advertisement

`supports_parallel_tool_calls=true` remains correct: the client can batch calls in one round trip, while the server serializes same-root work and permits different roots to run concurrently. Setting the flag false was rejected because it would not coordinate different clients and would forfeit batching efficiency.

### Provenance

No canonical Serena source is copied. The design is independently implemented by composing Serena Light's existing owned executor and workspace lifecycle primitives, so copied-source manifests and upstream hashes do not change.

## Risks / Trade-offs

- [Risk] A cold global query can delay a later path query for up to its bounded readiness budget. → Mitigation: retain the existing 30-second readiness bound, keep status and other roots responsive, and prefer deterministic results over self-invalidating overlap.
- [Risk] A second worker thread and queue per warm workspace increase owned lifecycle state. → Mitigation: construct both together, stop transaction admission before adapter cleanup, cancel queued entries, wait for the running entry, and test repeatable stop.
- [Risk] A transaction worker waiting on the LSP worker could deadlock if internal code re-enters the transaction queue or holds the LSP operation lock. → Mitigation: use separate executors, prohibit recursive submission, and add a timeout-backed unit test covering a real semantic call.
- [Risk] Serial transactions increase tail latency for very large batches. → Mitigation: retain a fixed queue bound and the 180-second MCP tool timeout; this change targets correctness, not scan optimization.
- [Risk] A service cancellation may arrive after an edit transaction starts. → Mitigation: never replay; preserve the existing edit commit-state rules and report uncertainty where completion cannot be proved.

## Migration Plan

1. Land the runtime and tests without changing public registration.
2. Run focused red/green concurrency tests, the full unit/integration suite, static checks, strict OpenSpec validation, and a real connector burst.
3. Let build identity rollover start a new daemon for fresh clients; do not terminate an older daemon that still owns leases.
4. Roll back by reverting the runtime/source change and starting fresh clients on the prior build. No stored data or schema migration is required.
