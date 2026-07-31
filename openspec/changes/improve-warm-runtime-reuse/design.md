## Context

The daemon already separates connector leases from shared workspace runtimes:
same-root holders reuse one runtime, zero holders enter a fixed ten-minute grace,
and `LeaseLifecycle.sweep` asks the registry to retire eligible runtimes. The
adapters themselves are fully lazy. This is safe but leaves frequent Codex,
Claude Code, and CC Agent reconnects cold after a short gap and makes switching
among a few active repositories pay repeated process startup/index warmup.

This change is deliberately a bounded policy adjustment, not a new daemon or
indexing architecture. It assumes `strengthen-call-freshness` and
`add-lexical-discovery` have both been accepted and archived. The warm pool can
retain computation, but it is never freshness authority: every subsequent
semantic, diagnostic, or lexical read still enters the accepted per-call
preflight/postflight owner.

## Goals / Non-Goals

**Goals:**

- Reuse recently released workspace runtimes across agents and sessions for a
  useful but bounded interval.
- Bound zero-holder retention by count, age, and recursively owned
  language-server RSS.
- Opportunistically start only language families actually present after a
  successful activation, without delaying real work or waiting for global
  readiness.
- Preserve active-holder safety, explicit cross-root activation, build-slot
  isolation, cleanup ownership, and minimal bounded status.

**Non-Goals:**

- A performance SLA, adaptive/autotuned policy, public configuration, or a
  machine-wide resource scheduler.
- Cross-build coordination, eviction of active sessions, per-client language
  servers, prewarming unsupported languages, or eager whole-workspace indexing.
- A watcher, persistent index, background freshness authority, or readiness
  shortcut.

## Decisions

### 1. Keep one daemon-local zero-holder pool with fixed limits

A runtime becomes a warm candidate when its holder count transitions to zero
without `immediate=true`. The lease lifecycle records monotonic `idle_since` and
`last_used`; the latter reflects the latest successful acquisition or workspace
operation before the zero-holder transition. A candidate remains eligible only
while all three conditions hold:

- no more than three zero-holder runtimes are retained;
- its zero-holder age is at most 30 minutes;
- the sum of recursively owned language-server process-tree RSS across retained
  zero-holder runtimes is at most 1.5 GiB (1,610,612,736 bytes).

Count pressure evicts the oldest `last_used` candidate first. TTL expiry retires
the expired candidate. RSS pressure repeatedly retires zero-holder candidates in
that same LRU order until the measured sum is at or below the soft cap. Exact
workspace identity breaks ties. An unreadable/identity-mismatched RSS tree is
treated as unknown pressure: if its identity is still unvalidated at sweep
sampling, that zero-holder candidate is retired in the same sweep. It is never
silently counted as zero bytes.

Active-holder runtimes are neither reclaimable nor counted against the warm
pool limits. This means total daemon RSS can exceed 1.5 GiB under real active
work; the cap is a retention policy, not permission to disrupt users.

### 2. Extend only the existing lifecycle/registry retirement seam

All candidate insertion, reacquisition, eviction selection, detach, and pending
cleanup publication occur under existing registry/lifecycle ownership.
`LeaseLifecycle.sweep -> registry.retire_idle` remains the sole periodic
retirement route. Last-holder release applies count pressure immediately; sweep
applies TTL and sampled RSS pressure and retries pending cleanup. Immediate
release bypasses the pool exactly as it bypasses grace today.

If a runtime is reacquired before atomic detach, it leaves the zero-holder pool
and cannot be evicted. If retirement wins first, acquisition creates or borrows
the replacement through existing registry rules while the detached runtime
remains service-owned pending cleanup. No second warm-pool manager or timer per
workspace is introduced.

Each versioned daemon enforces its own three/30-minute/1.5-GiB limits. Old and
new build daemons may overlap while both retain leases; no shared cross-build
file or process coordinator is added.

### 3. Measure only recursively owned language-server trees at a bounded cadence

The service walks `/proc` from every live adapter launch identity through all
descendants, validating PID/create-time identity before counting RSS. This is
needed because a TypeScript language-server launcher can own a `tsserver`
grandchild. The daemon process, connectors, and unrelated processes are
excluded. Shared descendants are deduplicated by validated process identity.

