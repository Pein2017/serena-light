# Phase 2 acceptance: protocol gate and stop decision

## Decision

Phase 2 is accepted on the immutable canonical receipt below. Pyright `1.1.403`
passes the protocol gate, ty `0.0.70` is correctly excluded as
`seam_incompatible_pull_only`, and Pyrefly `1.2.0` fails required protocol
evidence. No promotable competitor remains, so the contract-selected next action
is `retain_pyright_and_stop_after_protocol_phase`.

This is a recommendation to retain the current production backend. It changes no
production source, dependency lock, runtime, installation, client registration,
public schema, or canonical Serena configuration.

## Canonical execution

Working directory:

```text
/data/CoordExp/.worktrees/serena-light-backend-eval-phase2
```

Command:

```bash
.venv/bin/python -I -S -B scripts/backend_eval_bootstrap.py protocol-phase \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --parent-evaluation-identity d27d2bac03f7e85911f7e4d878275b8bf2e1803d5607a5e0ac7a58459c1f67aa \
  --parent-run-identity cccfd94dec9fb56379fbe921874ad56fe348c7e0f99685986bd343cd4a50f76a \
  --parent-receipt-sha256 7740ec76b9d0b36f52f8506ab44d0b6bbdf0489051c67a15507dd4f8f2ecb1c5 \
  --parent-artifact-tree-digest 7153b1bf2a6362271bab77fcd2f367c5dc0d2efa94cc79a146c077e0ec137e09 \
  --parent-candidate-lock-digest 6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062 \
  --parent-runtime-manifest-sha256 e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec \
  --parent-production-source-revision fd6335c5182a56bb266adc6f0ec07bf862bf3117 \
  --parent-production-dependency-lock-digest eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941 \
  --parent-production-build-identity 6498a4eb68c62e23561aa6b04e167fe54dd55b9d90b80c12bbb6560f078b9c39 \
  --workspace-root /data/ms-swift \
  --target /data/ms-swift/swift/infer_engine/lmdeploy_engine.py \
  --line 14 \
  --character 25 \
  --workspace-snapshot '/data/CoordExp/.worktrees/research-probes|git|c40a715089837613247260b59378edcf4692211a|b35ce62fd3a16a08351a930b6773a1cdbf1d8c148996eb16e59e59f5be8ac3b3' \
  --workspace-snapshot '/data/CoordExp/serena-light|git|fd6335c5182a56bb266adc6f0ec07bf862bf3117|404fa4e547e89ff5395d1da54f4189bf3c0233069c2b1f4825f405744e1d7929' \
  --workspace-snapshot '/data/ms-swift|git|f2797138dba0e224cfff735cd89a528a08d8732a|f26ed45a75cada25a81255f343dea266f9b679a82a882b53f397a8e0f9443151' \
  --workspace-snapshot '/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages|non_git|-|8af78cfa9a1f4950b9cc38e2084de2b1490dec2145c2c42ae53590a3be6db094' \
  --workspace-snapshot '/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers|non_git|-|8d9a9884993e7e25373b45a6b0d8fa18c9e80f8f7e115fd290e7d4f708fbaa72'
```

The sealed evaluator was clean at commit
`030e39307a9d14441ca539b533e345176a19458d`. The protocol budget was
5400 seconds; the canonical execution ran from `2026-08-13T03:31:56Z` through
`2026-08-13T03:34:06Z`.

## Immutable evidence

| Evidence | Identity or digest | Mode | Size |
|---|---|---:|---:|
| Phase 1 parent evaluation | `d27d2bac03f7e85911f7e4d878275b8bf2e1803d5607a5e0ac7a58459c1f67aa` | - | - |
| Phase 1 parent run | `cccfd94dec9fb56379fbe921874ad56fe348c7e0f99685986bd343cd4a50f76a` | - | - |
| Phase 1 parent receipt SHA-256 | `7740ec76b9d0b36f52f8506ab44d0b6bbdf0489051c67a15507dd4f8f2ecb1c5` | `0600` | 39,857,265 bytes |
| Phase 1 artifact tree | `7153b1bf2a6362271bab77fcd2f367c5dc0d2efa94cc79a146c077e0ec137e09` | - | - |
| Phase 2 evaluation | `1f761365be67ab7afa89daa44e41d62cdc4b0de7f6a0a3f942697f017ab04665` | - | - |
| Phase 2 run | `5a3b6e2721632197c81a3bd857b1da55d14198124af885c69d17bdee3576e7f4` | - | - |
| Phase 2 receipt SHA-256 | `3957dc7b059c4d29236a3a1aec1f729c756c1744aed56ed30d28e561b4400596` | `0600` | 39,863,464 bytes |
| Phase 2 artifact tree | `cba0cb2bade44bba653a741820f71388665c4d11544fec6acb7b632bbfc69b04` | - | - |
| Candidate lock | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` | - | - |
| Runtime manifest SHA-256 | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` | - | - |

