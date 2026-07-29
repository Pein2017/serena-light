# Serena Light Roadmap

This document is a non-normative execution map. Behavioral requirements and
acceptance criteria are owned by OpenSpec. If this roadmap disagrees with an
active OpenSpec change, OpenSpec wins.

## Current state

`build-serena-light-v1` is the sole repair owner. Its 2026-07-28 PASS was
superseded by HOLD; as of 2026-07-29 the repair and edit reacceptance gates
(tasks 11-14 and 15.1-15.7) have passed, so the state is
`REPAIR AND EDIT REACCEPTANCE PASSED — DUAL AUDITS PENDING`, not v1
PASS. Serena Light remains registered in parallel under `serena-light`;
canonical `serena` is unchanged. Guarded editing (`replace_symbol_body`) is
agent-public again; clients started during containment require a restart.

## Phase A: contain and repair v1

1. Contain guarded editing and isolate loopback traffic from ambient proxies.
2. Repair synchronous freshness, lexical trust, symlink safety, and edit commit
   state semantics.
3. Repair typed envelopes, document lifecycle, positions, symbol lookup,
   per-language isolation, bounded status, logging, dependency ownership, and
   provenance.
4. Install service-owned CPython and introduce reproducible build identities,
   versioned daemon slots, nonce-authorized startup, coexistence, and retirement.

Gate: all tasks 11–14 in `build-serena-light-v1` pass through real daemon and
connector tests. No canonical-name change is permitted. **Passed as of
2026-07-29**; see
[the final acceptance record](../openspec/changes/build-serena-light-v1/final-acceptance.md).

## Phase B: reaccept and release v1

Run clean and poisoned-proxy acceptance across `/data/CoordExp`,
`/data/CoordExp/cc-plugin-codex`, `/data/ms-swift`, and the pinned transformers
package with fresh Codex, Claude Code, and CC Agent clients. Restore
`replace_symbol_body` only after its complete fault matrix passes. Then obtain
independent `sol-xhigh` static-correctness and `opus-max` runtime/evidence
audits, disposition every finding, mark v1 PASS, sync stable specs, and archive.

**Progress as of 2026-07-29:** clean and poisoned-proxy acceptance across all
four targets has passed for fresh Codex, Claude Code, and CC Agent clients;
`replace_symbol_body` restoration plus real hash edit/release also passed
(tasks 15.1-15.7). The independent Sol-xhigh/Opus-max audits (15.8) remain
outstanding; task 15 is not yet complete and v1 is not yet PASS.

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
