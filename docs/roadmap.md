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
evidence are current through local commit `4f97e12`; only final independent
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

**Progress as of 2026-07-29:** the first two required audits returned HOLD, and
a later Sol-xhigh audit at `5e0f3e2` found same-scan healthy-family event loss,
an adapter-removal cleanup-owner race, and shutdown leakage under ordinary queue
saturation. Those findings and the adjacent detached-runtime owner gap are
repaired at `4f97e12`; the complete 524-test suite,
connector/edit contract selection, static/dependency/provenance gates, and
separately labelled real-process rollover acceptance pass. Current-build fresh
Codex and CC Agent clients pass all four required roots and release cleanly; a
fresh real-stdio client also proves the declaration schema and a cross-library
definition call at the same identity. Native Claude and the three-client hash
edit receipts remain labelled as historical rather than being promoted to the
current repair snapshot. Final dual audit and release remain pending. The
earlier Opus-max runtime/evidence lane passed at `6a0c58e`,
but an exact-current-head rerun did not complete because its OAuth token expired;
that historical PASS is not promoted to the current repair snapshot.

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
