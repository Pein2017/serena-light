# Serena Light v1 Final Acceptance

Date: 2026-07-29 UTC

## Decision

**REPAIR AND EDIT REACCEPTANCE PASSED — DUAL AUDITS PENDING.** Tasks 11-14 and
15.1-15.7 are green: containment, freshness,
lexical trust, symlink safety, edit commit-state semantics, typed envelopes,
document lifecycle, positions, symbol lookup, per-language isolation, bounded
status, logging, dependency ownership, provenance, service-owned CPython,
reproducible build identities, versioned daemon slots, nonce-authorized
startup, and coexistence/retirement all pass through real daemon and
connector tests. Agent-public `replace_symbol_body` is restored after the full
fault matrix and a poisoned-proxy real stdio hash edit/release passed. This is
**not** v1 PASS because the independent Sol-xhigh static-correctness and
Opus-max runtime/evidence audits (task 15.8) have not run. The canonical MCP
registration named `serena` remains unchanged.

### Historical decisions

The 2026-07-28 record was PASS for parallel `serena-light` registration, then
SUPERSEDED — HOLD after static/runtime audits found release-blocking gaps.
Neither historical entry is a current release decision and neither may be
used to archive this change.

## Reproducible gates

```text
uv run pytest -q tests
495 passed, 1 skipped, 1 warning in 278.89s
(the skip is a deliberate refusal to disrupt the active current-build Codex
holder, not a failure)

uv run pytest -q tests/acceptance/test_connector_contract_acceptance.py tests/acceptance/test_stdio_connector_acceptance.py tests/acceptance/test_versioned_rollover_acceptance.py
11 passed in 11.29s
(run only when no conflicting owner holds the daemon)

uv run pytest -q tests/acceptance/test_stdio_connector_acceptance.py
1 passed in 8.82s
(post-containment public hash edit/release under a poisoned proxy)

uv run pytest -q tests/acceptance/test_python_real_acceptance.py tests/acceptance/test_typescript_real_acceptance.py
10 passed in 107.47s

uv run ruff check src tests scripts
All checks passed!

uv run ty check
All checks passed!

uv run serena-light-bootstrap --check --json
PASS

uv run serena-light-source-budget --json
PASS: 13,529 production lines (informational; maximum=null, not gated)

openspec validate build-serena-light-v1 --strict
Change 'build-serena-light-v1' is valid
```

The locked runtime uses service-owned CPython 3.12.12 under `runtime/python`.
Source ownership/provenance passes with 9 copied hashes against official
Serena commit `9a9d07e83d8c1cba3458992707f440c624446c6d`. The dependency
digest is `34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f`
and the post-edit-restoration build identity is
`6abd545dbc1d232b662ff06e1f777a6091356994c5cff12c3fde2c89a1736599`. The
service executable used by live Codex/Claude configurations is
`/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light`,
not the repository `.venv`.

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
- Readiness budget: the prior 30-second LSP readiness budget is unchanged.
  End-to-end real-root acceptance now allows 40 seconds because mandatory
  no-cache Git freshness costs roughly 5 seconds; one measured cold call was
  28.85 seconds.

## Fresh clients and connector acceptance

Existing fresh Codex and native Claude Code clean-env acceptance passed
earlier; poisoned-proxy stdio acceptance proves internal loopback bypass. A
fresh CC Agent launched from `/data/CoordExp` auto-bound that root after a
service-owned `GIT_CONFIG_GLOBAL` trust fix, then explicitly activated
`/data/CoordExp/serena-light`, retrieved a real Pyright symbol for
`write_service_git_config`, and released. During containment,
`replace_symbol_body` was absent from its advertised 10-tool list. After every
preceding gate passed, the tool was restored and a fresh poisoned-proxy stdio
client edited an isolated `/data` Git fixture by exact whole-file hash, then
released; the test reclaimed its exact daemon and removed the fixture. A fresh
CC Agent then observed the restored tool, matched build identity
`6abd545dbc1d232b662ff06e1f777a6091356994c5cff12c3fde2c89a1736599`,
repeated the nested-root Pyright lookup, and released without editing.

### Proxy and Git configuration boundary

CC/Codex/model external network access and bootstrap downloads may keep the
ambient `9090` proxy; connector, health, and local acceptance traffic bypass
proxies; the daemon and LSP subprocesses strip all proxy environment
variables. No global proxy or `NO_PROXY` variable is mutated. The service Git
configuration is private under the service `HOME` and sets
`safe.directory=*` only inside the isolated daemon environment;
`WorkspacePolicy` still controls which roots may bind and editing still
permits only Git workspaces under `/data`. The user's global/user Git
configuration is untouched.

## Residual risks and rollback value

- A nested Git workspace without its own native Python config can inherit a
  parent Pyright program outside its root. Serena Light rejects this as
  `SCOPE_INCOMPATIBLE` and preserves the previous binding. A fixture-owned
  `pyrightconfig.json` proves the supported explicit boundary; no automatic
  overlay is generated.
- Production LOC (13,529) is informational only; ownership, forbidden
  imports, direct dependencies, census/manifest consistency, copied hashes,
  and the pinned Serena commit remain hard gates.
- TypeScript LSP diagnostics remain advisory when the repository uses a newer
  TypeScript than the pinned semantic engine; native typecheck remains the
  declared authority.
- Historical unregistered daemons that predate this repair cycle are
  pre-existing and unattributed; they are not safe cleanup candidates under
  this change and are left untouched pending separate identification.
- Parallel rollback is low-risk: stop clients, remove only the distinct
  `serena-light` sibling registrations, and leave canonical `serena` plus its
  hooks/runtime untouched.

`docs/compatibility.json` remains the machine-readable old/new contract. Its
deliberately dropped UI, memory, mode, JetBrains, broad file-edit, and project
server surfaces do not block parallel use; they block only an unreviewed claim
of full Serena compatibility or a canonical-name switch.
`replace_symbol_body` is marked agent-public there with the completed
reacceptance evidence. Clients that negotiated during containment must restart
to see the restored tool.
