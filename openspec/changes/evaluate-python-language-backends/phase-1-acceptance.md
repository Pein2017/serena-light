# Phase 1 acceptance: admission gate

**Disposition: PASS.** The admission receipt is canonical, production identity is
byte-identical before the run and after evaluation-owned cleanup, every bounded corpus root
shows zero unexpected writes,
and the permitted next action is `begin_protocol_probe_planning`. No candidate language
server was launched, no installed or canonical Serena Light registration was touched, and
the production dependency slot was not modified.

## Exact command

```bash
# Run 1 resolved the freeze timestamp; run 2 reused that exact frozen value.
backend_eval_freeze_at="2026-08-11T21:11:47Z"   # run 1: "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
test -n "$backend_eval_freeze_at"
echo "$backend_eval_freeze_at"
python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, wall time 6 s against the 1800 s admission ceiling
(`started_at=2026-08-11T21:31:13Z`, `ended_at=2026-08-11T21:31:19Z`).

**This is the receipt of the second, authoritative run.** The admission code changed after
the first run (the receipt's `after` production identity now brackets evaluation-owned
cleanup), so the gate was rerun against the *same* frozen `--exclude-newer
2026-08-11T21:11:47Z`. The evaluation identity is therefore unchanged, the frozen candidate
lock was accepted without a second resolution (lock file inode unchanged across both runs),
the prepared runtime was verified and reused, and the canonical receipt was replaced
atomically (receipt inode `125834142` -> `125834110`). The first run's own values were
identical apart from its timestamps and its 22 s cold wall time, which included the one
candidate resolution and the one runtime build.

**Recorded deviation.** The declared form of this command is
`conda run -n ms python -m scripts.backend_eval.admission ...`. That form fails before the
admission module is reached, with exit `1` and
`ModuleNotFoundError: No module named 'scripts.backend_eval'`, because the `ms` environment
resolves `scripts` to the unrelated regular package `/data/verl/scripts`, which shadows this
repository's `scripts` namespace package, and because `serena_light` is not installed there.
Neither environment was altered and nothing was installed globally. The CLI process was run
instead with `/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python`, which
resolves `scripts.backend_eval.admission` from this change's worktree and `serena_light`
from the Task 1-5 worktree at the identical commit `ef740c6` (`src/` trees compared equal).
The evaluated `ms` interpreter and `ms` `uv` were still passed explicitly through `--python`
and `--uv` and are what the receipt records, so the deviation changes the CLI host process
only, not the evaluated identity.

## Evaluation identity and contract

| Field | Value |
| --- | --- |
| `evaluation_contract_version` | `python-backend-evaluation-v1` |
| `schema_version` | `1` |
| `evaluation_identity` | `36696159de500c09275b6ff174d7df2551990a489610c9feffa67a532da99335` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/admission-receipt.json` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| admission budget | 1800 s (`budgets.admission`); elapsed 6 s (warm reuse; 22 s cold on the first run) |

## Candidate lock

| Field | Value |
| --- | --- |
| lock digest | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| `exclude_newer` | `2026-08-11T21:11:47Z` |
| resolved packages | 2 |
| `ty` | `0.0.70`, artifact `sha256:06aca758d1e0016c0a1f57fe9d8de7a21ff83f692306f747a0d97f32de24e27f` |
| `pyrefly` | `1.2.0`, artifact `sha256:25822ea9505f589ea8a725e4268b475132fb89e038fbf092e446510443ac142a` |

Production Pyright `1.1.403` is retained and untouched; neither candidate was installed into
the production dependency slot.

## Interpreters and runtime

| Role | Configured path | Real path | Version |
| --- | --- | --- | --- |
| resolver `uv` | `/root/miniconda3/envs/ms/bin/uv` | same | `uv 0.9.2` (`sha256:b399aea4…cc9fee`) |
| base interpreter | `/root/miniconda3/envs/ms/bin/python` | `…/bin/python3.12` | `3.12.11` (`sha256:068d88ca…ed28e4`) |
| environment `ms` | `/root/miniconda3/envs/ms/bin/python` | `…/bin/python3.12` | `3.12.11` |
| environment `llm-framework-study` | `/root/miniconda3/envs/llm-framework-study/bin/python` | `…/bin/python3.12` | `3.12.13` |
| runtime interpreter | `<runtime-root>/venv/bin/python` | — | candidate venv |

Runtime root: `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062/`
(content addressed by the lock digest; requirements snapshot `sha256` equals the lock digest).
Candidate executables: `ty 0.0.70` (`sha256:a0f425a3…0e00a5`), `pyrefly 1.2.0`
(`sha256:8ff3120d…407695`). Neither was launched as a language server in this phase.

## Service-owned configuration

All three configurations own the runtime `HOME` and cache
(`<runtime-root>/home`, `<runtime-root>/cache`).

| Backend | Config path (below `<runtime-root>/config/`) | `sha256` |
| --- | --- | --- |
| `pyrefly` | `pyrefly/pyrefly.toml` | `9cbcaf9b661d0f873cece8e71ee2bc5900ddd5687720f357687a6571d61ad914` |
| `pyright` | `pyright/pyrightconfig.json` | `eff18e93bdb98237d0a00f3a4df8c900402433601a510f5f9f149e11ac3b539f` |
| `ty` | `ty/ty.toml` | `a67784aafa3a72c8dc706ef26339509845ceebe84f7a3e1bb20abf40748c03d1` |

