## 1. Prerequisite and Lifecycle Baseline

- [ ] 1.1 Verify `strengthen-call-freshness` and `add-lexical-discovery` are accepted, synced, and archived; record the schema-4 source/build/lock baseline and stop if their stable contracts differ from this downstream delta.
- [ ] 1.2 Trace current lease expiry/release, registry acquisition/detach, ten-minute grace, sweep cadence, pending cleanup, adapter start/stop, and process identity ownership with exact concurrency locks before changing policy.
- [ ] 1.3 Add fake monotonic clock, deterministic registry ordering, fake recursive `/proc` process trees, and controllable adapter-start barriers for lifecycle tests without sleeps.

## 2. Bounded Zero-Holder Pool

- [ ] 2.1 Replace fixed warm-grace state with lease-lifecycle-owned zero-holder metadata (`idle_since`, `last_used`, deterministic identity tie-break) while preserving active lease and immediate-release semantics and registry-owned atomic runtime detach.
- [ ] 2.2 Enforce at most three zero-holder runtimes and a 30-minute monotonic TTL through the existing release/sweep/`retire_idle` seam; atomically detach LRU candidates and retain failed stops as pending cleanup.
- [ ] 2.3 Make reacquisition remove a candidate atomically before reuse, and prove acquisition/eviction races cannot stop a runtime after it gains a holder or publish two runtimes for one build/workspace identity.
- [ ] 2.4 Keep active-holder runtimes outside the reclaimable pool/count/TTL decisions and enforce policy independently inside each versioned daemon without cross-build mutation.

## 3. Recursive RSS Soft Cap

- [ ] 3.1 Implement validated PID/create-time recursive descendant discovery for each owned language-server launcher, deduplicate process identities, and include TypeScript `tsserver` grandchildren while excluding daemon/connectors/unrelated processes.
- [ ] 3.2 Sample newly zero-holder runtimes and refresh cached samples no more often than once per 30 seconds during sweep; represent known/unknown state explicitly.
- [ ] 3.3 Apply the fixed 1,610,612,736-byte soft cap only to zero-holder candidates, evict in deterministic LRU order until the known sum is bounded, and retire any candidate whose recursive process identity remains unknown at that sweep rather than counting it as zero bytes.
- [ ] 3.4 Add count/TTL/RSS interactions, PID reuse, disappearing `/proc`, shared-descendant deduplication, active-over-cap protection, immediate release, cleanup failure, and two-build isolation tests.

## 4. Finite Low-Priority Prewarm

- [ ] 4.1 Add one daemon-wide finite prewarm scheduler whose plan is derived only after activation refresh/binding commit from currently attributed compatible Python and JS/TS source families.
- [ ] 4.2 Before each start, revalidate runtime registration/not-stopping state, family attribution/capability, adapter absence, and an empty foreground semantic queue; serialize at most one adapter start daemon-wide.
- [ ] 4.3 Prewarm only through ordinary adapter start/initialize to document-capable phase, never wait for global-index readiness, and coalesce repeated activations without duplicate processes.
- [ ] 4.4 When foreground work appears, allow only an already admitted bounded start to settle and abandon remaining plan entries; prove user work waits for at most that one start and never for another prewarm step or scheduler decision.
- [ ] 4.5 Cancel/drop pending plan entries on release, retirement, config reattribution, incompatibility, cooldown, or shutdown and leave all admitted process cleanup with existing adapter/runtime owners.

## 5. Bounded Operational Status

- [ ] 5.1 Extend lease-bound runtime status with fixed pool limits, daemon candidate count, aggregate known warm RSS/unknown count, last eviction reason, active ownership, and the bound runtime's finite prewarm state/family; do not claim per-candidate status for an unbound zero-holder runtime.
- [ ] 5.2 Bound status so it does not enumerate all warm roots, process descendants, or unbounded histories, and keep warm/prewarm metadata out of navigation, diagnostics success, and lexical success payloads.
- [ ] 5.3 Update lifecycle/debug summaries, README, compatibility, roadmap, and every retained stable-spec phrase that still says `warm grace` to the zero-holder pool terminology before archive; add no public tuning configuration.

## 6. Acceptance and Stop Gates

- [ ] 6.1 Run deterministic multi-client scenarios proving same-root adapter reuse after release/reconnect, explicit `activate_workspace` switching among at least four roots, LRU count eviction, TTL retirement, RSS retirement, immediate release, and active-holder immunity.
- [ ] 6.2 Run prewarm scenarios for Python-only, JS/TS-only, mixed, incompatible, changing, busy, cooldown, release, and shutdown roots, asserting one start at a time, no global sentinel wait, and no duplicate/orphan process.
- [ ] 6.3 Run real-daemon/fresh-client Codex, Claude Code, and CC Agent lifecycle acceptance on representative Python `/data/CoordExp` and JS/TS `/data/CoordExp/cc-plugin-codex` roots, including two build daemons with live old-build leases; do not repeat lexical Unicode or transformers-scope matrices unchanged by this policy.
- [ ] 6.4 Verify every post-reuse semantic, diagnostic, and lexical read still runs the stable per-call freshness transaction and that warm/prewarm never converts cold, stale, incompatible, or timed-out state into empty success.
- [ ] 6.5 Pass full pytest, Ruff, Ty, bootstrap, dependency/source-ownership/provenance/census/copied-hash gates and strict OpenSpec; report resource observations and production LOC without introducing a hard speed or LOC threshold.
- [ ] 6.6 Stop and return to design review if the policy requires evicting active holders, per-workspace timers/watchers, persistent indexing, adaptive public configuration, cross-build coordination, per-client language servers, global-ready prewarm waits, or a new process/lifecycle owner.
- [ ] 6.7 Obtain final independent correctness and runtime-evidence reviews, disposition every blocker, re-run affected gates, then sync and archive only after all three policy limits and prewarm preemption are proven through the production daemon/connector path.
