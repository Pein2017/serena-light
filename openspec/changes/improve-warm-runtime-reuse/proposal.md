## Why

Same-root sessions already share one runtime, but the fixed ten-minute
zero-holder grace and fully lazy adapters still impose avoidable cold starts
when agents reconnect or alternate among a few repositories. A small,
service-owned warm pool can improve cross-agent reuse without weakening
freshness or introducing per-session language servers.

## What Changes

- Replace the fixed ten-minute zero-holder grace with a bounded daemon-local
  warm pool: retain at most three zero-holder workspaces for at most 30 minutes,
  subject to a 1.5 GiB soft cap on the summed RSS of their recursively owned
  language-server process trees.
- Evict the least recently used zero-holder runtime first when count, age, or
  memory pressure requires retirement. Never evict or count an active-holder
  runtime as reclaimable, and keep cleanup under the existing lifecycle sweep
  and registry ownership path.
- After a workspace activation commits, run finite low-priority prewarm only for
  Python or JS/TS families actually present in the current semantic source
  inventory. Start and initialize at most one adapter at a time, yield to queued
  user work, and abandon remaining prewarm rather than delaying a real call.
- Keep `activate_workspace("/absolute/path")` as the explicit session-switch
  operation and preserve same-root sharing, cross-root isolation, connector
  leases, build slots, and zero-holder retirement safety.
- Expose bounded warm-pool and prewarm state in runtime status for diagnosis;
  do not repeat it in successful navigation or lexical results.
- Keep the limits fixed for this internal deployment. Do not add public package
  configuration, a resident watcher, an infinite background indexer, or
  cross-build resource coordination.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Change zero-holder retention, eviction, RSS ownership,
  and adapter startup from fixed grace plus purely lazy start to bounded warm
  pooling plus finite opportunistic prewarm.
- `diagnostics-status`: Report bounded warm-pool capacity/occupancy, eviction
  reason, and current finite prewarm state as operational truth.

## Impact

This change affects `LeaseLifecycle`, workspace registry retirement, process
ownership/RSS measurement, adapter startup scheduling, status rendering, and
clock/process-tree/fault tests. It adds no public tool and requires no client
configuration or canonical-Serena switch. Existing clients remain API
compatible; the observable behavior change is that a recently released runtime
may stay warm for up to 30 minutes and an adapter may be initialized before its
first direct semantic request.

Admission requires archived acceptance of both `strengthen-call-freshness` and
`add-lexical-discovery`, deterministic count/TTL/RSS/LRU and preemption tests,
multi-client/multi-workspace real-daemon evidence, and proof that active holders
are never evicted. Warm reuse and prewarm are performance aids only: every
content-bearing call remains freshness-gated, cold or stale readiness never
becomes empty success, and cross-build overlap remains independently bounded
inside each daemon. Production LOC remains informational; existing dependency,
ownership, and provenance gates remain mandatory.
