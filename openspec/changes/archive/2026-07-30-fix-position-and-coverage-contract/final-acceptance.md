# Position and Coverage Contract Acceptance

Date: 2026-07-30 UTC

## Decision

**PASS — exact-build acceptance and the independent final audits are complete.**
The current working-tree candidate closes the public coordinate, assignment-body,
reference-coverage, TypeScript diagnostics-generation, response-owned target
snapshot, process-owned diagnostics, and changed-document publication-owner
gaps found by the first ablation and successive audits. Stable-spec sync and
archive are now the only remaining mechanical steps. `compact-success-schema`
remains blocked until this change is archived.

The candidate is based on Git HEAD
`9e4987e9f2190a4ff03cb7a35359483a5387f327` with reviewed uncommitted and
untracked implementation, tests, documentation, and OpenSpec artifacts. Its
current repair runtime build identity is
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`,
dependency-lock digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`,
public tool schema is `2`, and build-identity algorithm version is `3`.
Canonical `serena` was not changed. The preceding `4e004ea0...` candidate was
superseded after Sol-xhigh found that cross-file semantic locations could be
mapped against target snapshots read only after the LSP response, diagnostics
reuse could survive a transport restart without a fresh `didOpen`, and Python
assignment recovery still had a name-only fallback whose selection could point
outside the recovered AST target. It also found a diagnostics cancellation
race, a stale LOC receipt, and an acceptance-state test mismatch. The current
build owns bounded two-response semantic replay, process-owned document and
diagnostic state, post-cancel publication re-sampling, selection-bounded Python
assignment recovery, and the corrected acceptance state. Focused regressions
cover every finding. Two attempted Opus-max audits of the superseded build
failed before review because the external API returned `ECONNRESET` and then
`529 Overloaded`; neither attempt is counted as an audit decision.

The first final frozen-tree audits of the later `b173e7ec...` candidate also
returned HOLD. Sol-xhigh found that replay identity still omitted the exact LSP
runtime token and complete capability set, TypeScript guarded editing skipped
the read-side assignment recovery, and the target cap counted only workspace
paths. Opus-max independently reproduced the TypeScript corruption and found
that removing a diagnostics waiter also removed the publication owner, making
all same-generation retries time out. It additionally identified stale current
build wording. The predecessor `b3b9952e...` build binds replay to runtime and
capability identity, caps the complete unique target set before materialization,
retains late current diagnostics for retry, shares TypeScript recovery with the
edit bridge, preserves terminal semicolons, and recovers Python assignments in
module-executed control-flow suites. Its final Sol-xhigh audit returned HOLD
after reproducing a changed-document publication arriving before the new owner
was installed. Opus-max independently confirmed the ordering defect but rated
the current subprocess timing window non-blocking and returned PASS. The lead
retained the stricter disposition because pipe I/O and reader-thread scheduling
provide no happens-before guarantee. Build `481c45e...` installs the
new target before `didChange`, does not reinstall an owner already consumed by
an inline publication, and compare-and-removes only a matching undelivered
target after notification failure. Two deterministic production-class tests
cover inline publication and failure cleanup. Its exact-build Sol-xhigh audit
found that the locked TypeScript engine omits diagnostic document versions, so
a delayed old publication could still consume the new target. Opus-max
independently confirmed the actual unversioned wire shape and reproduced the
stale error publication crossing a `didChange` before the true clean
publication. Both therefore held `481c45e...`. Current build `22c80421...`
retains owner-before-`didChange` only for versioned engines, which reject a
publication without integer version evidence. For unversioned TypeScript it
forgets the old target before `didClose`, then installs the new target before
an exact-full-text `didOpen`; this makes close-generated publications
ineligible and gives the new analysis a causal epoch. Exact-build final review
disproved that claim. Sol-xhigh showed statically that `didClose` is only
written before the new owner is installed, without server acknowledgement, and
Opus-max reproduced the resulting false `CLEAN` through the real product seam
in six of six trials: the locked server's close-generated empty publication
arrived after owner installation, consumed generation 2, and permanently
dropped the true TS2322 publication. Sol-xhigh also found that TypeScript
assignment recovery never verifies that `candidate.name` equals the exact
selected identifier; body lookup and guarded editing can therefore recover a
different statement. Build `22c80421...` is superseded on HOLD. Build
`e26ccf65...` then added the missing bounded same-connection response barrier
after `didClose` and before new ownership, with asynchronous-close, timeout,
response-error, transport, freshness, guarded-edit, and exact-retry
regressions. It also requires the exact selected snapshot text to equal the
TypeScript/JavaScript semantic candidate name, with plain, destructured,
body-read, and guarded-edit fail-closed tests. Its exact-build Sol-xhigh and
Opus-max audits nevertheless returned HOLD: after a first barrier timeout the
retry inferred safety from the URI no longer being open, skipped a second
barrier, and could again accept a late close-generated `CLEAN` as generation
N+1. Opus reproduced that false `CLEAN` on the real locked TypeScript server in
three of three timeout-retry trials; it also identified the same undrained
state gap on LRU and watched-file close paths.

