# Schema 4 interaction acceptance

Captured on 2026-08-01 after the implementation tree stabilized and the
independent final audits completed.

## Fixed identity and environment

- Baseline source commit: `e66c70e043784a677b584682302b1702e363aa65`
- Public tool schema: `4`
- Production build identity:
  `a92919af0add2dc6dbc40aa2f5cefd5dfa8db808ab58cb794d82ee615a571374`
- Dependency lock digest:
  `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`
- Source-owned initialize guidance: 562 characters, SHA-256
  `166a446a317c1d4ff86e792fba8e12026fe61abb629ba89299220bdb46aad5cc`,
  byte-identical at the outer stdio connector and inner daemon.
- Public tool count: 11. No lexical-discovery or diagnostics-hook tool was
  advertised.
- Real-root snapshots:
  - codexUI:
    `git:7683a1a1c6a6932b5124f30a8b91651f8ee13815:1b9cd2a181f2c7973be1e92d2ea681f0c3194e05189c588d429f014557356903`
  - CoordExp:
    `git:0490f73b56b352826de5f4b3e697575037582718:59688a15f4ec760d887b435a3e20c3007ba6c932f50f5febe063bdb95d205152`
  - ms-swift:
    `git:f2797138dba0e224cfff735cd89a528a08d8732a:45696b3ae91193e921ccb9b1dbd5b33c27b7462d4b6281801d22f90d825de19a`
  - transformers:
    `transformers:4.57.1:4880a9c5bf65f2bb124b7739c74991c1bc2aaf7755133b7fa77ce1e017745dcf`

The live ms-swift Git root is `/data/ms-swift`; the path
`/data/CoordExp/ms-swift` does not exist. Acceptance therefore uses the actual
allowlisted root and records the correction rather than creating an alias.

## Schema-3 versus schema-4 connector-visible results

All schema-4 values below came from `CallToolResult.content[0].text` through a
fresh production stdio connector. Character reduction is observational, not a
pass threshold.

| Case | Schema 3 chars | Schema 4 chars | Schema 4 truth |
| --- | ---: | ---: | --- |
| default overview of `large_nested.py` | 961 | 208 | one root class, depth 0, `omitted=0` |
| explicit depth-0 overview | 209 | 208 | same root class, `omitted=0` rather than 20 |
| `ANSWER` references, snippets omitted | 1,073 | 486 | four non-declaration references, `coverage={"complete":true}` |
| exact path-scoped symbol miss | 598 | 239 | typed `SYMBOL_NOT_FOUND`, no runtime-authority repetition |
| file diagnostics at public 512 | 963 | 176 | current clean file group, `omitted=0` |
| symbol diagnostics at public 512 | 988 | 176 | current clean file group, `omitted=0` |

The same real stdio run proved canonical minified text equals
`structuredContent`. Exact body/hash, Unicode/CRLF/BOM decoded-text ranges,
incomplete coverage, ambiguity, operational errors, and minimum-answer behavior
are covered by the focused and complete suites below. A current compact JSON
dump of the 11 public tool models occupied 10,684 characters; initialize
guidance and field descriptions deliberately spend one-time session context so
repeated query successes can be smaller and easier to route.

Post-audit boundary probes additionally proved that long deterministic symbol,
path, and diagnostics misses remain typed and at most 512 characters;
candidate-free declaration ambiguity remains typed; all five navigation tools
return typed `INVALID_INPUT` for out-of-range answer budgets; and an
unfittable first diagnostic returns a truthful `minimum_required_chars` error
instead of an empty diagnostics success.

## Runtime, root, proxy, and freshness acceptance

- Clean environment: two concurrent fresh connectors received identical
  initialize instructions. One started in
  `/data/CoordExp/.worktrees/research-probes`, returned a 237-character depth-0
  overview for `public_data/pipeline/planner.py`, then explicitly rebound to
  `/data/ms-swift` and returned a 194-character overview for
  `swift/pipelines/base.py`. The second connector remained bound to
  `/data/CoordExp/serena-light`; the first connector's activation did not change
  its lease.
