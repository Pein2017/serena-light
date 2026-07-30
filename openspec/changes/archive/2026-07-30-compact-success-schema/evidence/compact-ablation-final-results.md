# Final Fixed-Contract Four-Arm Ablation

Date: 2026-07-30 UTC

## Locked setup

This is the task 7.4 repeat on final-repair Serena Light build
`92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`.
All arms began from the same two live filesystem snapshots and the same locked
prompt. Each arm was authorized to call exactly one semantic MCP, retained
read-only shell as a disclosed fallback, and returned a complete ledger. No arm
called its unassigned semantic MCP, changed either repository, used web, or
delegated work. The repository remained frozen until all four analysis answers
were complete.

The raw receipts and their hashes are owned by
[`ablation-arms-final/manifest.json`](ablation-arms-final/manifest.json). The
Q1 working tree was intentionally dirty and is identified by HEAD, a complete
porcelain digest, and hashes of conclusion-critical subject files. Q2's
conclusion-critical files were clean at its recorded HEAD.

## Scoring

Accuracy was scored before efficiency with the same 50-point-per-question
rubric as the prior round. All four arms correctly reached A/B/C/D =
YES/YES/NO/NO and correctly traced the main freshness, branch, exact-history,
FP32, runner, provenance, and claim-boundary contracts.

| rank | model | assigned MCP | accuracy | MCP calls | source-shell fallbacks | answer words | 2,500-word cap |
| ---: | --- | --- | ---: | ---: | ---: | ---: | --- |
| 1 | Opus 5 high | canonical Serena | 99.0 | 63 | 3 | 2,717 | FAIL |
| 2 | Opus 5 high | Serena Light | 98.5 | 57 | 0 | 3,531 | FAIL |
| 3 | Sol high | canonical Serena | 98.0 | 54 | 2 | 1,584 | PASS |
| 4 | Sol high | Serena Light | 97.5 | 64 | 3 | 1,557 | PASS |

The half-point Opus/Light deduction includes an over-strong claim that moving
already-computed logits to CPU FP32 makes the evidence independent of
accelerator nondeterminism; the code establishes fixed-precision CPU reduction,
not cross-device determinism of the preceding model forward. The Sol/Light
answer was slightly less complete about runner-specific test ownership. The
other small differences are completeness and limitation sharpness, not wrong
central conclusions. Response-cap failures are efficiency/compliance findings,
not deductions from semantic correctness.

## Paired comparison

- With Sol fixed, Serena Light was 0.5 point lower, used 10 more assigned-MCP
  calls, made one more source fallback, and returned 27 fewer answer words.
- With Opus fixed, Serena Light was 0.5 point lower, used six fewer assigned-MCP
  calls, eliminated all three source fallbacks, and returned 814 more answer
  words.
- Averaged across families, canonical Serena scored 98.5 and Serena Light 98.0.
  Canonical used 117 assigned-MCP calls versus Light's 121; Light used three
  source fallbacks versus canonical's five.
- This single matched repeat supports practical accuracy parity within normal
  model variation. It does not establish either MCP as uniformly more accurate
  or more call-efficient. The Opus pairing favors Light's semantic reach; the
  Sol pairing exposes remaining file-enumeration and dynamic-import discovery
  friction.
- Answer word count is model output, not MCP payload size. The deterministic
  real-connector fixture gates remain the stronger token-efficiency evidence:
  schema-3 exact-symbol, global, overview, and reference payloads are 26.0%,
  8.7%, 4.2%, and 31.3% of their schema-2 character baselines while preserving
  required evidence.

## Decision

The final fixed-contract ablation passes its correctness gate. It supports
continued parallel Serena Light registration and targeted improvement of file
enumeration/dynamic-import discovery. It does not justify replacing canonical
Serena, and it does not turn this four-answer sample into a broad statistical
claim. Release remains gated on the final independent Sol-xhigh static and
Opus-max runtime/evidence audits.
