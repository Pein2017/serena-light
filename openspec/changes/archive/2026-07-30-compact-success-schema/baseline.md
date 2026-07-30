# Repaired Pre-Compact Baseline Ledger

This is an information-only baseline for tasks 1.2--1.5. It does not mark
those tasks complete, and it is not a compact-schema acceptance report.

## Identity and pinned dependencies

The archived correctness-repair acceptance identifies the repaired candidate
as source commit `9e4987e9f2190a4ff03cb7a35359483a5387f327`, build identity
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`,
combined dependency-lock digest
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`,
public tool schema `2`, and build-identity algorithm `3`. It reports 16,761
production lines as informational only. The raw `uv.lock` SHA-256 recorded in
the predecessor capture is
`5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca`.

The locked engines are Node `22.22.0`, npm `11.13.0`, Pyright `1.1.403`,
TypeScript `5.9.3`, and TypeScript Language Server `5.1.3`. The pinned MCP
Python package is `1.27.1`; the archived predecessor capture reports MCP
protocol `2025-11-25` and Python `3.12.12`. Its client receipts name
Codex/Sol-high, native Claude Code `2.1.220`/Sonnet-high, and CC
Agent/Sonnet-high. Clean and poisoned-proxy connector coverage are distinct
evidence surfaces: loopback connector and health traffic explicitly bypasses
proxies, while daemon and LSP children strip every `*_PROXY` variant.

The archive also contains an earlier schema-1 capture with build identity
`d46175203f8b78749d2ae0341ef8157965aea31c454620e8f2840de5a2b8dff7`.
It remains useful only as an archived verbose-shape fixture; it must not be
misrepresented as the repaired schema-2 baseline above.

## Existing client-visible evidence

`proposal.md` records repaired-v1 actual MCP-text measurements of approximately
924 characters for an exact symbol without a body, 5,407 with a body, 2,733
for a global symbol, 20,674 for a large overview, and 12,613 for references.
It also records a `max_answer_chars=50_000` large overview whose observed
`CallToolResult.content[0].text` was 99,918 characters. The proposal and
design are the source for those measurements; no full saved repaired-schema-2
MCP texts, structured content, model-token counts, calls, or wall times are
present in this change at this point.

A read-only Claude Code Sonnet/high worker also queried the live accepted
schema-2 daemon (`4b0a5e2e...`, daemon `c5a2b4fd...`) before compact production
wiring began. Its native-client rendering measured 635 characters for an exact
symbol without body, 1,537 with body, 1,679 for the global symbol, 8,674 for
the selected overview, 10,463 for 22 multi-file references, 990 for a
declaration, 978 for a read-only external declaration, 1,154 for a typed
`SYMBOL_NOT_FOUND`, and 769 for a typed unsupported implementation query. The
same calls directly confirmed repaired 0-based position line 108 and the
schema-2 reference coverage object. These are real client-visible observations,
but the native Claude tool surface does not expose `content[0].text` and
`structuredContent` separately, so the SDK-level fixture harness remains the
owner of byte-for-byte dual-view acceptance.

The archived schema-1 content-text files are retained at:

- `openspec/changes/archive/2026-07-30-fix-position-and-coverage-contract/evidence/baseline-navigation.json`
- `openspec/changes/archive/2026-07-30-fix-position-and-coverage-contract/evidence/baseline-python-constant.json`
- `openspec/changes/archive/2026-07-30-fix-position-and-coverage-contract/evidence/baseline-references.json`

They record 926, 967, and 12,031 characters respectively. These files are
actual archived `content[0].text`, but are pre-repair/schema-1 and therefore
cannot satisfy the repaired pre-compact real-connector capture gate.

## Deterministic regression coverage added now

`tests/unit/test_compact_baseline_contract.py` does two bounded checks using
the current pinned types:

1. It locks the archived verbose envelope fields (workspace, adapter,
   generations, path, hash, and repeated reference path) and snapshots how the
   current `SuccessEnvelope` permits per-result path/hash repetition.
2. It gives FastMCP a deterministic 41,013-character compact inner `data`
   fragment. Under the pinned SDK, the resulting text content is 75,055
   characters, while `structuredContent` remains the same value. This
   reproduces the defect class without claiming it is the archived 99,918-character
   live overview.

## Fixed-fixture schema-2 capture

The remaining baseline gaps were closed against live repaired build
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`
and daemon `c5a2b4fd-fdac-44bc-96ca-17d2787c9021`. The fixed fixtures live in
`tests/integration/fixtures/compact_navigation`. The exact real-MCP evidence is
stored in:

- `evidence/precompact-schema2-fixed-fixtures.json` for the nine historical
  fixed cases; its retained reference entry predates `python_uncovered.py` and
  is not the current reference-replay owner;
- `evidence/reference-uncovered-schema2-vs-schema3.json` for current paired
  reference semantics in an independent Git root whose native Pyright
  configuration has three configured files, four trusted Python files, and one
  explicitly uncovered file;
- `evidence/final-schema3-current-build-recap.json` for the final-build
  non-reference hash-equality recap and separately paired final references.

Each case preserves the actual `CallToolResult.content[0].text`, the pinned MCP
SDK `structuredContent`, semantic fields, character and UTF-8 byte counts,
deterministic `o200k_base` client-visible token estimates, one tool call, wall
time, and a content SHA-256. Proxy values are redacted; the capture records only
the ambient proxy variable names and the connector's `trust_env=False`
loopback boundary. Clean and deliberately poisoned proxy behavior remains
owned by the real stdio acceptance suite rather than being inferred from this
ambient capture.

The historical fixed-fixture schema-2 character counts are 904 (exact no body),
938 (exact with body), 2,727 (global), 22,686 (large nested overview), 3,156
(multi-file references), 1,543 (declaration), 1,662 (TypeScript
implementation), 1,469 (read-only external declaration), and 686 (true empty
overview). In particular, the 3,156-character reference row is historical only
and must not be cited as the current replay owner. The current paired
configured-project reference capture is 3,036 characters and retains the
one-file uncovered coverage object later returned by schema 3.