- Poisoned proxy environment: upper- and lower-case HTTP/HTTPS/ALL proxy values
  pointed at `127.0.0.1:9` and both `NO_PROXY` variants were empty. A fresh
  connector still initialized and returned a current 354-character research
  overview through loopback.
- External-writer freshness: six focused acceptance cases passed for
  create/change/delete/config reconciliation, same-root second-lease refresh,
  settled symbol body/range replay, settled reference authority, and settled
  diagnostics findings. No manual refresh was used.
- Real Python/TypeScript roots: 13 non-performance semantic/scope/read-only
  smokes passed across CoordExp, `/data/ms-swift`, conda-`ms` transformers, and
  read-only codexUI. The three TypeScript reference assertions initially still
  expected the declaration; after replacing that schema-3 assertion with
  consumer/bridge completeness plus explicit declaration absence, all three
  targeted real-engine cases passed. `cc-plugin-codex` was not used.
- Versioned rollover: 9 build-identity and real rollover cases passed. In the
  isolated acceptance harness, old and new test-owned build holders remained
  isolated and retired through their own zero-holder policy without touching
  canonical Serena. This is not a host-wide no-orphan claim.
- Host census boundary: an independent Opus-max audit observed live legacy and
  leased build daemons plus accumulated build directories on the shared host;
  the final read-only census during disposition observed 20 daemon command lines,
  13 connector command lines, and 86 build directories. Counts are volatile
  because other sessions are active. No process was killed or cleaned up in
  this interaction-only change; legacy cleanup and lifecycle redesign remain a
  separate explicitly authorized task.

## Gates

- Complete default suite: `872 passed, 35 skipped` in 216.64 seconds. Every skip
  required an explicit external-root snapshot; the decision-bearing non-
  performance real-root cases were run separately with the recorded snapshots.
- Final focused schema/presentation/navigation/diagnostics/stdio suite: 45
  passed in 18.86 seconds. The last malformed-truncation unit and real FastMCP
  regressions passed separately for both diagnostics tools; source/identity/
  rollover gates added 12 passing cases.
- Ruff: pass.
- Ty: pass with no diagnostics.
- Bootstrap `--check --json`: pass with service-owned CPython 3.12.12, Node
  22.22.0, Pyright 1.1.403, TypeScript 5.9.3, and TypeScript Language Server
  5.1.3 under the pinned runtime.
- Source ownership/provenance/census: pass; 9 copied-source hashes verified,
  census/manifest agreement true, forbidden imports empty, undeclared direct
  dependencies empty.
- Production LOC information: 19,390; `maximum_production_lines=null`.
- Strict OpenSpec validation: both `tighten-agent-interaction` and deferred
  schema-5 `add-lexical-discovery` pass.

## Independent final review and disposition

Fresh Sol-xhigh and Claude/Opus-max clients independently reviewed the exact
production build identity
`a92919af0add2dc6dbc40aa2f5cefd5dfa8db808ab58cb794d82ee615a571374`.
Both returned `PASS` with no P0-P2 blocker. Sol-xhigh reproduced the current
source identity and the malformed-diagnostics FastMCP boundary; Opus-max
recomputed the identity from the dirty tree, received byte-identical initialize
instructions, observed exactly 11 public tools, reran the complete suite as
`872 passed, 35 skipped`, and independently exercised compact references,
overview omission, typed budgets, and the host-census boundary.

Opus-max reported five non-blocking P3 observations. The lead disposition is:

- a malformed internal ambiguity-detail value could use an additional
  defense-in-depth presenter guard, but every current producer emits the
  required integer and the minimum-error branches are unreachable at the 512
  floor;
- one diagnostics fallback branch and its second validation are redundant, but
  do not change behavior;
- one diagnostics catch could include `TypeError`, although its validator emits
  only `ValueError`;
- one coverage mapping compatibility arm accepts a shape no current producer
  emits, while the active reason mapping is total;
- repository-wide format drift predates this change and is not a configured
  release gate.

None changes current behavior or acceptance truth. They are not expanded into
release-churn after two reviewers accepted the exact build; future code that
changes these producer contracts should add the defensive checks in the same
owning change. Final gate disposition is `PASS`.