Build `ecc4689b...` owns explicit process-tokened per-URI
undrained-close state independent of open-document ownership. Unknown
`didClose` delivery is redelivered; timeout or response error retains the
marker; only a successful same-connection barrier clears it; process loss
clears it with the dead connection. Every later open drains all recorded
current-process closes before installing a new owner, and the 128-document LRU
plus watched-file/create batching keeps retained markers bounded. Stale
versions are rejected before any close mutation. The adapter suite passes 52
tests, including a 512-distinct-URI bound, and six production-adapter trials
against the pinned real TypeScript server pass the synthetic-first-barrier-
timeout exact retry while returning TS2322 `FINDINGS`. A focused Sol/high review found
no remaining causal or boundedness defect, and its frozen-tree Sol-xhigh final
audit returned PASS. The companion Opus-max audit returned HOLD after
reproducing a different ordinary watcher path on the real pinned server: a
barrier timeout while draining B can leave A locally open, a later created-file
batch can issue a temporary A open/close and record a marker without removing
local ownership, and unchanged cached diagnostics can retain a new owner
without draining that marker. The delayed close-empty publication then produces
sticky false `CLEAN` for A despite TS2322 findings.

The current `4b0a5e2e...` repair skips watcher-created temporary lifecycle for
an already-owned URI, defensively disowns any surviving marker/open overlap,
and drains all current-process close markers before cached diagnostics owner
retention. Barrier timeout or response error retains the marker and cannot
return clean success. The adapter suite now passes 57 tests, including the exact
two-file watcher timeout/recreate chain; the focused runtime/diagnostics suites
bring the lead-owned targeted receipt to 116 passed, and the real pinned-engine
file passes 12 tests. Ruff, Ty, and `git diff --check` pass. Task 5.14 is closed;
full fixed-snapshot and fresh-client acceptance pass, and task 6.6 is closed by
the final dual PASS described below.

The final frozen tree was HEAD `9e4987e9...`, review fingerprint
`ce73c06ed0dd8db2eaf8efba275ccdf245531670f72b4d18224157cddda74f9e`,
and 26 untracked paths. Sol-xhigh returned PASS with no P0, P1, or P2 after
running the exact regressions, 594-unit/57-adapter/12-real-TSLS and related
contract gates. Opus-max independently returned PASS after reconstructing the
ordinary watcher chain against the real pinned server: 8/8 current-code trials
returned TS2322 `FINDINGS`, and 9/9 in-memory negative controls proved the
removed defenses reproduce the prior false `CLEAN` only when both are absent.
It also ran the complete fixed-snapshot suite twice from the verified worktree,
obtaining `747 passed, 1 skipped, 1 warning` in 359.20s and 354.59s. Both
auditors reverified the exact end fingerprint and left the repository and
canonical Serena unchanged.

