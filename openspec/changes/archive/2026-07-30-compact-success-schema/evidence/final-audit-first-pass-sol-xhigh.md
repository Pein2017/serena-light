# Final audit first pass: Sol-xhigh static correctness

Date: 2026-07-30

Fixed point: HEAD `9e4987e9f2190a4ff03cb7a35359483a5387f327`, 53
unchanged porcelain status entries, runtime build
`ad9a36302533abd425cd742247d935734919acf98a1660150057c128b539750d`,
public schema 3. The reviewer remained read-only. The lead's ad-hoc dirty-tree
content fingerprint was not independently reproduced; the HEAD, status path
inventory, build identity, and on-disk review subject were stable.

Verdict: `HOLD` (no P0; four P1s).

## Findings

1. A workspace `ReferenceDocumentInput` could degrade to `raw_range` after
   symbol-tree or position mapping failure even though the active contract
   reserves raw coordinates for a read-only external target without a response
   snapshot.
2. Post-order overview filtering retained a nonmatching ancestor as a path to a
   matching descendant, but final budget pruning could remove the descendant
   and leave the nonmatching ancestor alone.
3. Stable-prefix flat pruning could fail on a large first record while
   reporting `minimum_required_chars` from a later smaller record, producing a
   stated minimum below the budget that had just failed.
4. Multi-adapter global results carried adapter/generation authority per item,
   but compact minimum-budget errors read only top-level authority and therefore
   returned workspace alone.

Two nonblocking follow-ups were also recorded: malformed-success conversion
discarded authority already parsed, and focused tests covered the components
without the four contract-critical compositions.

The reviewer passed 88 focused tests, strict OpenSpec, targeted Ruff, JSON
parsing, and `git diff --check`. Full-suite, runtime-client, proxy, provenance,
and performance gates were intentionally not repeated in this static lane.

Lead disposition: all four P1s and both P2s were accepted for repair and
targeted regression coverage. Stable-spec sync and archive remain blocked until
a changed-tree re-audit passes.
