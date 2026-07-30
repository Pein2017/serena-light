# Fresh Codex post-repair acceptance receipt

Archival copy of the immediate fresh-client Serena Light schema-3 acceptance receipt.

Fresh-client Serena Light schema-3 post-repair acceptance: PASS. Workspace activated and released cleanly; no file changes.

- Schema surface:
  - `find_symbol.max_matches`: present (`integer`, 1–100).
  - `get_symbols_overview.include_kinds` / `exclude_kinds`: present as lowercase LSP-kind string arrays.
  - `find_declaration.max_answer_chars`: present.
  - `find_implementations.include_kinds` / `exclude_kinds`: remain integer arrays.
  - No public `compact` flag or `max_candidates_per_adapter` parameter appeared in the fresh tool schemas.

Overview receipts (exact compact responses):

```json
{"ok":true,"data":{"workspace":"/data/CoordExp/serena-light","files":[{"path":"tests/integration/fixtures/compact_navigation/large_nested.py","symbols":[{"name":"LargeFixture","kind":"class"}]}],"omitted":20}}
```

```json
{"ok":true,"data":{"workspace":"/data/CoordExp/serena-light","files":[{"path":"tests/integration/fixtures/compact_navigation/large_nested.py","symbols":[{"name":"LargeFixture","kind":"class","children":[{"name":"method_00","kind":"method"},{"name":"method_01","kind":"method"},{"name":"method_02","kind":"method"},{"name":"method_03","kind":"method"},{"name":"method_04","kind":"method"},{"name":"method_05","kind":"method"},{"name":"method_06","kind":"method"},{"name":"method_07","kind":"method"},{"name":"method_08","kind":"method"},{"name":"method_09","kind":"method"},{"name":"method_10","kind":"method"},{"name":"method_11","kind":"method"},{"name":"method_12","kind":"method"},{"name":"method_13","kind":"method"},{"name":"method_14","kind":"method"},{"name":"method_15","kind":"method"},{"name":"method_16","kind":"method"},{"name":"method_17","kind":"method"},{"name":"method_18","kind":"method"},{"name":"method_19","kind":"method"}]}]}],"omitted":0}}
```

```json
{"ok":true,"data":{"workspace":"/data/CoordExp/serena-light","files":[],"omitted":21}}
```

- Overview counts: (a) 1 file / 1 returned node / omitted 20. (b) 1 file / 21 returned nodes / omitted 0; all 20 methods remain reachable beneath the retained `LargeFixture` class ancestor. (c) 0 files / 0 nodes / omitted 21, confirming empty filtering still reports omitted content.
- `find_implementations(Runner, typescript_symbols.ts)`:
  - `include_kinds=[5]`: `ok=true`, 0 entities.
  - `include_kinds=[1]`: `ok=true`, 0 entities.
  - `include_kinds=[999]`: typed `INVALID_INPUT` (`field: "include_kinds or exclude_kinds"`).

Budget error receipt (exact compact response):

```json
{"adapter":{"language":"python","name":"pyright"},"error":{"code":"INVALID_INPUT","details":{"field":"max_answer_chars","minimum_required_chars":1410},"message":"input is invalid","retry":null},"generations":{"document":4,"index":0,"program":1,"scope":"path","trust":0},"ok":false,"workspace":{"kind":"git","root":"/data/CoordExp/serena-light","working_subdirectory":"/data/CoordExp/serena-light"}}
```

`find_symbol(LargeFixture, include_body=true, max_answer_chars=512)` correctly failed atomically: `minimum_required_chars=1410`; workspace, adapter, and generations were present, and no partial body appeared.

Runtime: `build_identity=ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`; `daemon_id=9bc40cee-8724-4ef9-a91e-adc5348f1265`.

Release receipt: `released=true`, `bound=false`, `runtime_stopped=true`.