The lead accepts Opus-max's one residual P2 without holding this archive:
`LspTransportClosed` and `LspProtocolError` can escape this public diagnostics
path as loud generic MCP errors rather than typed retry envelopes. The gap is
pre-existing, never returns `CLEAN` or another wrong success, and is outside
this repair's false-success blocker. It remains explicit follow-up work for the
runtime envelope taxonomy.

## Deterministic and external acceptance

The current complete suite was run with all four external snapshots fixed explicitly:

```bash
SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT='git:deff2f5d117dbe9f9c47e7cd8d5fe3407f1469f7:8d73a4bf48c0489b38164119b158a8f4476b2878a99b9ec4aabbadcbaf5d6a4b' \
SERENA_LIGHT_COORDEXP_SNAPSHOT='git:0490f73b56b352826de5f4b3e697575037582718:107c365432ff36acd2c2af7309487c4dc013108935563951e42bd3e9b0140310' \
SERENA_LIGHT_MS_SWIFT_SNAPSHOT='git:f2797138dba0e224cfff735cd89a528a08d8732a:45696b3ae91193e921ccb9b1dbd5b33c27b7462d4b6281801d22f90d825de19a' \
SERENA_LIGHT_TRANSFORMERS_SNAPSHOT='transformers:4.57.1:4880a9c5bf65f2bb124b7739c74991c1bc2aaf7755133b7fa77ce1e017745dcf' \
uv run --frozen pytest -q
```

Current `4b0a5e2e...` result: `747 passed, 1 skipped, 1 warning in 356.57s`.
Predecessor `ecc4689b...` result: `742 passed, 1 skipped, 1 warning in 350.96s`.
Predecessor `e26ccf65...` result: `733 passed, 1 skipped, 1 warning in 348.08s`.
Superseded `22c80421...` result: `724 passed, 1 skipped, 1 warning in 338.10s`.
The only skip is the separately opt-in external performance case. All four
fixed snapshots matched before and after the run. The same snapshots bind the
foreign repositories without changing them. Their accepted heads are CoordExp
`0490f73`, `cc-plugin-codex` `deff2f5`, `ms-swift` `f279713`, and
research-probes `ccdadc4`.

Predecessor `481c45e...` result: `716 passed, 1 skipped, 1 warning in 331.42s`.

The predecessor `b3b9952e...` suite passed `714 passed, 1 skipped, 1 warning
in 330.85s`. Before the stable current run, one full rerun reached 701 passes
and correctly failed 15 setup gates while `cc-plugin-codex` was being modified;
a second attempt was stopped after the same gate observed another digest move.
Those fail-closed attempts are snapshot-governance evidence, not product test
failures or substitutes for the stable 716-test receipt.

Additional gates:

```text
uv run --frozen ruff check --no-cache src tests scripts
All checks passed!

uv run --frozen ty check --no-progress
All checks passed!

uv run --frozen serena-light-bootstrap --check --json
PASS (service-owned CPython 3.12.12 and locked Node/npm/Pyright/TypeScript)

uv run --frozen serena-light-source-budget --json
PASS: 16,761 production lines; maximum_production_lines=null

openspec validate fix-position-and-coverage-contract --strict
Change 'fix-position-and-coverage-contract' is valid

git diff --check
PASS
```

Source ownership/provenance reports no forbidden or undeclared direct imports,
bidirectional census/copied-manifest agreement, nine copied-symbol hashes, and
official Serena reference commit
`9a9d07e83d8c1cba3458992707f440c624446c6d`. Production LOC is informational
and is not a stop gate.

