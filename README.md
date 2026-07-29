# serena-light

`serena-light` is an internal, agent-first semantic code navigation service for
the CoordExp environment. It is an independently owned, deliberately small
derivative of selected MIT-licensed Serena and SolidLSP mechanisms.

The repository is implementing the first OpenSpec change. The locked runtime,
admission probes, provenance census, owned LSP core, process lifecycle, and
their regression tests are in place. Workspace, adapter, daemon, and tool
layers are still under construction.

## Scope

- Python through Pyright.
- JavaScript and TypeScript through a pinned TypeScript language server.
- Shared localhost daemon with per-client stdio connectors.
- Session-scoped workspace bindings, semantic queries, diagnostics, and
  hash-guarded symbol-body replacement.

JetBrains integration, UI, memories, telemetry, broad logging, and unrelated
editing operations are intentionally out of scope.

## Admission status

Section 1 passed after the source-scope contract was corrected to keep Git
trust and each language server's native configured program separate. The
superseding evidence is recorded in
`openspec/changes/build-serena-light-v1/scope-admission-report.md`; the older
`admission-report.md` is retained as historical evidence for the rejected
Git-equals-program assumption.

## Implementation status

The owned LSP core, workspace identity/trust/scope model, fixed Pyright and
TypeScript adapters, shared daemon, connector, readiness generations, and
process lifecycle are implemented and accepted. Parallel Codex, Claude Code,
and CC Agent fresh-session acceptance passed; canonical `serena` remains
unchanged pending a separate user decision.

**Current state: `REPAIR AND EDIT REACCEPTANCE PASSED — DUAL AUDITS
PENDING`.** Tasks 11-14 and 15.1-15.7 of `build-serena-light-v1` pass through
real daemon and connector tests, including
service-owned CPython, reproducible build identities, and versioned daemon
slots. Agent-public `replace_symbol_body` is restored after its fault matrix
and a poisoned-proxy real stdio hash edit/release passed. Independent Sol-xhigh
static-correctness and Opus-max runtime/evidence audits (task 15.8) have not yet
run, so this is **not** v1 PASS. Source ownership/provenance
passes with 9 copied hashes against official Serena commit
`9a9d07e83d8c1cba3458992707f440c624446c6d`; production LOC (13,529) is
informational only and not gated. See
[the final acceptance record](openspec/changes/build-serena-light-v1/final-acceptance.md)
for the full gate evidence and residual risks.

See [client registration](docs/client-registration.md) for parallel setup and
rollback, and [the compatibility inventory](docs/compatibility.json) for the
public contract delta.

## Local checks

```bash
serena-light-bootstrap --check --json
serena-light-source-budget --json
pytest -q
ruff check src tests scripts
ty check
```
