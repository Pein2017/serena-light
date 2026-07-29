# Serena Light v1 Final Acceptance

Date: 2026-07-29 UTC

## Decision

**DUAL AUDIT HOLD — FINAL REAUDIT PENDING.** The first exact-current-head final
audits at `8f51d9e` both returned HOLD. Sol-xhigh reproduced a current-generation
false negative after an already-open document changed externally, proved that
the production runtime owner discarded a failed stop, and found ordinary LSP
response/protocol failures escaping the typed service boundary. Opus-max
independently confirmed the stop/sweep/migration truth failures and also found
that the complete-suite claim depended on live mutable `cc-plugin-codex` and
transformers state. Both reviews identified inaccurate declaration-client
evidence: five CC calls, not four, all correctly rejected because their regexes
had zero capture groups.

The current repair candidate fixes those findings and their adjacent
agent-usability gaps. Open changed documents receive full-text `didChange` (or
`didClose`) and watcher futures are retained, settled, and retried before stale
facts can authorize success. Failed runtime stops remain owned, periodic sweeps
continue across failures, migration uses service-level idleness, and release
responses distinguish confirmed stop from pending cleanup. LSP failures are
translated without leaking server messages or replaying edits. All public tools
now have descriptions, the declaration regex schema states the one-capture-group
contract, and invalid details distinguish zero, multiple, and malformed groups.
Default tests no longer consume mutable external roots: explicit real-root and
performance gates require exact before/after snapshot equality. V1 remains HOLD
until this candidate is committed, fully reaccepted, and both Sol-xhigh and
Opus-max return PASS against that exact commit. The canonical MCP registration
named `serena` remains unchanged.

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

## Current second-audit repair candidate

The candidate runtime identity is
`c85f2b4fac40069593d42b0631a0626b8a5330b732414065f0cfeb801506d34a`;
the dependency digest remains
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`.
The following evidence is current, but does not yet constitute release PASS:

```text
uv run --frozen pytest -q -p no:cacheprovider tests
515 passed, 22 skipped, 3 warnings in 173.05s
(all 22 skips are explicit external_repo/performance gates without snapshot env)

high-risk daemon/freshness/schema selection
107 passed, 1 warning

snapshot-bound Python real-repository acceptance
4 passed, 2 deselected in 128.00s

snapshot-bound real Pyright integration
2 passed in 15.18s

snapshot-bound TypeScript acceptance, integration, and admission
15 passed in 20.12s

transformers first-call performance gate, three fresh runtimes
3/3 passed; production-call wall observations 30.77s, 28.33s, 34.16s

uv run --frozen ruff check .
All checks passed!

uv run --frozen ty check
All checks passed!

uv run --frozen serena-light-bootstrap --check --json
PASS (service CPython 3.12.12; dependency digest above)

uv run --frozen serena-light-source-budget --json
PASS: 14,597 production lines (informational; maximum=null, not gated)

openspec validate build-serena-light-v1 --strict
Change 'build-serena-light-v1' is valid
```

The default-suite skips are deliberate evidence isolation, not substituted
passes. Explicit TypeScript acceptance later found a stable window and passed
against exact snapshot
`git:7caa1823bd246deb0d690c83263bc4d4a80480c9:bb7e2813111fc635dc5ff6a3cf5ecd63d58247e9104d825dcdeb2cf292814a04`;
the before/after gate also covers repository-native typecheck authority. Serena
Light did not freeze or modify that foreign checkout. Fresh current-build
Codex, Claude Code, and CC Agent acceptance plus exact-current-head Sol/Opus
audits remain pending.

## Prior repair checkpoint (superseded by current candidate)

At repair commit `4f97e12`, the complete suite passes 524 tests. In addition to
the post-restoration gates, it proves that same-root and cross-root refresh
failure preserve the exact old binding and registry lease, attempt-created
orphan runtimes are stopped, borrowed warm runtimes retain their grace, native
config stop timeout is typed and retryable, and same-scan healthy-family events
advance exactly once before another family's failure is surfaced. Adapter
removal and pending cleanup publication are atomic for both config restart and
scope-incompatible reattribution; a saturated ordinary queue retains exactly
two fixed cleanup slots; runtime stop remains retryable until all cleanup
futures settle; and the daemon service retains a detached failed-stop runtime
as non-idle work for a later sweep. It also includes explicit
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
524 passed

uv run pytest -q tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_lifecycle_failure_matrix.py tests/unit/test_adapter.py \
  tests/unit/test_workspace_runtime.py tests/unit/test_workspace_runtime_semantics.py \
  tests/acceptance/test_typescript_real_acceptance.py
83 passed

uv run ruff check .
All checks passed!

uv run ty check
All checks passed!

uv run serena-light-bootstrap --check --json
PASS

uv run serena-light-source-budget --json
PASS: 14,285 production lines (informational; maximum=null, not gated)

openspec validate build-serena-light-v1 --strict
Change 'build-serena-light-v1' is valid
```

The post-repair build identity is
`eaa691e2425e7466f2f9c3d18666a050cfd53e8153de0c6db9a6f50c1538c3f5`.
The current dependency digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`;
Pydantic 2.13.4 is declared directly because production imports `StrictBool`.
The source/provenance gate reports 14,285 production lines (informational), no
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
test. Fresh current-build Codex and CC Agent clients pass the root matrix; a
fresh real-stdio client passes the public declaration schema and cross-library
call. Public edit restoration and the three model-client hash edits remain
recorded below with their exact historical build identity. Only the final dual
audit remains open.

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

At current repair identity
`eaa691e2425e7466f2f9c3d18666a050cfd53e8153de0c6db9a6f50c1538c3f5`, fresh
Codex (`gpt-5.6-terra/high`) and CC Agent (`claude-sonnet-5/high`) clients used
only Serena Light to activate `/data/CoordExp`, `cc-plugin-codex`,
`/data/ms-swift`, and the conda `ms` transformers package. Status matched the
current build and dependency digest, Python and TypeScript semantic/diagnostic
queries succeeded, transformers returned one bounded `NOT_READY` before global
symbol success, and every root was released immediately. The final release
reported `active_holders=0` and `runtime_stopped=true`.

The CC Agent made five `find_declaration` calls that returned `INVALID_INPUT`.
A review of its durable call history showed that all five regexes contained
zero capture groups, while the public contract requires exactly one group to
select the symbol. The failures were therefore correct input validation, not a
semantic lookup failure.

A separate fresh current-build stdio client then listed the public schema with
required string arguments `relative_path` and `regex`, invoked
`find_declaration` on
`swift/infer_engine/lmdeploy_engine.py` with
`from transformers import (GenerationConfig)`, resolved the read-only conda
transformers declaration, and released with zero holders. Native Claude and the
three model-client hash-edit receipts above are not relabelled as current-build
evidence; current guarded editing is covered by the real-stdio acceptance and
complete suite.

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
- Production LOC (14,285) is informational only; ownership, forbidden
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