The current `4b0a5e2e...` unit suite passes `594` tests. Its adapter suite
passes `57` tests, including timeout/response-error retry, uncertain close
delivery, LRU/watched reopen, stale-version ordering, restart, and a
512-distinct-URI marker bound plus the ordinary two-file watcher
timeout/recreate chain and cached-owner recovery. The real TypeScript adapter
file passes `12` tests, including six independent
first-barrier-timeout/exact-retry trials on the pinned server. The predecessor
`ecc4689b...` unit suite passed `589` tests and its adapter suite passed `52`.
The earlier `e26ccf65...` unit suite passed `586` tests
and its joint focused adapter, TypeScript recovery, runtime, and real-engine
suite passed `143` tests.
The discriminating regressions deliver an unversioned close publication only
after `didClose` notification delivery returns and before the same-connection
barrier response; they prove that publication is dropped before replacement
ownership. Barrier timeout, response error, transport loss, and `didClose`
failure install no phantom owner or reopen and permit one exact retry. Plain and
destructured wrong-name selection anchors fail closed through body lookup and
guarded editing. Real locked-engine probes confirm TypeScript omits diagnostic
versions, the product seam reports TS2322 after clean-to-error replacement, and
Pyright publishes an integer version.

The full suite includes real connector/daemon clean- and poisoned-proxy cases,
loopback bypass, exact child cleanup, readiness and typed failures, synchronous
create/change/delete/native-config freshness, Unicode/CRLF/BOM mapping,
symlink substitution, guarded-edit timeout and post-install uncertainty, and
versioned rollover. The final focused semantic/diagnostics regression set
passed 122 tests on the predecessor. The predecessor focused real-engine plus
adapter regression set passed 49 tests, and the predecessor fixed-snapshot
TypeScript/connector/guarded-edit subset passed 67 tests. Coverage includes
target mutation between semantic responses,
generation transition, self-target replay, external/reference typed failure,
transport restart followed by a fresh `didOpen`, publication/cancellation
races and retry recovery, Python assignment selection and control-flow scope,
complete target-cap enforcement, and TypeScript read/edit range parity.

## Fresh client receipts

Three independent fresh client processes exercised the affected diagnostics
surface on exact build `4b0a5e2e...`: Codex/Sol-high, standalone native Claude
Code 2.1.220/Sonnet-high, and CC Agent/Sonnet-high. All three attached to daemon
`c5a2b4fd-fdac-44bc-96ca-17d2787c9021`, read tracked
`src/error.ts` twice as TS2322 `FINDINGS` at SHA-256
`ca6a013872cd6d0ee25275da329487dfecf391549f47470363cf12da3e156872`
without any false `CLEAN`, read tracked `src/clean.ts` twice as `CLEAN` at
SHA-256 `351dca5cefab14f0fd77b610ad8e47442c2dbbfa94cb5d3e0da156cbcb3e7993`,
and returned the complete `export const answer: string = 42;` assignment. No
tool call timed out or returned a typed error. The native process used
`--setting-sources ''`, a Serena-Light-only `--strict-mcp-config`, no built-in
tools, and an exact MCP allowlist; it exited after an immediate release with
zero holders and a stopped runtime. Codex and CC Agent also released their
workspace bindings immediately. Their durable agent-side connector processes
remain live and keep two unbound build leases; control-plane status reports two
active holders attached to live connector PIDs, not orphaned workspaces or LSP
children. No accepted client created `.serena`, and the tracked fixture remained
byte-identical.

Three independent fresh clients exercised exact build `ecc4689b...` through
separate tracked TypeScript fixtures: Codex/Sol-high, standalone native Claude
Code 2.1.220/Sonnet-high, and CC Agent/Sonnet-high. All three read the complete
`export const answer: string = "ok";` assignment at whole-file SHA-256
`a22a59312bc598dd96986d8655f24bf93e6909b1d449a2a8c11dba1c578783b9`,
observed stable baseline `CLEAN`, used hash-guarded `replace_symbol_body` to
install `export const answer: string = 42;` at hash
`ca6a013872cd6d0ee25275da329487dfecf391549f47470363cf12da3e156872`,
and received stable TS2322 `FINDINGS`. Each restored the exact original body and
hash and then observed `CLEAN`. No call timed out or returned a typed tool
failure. All used daemon `22c7b909-442a-4a23-a0d3-3fa430f28d95`; the final
release receipts settled at zero holders with `runtime_stopped=true`, and lead
shell checks found every fixture byte/hash clean.

