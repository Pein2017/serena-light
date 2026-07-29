# Serena Light v1 Final Acceptance

Date: 2026-07-29 UTC

## Decision

**DUAL AUDIT HOLD — FINAL REAUDIT PENDING.** Sol-xhigh and Opus-max completed the
required independent review at commit `6a3acb6` and both returned HOLD. Their
accepted P1 findings cover guarded-edit parent-rename TOCTOU, runtime-source
build-identity closure, legacy retirement atomicity, native-config freshness,
typed activation failures, immediate release plumbing, poisoned-proxy process
evidence, reproducible production-path stdio acceptance, and fresh-client
receipt scope. The code findings are repaired at local commit `d129dee`; legacy
retirement was deliberately narrowed to authenticated inspection plus
fail-closed `atomic_retirement_unsupported` because v1 has no atomic lease
freeze. The first repair/reacceptance cycle restored public guarded edit through
local commit `9ba0d53`. A subsequent Sol-xhigh audit at `d7abf45` found two
further P1 failures: a refresh failure could commit a new workspace binding
before returning, and a native-config adapter-stop timeout could leave one
language family permanently absent without a retry trigger. Commit `9921257`
adds exact prepare/commit/abort activation, preserves the prior registry lease
and warm-runtime ownership on failure, and retains explicit pending-restart
cleanup and retry ownership. V1 remains HOLD until Sol-xhigh and Opus-max both
return PASS against the repaired current snapshot. The earlier Opus-max
runtime/evidence PASS at `6a0c58e` is retained as evidence but is not an
exact-current-head release vote; its first current-head rerun ended at external
OAuth authentication. The canonical MCP registration named `serena` remains
unchanged.

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

At repair commit `9921257`, the complete suite passes 516 tests. In addition to
the post-restoration gates, it proves that same-root and cross-root refresh
failure preserve the exact old binding and registry lease, attempt-created
orphan runtimes are stopped, borrowed warm runtimes retain their grace, native
config stop timeout is typed and retryable, unaffected families remain stable,
and runtime shutdown settles pending adapter cleanup without publishing a late
replacement. It also includes explicit
child environments for clean and poisoned-proxy
stdio clients, borrowed-daemon holder preservation, no retained connector
child/daemon descendant, `.mjs` build-identity closure, no-signal legacy lease
race, typed activation rollback, final/non-final immediate release, nested
native-config adapter replacement, and both pre-install and post-install parent
replacement races. Post-install ambiguity is `UNCERTAIN` with a mandatory
reread; it is never reported as a clean edit or replayed. The added real stdio
case advertises the restored tool, performs one expected-hash semantic edit in
an isolated `/data` Git workspace under a fully poisoned child proxy, releases
immediately, and exact-cleans only its isolated daemon and fixture.

```text
uv run pytest -q tests
516 passed

uv run pytest -q tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_lifecycle_failure_matrix.py tests/unit/test_adapter.py \
  tests/unit/test_workspace_runtime.py tests/unit/test_workspace_runtime_semantics.py \
  tests/acceptance/test_typescript_real_acceptance.py
75 passed

uv run ruff check .
All checks passed!

uv run ty check
All checks passed!

uv run serena-light-bootstrap --check --json
PASS

uv run serena-light-source-budget --json
PASS: 14,155 production lines (informational; maximum=null, not gated)

openspec validate build-serena-light-v1 --strict
Change 'build-serena-light-v1' is valid
```

