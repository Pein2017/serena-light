# Fresh CC Agent Post-Repair Acceptance

The following outer-assistant receipts are retained verbatim from the fresh
read-only CC Agent. The second receipt resolves the first receipt's omitted
`relative_path` argument and demonstrates path-scoped authority metadata.

## Receipt 1 — 2026-07-30T21:02:22.944Z

## Serena Light Schema-3 Post-Repair Acceptance — Receipt

**Scope honored:** activated `/data/CoordExp/serena-light` via Serena Light MCP only; no shell, no edits, no canonical `mcp__serena__*` tools invoked. Workspace released at the end.

### 1. Activation
`activate_workspace("/data/CoordExp/serena-light")` → `ok:true`, lease `91bb46bb-23b2-4838-88fe-e92216135af4`, workspace identity/working_subdirectory both `/data/CoordExp/serena-light`.

### 2. Fresh-client tool schema inspection
| Check | Result |
|---|---|
| `find_symbol.max_matches` exists | **Yes** — `default:20`, range `1–100` |
| `get_symbols_overview` lowercase string include/exclude filters | **Yes** — `include_kinds`/`exclude_kinds`: arrays of "stable lowercase LSP kind names" |
| `find_declaration.max_answer_chars` exists | **Yes** — `default:12000`, range `512–50000` |
| `find_implementations` keeps integer include/exclude filters | **Yes** — `include_kinds`/`exclude_kinds`: arrays of `integer` (raw LSP kind codes) |
| No public `compact`/`max_candidates_per_adapter` | **Confirmed absent** — not a parameter on any of the 11 exposed tools |

### 3. `get_symbols_overview` on `large_nested.py`
- **max_depth=0**: `{"files":[{"path":"...large_nested.py","symbols":[{"name":"LargeFixture","kind":"class"}]}],"omitted":20}` — class shown with no children at depth 0; the 20 methods are counted in `omitted`, not emitted as nodes.
- **max_depth=1, include_kinds=["method"]**: full node list, `LargeFixture` → 20 children `method_00`…`method_19`, all `"kind":"method"`, `"omitted":0`. Filter passed through cleanly with no unfiltered noise (no other kinds existed at that depth to strip).
- **max_depth=1, include_kinds=["unknown:99"]**: `{"files":[],"omitted":21}` — unrecognized kind name yields empty file list; every node (class + 20 methods = 21) is silently counted as omitted rather than erroring. No typed rejection for an unknown kind string, unlike the integer-kind path in step 4.

### 4. `find_implementations` for `Runner` in `typescript_symbols.ts`
- `include_kinds=[5]` (Class): `{"files":[],"omitted":0}` → **0 results**
- `include_kinds=[1]` (File): `{"files":[],"omitted":0}` → **0 results**
- `include_kinds=[999]` (invalid): typed failure — `{"ok":false,"error":{"code":"INVALID_INPUT","message":"input is invalid","details":{"field":"include_kinds or exclude_kinds"}}}`
- Cross-check (unfiltered, for calibration only): 1 target found at `[[4,13],[4,27]]`. So both kind-5 and kind-1 filters exclude the one real implementation — its actual kind is neither 5 nor 1 — while `999` is rejected outright as an unknown code rather than silently filtering to zero. Worth flagging: `get_runtime_status` (step 6) shows `typescript_symbols.ts` sits in `trusted_not_in_configured_program` (`omitted_by_engine_workspace_program`) for the typescript adapter, so filtered vs. unfiltered divergence here may be entangled with program-scope trust state rather than pure kind filtering — not re-verified further per read-only/no-shell scope.

### 5. `find_symbol` for `LargeFixture`, `include_body=true`, `max_answer_chars=512`
Exact error JSON:
```json
{"ok":false,"error":{"code":"INVALID_INPUT","message":"input is invalid","retry":null,"details":{"field":"max_answer_chars","minimum_required_chars":1410}},"workspace":{"root":"/data/CoordExp/serena-light","kind":"git","working_subdirectory":"/data/CoordExp/serena-light"}}
```
- `minimum_required_chars`: **1410**
- Authority reported: **workspace** only (`root`, `kind:"git"`, `working_subdirectory`) is present at top level of the error envelope — no `adapter` or `generation` fields appear in this error shape (those live under `get_runtime_status`, not per-call errors).
- **No partial body returned** — the call fails typed/whole rather than truncating, confirming "complete or fail typed" body semantics.

### 6. `get_runtime_status` + release
- `build_identity`: `ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`
- `daemon_id`: `9bc40cee-8724-4ef9-a91e-adc5348f1265`
- (server_version `0.1.0`, protocol_version `2025-11-25`; both adapters — pyright for python, typescript-language-server for typescript — reported `phase:"ready"`.)
- `release_workspace()` → `{"ok":true,"released":true,"bound":false,"active_holders":0,"runtime_stopped":false}`

**No files changed.** All findings above come solely from Serena Light MCP tool responses; no canonical Serena or shell commands were used.

## Receipt 2 — 2026-07-30T21:04:51.623Z

## Recheck receipt

**Activation:** `activate_workspace("/data/CoordExp/serena-light")` → `{"ok":true,"data":{"lease_id":"5fa06b3e-b6fa-4de9-8315-c1d52989c26e","workspace":{"identity":"/data/CoordExp/serena-light","working_subdirectory":"/data/CoordExp/serena-light"}}}`

**`find_symbol` for `LargeFixture`** (`relative_path="tests/integration/fixtures/compact_navigation/large_nested.py"`, `include_body=true`, `max_answer_chars=512`), called exactly once — complete raw JSON verbatim:

```json
{"ok":false,"error":{"code":"INVALID_INPUT","message":"input is invalid","retry":null,"details":{"field":"max_answer_chars","minimum_required_chars":1410}},"workspace":{"root":"/data/CoordExp/serena-light","kind":"git","working_subdirectory":"/data/CoordExp/serena-light"},"adapter":{"name":"pyright","language":"python"},"generations":{"trust":0,"program":1,"document":10,"index":1,"scope":"path"}}
```

**Discrepancy resolution:** in the prior receipt this same call was made with only `name_path` and no `relative_path`, and the error envelope carried `workspace` alone (no `adapter`, no `generations`). With `relative_path` supplied this time, the identical `INVALID_INPUT`/`minimum_required_chars:1410` error additionally carries `"adapter":{"name":"pyright","language":"python"}` and `"generations":{"trust":0,"program":1,"document":10,"index":1,"scope":"path"}` — **adapter and generations are present when the call resolves to a specific path-scoped adapter; they were absent in the earlier unscoped call.**

**`get_runtime_status`:**
- `build_identity`: `ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`
- `daemon_id`: `9bc40cee-8724-4ef9-a91e-adc5348f1265`
- `adapter` and `generations` fields: **present** — `runtime.adapters.python.generations = {"trust":0,"program":1,"document":10,"index":1}` (matches the error's document generation `10`) and `runtime.adapters.typescript.generations = {"trust":0,"program":1,"document":5,"index":1}`.

**Release:** `release_workspace()` → `{"ok":true,"data":{"lease_id":"5fa06b3e-b6fa-4de9-8315-c1d52989c26e","released":true,"bound":false,"active_holders":0,"runtime_stopped":false}}`

No files changed; Serena Light MCP only, no shell.