The same three client surfaces ran the exact-build read-only semantic matrix.
Codex and CC Agent each returned the complete `MAX_RESPONSE_OWNED_TARGETS = 64`
assignment, four references with Python coverage `122/122/0`, the complete
`EXPECTED_BUNDLE` statement ending `});`, and two TypeScript references with
honest coverage `1/11/10`. They reported the same two current Pyright findings
for `runtime.py`, a clean TypeScript probe, and two stable clean
`cc-plugin-codex/runtime/args.mjs` reads at diagnostics generation 1. Both
resolved `StrictBool` as read-only external Pydantic source and
`GenerationConfig` as read-only external Transformers source, then activated
the Transformers root with `identity.kind=allowlisted_non_git` and returned the
class at exact 0-based lines 81--1174. One Codex declaration call returned the
contracted retryable `NOT_READY` after adapter identity changed and succeeded
on its single exact retry. The CC Agent initially escaped the requested capture
parentheses and received two typed `INVALID_INPUT` errors, then corrected the
regexes; those model-input errors were not converted to empty success.

One monolithic native semantic run was rejected as evidence because the model
mis-summarized several verbose success envelopes, including claiming the
complete `EXPECTED_BUNDLE` result was capped. Three shorter fresh native
processes then independently covered A, B/C, and D/E/F with bounded
`max_answer_chars` and returned the same correct fields as Codex and CC Agent.
This is client-side evidence motivating `compact-success-schema`, not a
Serena-Light correctness failure. The accepted native processes used
`--setting-sources ''`, a Serena-Light-only `--strict-mcp-config`, disabled
built-in tools, and exact tool allowlists. An earlier run using only an empty
hooks `--settings` override created a canonical `.serena/` artifact and was
rejected; the lead removed that fixture-only artifact and verified the accepted
isolated run created none.

Three independent fresh clients exercised predecessor build `e26ccf65...` through
separate tracked TypeScript fixtures: Codex/Sol-high, standalone native Claude
Code 2.1.220/Sonnet-high, and CC Agent/Sonnet-high. All three first observed
`CLEAN` at diagnostics generation `1` and read the complete
`export const answer: string = "ok";` assignment with whole-file SHA-256
`a22a59312bc598dd96986d8655f24bf93e6909b1d449a2a8c11dba1c578783b9`.
Each then used hash-guarded `replace_symbol_body` to install
`export const answer: string = 42;`, verified hash
`ca6a013872cd6d0ee25275da329487dfecf391549f47470363cf12da3e156872`,
and received TS2322 `FINDINGS` at diagnostics generation `4`. An unchanged
repeat retained the same generation and finding. Each restored the exact
original body and hash, then observed `CLEAN` at diagnostics generation `7`.
All calls succeeded without a typed tool failure. They shared daemon
`bb51b5a7-3ba9-4e33-b3db-b46082b9adc4` while their runs overlapped or reused
its matching build slot, and every explicit release reported settled cleanup;
the last receipts showed zero holders and `runtime_stopped=true`.

The first native launch was deliberately rejected as acceptance evidence: the
shared Claude settings ran the canonical Serena SessionStart hook and created a
`.serena/` directory even though the model called only Serena Light. The lead
removed that hook artifact, overrode SessionStart/PreToolUse/Stop hooks with
empty lists for the isolated process, confirmed a probe created no `.serena/`,
and reran the full native sequence with the Serena-Light-only strict MCP config.
The accepted run created no canonical Serena artifact. All three fixtures were
byte/hash clean after restoration and then deleted, together with the temporary
native MCP/settings files.