The sealed evaluator source closure has SHA-256
`fdabb1643a5996f63af9ec04e21eb141ea33740734ac3dfe04f1d7ed91b5ee18`
over 30 bound files; the 19-file production-helper closure has SHA-256
`40ce0a67df02438a1ed2f9ced353d59b1dfe6e2adf3c6bc8984d52a99ac6968f`.
The host Python is `3.12.12`, configured through this worktree's `.venv`, with
realpath `/root/.local/share/uv/python/cpython-3.12.12-linux-x86_64-gnu/bin/python3.12`
and SHA-256 `52f97dd7591d651870416792ec5d9b8fe656669fe726a9fed4ec3140ecba8ae4`.

The exact parent corpus witnesses are:

| Root | Kind | Revision | Manifest SHA-256 |
|---|---|---|---|
| `/data/CoordExp/.worktrees/research-probes` | Git | `c40a715089837613247260b59378edcf4692211a` | `b35ce62fd3a16a08351a930b6773a1cdbf1d8c148996eb16e59e59f5be8ac3b3` |
| `/data/CoordExp/serena-light` | Git | `fd6335c5182a56bb266adc6f0ec07bf862bf3117` | `404fa4e547e89ff5395d1da54f4189bf3c0233069c2b1f4825f405744e1d7929` |
| `/data/ms-swift` | Git | `f2797138dba0e224cfff735cd89a528a08d8732a` | `f26ed45a75cada25a81255f343dea266f9b679a82a882b53f397a8e0f9443151` |
| `llm-framework-study` site-packages | non-Git | - | `8af78cfa9a1f4950b9cc38e2084de2b1490dec2145c2c42ae53590a3be6db094` |
| `ms` Transformers | non-Git | - | `8d9a9884993e7e25373b45a6b0d8fa18c9e80f8f7e115fd290e7d4f708fbaa72` |

The canonical Phase 2 receipt has `schema_version=4`, `status=pass`, no
phase-level issue, equal pre/post production identity, and an exact parent-bound
probe at `/data/ms-swift/swift/infer_engine/lmdeploy_engine.py` position
zero-based `(14, 25)`. All five parent corpus manifests are byte-identical before
and after the phase, with zero unexpected paths, declared mutations, or changed
manifest controls.

## Candidate outcomes

| Candidate | Gate | Decision-owning evidence |
|---|---|---|
| Pyright `1.1.403` | `pass` | All four Phase 2 required capabilities advertise, accept, and normalize; configuration was applied through three server requests; fresh exact-URI missing-import diagnostics were observed; all lifecycle fields pass. |
| ty `0.0.70` | `seam_incompatible_pull_only` | Required capabilities, configuration, external definition, lifecycle, and pull witness pass. The current Serena Light product seam requires push diagnostics, so ty is non-promotable. Optional implementation normalization remains deferred to the unentered feature phase. |
| Pyrefly `1.2.0` | `fail` | Configuration application and external definition are proven, but the bounded fresh diagnostics observation omitted the required missing import, required workspace-symbol normalization failed, and cold lifecycle evidence recorded cancellation `-32800` with candidate exit `-15`. Cleanup still reaped the process with no terminal or cleanup error. |

The three schema-v2 witness files are immutable `0600` files:

| Candidate | Witness SHA-256 | Size |
|---|---|---:|
| Pyright | `ae3315c3ee65262703f23426a972269fb99400e018791627bbf3906c217d8649` | 1,354 bytes |
| ty | `e5d9918d9efe7c16650c0b45ad001b8d78d2b477e677b628523641b1c8a774ef` | 1,476 bytes |
| Pyrefly | `8ab87f1e6007476a922f4472bc3aa2702448fefea7a1ff47064602d72dc1a033` | 1,613 bytes |

All bind unchanged fixture SHA-256
`84abcaab9124d982101995bc01a6119083c9a7c15de158e31015ee266a91ff40`,
the selected and observed `/root/miniconda3/envs/ms/bin/python`, and the external
Transformers definition at `generation/configuration_utils.py`. Pyright and
Pyrefly negotiated UTF-16 positions; ty negotiated UTF-8. This encoding difference
was normalized to the same decoded range. The target file SHA-256 was
`5ffe0d3d2fef5cc7ce353c9ec606579f088b93859154233985210b839450880e` and
the external definition file SHA-256 was
`d83f2281f939402be1633a29f3c760e29f5d2f284258d8ed99693b873744074b`.

