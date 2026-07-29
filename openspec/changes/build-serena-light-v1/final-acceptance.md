# Serena Light v1 Final Acceptance

Date: 2026-07-29 UTC

## Decision

**DUAL AUDIT HOLD — REPAIR IN PROGRESS.** Sol-xhigh and Opus-max completed the
required independent review at commit `6a3acb6` and both returned HOLD. Their
accepted P1 findings cover guarded-edit parent-rename TOCTOU, runtime-source
build-identity closure, legacy retirement atomicity, native-config freshness,
typed activation failures, immediate release plumbing, poisoned-proxy process
evidence, reproducible production-path stdio acceptance, and fresh-client
receipt scope. The code findings are repaired at local commit `d129dee`; legacy
retirement was deliberately narrowed to authenticated inspection plus
fail-closed `atomic_retirement_unsupported` because v1 has no atomic lease
freeze. Agent-public `replace_symbol_body` remains withheld for new clients
until the remaining real-layer and current-build client gates pass. This is not
v1 PASS. The canonical MCP registration named `serena` remains unchanged.

### Superseded pre-audit decision

Before the dual audit, tasks 11-14 and 15.1-15.7 appeared green: containment, freshness,
lexical trust, symlink safety, edit commit-state semantics, typed envelopes,
document lifecycle, positions, symbol lookup, per-language isolation, bounded
status, logging, dependency ownership, provenance, service-owned CPython,
reproducible build identities, versioned daemon slots, nonce-authorized
startup, and coexistence/retirement were reported as passing. That report
collapsed distinct evidence layers: some checks were unit/in-process, some used
a real connector with deterministic LSP, some used a real daemon fault driver,
and real-repository LSP tests did not use the daemon/connector. Agent-public
`replace_symbol_body` had been restored after the reported fault matrix and a
poisoned-proxy stdio hash edit/release. This is
**not** v1 PASS and is superseded by the HOLD above.

### Historical decisions

The 2026-07-28 record was PASS for parallel `serena-light` registration, then
SUPERSEDED — HOLD after static/runtime audits found release-blocking gaps.
Neither historical entry is a current release decision and neither may be
used to archive this change.

## Current repair checkpoint

At commit `d129dee`, the combined correctness/containment subset passes 143
tests. It includes explicit child environments for clean and poisoned-proxy
stdio clients, borrowed-daemon holder preservation, no retained connector
child/daemon descendant, `.mjs` build-identity closure, no-signal legacy lease
race, typed activation rollback, final/non-final immediate release, nested
native-config adapter replacement, and both pre-install and post-install parent
replacement races. Post-install ambiguity is `UNCERTAIN` with a mandatory
reread; it is never reported as a clean edit or replayed.

```text
uv run pytest -q \
  tests/unit/test_build_identity.py tests/unit/test_legacy_migration.py \
  tests/unit/test_symbol_editing.py tests/unit/test_workspace_runtime.py \
  tests/unit/test_workspace_runtime_semantics.py tests/unit/test_daemon_leases.py \
  tests/unit/test_daemon_semantic_api.py tests/unit/test_daemon_server.py \
  tests/unit/test_connector.py tests/integration/test_bounded_freshness_guarded_edit.py \
  tests/integration/test_shared_daemon_lifecycle.py tests/integration/test_daemon_http.py \
  tests/integration/test_connector_proxy.py \
  tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_versioned_rollover_acceptance.py \
  tests/acceptance/test_stdio_connector_acceptance.py
143 passed

uv run ruff check .
All checks passed!

uv run ty check
All checks passed!

uv run serena-light-bootstrap --check --json
PASS
```

The repaired build identity is
`efc38e91a11f88f29b57700d3cdd154ca67beb421dabe28d92e24648310bc5aa`.
The current dependency digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`;
Pydantic 2.13.4 is declared directly because production imports `StrictBool`.
These results close the accepted code findings and containment task 15.1; they
do not close the full suite, real daemon rollover/edit fault matrix,
current-build fresh-client matrix, or final dual audit.

## Superseded pre-audit receipts

The following commands describe the earlier `6abd545d...` build and are kept
only as historical evidence. They must not be combined with the current repair
checkpoint to claim release acceptance.

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

The runtime uses service-owned CPython 3.12.12 under `runtime/python`.
Source ownership/provenance passes with 9 copied hashes against official
Serena commit `9a9d07e83d8c1cba3458992707f440c624446c6d`. The dependency
digest is `34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f`
and the historical post-edit-restoration build identity was
`6abd545dbc1d232b662ff06e1f777a6091356994c5cff12c3fde2c89a1736599`. The
service executable used by live Codex/Claude configurations is
`/data/CoordExp/.codex/runtime/serena-light/deps/34cb251193d096e79e3d63381b0aa17c0c8aa12f0f4392e2517b371fe824379f/python/bin/serena-light`,
not the repository `.venv`. The dependency slot is an editable install and
imports the live repository source; build identity detects covered `.py` and
`.mjs` changes, but a slot is not a frozen source snapshot.

## Real repositories and failure evidence

The following bullets summarize the superseded pre-audit evidence. Real-root
entries use production workspace/LSP code without the daemon/connector;
connector-contract tests use a real connector/service with deterministic LSP;
daemon fault tests use a detached fault driver. They are not one end-to-end
layer and require current-build reruns before release.

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

Existing fresh Codex and native Claude Code clean-env acceptance passed on
earlier build identities. The repaired stdio acceptance now explicitly passes
the clean or poisoned environment to the child and proves internal loopback
bypass without skipping when an exact-build daemon is already live. A
fresh CC Agent launched from `/data/CoordExp` auto-bound that root after a
service-owned `GIT_CONFIG_GLOBAL` trust fix, then explicitly activated
`/data/CoordExp/serena-light`, retrieved a real Pyright symbol for
`write_service_git_config`, and released. During containment,
`replace_symbol_body` was absent from its advertised 10-tool list. Historically,
after every preceding gate appeared to pass, the tool was restored and a
poisoned-proxy stdio
client edited an isolated `/data` Git fixture by exact whole-file hash, then
released; the test reclaimed its exact daemon and removed the fixture. A fresh
CC Agent then observed the restored tool, matched historical build identity
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
- Historical unregistered or test/dev daemons have mixed timestamps, including
  processes created after the repair cycle reopened. They are unattributed and
  are not safe cleanup candidates under this change; no process-name cleanup is
  authorized. The repaired stdio test borrows an exact live daemon without
  signaling it and exact-cleans only a daemon the test itself starts.
- Parallel rollback is low-risk: stop clients, remove only the distinct
  `serena-light` sibling registrations, and leave canonical `serena` plus its
  hooks/runtime untouched.

`docs/compatibility.json` remains the machine-readable old/new contract. Its
deliberately dropped UI, memory, mode, JetBrains, broad file-edit, and project
server surfaces do not block parallel use; they block only an unreviewed claim
of full Serena compatibility or a canonical-name switch.
`replace_symbol_body` is marked `temporarily_withheld_pending_reacceptance`
there. Clients must restart after a later accepted restoration to negotiate the
tool; no current receipt authorizes its use.
