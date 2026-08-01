# Schema 3 Interaction Baseline

Captured on 2026-08-01 before schema-4 implementation.

## Fixed point

- Source commit: `e66c70e043784a677b584682302b1702e363aa65`
- Branch: `main`
- Pre-existing repository state: only the untracked planning change
  `openspec/changes/tighten-agent-interaction/`
- Public tool schema: `3`
- Dependency lock digest:
  `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`
- Build identity:
  `7d8dde45a8d91e2aeaaadc61e28e99771272cbdd81bc9c374584db82d7bf6d80`
- Runtime server version: `0.1.0`
- Runtime protocol version: `2025-11-25`

The stdio connector was started from `/data/CoordExp/serena-light` through the
repository venv. Its initialize result had `instructions=None`, listed 11 public
tools, and the Python-model dumps of those tool definitions occupied 9,483
characters. The fresh client released its workspace immediately after capture.

## Connector-visible payloads

All character counts below are the actual `CallToolResult.content[0].text`
length observed through the live schema-3 Serena Light connector. The workspace
was `/data/CoordExp/serena-light`; no `cc-plugin-codex` source was queried.

| Fixture | Text chars | Key schema-3 evidence |
| --- | ---: | --- |
| `get_symbols_overview(large_nested.py)` with public default | 961 | Default depth returned 20 method children. |
| Same overview with `max_depth=0` | 209 | `omitted=20`, although depth was caller-selected rather than budget truncation. |
| `find_referencing_symbols(ANSWER)` without snippets | 1,073 | Included the declaration and a full 10-field coverage/projection object even though coverage was complete. |
| Exact path-scoped symbol miss | 598 | Pretty JSON repeated adapter and generation authority for deterministic `SYMBOL_NOT_FOUND`. |
| `get_diagnostics_for_file(diagnostics.py, max_answer_chars=512)` | 963 | Returned oversized clean success with hash, generation, engine, interpreter, external-root, adapter, and generation repetition. |
| `get_diagnostics_for_symbol(_render, max_answer_chars=512)` | 988 | Returned the same oversized operational repetition plus symbol selection. |

The complete reference coverage facts were configured-program files `135`,
trusted-language files `135`, identical program/trust digests, and zero
uncovered files. Schema 4 must preserve the meaning while rendering that case as
exactly `{"complete":true}`.

## Admission stop rule

Payload reduction is rejected if it changes semantic entities, exact decoded
text ranges, body/hash bytes, incomplete-coverage truth, synchronous freshness,
or typed operational failure semantics. A failing lane is held and corrected;
token reduction never compensates for one of those regressions.
