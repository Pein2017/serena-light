## Why

Serena Light currently relies on pinned Pyright, but current ty and Pyrefly language servers expose different combinations of performance, incremental analysis, implementation lookup, call hierarchy, type hierarchy, type-definition, hover, and diagnostics behavior. We need production-shaped evidence on CoordExp workloads before choosing the one fixed Python backend that will anchor future Agent-facing semantic features.

## What Changes

- Add an isolated, reproducible evaluation harness for pinned Pyright, ty, and Pyrefly that reuses Serena Light's real LSP client, workspace identity, freshness, document lifecycle, position mapping, compact response, timeout, and cleanup seams without adding a production multi-backend registry.
- Evaluate candidates in stop-gated phases: manifest/admission, raw protocol behavior, current Serena Light surface, candidate-specific closed semantic operations, and, only when it can change the decision, a small backend-blinded Codex Agent comparison.
- Make correctness, workspace freshness, selected Conda/import resolution, zero workspace writes, bounded responses, and lifecycle cleanup non-compensable admission requirements.
- Choose among `promote_pyrefly`, `promote_ty`, `retain_pyright`, and `inconclusive_retain_pyright` using a reviewable decision receipt. Among candidates that pass every hard gate, prioritize demonstrated Agent value from future closed semantic operations, then current-surface quality, end-to-end efficiency, and maintenance cost.
- Keep the installed MCP, public tool schema, production Pyright default, canonical Serena registration, and guarded editing behavior unchanged throughout this change.
- End this change at the backend recommendation. Any production migration and any new public semantic query operation require separate approved changes.

## Capabilities

### New Capabilities

- `python-backend-evaluation`: Defines the isolated evidence, hard gates, Agent comparison, decision outcomes, and publication boundary for selecting Serena Light's fixed Python language backend.

### Modified Capabilities

None. This change does not alter the current public MCP or stable runtime behavior.

## Impact

- Adds evaluation-only Python modules, scripts, tests, fixtures, manifests, and ignored receipts under Serena Light ownership.
- Adds a separate evaluation-only candidate lock and service-owned runtime for ty and Pyrefly. Candidate packages MUST NOT enter `pyproject.toml`, `uv.lock`, `package-lock.json`, production bootstrap, dependency lock digest, or build identity.
- Exercises `/data/CoordExp/serena-light`, `/data/ms-swift`, one frozen representative CoordExp worktree, and a selected Conda `site-packages` snapshot as read-only inputs; controlled mutation tests operate only on disposable snapshots.
- Uses `/data/CoordExp/external/serena` as a read-only adapter reference. Copied code, if any, remains exceptional and must carry exact provenance and license evidence.
- Compatibility boundary: no production backend switch, public schema change, client reconfiguration, canonical Serena change, or permanent backend selector is authorized.
- Admission evidence must record exact source commit, backend versions and executable hashes, workspace snapshot identities, interpreter paths, configuration digests, commands, environment boundary, and before/after write checks.
- Every phase must prove the production dependency-lock digest and build identity are byte-identical to their pre-evaluation values.
- This change does not update the historically stale project context in `openspec/config.yaml`; that maintenance remains separately scoped. Internal hierarchy probes do not authorize public hierarchy tools, which remain subject to a separate user decision.

## Non-goals

- Migrating production from Pyright to ty or Pyrefly.
- Shipping or maintaining multiple production Python backends.
- Exposing backend identity or backend selection to Agents.
- Adding a raw LSP RPC tunnel, code actions, rename, completion, formatting, or arbitrary hierarchy methods.
- Synthesizing implementations from references or hand-built AST heuristics.
- Treating dynamic imports, decorators, registries, or pytest fixtures as fully resolved when the backend evidence is incomplete.
- Allowing speed, memory use, or feature count to compensate for stale results, wrong-workspace answers, environment mis-resolution, workspace mutation, or current-surface correctness regressions.
