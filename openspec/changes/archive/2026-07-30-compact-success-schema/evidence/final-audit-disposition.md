# Final Audit Disposition

Date: 2026-07-30 UTC

Independent final audits:

- Sol-xhigh static correctness: PASS, P0/P1 none; raw report
  `final-audit-sol-xhigh.md` SHA-256
  `4f342dca0b1a2c8f5480bfd2a8c807b04fe84966413a7798707a3527bfb3c7f1`.
- Opus-max runtime/evidence: PASS, P0/P1 none; raw report
  `final-audit-opus-max.md` SHA-256
  `17dde8761b0bb90c906d8313f9da1efcf552cf77d3dc082012cf3856df2c5f32`.

The lead accepted both PASS verdicts after checking that they independently
closed every first-pass blocker. Neither verdict is treated as a vote; each
production-path claim has its own source, test, or real-connector evidence.

## P2 dispositions

| Finding | Lead disposition |
| --- | --- |
| Rich error FastMCP text can exceed `max_answer_chars` even though candidate evidence is bounded | **Accept as known limitation.** This change deliberately hard-bounds canonical navigation success while retaining rich ordinary error envelopes. Record the exact boundary in `docs/compatibility.json`; do not broaden the change into a second error-schema rollover. |
| Fresh Codex receipt claimed semantic detail that the adapter did not publish | **Fix.** The receipt now states only the reproduced class kind/name path and explicitly says no separate detail field was returned. Compatibility now records fail-closed external `include_info` behavior. |
| README said the final ablation repeat was still in progress | **Fix.** README now records the completed repeat and both final PASS audits, with sync/archive still in progress. |
| Frozen porcelain digest lacked a reproducible command/timing statement | **Fix.** The final manifest now records the exact `git status --porcelain=v1 -uall | sha256sum` command and that the capture preceded materialization of the final evidence files. Conclusion-critical prompt, receipt, and subject-file hashes remain independently reproducible. |
| Declaration/implementation source-symbol ambiguity candidates are not public-budget bounded | **Accept as pre-existing known limitation.** They remain limited to one authorized document's symbol set and were not introduced by compact success. Record the boundary in compatibility; a future error-budget change may address it without delaying this archive. |

The Sol-only README P2 is covered by the third row. No accepted P2 changes the
compact-success correctness decision or hides a failed required gate.
