# Disposition: stopped with repository maintenance

Date: 2026-08-13

Status: **STOPPED — repository archived, incomplete tasks preserved**

## Decision

The owner has decided to stop maintaining Serena Light and return to the
official `oraios/serena` project. This change is therefore closed without
finishing its machine-readable decision receipt, candidate-runtime cleanup
workflow, or the stop-gated Phase 3–5 comparisons.

The unchecked task boxes in `tasks.md` are intentional historical evidence.
They MUST NOT be interpreted as completed work.

## Last trustworthy result

The final accepted Phase 2 protocol receipt retained the existing Pyright
backend for Serena Light and stopped the remaining comparison phases:

- Pyright 1.1.403 passed the frozen Phase 2 protocol gate.
- ty 0.0.70 passed the required semantic evidence but was incompatible with
  Serena Light's then-current push-diagnostics product seam.
- Pyrefly 1.2.0 failed the frozen diagnostics/workspace-symbol/lifecycle gate.
- The decision was scoped to the pinned versions, corpus, configuration, and
  Serena Light product seam. It is not a general ranking of the backends.

The exact evidence and independent Sol-xhigh/Sol-max review are retained in
`phase-2-acceptance.md`. No production backend migration was authorized or
performed.

## Unfinished work

- Tasks 3.1–5.6 were skipped by the Phase 2 stop gate.
- Tasks 6.1, 6.2, and 6.4–6.6 were not completed.
- The 540-second published-receipt total remains a lower bound, not a complete
  active-time ledger.
- `scripts/backend_eval_closeout.py` and its tests are retained as an
  unexecuted historical closeout scaffold; no final decision receipt was
  published from it.

## Successor

Official Serena is the successor. Future semantic-tool improvements, language
support, client contexts, hooks, and updates should follow upstream Serena's
published installation and configuration rather than extending this codebase.

This delta specification is archived without syncing to the stable Serena
Light specifications because it is evaluation-only, incomplete, and cannot
become a maintained product contract after repository retirement.
