## Context

The current coordinator coalesces concurrent callers behind one in-flight
freshness scan. That is efficient when all calls are already pending before the
scan starts, but it is not a sufficient admission rule for a call that arrives
after the scan has begun: the later call can accept evidence older than itself.
The common read envelope then executes semantic or diagnostic work without a
final workspace validation. Existing guarded scans already provide the hard
part—two agreeing full-byte observations, same-stat rewrite detection, Git
membership/config reconciliation, adapter notifications, and targeted non-Git
validation—so the design strengthens transaction ownership instead of adding a
second freshness mechanism.

The callers are Codex, Claude Code, and CC Agents sharing a daemon while other
agents may write directly through shell or another session. Those writers do
not participate in a cooperative lock. The best implementable guarantee is
therefore a clear linearization point at the final guarded validation, not an
impossible guarantee that a response reflects writes occurring after that
point.

## Goals / Non-Goals

**Goals:**

- Make every content-bearing read depend on a guarded scan that begins after
  that call arrived.
- Detect a completed foreign write during navigation or diagnostics work before
  returning success.
- Discard and replay a raced read at most once, then fail retryably under
  continuous churn.
- Preserve same-stat detection, Git/config attribution, exact snapshot mapping,
  adapter generation ownership, and targeted transformers behavior.
- Keep edits non-replayable and retain the current commit-point/
  `UNCERTAIN` contract.

**Non-Goals:**

- Filesystem watchers as an authority, background polling, an inotify journal,
  a persistent content database, kernel filesystem snapshots, or cross-process
  write locks.
- Delivery-time "latest possible" semantics after final validation.
- A new public generation field, client option, hook, or tool.
- Performance thresholds; the first objective is usable freshness correctness.

## Decisions

### 1. Queue a unique freshness ticket for every content-bearing read

The coordinator will assign an arrival ticket before a read waits on another
scan. Preflight tickets execute FIFO under the coordinator's existing bounded
scan ownership, and every ticket starts a fresh guarded scan after that ticket
was assigned. A call arriving during scan A waits for A to settle and then runs
scan B; it cannot use A as its own admission evidence.

This deliberately removes cross-call preflight coalescing. A time cache or a
"scan covered all calls waiting at completion" watermark is cheaper, but both
allow evidence that began before a caller to authorize that caller. Retaining a
single scan owner still bounds filesystem concurrency and avoids parallel Git
walks.

The first implementation uses one scan per ticket. A future optimization may
coalesce tickets only when one guarded scan starts after every coalesced call
arrived; accepting a scan already in progress at any caller's arrival remains
unsound and outside the contract.

Same-root `activate_workspace` uses the same ticketed preflight. Control-plane
operations such as heartbeat, lease release, and bounded runtime status do not
return source-derived content and do not take the two-pass read transaction.

### 2. Make the read envelope own preflight, operation, and postflight

Navigation and diagnostics enter one `run_fresh_read` boundary:

1. acquire a unique preflight ticket and complete guarded reconciliation;
2. record the resulting workspace identity token and execute the existing read,
   retaining an internal byte-identity witness for every response-owned source
   snapshot that contributes content, a range, or diagnostic authority;
3. complete a second guarded scan and compare the final identity/generations
   plus every response witness with the final observed byte identity for that
   workspace or trusted-external path;
4. return the result only when the postflight reports no relevant change;
5. otherwise discard every source-derived result and repeat the complete
   transaction once.

If the second attempt also changes, the boundary returns rich retryable
`NOT_READY` with reason `workspace_changed_during_read` and bounded attempt/
generation evidence. It never turns churn into empty success. Existing
adapter-process retry and semantic target-stabilization bounds remain
independent but finite; none may introduce an unbounded outer loop.

The witness is internal and does not add success metadata. It closes the case
where a writer exposes bytes B to the operation, restores bytes A before
postflight, and aggregate pre/post workspace identities are both A: a returning
B snapshot cannot validate against the final A digest and the attempt replays.

The postflight is a real guarded scan, not a stat-only comparison. It can
advance generations and reconcile open documents before the replay. This is
the intentional cost of detecting same-size, same-inode, restored-timestamp
rewrites.

For the exact trusted non-Git transformers root, path-scoped reads guard the
selected paths before and after the operation. A global semantic read has no
honest targeted read set, so it performs a bounded full-root no-symlink guarded
scan in both positions. The existing global functionality is retained; it is
not allowed to claim freshness from a no-op or one candidate path.

### 3. Apply final validation only to source-derived success

Invalid input and trust failures occur before expensive work. Adapter failure,
cooldown, timeout, or readiness errors retain their existing typed authority
and are not replayed merely to manufacture a different error. Any successful
navigation or diagnostic value—including a diagnostic `clean` state—must pass
postflight. Later lexical tools will use the same owner rather than inventing a
parallel contract.

Edits remain outside `run_fresh_read`. They keep one preflight plus the existing
workspace lock, lexical authorization, expected hash, atomic replace, and
commit-state handling. An edit that started or may have committed is never
replayed by this change.

### 4. Define the guarantee at the final guarded byte observation

A write whose changed bytes are visible to the final guarded scan invalidates
the attempt. Once the final scan has completed with two agreeing byte passes,
the read is linearized. A non-cooperating write after that point belongs to the
next call. This matches the strongest portable guarantee available without
forcing every external editor and agent to share a lock.

### 5. Test races with explicit barriers, not timing sleeps

Unit and integration fixtures will pause scans, LSP responses, diagnostic
publication, and postflight at named barriers. Tests will prove call-arrival
ordering, same-stat rewrites, one replay, repeated-churn failure, and no edit
replay. Real connector smokes will mutate files from a separate process between
the barriers and verify returned bodies/ranges/diagnostics against the final
accepted bytes.

## Risks / Trade-offs

- [Every successful read performs at least two guarded scans] → Accept the
  latency in this correctness-first change, preserve one scan at a time, measure
  actual repositories, and optimize only if a later trace identifies a real
  bottleneck without weakening the contract.
- [A hot writer can prevent success] → Bound the transaction to one replay and
  return retryable `NOT_READY` with a clear reason.
- [Nested adapter and target-stabilization retries increase worst-case work] →
  Keep each existing retry bound explicit and add tests proving termination.
- [A writer can change files immediately after validation] → Document the final
  validation as the linearization point; the next call receives its own scan.
- [Non-Git transformers cannot afford a whole-tree scan] → Retain exact-root,
  read-only targeted path/config validation for path-scoped calls; accept a
  bounded full-root scan only for existing global calls that claim whole-root
  semantic coverage.

## Migration Plan

1. Replace the shared-scan concurrency tests with call-ticket and barrier tests.
2. Introduce `run_fresh_read` and route navigation and diagnostics through it;
   leave edit routing separate.
3. Run unit, fault, real-daemon, and connector acceptance against current public
   schema 3 in clean and concurrent-writer environments.
4. Let source-based build identity start a new daemon slot; restart fresh
   clients for acceptance while older leased builds retire normally.
5. Sync and archive this change before beginning either dependent change.

Rollback is a source/build rollback to the prior build slot. No on-disk data or
public schema migration is required.

## Open Questions

None. Further scan optimization requires separate measured evidence and must
preserve the per-call start and final-validation guarantees.
