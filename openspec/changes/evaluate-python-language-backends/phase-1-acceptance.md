# Phase 1 acceptance: admission gate

**Admission gate disposition: PASS, pending the two independent final reviews of this
rerun.** The repaired instrument produced a new, code-bound receipt under a new evaluation
identity; it passes every invariant, brackets every Phase 1 setup operation, and leaves
zero unexpected writes across 68,059 in-scope corpus paths. Tasks 1.7 and 1.8 remain
unchecked on purpose: this rerun has not yet been reviewed, and a checked box must never
stand for an unreviewed run. The lead may check them once the Sol-xhigh and Opus-max
reviews of this receipt approve it.

The earlier attempted run is preserved below, unchanged and superseded. Its receipt bytes,
lock, and artifacts were not touched by the rerun.

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

## The authoritative run

| Field | Value |
| --- | --- |
| `evaluation_identity` | `1d00793bf0aea4944d6d3edec1a355175a117d1d0e566c1911b50a3ae8a36297` |
| `run_identity` | `c7136711d06c032d3b7ff1367404776ca11d78682a934f003b365b4382166767` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/receipts/<run-identity>.json` |
| receipt `sha256` | `29ed04ed65a447100064265b7540dfec1a13bd5174a198d2f526a56971b6f45e` (39,744,615 bytes, mode `0600`) |
| `schema_version` / contract | `2` / `python-backend-evaluation-v1` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| window | `started_at=2026-08-11T22:55:38Z`, `ended_at=2026-08-11T22:55:46Z` -- 8 s of the 1800 s ceiling |
| `artifact_tree_digest` | `4f977e63c61fef4405d65da1b23f461634f404bbe520ad5ee7155d8d8a34eb59` |

### Exact command

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # resolved to 2026-08-11T22:55:38Z
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`.

**Recorded deviation, unchanged and now receipt-bound.** The declared
`conda run -n ms python -m scripts.backend_eval.admission ...` form still fails before the
module is reached: in the `ms` environment `import scripts` resolves to
`/data/verl/scripts/__init__.py`, a regular package that shadows this repository's `scripts`
namespace package, so `import scripts.backend_eval` raises `ModuleNotFoundError` (probe exit
`1`). Neither environment was altered and nothing was installed globally. The CLI host was
the parent evaluation `.venv`, whose interpreter is now recorded *in the receipt* --
configured path, realpath, SHA-256, and version -- and is part of the evaluation identity,
so the deviation is bound evidence rather than prose. The evaluated `ms` `uv` and `ms`
interpreter were still passed explicitly through `--uv` and `--python`.

### Evaluator, host, and bootstrap environment

| Field | Value |
| --- | --- |
| evaluator source closure | 10 modules of `scripts/backend_eval`, digest `dd01ad2d0702e14dcb8a46b11dc75f5325ceca56e0056dcbb52364f8cb909ba1` |
| evaluator source commit | `b00be8dde847e5adbb8a7805e08f9ed22ca04a92`, source clean |
| CLI host configured path | `/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python` |
| CLI host realpath / version | `/root/miniconda3/envs/ms/bin/python3.12` / `3.12.11` |
| CLI host `sha256` | `068d88ca469ae96121a1a220eb9e0ac3e1a6400b193e3b9cc7c14f54f9ed28e4` |
| bootstrap inherited keys | `ALL_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`, `LANG`, `NO_PROXY`, `all_proxy`, `http_proxy`, `https_proxy`, `no_proxy` (names and value digests only) |
| bootstrap service keys | `HOME`, `PATH`, `TMPDIR`, `UV_CACHE_DIR`, `UV_NO_CONFIG`, `UV_PYTHON_DOWNLOADS`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` |
| bootstrap refused keys | `CONDA_EXE`, `GIT_EDITOR` |

No proxy value, CA path, or other environment value is published in plaintext.

### Candidate lock and runtime

The fresh `--exclude-newer 2026-08-11T22:55:38Z` resolved to the *same* frozen bytes as the
attempted run: lock digest
`6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062`, `pyrefly==1.2.0` and
`ty==0.0.70`, with the raw-lock witness recording 2,576 raw bytes. The runtime was therefore
content addressed to the same root, verified in full, and reused rather than rebuilt; no
candidate language server was launched.

| Field | Value |
| --- | --- |
| runtime root | `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062/` |
| `runtime-manifest.json` `sha256` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` (recomputed from disk before PASS, and again after the run) |

The selected Linux x86_64 wheels and the untouched production `ty 0.0.24` slot are exactly as
recorded for the attempted run above.

| Backend | Config path (below `<runtime-root>/config/`) | `sha256` |
| --- | --- | --- |
| `pyrefly` | `pyrefly/pyrefly.toml` | `9cbcaf9b661d0f873cece8e71ee2bc5900ddd5687720f357687a6571d61ad914` |
| `pyright` | `pyright/pyrightconfig.json` | `eff18e93bdb98237d0a00f3a4df8c900402433601a510f5f9f149e11ac3b539f` |
| `ty` | `ty/ty.toml` | `a67784aafa3a72c8dc706ef26339509845ceebe84f7a3e1bb20abf40748c03d1` |

| Environment | Configured path | Real path | Version |
| --- | --- | --- | --- |
| `llm-framework-study` | `/root/miniconda3/envs/llm-framework-study/bin/python` | `…/bin/python3.12` | `3.12.13` |
| `ms` | `/root/miniconda3/envs/ms/bin/python` | `…/bin/python3.12` | `3.12.11` |