## Production identity invariant

`production_identity_before == production_identity_after` (receipt-enforced for `pass`).
The published `after` side is captured *after* evaluation-owned cleanup has run, so it also
covers anything cleanup itself could have changed; the mid-run post-operation capture is
still taken and asserted under the deadline.

| Field | Value |
| --- | --- |
| `pyproject.toml` | `97c8e100f9dd8b0f77a1cbf69a02bea1b1c4ff3d04d168cbf4f4854e914e17d4` |
| `uv.lock` | `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca` |
| `package-lock.json` | `c4f17c7f2e5faf7f69a1d11642792cb8bae5b6502c7288cde3577a8ec3fe0cba` |
| `dependency_lock_digest` | `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941` |
| `compute_build_identity` | `77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` |
| `runtime_paths` | 9 production entries, all below `…/serena-light/deps/eff6ebdf…ab4941/`, unchanged |

Independently re-measured after the run against the production root
`/data/CoordExp/serena-light`: `dependency_lock_digest` and `compute_build_identity` return
`eff6ebdf…ab4941` and `77e0ff6e…d183aa`, matching the receipt exactly. `git status --short`
in the production repository is empty.

**How that re-measurement was run.** It was *not* run with the declared
`conda run -n ms python -c ...` form, which fails for the same reason as the admission
command itself (see *Recorded deviation*); it used the same
`/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python` host, whose editable
install resolves `serena_light` to `/data/CoordExp/.worktrees/serena-light-backend-eval/src/serena_light`
rather than to the production checkout. No claim is made that the literal `conda` command
reproduced these values. The re-measurement is trustworthy because the *measuring code* is
provably the same code production carries: the Git tree object for `src/serena_light` is
`e29217431a15bee9a95bf4339c2583213d302fab` in the production checkout (`main`, `5e7d8ba`),
in the editable worktree (`ef740c6`), and in this change's worktree, and `git status
--porcelain -- src pyproject.toml` is empty in both checkouts, so the on-disk sources match
those trees. The measured *input* is in every case the production root passed explicitly as
an argument.

## Bounded corpus manifests and write deltas

Five roots, captured twice around the no-backend admission operation. Every delta is bound to
its own before/after manifest digest, `declared` is empty everywhere, and `unexpected` is
empty everywhere (0 unexpected paths in total).

| Root | Kind | Inventory count | Source revision | Before = after manifest digest |
| --- | --- | --- | --- | --- |
| `/data/CoordExp/.worktrees/research-probes` | git | 1280 | `f4b061b73e89e19c19062fac0c9c68030ef00082` | `de9da5c3d415dd2c430d02185123c6c658c4e8ddbcef365e5547209e04a3cee1` |
| `/data/CoordExp/serena-light` | git | 158 | `5e7d8ba84ecee8612a964f7b972bfe94f8cfa4b0` | `b18086fb255c5be3f50d74288e0965d6c72e7eafead6cf8040443fa80f89fcc6` |
| `/data/ms-swift` | git | 617 | `f2797138dba0e224cfff735cd89a528a08d8732a` | `cf26adb02825172b3c1af97eba8dd7278008d948063d01327da16b0065293e7a` |
| `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages` | non_git | 3 | — | `30af637d5377d8b3abc5659b899b41e2052d6f3e8a46ba0d800145a21f19f57e` |
| `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` | non_git | 2214 | — | `82974ea634285cc367fbb1c4f30d401aa1101c7f5b5309be9339b9026b6e7c63` |

## Artifact tree

`artifact_tree_digest = 12c15677bac4a4ca1ee7ea13b53d928f3990421eebb8514745e32f2237b7bc30`
over `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/`, excluding the
rebuildable resolver cache and the receipt itself. Published members:
`candidate-requirements.in`, `candidate-requirements.lock`, `candidate-lock-receipt.json`,
`admission-receipt.json`, plus the `uv-cache/` download store. The directory is git-ignored.

## Cleanup

Evaluation-owned cleanup ran once on the passing path and removed nothing (`issues` empty);
a cleanup that had to remove partial state, or that failed, would have made the run
`incomplete` whatever it looked like beforehand. Production identity was then captured a
third time, after cleanup, and that post-cleanup value is the receipt's `after` side: it
equals the pre-work value, so cleanup changed no production lock, digest, build identity, or
runtime path. A cleanup that reported success while changing any of those would have been
held; a failure to take that final capture would have failed the run closed with no receipt. No evaluation-owned child process remains: admission launches only synchronous
`uv` invocations and starts no candidate language server, and a post-run process scan found
no surviving `uv`, `ty`, or `pyrefly` process. The frozen candidate lock and the prepared
runtime are retained deliberately as Phase 2 inputs.

## Residual risks carried into Phase 2

- `ty 0.0.70` is a pre-`0.1` release; its protocol surface is unproven and Phase 2 must record
  an explicit negative result where a capability is not advertised.
- The candidate runtime is prepared but never exercised; readiness, crash, and cleanup
  behaviour are entirely Phase 2 evidence.
- The freeze is bound to `--exclude-newer 2026-08-11T21:11:47Z`; a later candidate release
  requires a new evaluation identity rather than mutating this evidence.
