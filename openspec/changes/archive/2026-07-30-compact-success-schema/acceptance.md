# Compact Success Acceptance

This report compares the repaired schema-2 baseline build
`4b0a5e2e4460afbfde1456045d3fc381833c7c1dc41959d36742dbb094371f77`
with the final-repair compact schema-3 candidate build
`92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`.
The retained full-text schema-3 fixed-fixture capture is build
`4ff261a7ebda7bbddf182e836742103c47a11c18becce01dad266b6829df4290`.
Pre-audit build `a9d7e7ae...` is a hash-equality recapture for its eight
non-reference payloads, plus separately paired final reference captures; it is
not a replacement claim that the older full-text payload capture was performed
at `a9d7e7ae...`. Post-repair build `ad9a3630...` owns a separate historical
full-text nine-case runtime recap, including the repaired external raw-range
path and current-root reference coverage. Final build `92b2618...` is covered
by the full suite plus three independent fresh-client boundary receipts. These
are current-build smokes rather than a
replacement for the isolated paired character-ratio fixtures below. All
captures cross the real daemon and connector through
pinned MCP 1.27.1. Character counts use the actual client-visible
`content[0].text`; token counts use deterministic `o200k_base` encoding and
are informational.

## Deterministic payload gates

| Fixture | Schema 2 chars | Schema 3 chars | Ratio | Gate | Result | Schema 2 / 3 tokens |
| --- | ---: | ---: | ---: | ---: | --- | ---: |
| Exact symbol, no body | 904 | 235 | 26.0% | <=50% | PASS | 297 / 67 |
| Global symbol | 2,727 | 236 | 8.7% | <=40% | PASS | 716 / 67 |
| Large nested overview | 22,686 | 961 | 4.2% | <=25% | PASS | 4,911 / 260 |
| Multi-file references | 3,026 | 947 | 31.3% | <=40% | PASS | 865 / 318 |
| Body-external characters | 922 | 321 | 34.8% | <=50% | PASS | body unchanged |

The body comparison subtracts the identical complete 16-character body
`ANSWER: int = 42` from both serialized texts. The independent configured-root
reference case was separately paired against the final fixture snapshot. It
retains identical three-reference evidence, coverage digests and counts, and
the one-item uncovered sample while excluding that uncovered file from returned
semantic locations. Thus the table uses the retained `4ff261a7...` full text
for non-reference schema-3 rows and the final paired reference receipt for the
reference row.

## Semantic equivalence

- Exact and global symbol name paths, kinds, source paths, and decoded-text
  ranges are unchanged after removing redundant offsets and runtime metadata.
- The exact body and whole-file SHA-256 are byte-for-byte unchanged.
- The overview retains the class and all 20 methods in the same sibling order.
- References retain all three configured-program semantic locations across two
  files, their containing symbols, requested snippets, and one complete
  coverage object. The independently discovered path-open regression is fixed:
  `python_uncovered.py` remains disclosed by coverage but is not returned as a
  semantic reference after another operation opens it.
- Declaration and TypeScript implementation targets retain their paths and
  ranges.
- The external transformers declaration retains its authoritative absolute
  read-only path and explicit raw UTF-16 LSP position basis; it is not
  mislabelled as a decoded-text range without a response-owned snapshot.
- The true empty result is exactly `files=[]` and `omitted=0`.
- Every schema-3 case has one text block, is within 12,000 characters, and has
  text byte-for-byte equal to canonical JSON of `structuredContent`.

## Calls and wall time

Every fixture uses one MCP tool call. The post-repair current-build schema-3
runtime recap recorded wall times in seconds of 0.160 exact no body, 0.172
body, 0.548 global, 0.190 overview, 0.353 current-root references, 0.183
declaration, 0.180 implementation, 0.204 external declaration, and 0.140 empty
overview. All nine client-visible texts equal canonical JSON of their
`structuredContent`, retain SHA-256 plus character and UTF-8 byte counts, and
fit the 12,000-character public bound. The deterministic paired fixture token
counts above remain the actual client-visible `o200k_base` counts from their
own captures; the post-repair smoke does not relabel its different global and
current-root reference arguments as the fixed comparison cases.

## Evidence owners

