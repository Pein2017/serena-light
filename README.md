# serena-light

`serena-light` is an internal, agent-first semantic code navigation service for
the CoordExp environment. It is an independently owned, deliberately small
derivative of selected MIT-licensed Serena and SolidLSP mechanisms.

The repository has archived its first OpenSpec change. The admission probes,
provenance census, owned LSP core, workspace/adapter/daemon layers, containment,
repair-state test/static gates, fresh clients, guarded-edit restoration, and
the final dual audit, stable-spec sync, and archive all pass.

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
`openspec/changes/archive/2026-07-29-build-serena-light-v1/scope-admission-report.md`; the older
`admission-report.md` is retained as historical evidence for the rejected
Git-equals-program assumption.

## Implementation status

The owned LSP core, workspace identity/trust/scope model, fixed Pyright and
TypeScript adapters, shared daemon, connector, readiness generations, and
process lifecycle are implemented. Fresh Codex, native Claude Code, and CC
Agent sessions pass the four-root semantic/status matrix plus isolated guarded
edit/restore on the current repaired identity.
The current real-stdio connector independently passes its guarded-edit and
cross-library declaration contracts.
Canonical `serena` remains unchanged pending a separate user decision.

**Current state: `PASS — V1 ARCHIVED`.** Exact-head audits
of `6fce244` confirmed the prior freshness, transport, adapter-admission, native
source, and hermetic-stdio blockers were closed, then found that a runtime owner
could still pin one failed cleanup future forever and that the native TypeScript
gate used ambient Node/npm. They also required exact-build native Claude Code
evidence. A later exact-head Sol audit then reproduced a same-size/same-stat
freshness miss; Opus passed the runtime repair and identified only diagnostic
test/documentation cleanup. A later Sol audit showed that one streaming pass
could still miss a concurrent same-stat rewrite to an already-read region. The
current candidate therefore requires two matching guarded byte-identity passes
for Git sources and native configs while keeping external transformers checks
caller-targeted. Fresh Codex/Terra, native Claude Code/Sonnet, and CC
Agent/Sonnet all pass this exact build across four roots plus isolated
hash-guarded edit/restore.
Exact-current-head Sol-xhigh and Opus-max both returned PASS on clean commit
`c2dffca` with no P0/P1/P2 release blocker.
Agent-public
`replace_symbol_body` remains restored after its fault
matrix, full regression, and three fresh-client hash-edit receipts passed. This
is v1 PASS. Stable specs are synced and the owning change is archived. Source ownership/provenance
passes with 9 copied hashes against official Serena commit
`9a9d07e83d8c1cba3458992707f440c624446c6d`; production LOC (14,837) is
informational only and not gated. That archived v1 repair has build identity
`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`.
See
[the final acceptance record](openspec/changes/archive/2026-07-29-build-serena-light-v1/final-acceptance.md)
for the full gate evidence and residual risks.

See [client registration](docs/client-registration.md) for parallel setup and
rollback, and [the compatibility inventory](docs/compatibility.json) for the
public contract delta.

## Correctness revision archived

