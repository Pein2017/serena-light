# TypeScript Real Acceptance (OpenSpec 9.3)

## Current snapshot-bound candidate status

**PASS: 23 passed in 23.02s.** Candidate build `d46175203f8b...` removes these real
TypeScript acceptance, integration, and admission cases from the deterministic
default suite unless
`SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT` matches the exact observed Git HEAD,
tracked binary diff, untracked file/symlink content, and the ignored TypeScript
launcher/native-executable authority before and after every test. The same gate
covers real TypeScript integration and the real-root scope probe. A supplied
mismatch or any mid-test mutation fails; it is never `xfail`.

`/data/CoordExp/cc-plugin-codex` initially changed repeatedly, then remained
stable at authority-profile snapshot
`git:a782af11090b8990fc3717dc2a809a28844ab948:4129f2174f2f7ac8476b9edb87b1d4cab798d00410b464d3b53abd3d6420ff17`
through six semantic/diagnostic cases, the repository-native TypeScript
authority check, three real TypeScript integration cases, and six admission
probe cases. The gate verified the same identity after every test. Historical
passes below remain evidence for older builds only.

## Post-audit current-build rerun

**PASS at build `f4ee8a248a8c...`: 6 passed.** This 2026-07-29 post-restoration
full-suite rerun used the
production workspace/runtime and real TypeScript language server against
`/data/CoordExp/cc-plugin-codex`, including the repository-native typecheck
contrast. It is real-repository plus real-LSP evidence; it does not traverse
the shared daemon/connector.

```text
uv run pytest -q tests/acceptance/test_typescript_real_acceptance.py
6 passed
```

## Superseding final rerun

**PASS.** The repaired production API passes all six real-repository scenarios:

```text
uv run pytest -q tests/acceptance/test_typescript_real_acceptance.py
6 passed in 8.1s
```

The current run selects `tsconfig.json`, attributes the pinned TypeScript 5.9.3
configured program without engine-library leakage, serves configured and
path-scoped MJS, preserves Unicode offsets, resolves definition/references and
implementation locations lacking `SymbolKind`, returns current advisory
diagnostics with the discoverable authoritative `npm run typecheck` command,
and cleans owned language-server children. The blocked record below is retained
only as historical red evidence.

---

## Historical pre-fix decision

## Decision

**BLOCKED.** The production `WorkspaceRuntime` and public semantic API were
exercised against the live `/data/CoordExp/cc-plugin-codex` checkout with the
locked Serena Light engines. Three of six focused acceptance tests pass. The
remaining failures are decision-bearing product gaps, so task 9.3 must remain
unchecked.

This acceptance deliberately distinguishes three authorities:

- the production Serena Light API is the navigation and diagnostic contract
  under test;
- the pinned TypeScript 5.9.3 engine supplies advisory LSP semantics;
- the repository's `npm run typecheck`, currently TypeScript 7.0.2, is the
  authoritative repository correctness check.

No mock or raw-LSP result is used for a pass claim below. The existing raw LSP
integration tests remain supporting evidence only.

## Frozen inputs

- Serena Light root: `/data/CoordExp/serena-light`
- Target root: `/data/CoordExp/cc-plugin-codex`
- Target branch/HEAD: `main` / `f08dceed02cf70b26ca86fb9bfc44913e8c4656f`
- Target state: dirty, with 33 tracked/untracked status entries at acceptance
  time; the test is read-only and evaluates that exact live tree.
- Lock digest: `f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865`
- Locked Node: `22.22.0`
- Locked `typescript-language-server`: `5.1.3`
- Locked TypeScript: `5.9.3`
- Repository-native TypeScript: `7.0.2`
- Focused acceptance owner:
  `tests/acceptance/test_typescript_real_acceptance.py`

## Acceptance evidence

