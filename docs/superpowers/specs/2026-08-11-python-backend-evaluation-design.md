# Python Backend Evaluation Design

> Execution companion only. The authoritative scope, behavior, gates, and completion criteria are owned by OpenSpec change [`evaluate-python-language-backends`](../../../openspec/changes/evaluate-python-language-backends/).

## Approved direction

The user approved an isolated three-plane evaluation: raw protocol probes, Serena Light product-seam probes, and a small backend-blinded Codex Agent comparison. Correctness, workspace freshness, environment/import resolution, zero workspace writes, current-surface compatibility, boundedness, and cleanup are hard gates. Among candidates that pass every gate, demonstrated Agent value from future closed semantic operations is the primary discriminator.

The evaluation may test closed implementation, type-definition, hover, call-hierarchy, and type-hierarchy operations internally. It may not expose a raw LSP tunnel, change the installed MCP, create a permanent multi-backend registry, migrate production, or publish a new public tool in this change.

Candidate dependencies remain outside production lock inputs and build identity. The product probe uses an explicit evaluation-only diagnostics assembly seam rather than relaxing the Pyright-only production invariant. Full content hashing is bounded to trust/fixture paths, and the Agent phase runs only when earlier evidence leaves a decision-relevant utility claim or candidate tie. The OpenSpec phase and total ceilings are binding.

## Superpowers execution boundary

- OpenSpec `proposal.md`, `specs/python-backend-evaluation/spec.md`, `design.md`, and `tasks.md` are authoritative.
- Execute one stop-gated task group at a time with TDD and independent review at each phase boundary.
- Use evaluation-only code under `scripts/backend_eval/` and `tests/backend_eval/`; production `src/serena_light` must not import it.
- Raw receipts remain under `.admission-artifacts/backend-eval/<evaluation-identity>/`; commit only bounded acceptance summaries and digests.
- A failed candidate is removed from later phases. A winner recommendation stops the change and requires new user authorization before production integration.

## Review checkpoint

Do not begin implementation until the user reviews the written OpenSpec artifacts and explicitly approves proceeding to the implementation plan.
