# serena-light

`serena-light` is an internal, agent-first semantic code navigation service for
the CoordExp environment. It is an independently owned, deliberately small
derivative of selected MIT-licensed Serena and SolidLSP mechanisms.

The repository is reaccepting its first OpenSpec change. The admission probes,
provenance census, owned LSP core, workspace/adapter/daemon layers, containment,
repair-state test/static gates, fresh clients, and guarded-edit restoration
pass; only the final dual audit remains open.

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
process lifecycle are implemented. Fresh Codex and CC Agent sessions pass the
four-root semantic/status matrix on the current repaired identity; the native
Claude Code and model-client hash-edit receipts remain explicitly historical.
The current real-stdio connector independently passes its guarded-edit and
cross-library declaration contracts.
Canonical `serena` remains unchanged pending a separate user decision.

**Current state: `DUAL AUDIT HOLD — FINAL REAUDIT PENDING`.** The follow-up
exact-head audits of `7ba6773` found a post-stop admission race, partial
multi-family invalidation after watcher failure, incomplete transport-loss
translation, mutable ignored TypeScript authority outside the external
snapshot, and a shared-daemon holder race in the stdio acceptance. The current
candidate seals ordinary adapter admission synchronously, advances every
affected family before delivery, completes the typed transport boundary,
content-binds the native TypeScript authority, and uses an isolated test-owned
daemon/leases for stdio acceptance. Exact-current-head Sol-xhigh and Opus-max
PASS verdicts are still required.
Agent-public
`replace_symbol_body` remains restored after its fault
matrix, full regression, and three fresh-client hash-edit receipts passed. This
is **not** v1 PASS. Source ownership/provenance
passes with 9 copied hashes against official Serena commit
`9a9d07e83d8c1cba3458992707f440c624446c6d`; production LOC (14,706) is
informational only and not gated. The current repair candidate has build
identity `f46812e239fbf614c3885b50057734a31ddf7fb27d6e39e7239c01742d3e1fda`.
See
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
