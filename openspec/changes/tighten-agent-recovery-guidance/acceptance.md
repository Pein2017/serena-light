# Acceptance — tighten agent recovery guidance

## Verdict

PASS for the schema-6 source, installed daemon/connector, shared runtime, and
fresh Agent surfaces. The accepted build identity is
`77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa`, the
dependency lock digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`, and the
public surface remains 11 tools. The source-owned initialize guidance is 215
bytes and was byte-identical in the connector, daemon, fresh Codex Agent,
native Claude Code, and fresh CC Agent observations.

This acceptance does not switch or remove canonical Serena. Existing clients
may retain their old build slot; a new top-level task is still the required
boundary for loading the newly installed Codex plugin version.

## Deterministic verification

All commands ran from `/data/CoordExp/serena-light` through either the `ms`
Conda environment or the repository service virtual environment, unless the
command is a host-level OpenSpec or plugin command.

- Full suite: `rtk run '.venv/bin/python -m pytest -q'` — 940 passed, 35
  skipped, one Starlette/httpx deprecation warning, 238.06 seconds.
- Static checks: Ruff passed for `src`, `tests`, and `scripts`; Ty passed for
  `src/serena_light`.
- Bootstrap check passed with runtime-owned CPython, Node 22.22, npm 11.13,
  Pyright 1.1.403, TypeScript 5.9.3, and TypeScript language server 5.1.3.
- Source ownership/provenance passed: 20,281 production lines were reported as
  informational, `maximum_production_lines` remained null, census and manifest
  agreed, nine copied-symbol hashes matched, direct dependency ownership and
  forbidden-import checks passed, and official Serena reference commit
  `9a9d07e83d8c1cba3458992707f440c624446c6d` matched.
- `openspec validate tighten-agent-recovery-guidance --strict` passed before
  client materialization and is rerun as part of closeout.
- Focused real schema-6 shared-daemon, rollover, and stdio connector acceptance
  passed: 6 tests in 77.96 seconds.
- Current external-snapshot Python and TypeScript acceptance passed: 14 tests,
  with three observation-only performance/latency cases excluded (deselected in
  the recorded command and marker-skipped in the independent audit), in 182.63
  seconds.
- The final long-root/path/name recovery regressions passed in the 107-test
  compact/navigation slice. Independent Sol-xhigh reproduction measured 435
  characters at a 512-character budget, verified bounded path/name witnesses,
  followed the exact no-body recovery, retried the measured legal budget, and
  measured the malformed fallback at 276 characters.

## Production-shaped runtime evidence

- A service-owned installed stdio client activated `/data/CoordExp`, resolved
  `PipelinePlanner`, returned diagnostics, and released its lease.
- The host does not contain the historical `/data/CoordExp/ms-swift` spelling;
  the live repository is `/data/ms-swift`. Acceptance activated that exact
  current root with `ms`, resolved an external Transformers declaration, and
  returned diagnostics. This is a path relocation, not a skipped language or
  workspace case.
- The read-only `llm-framework-study` site-packages root first activated with
  default `ms` and returned exactly one advisory
  `PYTHON_ENVIRONMENT_PATH_MISMATCH`; explicit reactivation selected
  `llm-framework-study`. No automatic environment switch or write occurred.
- A real Pyright implementation query returned typed `UNSUPPORTED` with
  `implementation_provider_unavailable` and
  `find_referencing_symbols`. A real TypeScript implementation query returned
  an actual implementation without fallback metadata.
- Unicode ranges, all three exact oversized-body recovery actions, and final
  MCP text/structured-content parity passed in focused FastMCP and integration
  coverage.
- Clean and poisoned-proxy acceptance passed with loopback proxy bypass. The
  shared daemon served multiple clients and workspaces, preserved leased old
  builds during rollover, and retired after zero holders and grace. No new
  orphan daemon or language-server process remained.
- External snapshots used for real-repository acceptance were:
  `git:7683...:1b9...` (codexUI), `git:b808...:750...` (CoordExp),
  `git:f279...:456...` (ms-swift), and `transformers:4.57.1:4880...`.

## Fresh client and plugin evidence

- The plugin validator passed before and after materialization. Codex installed
  `serena-light@coordexp-local` as
  `0.1.0+codex.20260811133933` from this repository. The shared Claude Code
  configuration and the installed Codex plugin both use the service-owned
  connector under the pinned dependency digest; canonical Serena is unchanged.
- A newly spawned Codex Agent observed exactly 11 Serena Light tools and the
  exact 215-byte instructions. It proved the default `ms` mismatch warning and
  explicit `llm-framework-study` correction, reported compact
  `workspace/build/languages/executor/issues` status with the accepted build
  identity, received a 418-character `WorkspaceRuntime` recovery under a 512
  budget, followed the overview/no-body route, resolved a normal exact symbol,
  and released to zero holders.
- A separate non-persistent native Claude Code Sonnet session loaded only the
  Serena Light MCP and denied shell/file tools. With an explicit MCP allowlist,
  it independently observed the 11 tools and exact instructions, reported the
  accepted build and compact status, resolved `compact_runtime_status`, and
  released its lease. An initial `dontAsk` run without that allowlist correctly
  denied MCP activation and is not counted as a product failure or pass.
- A newly spawned Claude Sonnet 5 CC Agent independently observed the same 11
  tools and instructions, proved the mismatch/correction sequence, reported
  the same build identity and compact status, received a 418-character
  oversized-container recovery, followed it through overview to an exact child
  body, and released. It encountered no auth, network, configuration, or
  runtime error.

## Dispositions and boundaries

- The default environment/path mismatch remains advisory; the Agent chooses an
  explicit environment when target-environment dependency truth matters.
- Non-Git roots remain freely activatable but read-only.
- Normalized public ranges remain 0-based. There is no 1-based response mode.
- Pyright implementation lookup remains honestly unsupported when the server
  capability is absent; Serena Light does not relabel references as
  implementations.
- Large bodies remain exact and use closed recovery advice; no source slicing,
  pagination, or selective child expansion was added.
- No lexical discovery, diagnostics hook, RTK wrapper, cross-workspace compare,
  or expanded editing surface was added.

No implementation blocker remains. Archive and publication are performed only
after the complete branch review and closeout checks pass.