All three fresh clients then ran the same read-only exact-build semantic matrix.
On `/data/CoordExp/serena-light`, each returned the complete Python assignment
`MAX_RESPONSE_OWNED_TARGETS = 64`, four semantic references, and coverage of
122 configured/trusted Python files with zero uncovered. Each returned the
complete TypeScript `EXPECTED_BUNDLE` statement ending in `});`, two references,
and honest partial coverage of one configured file versus eleven trusted files
with ten uncovered. Current diagnostics were typed and non-timeout: `runtime.py`
reported the same two Pyright findings, the TypeScript probe was clean, and two
unchanged `cc-plugin-codex/runtime/args.mjs` calls remained clean at generation
1. From `/data/ms-swift`, all three resolved `GenerationConfig` into
`/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/generation/configuration_utils.py`
as `read_only_external` with an explicit raw LSP basis; direct activation of the
Transformers root returned identity kind `allowlisted_non_git` and a semantic
`GenerationConfig` class at lines 81--1174. The status contract has no separate
top-level `read_only` boolean; the lead rejected that prompt-only assumption and
used the contracted identity/location markers. The two Claude clients initially
escaped the regex grouping parentheses as literal source parentheses and
received `SYMBOL_NOT_FOUND`, then issued a valid one-capture-group regex and
resolved the same declaration. Native Claude also requested unsupported body/info
materialization before the external root was activated and correctly received
`verified_target_snapshot_unavailable`; neither typed failure was converted into
empty success. All three finally activated `/data/CoordExp`, confirmed the root
switch, and released their bindings.

Historical build `22c80421...` was exercised by two fresh clients against
separate tracked TypeScript fixtures while sharing daemon
`55a5a10f-f6e0-463e-ae30-f78c2950566d`: Codex/Sol-high and CC Agent/
Sonnet-high. Those receipts remain predecessor evidence and are not relabelled
as current.

Three fresh clients used predecessor build identity `b3b9952e...`. Each used Serena Light,
not canonical Serena, for the acceptance calls and released its binding with
`runtime_stop_pending=false`.

- Fresh Codex/Sol-high returned the complete `MAX_RESPONSE_OWNED_TARGETS = 64`
  assignment, four references with complete Python coverage, the complete
  `EXPECTED_BUNDLE` statement ending in `});`, honest TypeScript coverage of
  one configured file versus eleven trusted files, and two identical
  `runtime/args.mjs` diagnostic payloads at generation `2`. It also resolved
  external transformers `StrictBool` as `read_only_external` with an explicitly
  raw LSP range and no falsely decoded range/body/info. The requested prompt
  contained one nonexistent `scripts/...` path; the client correctly returned
  `INVALID_PATH`, located the actual `src/...` path semantically, and completed
  the intended evidence. The lead treats the exact-path HOLD as a prompt defect,
  not a product failure.
- Native Claude Code/Sonnet-high ran as a new non-persistent `claude -p`
  process with only the supplied Serena Light MCP configuration and read-only
  Serena Light tools allowed. Built-in tools were disabled. It returned the
  same Python/TypeScript contract and two clean diagnostic reads whose
  generation advanced monotonically from `1` to `2` without timeout. It
  released its holder while another acceptance lease remained.
- A fresh CC Agent/Sonnet-high returned the same contract and two stable clean
  diagnostic reads at generation `1`. In a dedicated nested Git fixture under
  `/data/CoordExp`, it read `export const answer = 1;`, replaced the complete
  statement with `export const answer = 2;`, verified exact bytes and hash,
  then restored the original bytes and original hash. Hashes moved from
  `e1596ee4...cafa` to `6337aece...6a827` and back without duplicating
  declaration syntax. The final holder stopped the runtime, and the exact
  scratch root was deleted after release.

The reproducible native-Claude isolation pattern is:

```bash
claude --no-session-persistence \
  --setting-sources '' \
  --model sonnet --effort high \
  --tools '' --permission-mode dontAsk \
  --strict-mcp-config --mcp-config <serena-light-only-config.json> \
  --allowedTools 'mcp__serena-light__activate_workspace,mcp__serena-light__release_workspace,mcp__serena-light__get_runtime_status,mcp__serena-light__get_symbols_overview,mcp__serena-light__find_symbol,mcp__serena-light__find_referencing_symbols,mcp__serena-light__find_declaration,mcp__serena-light__find_implementations,mcp__serena-light__get_diagnostics_for_file,mcp__serena-light__get_diagnostics_for_symbol' \
  -p '<acceptance prompt>'
```

