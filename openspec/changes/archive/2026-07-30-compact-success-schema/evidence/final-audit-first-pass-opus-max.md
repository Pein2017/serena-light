# Final audit first pass: Opus-max runtime and evidence

Date: 2026-07-30

Fixed point: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`, 53
unchanged porcelain status entries, live build
`ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`,
daemon `9bc40cee-8724-4ef9-a91e-adc5348f1265`, public schema 3, MCP
1.27.1, protocol `2025-11-25`. The reviewer verified that the registered
connector imported this dirty source tree, exercised the daemon through real
stdio, released its lease, and made no repository writes.

Verdict: `HOLD` (no P0; five P1s).

## Findings

Opus independently confirmed all four Sol findings: workspace raw-coordinate
fallback, filtered-overview ancestor leakage after budget pruning, inconsistent
flat minimum, and multi-adapter minimum-error authority loss.

It additionally reproduced a regression through the real connector: file-
scoped broad substring `find_symbol` replaced the caller's public answer budget
with the 2,147,483,647-character internal success budget before the
`AMBIGUOUS_SYMBOL` path. Both `max_answer_chars=12,000` and `512` returned the
same 839 candidates and roughly 424,683 client-visible characters with
`truncated=false`. Compact success needs the large private internal budget, but
candidate-bearing errors must retain the caller's public budget.

## Accepted P2 follow-ups

- Implementation kind filters reached dispatch, but ordinary LSP
  Location/LocationLink results lacked SymbolKind; a positive include filter
  therefore erased every result and did not count the removal in `omitted`.
- Declaration/implementation `include_info` retained only selection-range data,
  which the compact boundary correctly removes, making the public option inert.
- The acceptance report must keep build provenance explicit: deterministic
  paired character gates, current-build runtime recap, and shipping-build
  evidence are distinct owners.
- The ablation predates the audit repairs and remains comparison evidence, not
  release evidence, unless rerun on the final fixed subject.

The reviewer independently verified the nine-case post-repair recap, exact MCP
text/structured equality, fresh-client scope, schema listing, budget arithmetic,
source budget, strict OpenSpec, and 101 focused tests. It did not repeat the full
807-test, poisoned-proxy, rollover, or performance suites.

Lead disposition: all five P1s and the two executable-contract P2s were accepted
for repair. Evidence provenance remains explicitly split. Final release requires
a changed-tree Sol-xhigh and Opus-max re-audit; no prior PASS is carried forward.
