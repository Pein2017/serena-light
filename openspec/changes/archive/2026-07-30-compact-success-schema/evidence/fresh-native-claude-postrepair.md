# Fresh Native Claude Code Post-Repair Acceptance

- Date: 2026-07-30 UTC
- Claude Code: 2.1.220
- Model / effort: `claude-sonnet-5` / `high`
- Session: `e81e0ddb-e713-45b1-967f-54f676c3a8d5`
- Duration: 42.548 seconds API, 40.575 seconds end to end
- Exit status: success; no permission denials
- MCP boundary: `--strict-mcp-config` with only `serena-light`,
  `--setting-sources ''`, `--permission-mode dontAsk`, and an explicit allowlist
  containing only the five read-only Serena Light tools used by the prompt
- Shell/file/edit/web/canonical-Serena calls: none

## Receipt

Fresh tool-schema inspection found:

- `find_symbol.max_matches`: integer 1 through 100, default 20.
- `get_symbols_overview.include_kinds` and `exclude_kinds`: lowercase LSP-kind
  string arrays.
- `find_declaration.max_answer_chars`: 512 through 50,000, default 12,000.
- `find_implementations.include_kinds` and `exclude_kinds`: integer arrays.
- No public `compact` or `max_candidates_per_adapter` parameter.

Activation returned workspace identity and working subdirectory
`/data/CoordExp/serena-light`.

Exact `get_symbols_overview(max_depth=0)` result:

```json
{"ok":true,"data":{"workspace":"/data/CoordExp/serena-light","files":[{"path":"tests/integration/fixtures/compact_navigation/large_nested.py","symbols":[{"name":"LargeFixture","kind":"class"}]}],"omitted":20}}
```

With `max_depth=1, include_kinds=["method"]`, the response retained the class
as the structural ancestor of all 20 methods: 21 returned nodes and
`omitted=0`.

Exact `get_symbols_overview(max_depth=1,
include_kinds=["unknown:99"])` result:

```json
{"ok":true,"data":{"workspace":"/data/CoordExp/serena-light","files":[],"omitted":21}}
```

Exact indivisible-body budget error from
`find_symbol(LargeFixture, include_body=true, max_answer_chars=512)`:

```json
{"ok":false,"error":{"code":"INVALID_INPUT","message":"input is invalid","retry":null,"details":{"field":"max_answer_chars","minimum_required_chars":1410}},"workspace":{"root":"/data/CoordExp/serena-light","kind":"git","working_subdirectory":"/data/CoordExp/serena-light"},"adapter":{"name":"pyright","language":"python"},"generations":{"trust":0,"program":1,"document":9,"index":1,"scope":"path"}}
```

The error retained workspace, adapter, and generation authority, reported the
exact minimum, and contained no partial body.

Runtime identity:

- build identity:
  `ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`
- daemon ID: `9bc40cee-8724-4ef9-a91e-adc5348f1265`

Release succeeded with `released=true`, `bound=false`, zero active holders, and
`runtime_stop_pending=false`.