The MCP config contains only the service-owned connector executable and no
canonical Serena entry. `--setting-sources ''` excludes user, project, and
local settings, including the shared canonical Serena SessionStart hook; an
empty hook list supplied through `--settings` is not a reliable override in
Claude Code 2.1.220. CC Agent acceptance uses the same native Claude MCP
registration through `CLAUDE_CONFIG_DIR=/data/CoordExp/.claude`; the Codex lead
uses the plugin's native subagent path rather than starting `codex exec`.

## Correctness-only ablation rerun

The original frozen four-arm baseline remains unchanged:

| Model | MCP | Baseline score |
|---|---|---:|
| Opus/high | canonical Serena | 98.5 |
| Opus/high | Serena Light | 98.0 |
| Sol/high | canonical Serena | 97.0 |
| Sol/high | Serena Light | 96.5 |

Only the two Serena Light arms were rerun again against final repair build
`b3b9952e...`, with the original two-question prompt. Each arm had only Serena
Light exposed, was instructed to prefer MCP, could use shell only as a declared
fallback, and had the same fixed Q2 verdict key: `YES / YES / NO / NO`.

| Model | Current score | Lead disposition |
|---|---:|---|
| Sol/high | 99.5 | No semantic error; exact snapshot/build ledger, complete freshness/branch/64-target trace, and independently derived `YES / YES / NO / NO`. No source-text shell fallback; the final report was the shorter arm. |
| Opus/high | 99.25 | No semantic error; exact snapshot/build ledger, independently derived `YES / YES / NO / NO`, and the most detailed typed-failure ledger. It correctly treated one `ConnectorSessionLost` release as non-replayable `UNCERTAIN`, read status to obtain the replacement lease, and issued a new release. Its answer and fallback ledger were longer. |

These scores establish correctness recovery. Sol ranks first on the
accuracy-then-efficiency rule by a small efficiency margin; they do not compare
canonical Serena or the still-unimplemented compact success schema.
The original canonical arms were not rerun, and response compaction was not
applied during the exam. The locked four-arm accuracy-then-efficiency rerun
belongs to `compact-success-schema`.

## Proxy and lifecycle boundary

Model clients retain the ambient `9090` proxy when external network access is
needed. Bootstrap may also inherit it for Python/Node/npm downloads. Connector
to localhost, daemon health checks, and local acceptance/fault HTTP use an
explicit no-proxy transport; daemon and LSP child environments remove every
case variant of `*_PROXY`. No global proxy or `NO_PROXY` setting is modified.
Fresh-client runs and clean/poisoned internal tests are therefore different,
recorded evidence surfaces rather than interchangeable labels.

All fresh-client workspace bindings were released. The standalone native
client exited with zero holders and `runtime_stopped=true`. Durable Codex and
CC Agent identities keep unbound live connector leases while those agent
sessions remain addressable; the final audit distinguished those live holders
from old zero-client `.venv` daemons and found no current-build orphan or leaked
LSP child. Nothing was signalled or killed, and canonical Serena processes were
not cleanup candidates.

## Stop-rule and residual boundaries

The repair does not require Serena's agent, modes, or project-server
architecture; a new language; lexical-semantic result mixing; or a new
trust/edit authority. The change therefore proceeds to stable-spec sync and
archive rather than returning to design. Remaining declared limits are:

- TypeScript fixture reference coverage is intentionally path-scoped and
  reports `configured=1`, `trusted=11`, `uncovered=10`; it is not represented
  as workspace-complete.
- TypeScript/JavaScript later declarators in an unowned comma declaration stay
  fail-closed when the server supplies no unique variable-statement ancestry.
- Compact success envelopes and their efficiency comparison remain entirely
  outside this change.
- Transport/protocol loss can still surface as a loud generic MCP error rather
  than a typed retry envelope; this does not produce false semantic success and
  is deferred to runtime-envelope hardening.
