# Correctness Repair Baseline

Captured before production-source edits on 2026-07-30 UTC through the registered
Serena Light connector and its actual MCP `CallToolResult.content[0].text`.

## Source and runtime identity

- Git source commit: `9e4987e9f2190a4ff03cb7a35359483a5387f327`
- Live/source build identity: `d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`
- Dependency-lock digest: `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`
- Raw `uv.lock` SHA-256: `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca`
- Build-identity algorithm: `3`
- Public tool schema: `1`
- Serena Light server: `0.1.0`, MCP protocol `2025-11-25`
- Pyright: `1.1.403`, position encoding `utf-16`, interpreter `/root/miniconda3/envs/ms/bin/python`
- TypeScript language server: `5.1.3`, position encoding `utf-16`
- Production LOC: `14,837` (informational only; `maximum_production_lines=null`)
- Source/provenance census at capture: pass, 9 copied hashes verified, no forbidden or undeclared direct external imports

## Client-visible response fixtures

The complete observed text is preserved without reserialization:

| Query | Evidence | Characters |
| --- | --- | ---: |
| exact `PositionMapper/lsp_to_text_offset` | `evidence/baseline-navigation.json` | 926 |
| `PUBLIC_TOOL_SCHEMA_VERSION` with `include_body=true` | `evidence/baseline-python-constant.json` | 967 |
| semantic references for the same symbol | `evidence/baseline-references.json` | 12,031 |
| diagnostics for `positions.py` | `evidence/baseline-diagnostics.json` | 962 |

The navigation fixture demonstrates the defect directly: the Python method
beginning at source line 108 (0-based) is emitted as `line=109`, and its column
is likewise incremented. The constant fixture returns only
`PUBLIC_TOOL_SCHEMA_VERSION` as the body and omits ` = "2"`. The reference fixture records the current semantic
result without a query-level configured-program coverage object. The diagnostic
fixture is a genuine current-generation `clean` result, retained to compare the
unchanged authority/generation envelope after range rendering changes.

## Capture commands

- `mcp__serena_light__activate_workspace(/data/CoordExp/serena-light)`
- `mcp__serena_light__get_runtime_status()`
- `mcp__serena_light__find_symbol(... PositionMapper/lsp_to_text_offset ...)`
- `mcp__serena_light__find_referencing_symbols(... PositionMapper/lsp_to_text_offset ...)`
- `mcp__serena_light__get_diagnostics_for_file(... positions.py ...)`
- `.venv/bin/python -m serena_light.source_budget --json`

The three saved fixtures are the MCP content text itself, not `SuccessEnvelope.to_json()`
or a service-internal dictionary dump.