### The measurement window and the corpus

The first capture ran **before** the candidate lock was compiled and before the runtime was
prepared; the second ran **after** preparation and before cleanup and publication. Every
Phase 1 setup operation is therefore inside the delta.

| Root | Kind | Inventory | Hashed | Remainder | Excluded | Source revision | Before = after manifest digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `/data/CoordExp/.worktrees/research-probes` | git | 1280 | 1281 | 61422 | 27 | `f4b061b73e89e19c19062fac0c9c68030ef00082` | `1a36381bf389553202246647de133e06e2c5617dabaaee76aa209e99061ec032` |
| `/data/CoordExp/serena-light` | git | 158 | 159 | 651 | 3 | `5e7d8ba84ecee8612a964f7b972bfe94f8cfa4b0` | `8e00ef0c49396e7ed0972b9444fec7b497324381be2907ed239e7f8fbda6ac7d` |
| `/data/ms-swift` | git | 617 | 618 | 1711 | 1 | `f2797138dba0e224cfff735cd89a528a08d8732a` | `f26ed45a75cada25a81255f343dea266f9b679a82a882b53f397a8e0f9443151` |
| `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages` | non_git | 3 | 3 | 0 | 0 | — | `8af78cfa9a1f4950b9cc38e2084de2b1490dec2145c2c42ae53590a3be6db094` |
| `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` | non_git | 2214 | 2214 | 0 | 0 | — | `8d9a9884993e7e25373b45a6b0d8fa18c9e80f8f7e115fd290e7d4f708fbaa72` |

**Counts.** 68,090 paths observed, 68,059 in scope, 31 excluded. Every excluded path is
published in the receipt: `.git` in all three Git roots, `.admission-artifacts` and `.venv`
in the production checkout, and 25 `node_modules` trees below `research-probes/.pi-worker`.
`research-probes/model_cache` is in scope and contributes to its root's 61,422 remainder
records. Both non-Git roots stay bounded by declared task paths and the bounded transformers
inventory, as Decision 3 requires.

**Deltas.** One delta per root, each bound to its own before and after manifest digest; every
`unexpected`, `declared`, and `control_changes` list is empty, and every before digest equals
its after digest. `RootManifest.manifest_digest` was independently recomputed from each
manifest's own canonical fields after the run and matched in all ten cases.

### Production identity invariant

`production_identity_before == production_identity_after`, with the published `after` side
captured after evaluation-owned cleanup, and independently re-measured after the run against
`/data/CoordExp/serena-light`:

| Field | Value |
| --- | --- |
| `pyproject.toml` | `97c8e100f9dd8b0f77a1cbf69a02bea1b1c4ff3d04d168cbf4f4854e914e17d4` |
| `uv.lock` | `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca` |
| `package-lock.json` | `c4f17c7f2e5faf7f69a1d11642792cb8bae5b6502c7288cde3577a8ec3fe0cba` |
| `dependency_lock_digest` | `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941` |
| `compute_build_identity` | `77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` |
| `runtime_paths` | 9 production entries, unchanged |

`git status --short` in the production repository is empty and its HEAD is unchanged at
`5e7d8ba`.

### Cleanup and process ownership

Evaluation-owned cleanup ran once on the passing path and removed nothing. Admission launches
only synchronous `uv` invocations, each bounded by the phase's remaining time and run in its
own session; a post-run process scan found no surviving `uv`, `ty`, or `pyrefly` process. The
frozen candidate lock and the prepared runtime are retained deliberately as Phase 2 inputs.

### Preservation of the attempted evidence

The attempted run's artifacts are byte-for-byte unchanged after the rerun:
`admission-receipt.json` `sha256:de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348`
(2,367,756 bytes, inode `125834110`, mtime unchanged),
`candidate-requirements.lock` `sha256:6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062`,
`candidate-lock-receipt.json` `sha256:dc2bdffeae55ed34e16d91dc9c5dd3112c089f7a95d677fa3a27e41c2ed1e157`,
`candidate-requirements.in` `sha256:677ea5585fb4b5c2dee18b92764ab7ad192572d0a40bf7d6e1336d8c05e4044e`.
The rerun published under a different evaluation identity and a per-run receipt path, so it
could not have replaced them.


## Residual risks carried forward

- `ty 0.0.70` is a pre-`0.1` release; its protocol surface is unproven and Phase 2 must
  record an explicit negative result where a capability is not advertised.
- The candidate runtime is prepared but never exercised; readiness, crash, and cleanup
  behaviour are entirely Phase 2 evidence.
- A freeze is bound to its `--exclude-newer` value; a later candidate release requires a new
  evaluation identity rather than mutating existing evidence.
- The remainder sweep is bound by what metadata can express. A rewrite that preserves size,
  inode, *and* `mtime_ns` inside one filesystem timestamp tick is not observable there; the
  trust-inventory closure and the declared configuration paths are content hashed on every
  capture and are not subject to that bound.
- The corpus roots are live worktrees other lanes may write. A concurrent write during a
  future phase will hold rather than pass. That is the instrument working; it must not be
  compensated for by narrowing the sweep.
- `serena_light.workspace.inventory.git_trust_inventory` starts one further `git ls-files`
  that production owns and this change may not modify, so that single call is not bounded by
  the phase deadline. The evaluation runs the identical bounded probe immediately before it,
  so a hung Git is observed and the phase stops before the production helper is reached.