- `evidence/precompact-schema2-fixed-fixtures.json`
- `evidence/compact-schema3-fixed-fixtures.json`
- `evidence/reference-uncovered-schema2-vs-schema3.json`
- `evidence/compact-schema3-warm-repeat.json`
- `evidence/final-schema3-current-build-recap.json`
- `evidence/final-schema3-postrepair-recap.json`
- `evidence/compact-ablation-results.md`
- `evidence/ablation-arms/manifest.json`
- `evidence/compact-ablation-final-results.md`
- `evidence/ablation-arms-final/manifest.json`
- `evidence/actual-schema-rollover.json`
- `evidence/fresh-client-schema3.json`
- `evidence/fresh-codex-postrepair.md`
- `evidence/fresh-native-claude-postrepair.md`
- `evidence/fresh-cc-agent-postrepair.md`
- `evidence/fresh-codex-final-repair.md`
- `evidence/fresh-native-claude-final-repair.md`
- `evidence/fresh-cc-agent-final-repair.md`
- `evidence/final-audit-sol-xhigh.md`
- `evidence/final-audit-opus-max.md`
- `evidence/final-audit-disposition.md`
- `tests/unit/test_compact_baseline_contract.py`
- `tests/acceptance/test_stdio_connector_acceptance.py`

The two fixed-fixture files retain pre-`python_uncovered.py` reference payloads
as historical evidence only; current reference replay ownership is
`evidence/reference-uncovered-schema2-vs-schema3.json`, with final current-build
recap in `evidence/final-schema3-current-build-recap.json`. Fresh-client,
rollover, full-suite, ablation, post-repair runtime, and three-surface fresh
client evidence are complete. The first independent audits' implementation
blockers have been repaired, and the final Sol-xhigh static-correctness and
Opus-max runtime/evidence audits both pass with no P0/P1 findings. Their five
nonblocking P2s have explicit lead dispositions in
`evidence/final-audit-disposition.md`. This report records release PASS.

## Full implementation gate

The final current tree passed the following checks on 2026-07-30:

- `uv run --frozen pytest -q` with the live `/data/CoordExp`,
  `cc-plugin-codex`, `ms-swift`, and transformers snapshot variables: 821
  passed, one explicitly opt-in performance case skipped, one deprecation
  warning.
- The skipped transformers first-readiness performance case was then run with
  `SERENA_LIGHT_RUN_PERFORMANCE_ACCEPTANCE=1`: one passed in 32.52 seconds.
- The final independent Opus-max runtime audit repeated the same full suite
  twice at 821 passed, one skipped, and one warning (370.38 and 370.56
  seconds), repeated the performance case in 30.75 seconds, and replayed all
  nine production connector cases byte-for-byte on build `92b2618...`.
- `uv run --frozen ruff check --no-cache src tests scripts`: passed.
- `uv run --frozen ty check --no-progress`: passed.
- `uv run --frozen serena-light-bootstrap --check --json`: passed with the
  service-owned CPython 3.12.12 and dependency slot `eff6ebdf...`.
- `uv run --frozen serena-light-source-budget --json`: passed; production LOC
  is informational at 18,340, maximum is null, forbidden and undeclared direct
  imports are empty, census/manifest agreement is true, and all nine copied
  source hashes verify against official Serena commit `9a9d07e...`.
- JSON parsing, `git diff --check`, and strict OpenSpec validation: passed.

The first independent audit pass exposed five material contract defects:
workspace references could degrade to raw coordinates, overview filtering
could retain a childless structural ancestor, stable-prefix minimum budgets
could be derived from a later smaller record, multi-adapter budget errors could
lose per-item authority, and a file-scoped ambiguity error could ignore the
public response budget. It also found incomplete response-owned implementation
kind/info semantics. The repairs make workspace mapping fail closed as typed
`NOT_READY`, retain intrinsic overview-match state through final pruning, base
minimums on the first reachable prefix, preserve bounded multi-adapter
authorities, budget ambiguity candidates at the public boundary, and resolve
implementation kind/name/info within the existing replay transaction. A
combined focused repair run passed 143 cases before the final 821-test run.
Fresh Codex, native Claude Code, and CC Agent clients then selected build
`92b2618...`, exercised the repaired tool schemas and boundary cases, and
released their leases without cross-client leakage.