OpenSpec change `fix-position-and-coverage-contract` has passed exact-build
acceptance and independent Sol-xhigh/Opus-max final audits. It changes agent-facing
navigation and diagnostic positions to 0-based decoded-text lines and Unicode
code-point columns, makes Python and TypeScript/JavaScript variable-assignment
body requests and guarded edits share one complete fail-closed statement range,
including Python module control-flow suites and TypeScript/JavaScript terminal
semicolons, and adds one
bounded configured-program coverage
object to semantic reference success. The revision is accepted, synchronized
to the stable specs, and archived; existing success envelopes remain verbose
until the separate dependent response-compaction change is accepted. Canonical
`serena` remains unchanged.
The current repair candidate build identity is
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`.
This is the latest independently dual-audited candidate; predecessor
`ecc4689b781c2de8c4bf03788a4dc17388c28e402220b99519294f31010dc358`
is superseded on HOLD.
The final audits of the preceding `481c45e...` build showed that the locked
TypeScript server omits document versions from `publishDiagnostics`, so a
delayed old publication could still be assigned to a newer `didChange` owner.
That build forgot old tracking before `didClose` and installed the new owner
before exact-full-text `didOpen`, but Sol-xhigh and Opus-max proved the close
publication can arrive after owner installation and deterministically become a
false `CLEAN`. Sol-xhigh also found that TypeScript assignment recovery did not
bind `candidate.name` to the selected identifier, so a wrong statement could be
read or edited. Build `e26ccf65...` inserted the bounded same-connection
response barrier and exact candidate-name check, but final Sol-xhigh and
Opus-max audits found that a barrier timeout lost the retry obligation and could
again produce false `CLEAN`. Build `ecc4689b...` kept explicit process-tokened
undrained-close state across timeout/response failure and across LRU or watched
closes, drains all recorded closes before any later open, and bounds retained
markers. Its Sol-xhigh final audit passed, but Opus-max reproduced a sticky
false `CLEAN`: a failed watcher drain could leave A locally open, a later
created-file batch could temporarily open/close A and record a marker, and the
unchanged diagnostics fast path could retain a new owner without draining it.
The current `4b0a5e2e...` candidate skips temporary watcher lifecycle for an
already-owned URI and drains any current-process close marker before cached
diagnostics owner retention. Focused unit and real-engine tests pass, including
the ordinary two-file watcher timeout/recreate chain. The four-snapshot suite
passes 747 tests with one intentional performance skip, and fresh Codex, native
Claude Code, and CC Agent diagnostics acceptance passes on this build. Sol-xhigh
found no P0/P1/P2; Opus-max reproduced the repaired chain 8/8 and returned PASS
with one non-blocking pre-existing P2 for loud untyped transport/protocol errors.
See [its archived acceptance record](openspec/changes/archive/2026-07-30-fix-position-and-coverage-contract/final-acceptance.md).

## Compact navigation success schema (accepted)

OpenSpec change `compact-success-schema` implements replacement of repeated
navigation-success metadata with one deterministic file-grouped `{"ok":true,
"data":{"workspace":...,"files":[...],"omitted":...}}` envelope, bounded by the
actual client-visible MCP text rather than an inner fragment. Its hard
prerequisite, the archived `fix-position-and-coverage-contract` revision above,
is satisfied. The final-repair schema-3 candidate is implemented at build
`92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
and has passed fixed-fixture real-connector payload gates, the 821-test
snapshot-bound suite, the opt-in transformers performance gate, and fresh
Codex, native Claude Code, and CC Agent boundary checks. The final fixed-contract
four-arm repeat and the independent Sol-xhigh static and Opus-max
runtime/evidence audits are complete and pass with no P0/P1 findings. Stable
specs are synchronized and the change is archived at
`openspec/changes/archive/2026-07-30-compact-success-schema`. A live
schema-2 holder continued serving the verbose schema during rollover.
`openspec/changes/archive/2026-07-30-compact-success-schema/design.md`
records the fixed decisions — one navigation-only compact envelope,
tool-specific compact records, deterministic match selection, exact-MCP-text
budgeting, and an explicit no-shim schema rollover — and its delta
`specs/semantic-navigation/spec.md` states the target requirements. A
read-only external target lacking an exact response-owned snapshot carries
`raw_range` plus a `position_basis` string instead of `range` and is never
relabelled as decoded-text; trusted external identity remains the file group's
own absolute `path` plus an optional `read_only=true` marker, with no second
non-forgeable external identifier. See
[the compatibility inventory](docs/compatibility.json)'s `migration_examples`
for exact old-to-new field mappings and representative compact payloads for
all five navigation tools. Both final audits passed and the compatibility
inventory records release acceptance without changing canonical Serena.

## Local checks

```bash
serena-light-bootstrap --check --json
serena-light-source-budget --json
pytest -q
ruff check src tests scripts
ty check
```