| Surface | Result | Production evidence |
| --- | --- | --- |
| Configured-program admission | **FAIL** | Initial public status reports `selected_native_config=null`, `project_kind=workspace_default`, and configured-program count `1`. It attributes `eslint.config.mjs` instead of selecting `tsconfig.json`; `runtime/args.mjs` is consequently reported omitted. This cannot support a configured-program claim. |
| MJS overview | PASS (path-scoped only) | `get_symbols_overview(runtime/args.mjs, max_depth=2)` returns `parseArgs` and `splitRawArgumentString` with typed workspace, adapter, generation, range, and truncation metadata. It is not promoted to a configured-program pass because admission failed above. |
| Exact find/body | PASS (path-scoped only) | `find_symbol(parseArgs, runtime/args.mjs, include_body=true, include_info=true)` returns the exact source slice and the same whole-file SHA-256 as overview. |
| Definition | PASS (path-scoped only) | `find_declaration` resolves the captured `parseArgs` import occurrence in `runtime/cli.mjs` to one `runtime/args.mjs` location through public definition semantics. |
| References | PASS (path-scoped only) | `find_referencing_symbols` returns at least the declaration and `runtime/cli.mjs` call, maps the call to containing symbol `parse`, and also tolerates the live checkout's additional `runtime/operator-cli.mjs` reference. |
| Implementation | **FAIL** | `find_implementations(parseArgs, runtime/args.mjs)` returns `ok=false`, `code=INVALID_INPUT`, `details.field=normalized_locations.kind`. The raw server capability is advertised, but the public result is rejected because the returned location lacks the kind required by the implementation core. |
| Omitted-file path scope | PASS | The trusted omitted `tests/runtime/agent-completion-projection.test.mjs` is served by `get_symbols_overview` with generation scope `path`; configured-program count/digest and native-config/project-kind status are byte-for-byte unchanged before and after the query. |
| Unicode range | PASS | For a symbol after both `界` and `🙂`, public range start `text_offset=6075` and `byte_offset=6080` reproduce the exact UTF-8 prefix length. The byte/text difference is retained rather than treating UTF-16 positions as Python or byte offsets. |
| Current diagnostics | **FAIL** | A fresh production runtime returns `TIMED_OUT` after 20.0 seconds for `runtime/args.mjs`, target generation `1`; it does not return `findings` or `clean`. Timeout remains correctly non-clean. |
| Diagnostic authority metadata | Partial | The timeout envelope correctly reports `authority=advisory`, repository-native typecheck as authoritative, and pinned engine `typescript 5.9.3`. Its `native_typecheck` object has no `command`, so it does not disclose the discoverable `npm run typecheck` authority required by the spec. |
| Native typecheck comparison | PASS as independent authority | `npm run typecheck` exits `0` under repository TypeScript `7.0.2`. Direct pinned TypeScript `5.9.3` compilation exits `2` with `runtime/internal-runtime.mjs(166,15): TS2721 Cannot invoke an object which is possibly 'null'.` This records a real version divergence; it does not claim that compiler output is equivalent to the timed-out LSP response. |
| Process cleanup | PASS | The module fixture stops `WorkspaceRuntime`, waits up to five seconds, and proves no newly created `typescript-language-server` or `tsserver.js` descendant remains. No target artifact is written. |

## Commands and receipts

Focused acceptance, using the repository-owned locked Python environment:

```text
/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/python/bin/python \
  -m pytest -q tests/acceptance/test_typescript_real_acceptance.py

FF.F..                                                                   [100%]
3 failed, 3 passed in 27.35s
```

The three failures are:

1. production configured-program attribution does not select `tsconfig.json`;
2. diagnostics time out and omit the native typecheck command;
3. public implementation normalization rejects a location without `kind`.

Static validation of the new acceptance owner:

```text
.../python/bin/ruff check tests/acceptance/test_typescript_real_acceptance.py
All checks passed!

.../python/bin/ty check tests/acceptance/test_typescript_real_acceptance.py
All checks passed!
```

Repository-native authority:

```text
cd /data/CoordExp/cc-plugin-codex
npm run typecheck
# exit 0
```

Pinned-engine contrast:

```text
.../node/bin/node .../node_modules/typescript/lib/tsc.js -p tsconfig.json --pretty false
runtime/internal-runtime.mjs(166,15): error TS2721: Cannot invoke an object which is possibly 'null'.
# exit 2
```

## Stop boundary

The acceptance test and this report are the only owned writes. Product source
and `tasks.md` were not edited. Re-run this focused acceptance after the three
product gaps are repaired; only a fully green production-API run can close
OpenSpec task 9.3.
