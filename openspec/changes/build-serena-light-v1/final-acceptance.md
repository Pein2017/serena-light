# Serena Light v1 Final Acceptance

Date: 2026-07-28 UTC

## Decision

**SUPERSEDED — HOLD (2026-07-29).** The PASS below is retained as historical v1
evidence, but subsequent static and runtime audits found release-blocking gaps
in freshness wiring, lexical edit authorization, edit timeout/commit-state
semantics, loopback proxy isolation, document lifecycle, typed error mapping,
per-family scope isolation, provenance enforcement, and daemon build ownership.
`replace_symbol_body` remains withheld from new tool negotiations until the
repair and reacceptance tasks pass. The canonical MCP registration named
`serena` remains unchanged.

### Historical decision (2026-07-28)

The original decision was PASS for parallel `serena-light` registration. It is
not a current release decision and MUST NOT be used to archive this change.

## Reproducible gates

```text
uv run pytest -q tests
401 passed

uv run ruff check src tests scripts
All checks passed!

uv run ty check
All checks passed!

uv run serena-light-bootstrap --check --json
PASS: lock f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865

uv run serena-light-source-budget --json
PASS: 11,657 / 12,000 production lines; forbidden_imports=[]

openspec validate build-serena-light-v1 --strict
Change 'build-serena-light-v1' is valid
```

The locked runtime uses Python 3.12.12, Node 22.22.0, npm 11.13.0,
Pyright 1.1.403, typescript-language-server 5.1.3, and TypeScript 5.9.3, all
from the repository-derived dependency root rather than ambient Node or pip
state. Provenance checks retain Serena commit
`9a9d07e83d8c1cba3458992707f440c624446c6d` and MIT ownership evidence.

## Real repositories and failure evidence

- `/data/CoordExp`: configured Python projection, explained exclusions,
  ignored-data pruning, mixed-adapter global readiness, exact
  `PipelinePlanner` recall, current `program=index=1`, and RSS below 8 GB.
- `/data/CoordExp/cc-plugin-codex`: native TypeScript configured program; MJS
  overview/find/definition/references/implementation; Unicode offsets;
  path-scoped omitted files; advisory diagnostics; authoritative
  `npm run typecheck` contrast.
- `/data/ms-swift` and transformers: exact `ms` interpreter, current Python
  diagnostics, cross-library definitions, transformers global symbol, and
  fail-closed `READ_ONLY_ROOT` editing.
- Lifecycle/fault matrix: same-root reuse, cross-root isolation, rollback,
  expiry/grace/release, generation barriers, crash/cooldown, queue/cancellation,
  HTTP-session loss, daemon SIGKILL, no edit replay, and no owned orphan.
- A real 62-second request preserved 15-second heartbeats, status, another
  root, and bounded worker count; the measured test completed in 63.57 seconds.

## Fresh clients and daemon reuse

Parallel registrations were added under the distinct `serena-light` name in
the shared Codex and Claude configuration. Fresh Codex, Claude Code, and CC
Agent sessions used only `serena-light` and verified inherited-cwd activation,
explicit cross-root switching, status, Python and MJS semantics, diagnostics,
hash-guarded `replace_symbol_body`, release, and cleanup. The same daemon ID
`c74dfb66-3a18-4d3c-b420-8c0235ab0187` was reused across the initial three
client sessions. After the final metadata fix, that zero-holder daemon was
terminated by its exact PID/create-time identity; a fresh Claude session
started daemon `eeeeaf21-967a-4086-833d-a90f34e37610` and proved the corrected
Python edit metadata `{name: pyright, language: python}`.

Task-owned temporary Git fixtures were released and removed from `/data`; the
interrupted Codex fixture was moved to a recoverable `/tmp` quarantine after
exact Git-root validation. No target repository or canonical Serena file was
modified by fixture cleanup.

## Residual risks and rollback value

- A nested Git workspace without its own native Python config can inherit a
  parent Pyright program outside its root. Serena Light rejects this as
  `SCOPE_INCOMPATIBLE` and preserves the previous binding. A fixture-owned
  `pyrightconfig.json` proves the supported explicit boundary; no automatic
  overlay is generated.
- Production LOC is now informational; ownership, forbidden imports, direct
  dependencies, census/manifest consistency, copied hashes, and the pinned
  Serena commit remain hard gates.
- TypeScript LSP diagnostics remain advisory when the repository uses a newer
  TypeScript than the pinned semantic engine; native typecheck remains the
  declared authority.
- Parallel rollback is low-risk: stop clients, remove only the distinct
  `serena-light` sibling registrations, and leave canonical `serena` plus its
  hooks/runtime untouched.

`docs/compatibility.json` remains the machine-readable old/new contract. Its
deliberately dropped UI, memory, mode, JetBrains, broad file-edit, and project
server surfaces do not block parallel use; they block only an unreviewed claim
of full Serena compatibility or a canonical-name switch.
