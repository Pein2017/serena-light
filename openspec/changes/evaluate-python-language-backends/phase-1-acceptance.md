# Phase 1 acceptance: admission gate

**Disposition: HOLD. Phase 1 is not complete.** Two independent reviews of the first
admission implementation returned HOLD, and the accepted disposition retired the earlier
PASS claim. The run recorded below is preserved unchanged as an authentic *attempted* run;
it is not PASS evidence for Task 1.7 or for the complete Task 1.8 claim. Tasks 1.7 and 1.8
stay unchecked until a new receipt, produced by the repaired instrument, passes every
invariant and both final reviewers approve the rerun.

The repair wave that follows this record is owned by tasks 1.9-1.15.

## What the reviews held on

The attempted run's instrument could not have seen the writes it claimed were absent, and
its receipt could not name the code that produced it:

- the corpus was captured twice around a *quiet* window, after the candidate lock and the
  runtime had already been prepared, so no Phase 1 setup operation was inside the delta;
- a Git root's remainder was metadata-scanned only where a request declared a metadata
  root, so a new `.py` file, `pyrefly.toml`, `ty.toml`, `pyrightconfig.json`,
  `.pyrefly_cache/`, or empty directory anywhere else was invisible or was reported as an
  unstable root rather than as a write;
- the receipt bound no evaluator source closure, no CLI host interpreter, no bootstrap
  environment, and no candidate runtime manifest digest, and a rerun replaced the previous
  receipt at one canonical path;
- the 1800-second ceiling covered only the external middle of the gate, and no subprocess
  received a remaining-time bound.

## The attempted run (preserved, superseded)

Evaluation identity `36696159de500c09275b6ff174d7df2551990a489610c9feffa67a532da99335`,
receipt `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/admission-receipt.json`
(`sha256:de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348`, schema 1).
Those bytes and the artifacts beside them are immutable evidence and are not rewritten by
this change.

### Exact command