The post-repair build identity is
`601e547bb028e20dc9dbb73a3921a54066273269ab3d8a7542d32a2527e25d05`.
The current dependency digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`;
Pydantic 2.13.4 is declared directly because production imports `StrictBool`.
The source/provenance gate reports 14,155 production lines (informational), no
forbidden or undeclared imports, bidirectional census/manifest agreement, nine
verified copied hashes, and official Serena commit
`9a9d07e83d8c1cba3458992707f440c624446c6d`.
These results close the accepted code findings, containment, connector/edit
fault contracts, and the complete repair-state quality gate. The connector
contract uses the real connector and daemon HTTP service with the real
`WorkspaceRuntime` plus deterministic LSP adapters; document lifecycle and
family isolation are also covered in the named runtime suites, while the
Unicode real-engine range check uses the TypeScript real-repository acceptance.
These layers are intentionally not described as one subprocess end-to-end
test. The fresh-client root matrix, public edit restoration, and three
post-restoration fresh-client hash edits also pass as recorded below. Only the
final dual audit remains open.

The isolated real-process rollover gate launches the locked service connector
and two detached `serena_light.cli daemon` processes for three clients and two
workspaces. Its two acceptance variants are derived from the real
source/lock/schema identity, run under a temporary root that must not overlap
production, retain the old build's holders while the new build serves, and
naturally retire only after a bounded test grace. It proves connector/daemon
process and slot mechanics; it does not claim frozen source-copy packaging.

```text
uv run pytest -q tests/acceptance/test_real_versioned_rollover_acceptance.py
1 passed

uv run pytest -q tests/acceptance/test_python_real_acceptance.py \
  tests/acceptance/test_typescript_real_acceptance.py
10 passed
```

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

Before edit restoration, fresh Codex (`gpt-5.6-terra`), native Claude Code
(`claude-sonnet-5/high`), and CC Agent (`claude-sonnet-5/high`) clients all
matched repaired containment build identity
`f92e13abd297ca1786bada925660062df2e61b3454127cf4bf28ad54295af1a7`.
Each client used only parallel Serena Light to activate and query all four
required roots: `PipelinePlanner` in `/data/CoordExp`, a TypeScript/MJS overview
in `cc-plugin-codex`, a Pyright overview in `/data/ms-swift`, and
`PreTrainedModel` in the conda `ms` transformers package. Every query returned
`ok`; transformers reported `allowlisted_non_git` and read-only; each client
released with `immediate=true`. During containment, `replace_symbol_body` was
absent from every fresh tool list. The first non-interactive native Claude and
safe-profile CC Agent attempts were denied by their client permission policies
before any MCP call; fresh retries used an explicit read-only Serena Light
allowlist and are the accepted receipts.

After restoration, new clients of all three types matched build identity
`f4ee8a248a8cd2389b7b2d95083fd0d409548421b4933ac29a138ef0badf8721`,
advertised `replace_symbol_body`, and independently edited the same isolated
Git fixture from hash
`3ac0e0dde718f14d961d87f61fefa93cd4c410adf5e69107b2f2b28a785ae34b`
to `ac1d5ed4cc123bfc41942f7da9c929871c83cc682f60e8dd4e15b5d837b14a9f`.
Each returned the new file generation and completed
`release_workspace(immediate=true)`; the fixture was reset between clients and
deleted after the final receipt. Fresh Codex also persisted the fixture in its
trusted-project list; that exact stale block was removed after the directory was
deleted, and neither the canonical Serena nor unrelated project entries were
changed. No client edited any production repository.

Model-facing Codex/Claude processes retained the working ambient `9090` proxy
because their external API traffic requires it. The real service-executable
stdio acceptance separately launches exact clean and fully poisoned child
environments, proves both reuse the same loopback daemon without proxy routing,
and leaves no connector child or new daemon descendant. This is the declared
proxy split; a poisoned model process is not a meaningful localhost test because
it prevents the client from reaching its external model service.

Historically, after every preceding gate appeared to pass, the tool was restored
and a poisoned-proxy stdio client edited an isolated `/data` Git fixture by exact
whole-file hash, then released; that receipt belongs to build
`6abd545dbc1d232b662ff06e1f777a6091356994c5cff12c3fde2c89a1736599` and does
not authorize edit restoration on the repaired build.

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
- Production LOC (13,816) is informational only; ownership, forbidden
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
`replace_symbol_body` is marked `agent_public` there after the post-restoration
full suite and fresh-client hash-edit receipts. The final audit HOLD blocks v1
PASS/archive, not current use of the reaccepted guarded-edit contract.
