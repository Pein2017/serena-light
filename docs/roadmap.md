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
No canonical-name change is permitted. The accepted code findings and repair
evidence are current through local commit `9ba0d53`; only final independent
reaudit and release are still open. See
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

**Progress as of 2026-07-29:** both required auditors returned HOLD. Their
accepted code findings are repaired; the post-restoration 507-test suite,
explicit 75-test
connector/edit contract selection, static/dependency/provenance gates, and
separately labelled real-process rollover acceptance pass. Current-build fresh
Codex, native Claude, and CC Agent clients also pass all four required roots.
All three fresh client types advertise the restored edit, complete the same
isolated hash transition, and release immediately. Final dual audit and release
remain pending.

Gate: every blocker is cleared and task 15 is complete. Local commits are kept;
no push and no canonical Serena switch.

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