RSS is sampled when a runtime first becomes zero-holder and thereafter no more
often than once per 30 seconds during lifecycle sweep. Cached samples include
time and known/unknown state. A still-unknown candidate is retired at that
sweep. The soft cap does not justify one-second polling.

### 4. Add one daemon-wide finite low-priority prewarm scheduler

After `activate_workspace` has validated the root, completed mandatory
freshness, and atomically committed the binding, it may enqueue a finite prewarm
plan from the current semantic source inventory. The plan contains at most the
fixed Python and JS/TS families actually present. One daemon-wide scheduler
starts at most one adapter at a time.

Before each adapter start the scheduler verifies that the runtime is still
registered, not stopping, the family is still attributed and compatible, and
the workspace semantic queue has no running or queued user work. Prewarm only
starts and initializes the adapter to its ordinary document-capable phase; it
does not issue workspace-symbol sentinels or wait for global readiness. If user
work appears, the scheduler does not interrupt an already admitted atomic start
but abandons all remaining prewarm steps after it settles. Actual requests use
normal priority and never wait on the prewarm scheduler lock. A foreground
request can wait for at most the one adapter start/initialize that was already
admitted before it arrived; it never waits for another prewarm step or a
scheduler decision.

Repeated activations coalesce to the current finite plan; they do not enqueue
duplicate starts. Release, config reattribution, adapter cooldown, or runtime
stop invalidates pending plan entries. Cleanup remains with the adapter/runtime
owners, not the scheduler.

Fully lazy start was considered simpler, but it preserves the known cold-start
friction. Prewarming both fixed adapters everywhere was rejected because it
wastes memory and violates source-family attribution. Waiting for global index
readiness was rejected because it turns activation into hidden foreground work.

### 5. Expose bounded pool and prewarm truth only in status

Runtime status is lease-bound, so it does not pretend to address an individual
zero-holder runtime. It adds fixed policy limits, daemon-local zero-holder
candidate count, aggregate last-sampled known RSS plus unknown-candidate count,
last bounded eviction reason, and the bound active runtime's finite prewarm
state (`idle`, `queued`, `starting`, `abandoned`, or `complete`) with current
family. It does not list candidate roots, per-candidate ages/LRU positions,
process descendants, or historical candidates. Navigation, diagnostics
success, and lexical success remain unchanged and compact.

## Risks / Trade-offs

- [Warm runtimes consume memory after clients leave] → Bound retention by three
  candidates, 30 minutes, recursive RSS, immediate release, and LRU eviction.
- [RSS accounting can miss a grandchild or PID reuse] → Traverse recursively,
  validate create-time identity, deduplicate identities, and treat unknown warm
  trees as pressure rather than zero.
- [Prewarm can race real work] → Use one finite low-priority scheduler, inspect
  the user queue before admission, never wait for global readiness, and abandon
  remaining steps when work appears.
- [A fourth workspace release causes synchronous cleanup work] → Detach the LRU
  candidate atomically, retain failed cleanup as existing pending service work,
  and keep unrelated roots responsive.
- [Two build daemons can each retain a pool] → Accept the bounded overlap during
  version rollover; cross-build coordination would create a new shared owner and
  is outside this change.
- [No hard speed target means uncertain benefit] → Verify behavior and resource
  bounds now; optimize from observed use rather than adding instrumentation or
  autotuning prematurely.

## Migration Plan

1. Require archived freshness and lexical changes and start from public schema
   4/stable specs.
2. Replace fixed grace constants/state with pool metadata and deterministic
   count/TTL/LRU selection under registry ownership.
3. Add recursive process-tree RSS sampling and soft-cap eviction with fake
   clock/proc fixtures.
4. Add the finite prewarm scheduler and lifecycle/race tests.
5. Extend bounded status, then run multi-client/two-build/four-workspace real
   daemon acceptance proving active-holder safety and zero-holder retirement.
6. Deploy by normal source build-identity rollover; no client schema bump is
   required.

Rollback reconnects clients to the prior schema-4 build with ten-minute grace
and lazy adapters. No workspace or dependency data migration is required.

## Open Questions

None. Limit tuning or cross-build resource policy requires production usage
evidence and a separate owning decision.