```bash
# Run 1 resolved the freeze timestamp; run 2 reused that exact frozen value.
backend_eval_freeze_at="2026-08-11T21:11:47Z"   # run 1: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, wall time 6 s (`started_at=2026-08-11T21:31:13Z`,
`ended_at=2026-08-11T21:31:19Z`). Its receipt reported `status=pass`,
`next_action=begin_protocol_probe_planning`, and zero unexpected paths across five roots.
That report was *internally* consistent; the disposition above is about what the
instrument was able to observe, not about a contradiction inside the receipt.

**Recorded deviation.** The declared form `conda run -n ms python -m
scripts.backend_eval.admission ...` fails before the admission module is reached, with exit
`1` and `ModuleNotFoundError: No module named 'scripts.backend_eval'`, because the `ms`
environment resolves `scripts` to the unrelated regular package `/data/verl/scripts`, which
shadows this repository's `scripts` namespace package, and because `serena_light` is not
installed there. Neither environment was altered and nothing was installed globally. The
CLI process ran instead with
`/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python`. The evaluated `ms`
interpreter and `ms` `uv` were still passed explicitly through `--python` and `--uv`, so the
deviation changed the CLI host process only. That host is now itself receipt-bound rather
than merely described in prose.

### Candidate lock

| Field | Value |
| --- | --- |
| lock digest | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| `exclude_newer` | `2026-08-11T21:11:47Z` |
| resolved packages | 2 |
| `ty` | `0.0.70`, 18 lock-set hashes |
| `pyrefly` | `1.2.0`, 12 lock-set hashes |

**Corrected artifact-hash wording.** Each candidate's lock entry carries the *complete
multi-platform* hash set for that release, not a single artifact. The earlier record quoted
`sha256:06aca758…24e27f` (ty) and `sha256:25822ea9…ac142a` (pyrefly) as "artifact"; those
are only the first members of the sorted lock sets and are **not** the wheels this host
installed. The wheels actually selected and installed for Linux x86_64 are:

| Candidate | Selected wheel | `sha256` |
| --- | --- | --- |
| `ty 0.0.70` | `ty-0.0.70-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` | `d81825524f1b57ecbcb5fce7d61fb159cb4837a6167a4569309c9fa7fc15a77d` |
| `pyrefly 1.2.0` | `pyrefly-1.2.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl` | `90efe75e17491ef5d636e10469e9278d7d0256b3b4c5e1f4750069bf3ae0f5d1` |

Both digests are members of the frozen lock sets. Each was recovered from the retained
resolver index in the attempted run's own `uv-cache/simple-v18/pypi/<name>.rkyv`, adjacent
to the exact wheel filename, and independently confirmed against the official index's
release metadata. The installed `dist-info/WHEEL` of both packages in the prepared runtime
records `Tag: py3-none-manylinux_2_17_x86_64`, which is the same wheel.

**Production `ty` is a different, untouched slot.** This repository already pins its own
development-only `ty 0.0.24` in `pyproject.toml`, used for repository type checking. The
candidate `ty 0.0.70` is installed only into the service-owned evaluation runtime below
`/data/CoordExp/.codex/runtime/serena-light/backend-eval/<candidate-lock-digest>/`. The two
never share a slot: the evaluation may not modify `pyproject.toml`, `uv.lock`, or the
production dependency slot, and every phase asserts that byte-for-byte.

### Runtime and interpreters

| Role | Configured path | Real path | Version |
| --- | --- | --- | --- |
| resolver `uv` | `/root/miniconda3/envs/ms/bin/uv` | same | `uv 0.9.2` |
| base interpreter | `/root/miniconda3/envs/ms/bin/python` | `…/bin/python3.12` | `3.12.11` |
| environment `ms` | `/root/miniconda3/envs/ms/bin/python` | `…/bin/python3.12` | `3.12.11` |
| environment `llm-framework-study` | `/root/miniconda3/envs/llm-framework-study/bin/python` | `…/bin/python3.12` | `3.12.13` |

Runtime root:
`/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062/`
(content addressed by the lock digest). No candidate language server was launched in this
phase.

### Production identity invariant

`production_identity_before == production_identity_after`, with the published `after` side
captured after evaluation-owned cleanup.

| Field | Value |
| --- | --- |
| `pyproject.toml` | `97c8e100f9dd8b0f77a1cbf69a02bea1b1c4ff3d04d168cbf4f4854e914e17d4` |
| `uv.lock` | `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca` |
| `package-lock.json` | `c4f17c7f2e5faf7f69a1d11642792cb8bae5b6502c7288cde3577a8ec3fe0cba` |
| `dependency_lock_digest` | `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941` |
| `compute_build_identity` | `77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` |
| `runtime_paths` | 9 production entries, all below `…/serena-light/deps/eff6ebdf…ab4941/`, unchanged |

### Bounded corpus manifests and write deltas

Five roots, captured twice around a window that did **not** include the candidate
resolution or the runtime preparation. Every delta was bound to its own before/after
manifest digest and reported zero unexpected paths, but the observation covered only the
trust-inventory closure, the declared configuration paths, and one declared metadata root.

| Root | Kind | Inventory count | Source revision |
| --- | --- | --- | --- |
| `/data/CoordExp/.worktrees/research-probes` | git | 1280 | `f4b061b73e89e19c19062fac0c9c68030ef00082` |
| `/data/CoordExp/serena-light` | git | 158 | `5e7d8ba84ecee8612a964f7b972bfe94f8cfa4b0` |
| `/data/ms-swift` | git | 617 | `f2797138dba0e224cfff735cd89a528a08d8732a` |
| `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages` | non_git | 3 | — |
| `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` | non_git | 2214 | — |

`artifact_tree_digest = 12c15677bac4a4ca1ee7ea13b53d928f3990421eebb8514745e32f2237b7bc30`.

## Residual risks carried forward

- `ty 0.0.70` is a pre-`0.1` release; its protocol surface is unproven and Phase 2 must
  record an explicit negative result where a capability is not advertised.
- The candidate runtime is prepared but never exercised; readiness, crash, and cleanup
  behaviour are entirely Phase 2 evidence.
- A freeze is bound to its `--exclude-newer` value; a later candidate release requires a new
  evaluation identity rather than mutating existing evidence.