## Historical receipts and repair disposition

The earlier receipt under evaluation
`f081b5e69385020072840528e865d79426cebb1d0f08a58070afc0ddefae875b`
and run `cf6a429567497dabf8760fc4cf8306395904f9d1dca89ff9e9c2d831640cf897`
remains immutable and **infrastructure-invalid**. The `0600`, 39,862,852-byte
receipt has SHA-256
`5b545d1ffd254477bfd3794ec93e67890d10d75913ce0db36f17362bb57bf1d8`.
Its unconsumed ty configuration
notification, stale/early diagnostics acceptance, schema mismatch, and incomplete
minimal-environment cause made it unusable as a gate.

The later receipt under evaluation
`fecf778e2b86ae591ab8cabc17433fc694b8d6ca027323b99a5da85fe35238af`
and run `9faee89d273a59e0a25d274df2f81b492c73923f7a197bb40b2c30eb4a11e6c2`
remains immutable and integrity-valid but **classification-invalid**. The `0600`,
39,863,184-byte receipt has SHA-256
`44626320660d766850165b434620b3baab31c4d25aae1a6cec83b0b1e6c10847`.
It
misclassified proven-configuration diagnostic failure, treated optional
implementation as a Phase 2 hard gate, and failed to give otherwise-valid ty pull
diagnostics the closed seam disposition. Neither historical receipt is erased or
used to support the final recommendation.

An Opus-max HOLD on the prior evidence drove the final repair wave. Per the user's
subsequent route decision, the final exact receipt was independently reviewed by
the following Sol reviewers instead of treating that earlier Opus review as a
final approval:

- **Sol-xhigh**, task `/root/phase2_task8_orchestrator_sol`: PASS, no P0-P2 or
  false-PASS; independently recomputed identity, parent and artifact bindings,
  witness hashes, write deltas, and all three classifications. Focused verification
  reported 309 passed with one expected external-snapshot skip; full backend-eval,
  Ruff, Ty, strict OpenSpec, diff, clean-worktree, and process checks passed.
- **Sol-max**, task `/root/phase2_final_runtime_solmax`: PASS, no P0-P2 or
  false-PASS; independently verified both canonical receipt bytes, artifact and
  witness digests, runtime/configuration evidence, zero-write containment, and
  cleanup. Its read-only verification reported 337 passed and one deliberately
  deselected external real-backend lifecycle test.

## Stop-gate disposition

Phases 3, 4, and 5 are `NOT_REQUIRED / SKIPPED_BY_STOP_GATE`. Pyright is the only
promotable candidate after Phase 2: ty is excluded by the current pull-only seam and
Pyrefly failed hard evidence. Product-seam comparison, future-feature probes, and a
backend-blinded Agent comparison therefore cannot change this evaluation's backend
decision and would only spend additional implementation, backend, and model budget.
Their implementation checkboxes remain deliberately unchecked because those tasks
were not implemented or executed.

## Claim boundary and next action

This evidence supports only `retain_pyright` for Serena Light under the locked
versions, selected `ms` interpreter, frozen corpus, controlled configuration, and
current push-diagnostics product seam. It does not establish that ty or Pyrefly are
universally unsuitable, compare later releases, or authorize migration,
installation, a multi-backend registry, public feature expansion, archival, push,
or artifact deletion.

The permitted next action is evidence-only closeout and an explicit user decision.
Production remains on Pyright without any integration change.

Task 6.1 remains open. This protocol receipt is not the change's final
machine-readable decision receipt: it does not carry the closed decision enum, a
complete active-time ledger including unpublished and temporary repair attempts,
the repair/rerun subtotal, lexicographic exclusions, skipped-phase dispositions,
or final-review binding. Published receipt windows alone are an incomplete time
sample and are not summed into a false total. A later evidence-only closeout may
bind this immutable receipt and add those fields after the complete attempt ledger
is available; it does not require another backend run.

The sum of `started_at`/`ended_at` windows across all 17 currently published
admission and protocol receipts under the artifact root is 540 seconds: 381
seconds across the three protocol receipts and 159 seconds across fourteen
admission receipts. This is a receipt-backed lower bound and incomplete subset,
not the Task 6.1 cumulative active-time total. It omits no-receipt failures and
ad-hoc `/tmp` repair/rerun attempts, and it is a sum of attempt windows rather
than a unique wall-clock span.

Tasks 6.4 through 6.6 also remain open. The candidate runtime and lock are retained
until the user authorizes cleanup; raw artifacts and production registrations are
untouched. Roadmap update, user decision, archive strategy, archive, and push are
outside this evidence-only commit.
