# Serena Light Roadmap

This document is a non-normative execution map. Behavioral requirements and
acceptance criteria are owned by OpenSpec. If this roadmap disagrees with an
active OpenSpec change, OpenSpec wins.

## Current state

`build-serena-light-v1` is the sole repair owner. Its 2026-07-28 PASS was
superseded by HOLD. The 2026-07-29 Sol-xhigh and Opus-max audits found new
release-blocking correctness and evidence gaps, so the current state is
`DUAL AUDIT HOLD — FINAL REAUDIT PENDING`, not v1 PASS. Serena Light remains
registered in parallel under `serena-light`; canonical `serena` is unchanged.
Guarded editing (`replace_symbol_body`) is restored after reacceptance.

## Phase A: contain and repair v1

1. Contain guarded editing and isolate loopback traffic from ambient proxies.
2. Repair synchronous freshness, lexical trust, symlink safety, and edit commit
   state semantics.
3. Repair typed envelopes, document lifecycle, positions, symbol lookup,
   per-language isolation, bounded status, logging, dependency ownership, and
   provenance.
4. Install service-owned CPython and introduce reproducible build identities,
   versioned daemon slots, nonce-authorized startup, coexistence, and retirement.

Gate: all tasks 11–14 in `build-serena-light-v1` pass at their declared evidence
layer. Unit/in-process, real connector plus deterministic LSP, real daemon fault,
and real LSP/repository evidence must be named separately; only the stdio gate
currently combines the production connector executable with the shared daemon.
No canonical-name change is permitted. The current candidate also reconciles
changed open documents before generation success, retains and reports failed
runtime-stop ownership, translates LSP failures at the service boundary, and
snapshot-isolates mutable external acceptance. Only final current-build client
acceptance, independent reaudit, and release are still open. See
[the final acceptance record](../openspec/changes/build-serena-light-v1/final-acceptance.md).

## Phase B: reaccept and release v1

Run fresh Codex, Claude Code, and CC Agent acceptance across `/data/CoordExp`,
`/data/CoordExp/cc-plugin-codex`, `/data/ms-swift`, and the pinned transformers
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
edit/restore. Only the final dual re-audit and release remain pending.

Gate: every blocker is cleared and task 15 is complete. Sync/archive and the
authorized GitHub push occur only after both final audits pass; no canonical
Serena switch is part of this roadmap.

## Phase C: agent-facing position queries

After v1 archive, create the independent OpenSpec change
`add-agent-lsp-query`. Add a closed-enum `lsp_query` beginning with `hover`,
using the shared position mapper and capability gating. Extend the same tool to
incoming/outgoing calls only after real Pyright and TypeScript probes establish
stable call-hierarchy behavior. Do not expose arbitrary LSP RPC.

## Phase D: client diagnostics adapters

After v1 archive, create the independent OpenSpec change
`add-client-diagnostics-adapters`. Add connector-internal
`notify_file_changed(relative_path)` and reliable Claude Code/CC Agent post-edit
adapters. Add a Codex adapter only if a reliable path-bearing post-edit surface
is proven. Do not add polling sidecars or per-session language servers.

## Deferred

- A third language and a generic `LanguageServerSpec` abstraction.
- UI, JetBrains integration, memories, modes, broad editing, ambient PATH, and
  automatic upstream synchronization.
- Installing the Claude LSP plugin or replacing the shared daemon with one LSP
  process per session.
