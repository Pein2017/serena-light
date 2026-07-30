# Compact Schema Four-Arm Ablation Results

Date: 2026-07-30 UTC

## Locked setup

All four arms used the unchanged prompt in `ablation-prompt.md`. Each arm was
read-only, was authorized to use one assigned semantic MCP, was allowed shell
only as the documented fallback, and had to include a complete tool ledger. A
call to the other semantic MCP would have invalidated the arm; no cross-use
occurred. The harness could not physically hide the unassigned MCP schema, so
this is authorization plus complete-ledger enforcement, not a claim of physical
schema-exposure isolation.

The auditable manifest records the enforcement boundary, scores, semantic-call
and fallback counts, retained-response hashes, and timestamps only where known:

- [ablation manifest](ablation-arms/manifest.json)
- [Opus / canonical Serena receipt](ablation-arms/opus-canonical-serena.md)
- [Opus / Serena Light receipt](ablation-arms/opus-serena-light.md)
- [Sol / canonical Serena receipt](ablation-arms/sol-canonical-serena.md)
- [Sol / Serena Light receipt](ablation-arms/sol-serena-light.md)

All arms reported the same commit identities, but Q1 was a dirty working-tree
subject rather than a clean repository snapshot:

- `serena-light`: `9e4987e9f2190a4ff03cb7a35359483a5387f327`
- `research-probes`: `ccdadc4e2d8c00a091dde8d684a14982f05715f2`

The retained subject hashes identify the live files inspected during the run:

- `src/serena_light/workspace/runtime.py`:
  `8165bc40212d7c7b99657f727c9ce7acc971d246e7b14b56df7af4131cc8e91c`
- `src/serena_light/workspace/inventory.py`:
  `5c3b3fd5019812107d5cff73b5a689f538b6bc6711d93cafb794935fdeadc89a`
- `tests/unit/test_workspace_runtime.py`:
  `af5cf360163dd036a5ab688dc55ddcb7912617e5aef8d75394a6631f44cbb915`
- `tests/unit/test_workspace_inventory.py`:
  `36e19c9f53cc6cc950ca1636288345b8bd2be5b6a71c471290dee6f2213e79f1`
- `research-probes/src/inference/hf_backend.py`:
  `e21beb1e6b5c475ca8f0102b3500636620e880709589388bb8c56e7d8a99db95`
- `research-probes/scripts/research/run_continuation_locality_boundary_scoring.py`:
  `747a4efd007dcbfbf0d59bb6bf2d60b6e4179390dabb033f74f4281dde7b110d`
- `research-probes/tests/inference/test_hf_exact_history_evidence.py`:
  `5b90b6b79326ea3b92ccc702ab64680676f5187dd2ee71f90c4361114cc6aa4c`
- `research-probes/tests/research/test_continuation_locality_owner_compositionality.py`:
  `f4dd39fbecee67aa70932234f57db1e66cf932e04e16d012c24918a35f0b8cc1`

The Q1 working-tree subject differs from the precompact round. The comparison
therefore controls the four arms within this round; it does not assert a
clean-snapshot or unchanged-subject comparison against that earlier round.

## Scoring method

Accuracy was scored before efficiency. Each question was worth 50 points.
Question 1 covered the preflight freshness seam, all three search branches,
concurrent reconciliation and change classification, observable lifecycle and
typed-error consequences, and test/failure/limitation evidence. Question 2
covered exact-history identity and lifecycle, logits/FP32/logprob/rank
semantics, runner/provenance/subtraction semantics, tests, and the four claim
verdicts. Efficiency was then compared using assigned semantic-MCP call count,
source shell fallbacks, response-length compliance, and parent-observed wall
time. Wall time is secondary because the built-in and CC-plugin transports are
not the same execution surface.

## Results

| model | assigned MCP | accuracy | semantic MCP calls | source shell fallbacks | response limit | observed wall time |
|---|---|---:|---:|---:|---|---|
| Opus 5 high | canonical Serena | 99.0 | 68 | 3 | 3,531 words; exceeded 2,500 | 7m 26s |
| Opus 5 high | Serena Light | 98.5 | 53 | 0 | 2,784 words; exceeded 2,500 | 8m 18s |
| Sol high | Serena Light | 98.0 | 60 | 0 | participant reported within 2,500 | at most 6m 24s |
| Sol high | canonical Serena | 97.5 | 68 | 2 | participant reported within 2,500 | not reliably captured after interruption |

All four arms reached the correct A/B/C/D verdicts (`YES`, `YES`, `NO`,
`NO`) and correctly described the core Q1 freshness and scope contract. The
small score differences come from completeness and precision rather than a
wrong central conclusion. Opus/canonical gave the most exhaustive answer;
Opus/Light and Sol/Light both identified the silent partial-family omission as
the sharper remaining Q1 limitation. Sol/canonical instead chose the also-valid
bounded-global-search limitation.

## Paired MCP comparison

- With Sol held fixed, Serena Light improved the score by 0.5 point, reduced
  semantic calls from 68 to 60 (11.8%), and reduced source fallbacks from two
  to zero.
- With Opus held fixed, Serena Light was 0.5 point lower, reduced semantic
  calls from 68 to 53 (22.1%), and reduced source fallbacks from three to zero.
- Averaged across the two model families, the accuracy score was identical:
  98.25 for canonical Serena and 98.25 for Serena Light. This one-run sample
  supports accuracy parity, not superiority.
- Serena Light removed every source shell fallback. In both canonical arms,
  module-level constant/import values were the main gap; Sol also needed shell
  to find a path-imported test. Light recovered those facts through compact
  symbol bodies and inventory-bounded directory search.
- The response word-cap violation is model-correlated in this run: both Opus
  answers exceeded 2,500 words, while both Sol answers reported compliance. It
  is not evidence against either MCP.
- Wall-clock results are not used to rank the MCPs because transport, network,
  batching, and model surface differed. Call count and fallback count are the
  cleaner efficiency observations.

## Comparison with the repaired pre-compact round

The preceding repaired-schema round scored Opus/canonical 98.5,
Opus/Serena-Light 98.0, Sol/canonical 97.0, and Sol/Serena-Light 96.5. The
current round is directionally no worse in every arm, but the sample is too
small to attribute score changes to compact rendering rather than normal model
variation. The deterministic real-connector fixture evidence is the stronger
token-efficiency result: current exact-symbol no-body, global, overview, and
references payloads are respectively 26.0%, 8.7%, 4.2%, and 31.3% of their
schema-2 character baselines while preserving the required semantic evidence.

## Decision

The ablation evidence is complete. Compact Serena Light preserves practical
answer accuracy while materially reducing semantic calls, eliminating source
shell fallbacks in this task, and sharply reducing deterministic connector
payloads. The result supports continuing the parallel `serena-light`
registration. It does not justify replacing canonical Serena, and it is not a
broad statistical claim about all repositories or tasks. It does not release
the candidate: the remaining HOLD is code remediation followed by a fresh,
independent dual re-audit.
