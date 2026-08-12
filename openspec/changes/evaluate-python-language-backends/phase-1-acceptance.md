# Phase 1 acceptance: admission gate

**Admission gate disposition: PASS on the run below. Phase 1 completion remains on HOLD
pending two independent re-reviews of *that* receipt.** A second Sol-xhigh re-review
overruled a passing review of the previous run -- evaluator HEAD `285c203`, evaluation
identity `207e7521…81e4` -- with executable evidence of three further defects, and completing
their repair exposed a fourth. All four are repaired, tasks 1.13 and 1.15 were reopened and
closed for them, and the run recorded below was produced by the repaired evaluator from the
clean committed checkout `517a451`.

**Task 1.8 stays unchecked and on HOLD.** A checked box may never stand for an unreviewed
run, and neither re-review of this receipt has happened. Nothing else blocks it.

**No receipt has been erased.** Five runs are now on record byte-for-byte: the original
attempted run (instrument-limited), the repaired-instrument run (superseded when the last
unbounded Git child was removed), the reviewed run (superseded by the first repair), the
previously admitting run (superseded by this one), and the admitting run below. All 62
artifact files of the four earlier runs were captured immediately before this run and
re-captured immediately after: **62 files, 0 changed** in content, inode, size, `mtime`, or
mode, and 62 of the 63 earlier directories unchanged in every one of those fields. The single
exception is the artifact root's own `mtime`, which advanced because it gained the new
evaluation-identity child; every one of the 33 new entries is under that child alone.


## What the second re-review held on

Three defects it found with executable evidence, plus a fourth found while completing the
repair -- each now fixed and covered by adversarial tests:

1. **A delayed post-link directory `fsync` let a run return `pass` after the ceiling with
   the final receipt present.** The ceiling was re-observed once after the atomic `link`,
   but the temporary unlink and the *last* directory `fsync` came after that observation
   with no further check, so a run could earn its pass with work done past 1800 s -- the
   re-review demonstrated a `pass` returned at 1811 s. Fixed: every post-link namespace
   mutation and every durability barrier is followed by its own ceiling observation,
   including one immediately before the function returns, while withdrawal is still
   possible; only descriptor closes follow it. The first observation that sees expiry
   withdraws this run's own link and its own temporary and fails closed. A FakeClock
   regression makes each post-link barrier slow in turn and requires that no `pass` is
   returned and no receipt is left, and the alternation itself is pinned structurally so no
   post-link syscall can be added later without an observation after it.
2. **Cleanup received no deadline and could spend the whole budget.** Fixed: the cleanup
   protocol and the real implementation both take the same monotonic deadline, the
   implementation checks it around each of its own syscalls, and the call is bracketed by one
   owner on both sides. A ceiling reached in cleanup raises rather than being downgraded to
   an issue on an otherwise passing receipt.
3. **A retained runtime still carried five harness-written files at `0660` from a
   pre-contract build.** Fixed: serialized reuse repairs them under the per-digest runtime
   lock and then re-verifies the whole contract, so a violating runtime is never returned.
   The repair is mode-only -- no byte moves, so neither the installed snapshot digest nor
   the published manifest digest can change -- and the contract is scoped by *ownership*:
   third-party resolver and environment internals keep their tool-defined modes behind
   `0700` service-owned ancestors and stay outside the artifact-tree digest, pinned by test
   on both sides. No recursive chmod exists anywhere in the harness.
4. **The first cut of that repair could `fchmod` a file outside the runtime root.** It
   opened each harness-written file by its whole relative path under a single `O_NOFOLLOW`,
   which constrains only the *last* component; a symlinked `config/ty` therefore carried the
   `fchmod` outside the root. The escape was reproduced -- a decoy at `0644` outside the root
   came back `0600` -- before being closed. Fixed: both the repair and its verification open
   every component from its parent's descriptor and prove the target regular through that
   same descriptor, and the reproduction is kept as a regression.

### The residual boundary, stated rather than papered over

The ceiling is enforced *cooperatively*, at the boundaries between syscalls, in the calling
thread. A `link`, `unlink`, or `fsync` already in flight is not preemptible, and a watchdog
thread that interrupted one would trade a bounded, observable overrun for an unbounded
correctness hazard in the middle of a durability barrier. The consequence: for as long as one
in-flight post-link `fsync` takes to complete, the final receipt name can exist in the
directory after the ceiling has passed. It is withdrawn at the next boundary, and it is not
admitted evidence -- every consumer of this gate requires the command to have exited
successfully *and* the receipt to verify canonically against its own digest and artifact-tree
digest, and an overrun run supplies neither. The two invariants that are actually guaranteed
are the ones that matter: no `pass` is ever returned after the ceiling, and no final receipt
remains once an overrun has been observed. No claim of preemption is made.


## What the first final review held on

Three defects, each repaired and covered by adversarial tests:

1. **The 1800 s ceiling did not cover publication or waiting, so the whole gate could
   report a false PASS.** `ended_at` was recorded before publication; the publication lock,
   the write, the `fsync`, the link, and the directory `fsync` of a 39.7 MB receipt had no
   deadline and no post-check; and the candidate-resolution and candidate-runtime `flock`s
   were blocking, so another process could hold a run past its ceiling with no deadline ever
   consulted. Repaired: every lock is acquired non-blockingly against the same monotonic
   deadline with no background thread; publication checks the ceiling before it starts,
   writes in deadline-checked chunks, links only while a 5 s publication reserve remains,
   and withdraws its own link inside the lock if the ceiling arrives during it. A PASS can no
   longer be published or returned after the ceiling, and a failed publication leaves nothing
   at the final path. Atomicity and immutability are unchanged: the only name ever unlinked
   is the one this run's `O_EXCL` temporary and failing `link` prove no other run published.
2. **The receipt hashed only `scripts/backend_eval` while executing `serena_light` helpers
   from an older parent checkout.** The CLI host was the parent evaluation `.venv`, whose
   editable `.pth` points at `/data/CoordExp/.worktrees/serena-light-backend-eval/src`; the
   corpus manifests, the write guard, and the production-identity capture therefore ran that
   worktree's trust-inventory normalization, guarded directory opener, dependency-lock
   digest, build identity, and runtime paths, none of which the evaluation identity named.
   Repaired: the evaluator binds `serena_light` to *its own* checkout's `src` before any
   helper import, refuses by realpath any loaded `serena_light` module that resolves outside
   that checkout, and records the executed production closure -- origin root, per-file byte
   digest, recomputed closure digest, and cleanliness at the recorded commit -- inside the
   evaluator identity and therefore inside the evaluation identity. An adversarial test
   changes helper bytes and repoints a helper's path without touching `scripts/backend_eval`
   and requires the identity to change or the run to be refused; a second test proves the
   bound set is *complete* by showing the admission CLI loads no non-stdlib module other
   than `scripts` and `serena_light`. The recorded host deviation therefore no longer imports
   unbound parent source.
3. **Strict PASS accepted any positive admission budget.** It checked only the frozen budget
   *names* plus `admission.seconds > 0`, so a receipt could claim a pass against a widened
   ceiling. Repaired: every budget must equal its frozen `DEFAULT_PHASE_BUDGETS` seconds, in
   construction and in parsing, with a mutation test per budget.


## The admitting run

| Field | Value |
| --- | --- |
| `evaluation_identity` | `0960ec132cd77fbc70d881bbba9af54ad93f40029312869fe0f9f7a1d9037025` |
| `run_identity` | `991c9866327d9c57de11e35d8e5e82c82459ed92d72ab84a0a9d970b514cff33` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/receipts/<run-identity>.json` |
| receipt `sha256` | `a0b1ff57dde7a8ec1e205793f1a15e4e7bda61a7e65e6e61e565e87b96367dfd` (39,745,560 bytes, inode `125834957`, mode `0600`) |
| `schema_version` / contract | `2` / `python-backend-evaluation-v1` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| window | `started_at=2026-08-12T01:23:33Z`, `ended_at=2026-08-12T01:23:40Z` -- **7 s** of the 1800 s ceiling |
| `artifact_tree_digest` | `5794c162acb4fca6ea20d41489f48ddcdfbadcc04a0985b9a55293ec04de5d0f` |

### Exact command

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # resolved to 2026-08-12T01:23:33Z
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, empty stderr. Run once, from the clean committed checkout `517a451`, with
`git status --porcelain` empty before the run.

**Recorded deviation, unchanged and receipt-bound.** The declared
`conda run -n ms python -m scripts.backend_eval.admission ...` form still fails before the
module is reached: in the `ms` environment `import scripts` resolves to
`/data/verl/scripts/__init__.py`, a regular package that shadows this repository's `scripts`
namespace package. Neither environment was altered and nothing was installed globally. The
CLI host was the parent evaluation `.venv`, whose interpreter is recorded *in the receipt*
and is part of the evaluation identity; the evaluator binds `serena_light` to its own
checkout before any helper import and records that closure. The evaluated `ms` `uv` and `ms`
interpreter were passed explicitly through `--uv` and `--python`.

### Evaluator, production helpers, host, and bootstrap environment

| Field | Value |
| --- | --- |
| evaluator source closure | 11 files of `scripts/backend_eval`, digest `efc736bd4b7036149af288c377c13aba9fc98c8bfe34a7dd125c18d81623fe65` |
| evaluator source commit | `517a45197aae2978da728d7f12cb2a5129c68fff`, source clean |
| executed production closure | digest `d7ed23955949067b932e9b18e5818ca6bece52797cbd2b2241fb84981331966b`, clean at that commit |
| CLI host interpreter | `/root/miniconda3/envs/ms/bin/python3.12` |
| environments | `llm-framework-study`, `ms` |
| service configurations | `pyrefly`, `pyright`, `ty` |

### Candidate lock and runtime

| Field | Value |
| --- | --- |
| candidate lock digest | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| candidates | `pyrefly==1.2.0`, `ty==0.0.70` (production Pyright `1.1.403` retained) |
| runtime root | `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| runtime manifest `sha256` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` |

The lock digest is unchanged from the previous run, so this run *reused* the retained runtime
-- which is exactly the path the mode repair had to survive.

### The pre-contract runtime repair, measured on both sides

`runtime_permission_repairs=candidate-requirements.lock,config/pyrefly/pyrefly.toml,config/pyright/pyrightconfig.json,config/ty/ty.toml,runtime-manifest.json`

Captured immediately before and immediately after the run, under the same per-digest runtime
lock the reuse holds:

| Harness-written file | mode before | mode after | size | inode | `sha256` |
| --- | --- | --- | --- | --- | --- |
| `candidate-requirements.lock` | `0660` | `0600` | 2,576 | `129273354` | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| `runtime-manifest.json` | `0660` | `0600` | 5,210 | `129273453` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` |
| `config/pyrefly/pyrefly.toml` | `0660` | `0600` | 81 | `129273450` | `9cbcaf9b661d0f873cece8e71ee2bc5900ddd5687720f357687a6571d61ad914` |
| `config/pyright/pyrightconfig.json` | `0660` | `0600` | 160 | `129273452` | `eff18e93bdb98237d0a00f3a4df8c900402433601a510f5f9f149e11ac3b539f` |
| `config/ty/ty.toml` | `0660` | `0600` | 93 | `129398425` | `a67784aafa3a72c8dc706ef26339509845ceebe84f7a3e1bb20abf40748c03d1` |

Size, inode, and `sha256` are identical on both sides for all five: the mode is the only
field that moved. The published manifest digest the receipt binds is therefore the same
`e578bf4d…` value it was before the repair. The runtime root and all eight service-owned
directories below it were `0700` before and after. `uv`'s own cache interior was not touched.

The new evaluation root observes the same contract: `.admission-publication.lock`,
`.candidate-lock.lock`, `candidate-lock-receipt.json`, `candidate-requirements.in`,
`candidate-requirements.lock`, and the receipt are all `0600`; the evaluation root, `receipts`,
and `uv-cache` are all `0700`; and the only wider mode anywhere below it is `uv-cache/uv/.lock`
at `0777` -- `uv`'s own file, behind `0700` ancestors and excluded from the artifact-tree
digest, which is the documented boundary rather than an exception to it.

### The measurement window and the corpus

Five roots, ten manifests (before and after), **68,059 in-scope paths**, 31 declared excluded
paths. `unexpected_write_paths=0`, `manifest_control_changes=0`, declared mutations `0`. One
write delta per root, each bound to both of its own manifest digests.

### Production identity invariant

`production_build_identity` and `production_dependency_lock` are equal on both sides and equal
to a fresh live capture taken after the run:
`77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` and
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`.

### Deadline, process, and cleanup evidence

The run used 7 s of the 1800 s ceiling and completed publication inside it: the receipt is
returned only after the link, the temporary unlink, and *both* directory `fsync`s have
happened below the ceiling, each followed by its own observation. Cleanup ran once on the
passing path, under the same deadline, and removed nothing. A post-run scan for `uv`, `ty`,
`pyright`, or `pyrefly` processes found none.

### Independent re-verification

From the published bytes alone, after the run: strict parsing succeeds and the canonical
round trip is byte-identical; the budget set equals `DEFAULT_PHASE_BUDGETS` exactly; all ten
`RootManifest.manifest_digest` values recompute from their own canonical fields; there is one
delta per root and each is bound to both of its manifest digests; `artifact_tree_digest`
recomputed over the evaluation root equals `5794c162…`; the runtime manifest digest re-read
from disk equals the receipt's `e578bf4d…`; and the live production identity equals both
receipt sides.

### Preservation of all four earlier runs

Captured immediately before this run and re-captured immediately after: **62 artifact files,
0 changed** in content, inode, size, `mtime`, or mode, and 62 of the 63 earlier directories
unchanged in all of those fields. The one exception is the artifact root directory's own
`mtime`, which advanced because it gained the new evaluation-identity child -- the same
behaviour every earlier run showed. Thirty-three entries were created, all of them under
`0960ec13…7025/`. The run published under its own evaluation identity and its own per-run
receipt path, so it shared no name with any earlier record.

| Artifact | `sha256` | size | inode |
| --- | --- | --- | --- |
| `36696159…99335/admission-receipt.json` | `de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348` | 2,367,756 | `125834110` |
| `1d00793b…a36297/receipts/c7136711…166767.json` | `29ed04ed65a447100064265b7540dfec1a13bd5174a198d2f526a56971b6f45e` | 39,744,615 | `125834210` |
| `380aaeb4…9147d/receipts/7749b4f9…74be4.json` | `830705ebee286d49d64df18ede84de803d632cadb9292537631b587a212709ae` | 39,744,615 | `125834243` |
| `207e7521…81e4/receipts/2f9e7a08…507b.json` | `3ef7be84035c01538be7ad73722fb82e4373e2464a0cabe9ffa5906a850dcdc9` | 39,745,560 | `125834277` |

### Repository gates at this commit

`pytest -q tests`: 1422 passed, 35 skipped (the 35 skips are the external-root snapshot gates,
unchanged; the count rose from 1400 with the new regressions). `ruff check src tests scripts`:
clean. `ty check --python <eval venv>`: clean. `openspec validate --all --strict`: 5 passed,
0 failed. `git diff --check`: clean.


## Superseded but retained (4): the previously admitting run

| Field | Value |
| --- | --- |
| `evaluation_identity` | `207e75213dbab46151297694d0dc854208112c72821420d75aea2320e1aa81e4` |
| `run_identity` | `2f9e7a084790d7c4d63426bb9d65e07763cdb6513db332735d1fab20b915507b` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/receipts/<run-identity>.json` |
| receipt `sha256` | `3ef7be84035c01538be7ad73722fb82e4373e2464a0cabe9ffa5906a850dcdc9` (39,745,560 bytes, mode `0600`) |
| `schema_version` / contract | `2` / `python-backend-evaluation-v1` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| window | `started_at=2026-08-12T00:13:38Z`, `ended_at=2026-08-12T00:13:45Z` -- **7 s** of the 1800 s ceiling (8 s process wall) |
| `artifact_tree_digest` | `aed095bce63a9e4ee09057511f18223c160581ebe95b77d0a3324ad5fd5f5b4f` |

### Exact command

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # resolved to 2026-08-12T00:13:38Z
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, empty stderr. Run once, from the clean committed checkout `285c203`.

**Recorded deviation, now fully receipt-bound.** The declared `conda run -n ms python -m
scripts.backend_eval.admission ...` form still fails before the module is reached: in the
`ms` environment `import scripts` resolves to `/data/verl/scripts/__init__.py`, a regular
package that shadows this repository's `scripts` namespace package. The CLI host was again
the parent evaluation `.venv`. What changed is that the deviation no longer imports unbound
parent source: the package binds `serena_light` to this checkout's `src` before any helper
import, and the receipt records the executed production closure by origin, bytes, and
cleanliness. Neither environment was altered and nothing was installed globally. The
evaluated `ms` `uv` and `ms` interpreter were passed explicitly through `--uv` and `--python`.

### Evaluator, production helpers, host, and bootstrap environment

| Field | Value |
| --- | --- |
| evaluator source closure | 11 modules of `scripts/backend_eval`, digest `439c392512602129faaa173b85ba8c726749913cf2a4b7f586894a97fa6f49ec` |
| evaluator source commit | `285c2034c50a53540695f390957bae4fb530106b`, source clean |
| production closure root | `/data/CoordExp/.worktrees/serena-light-backend-eval-final-fix/src` -- the evaluator's *own* checkout |
| production closure | 6 executed `serena_light` modules, digest `d7ed23955949067b932e9b18e5818ca6bece52797cbd2b2241fb84981331966b`, source clean |
| CLI host configured path | `/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python` |
| CLI host realpath / version | `/root/miniconda3/envs/ms/bin/python3.12` / `3.12.11` |
| CLI host `sha256` | `068d88ca469ae96121a1a220eb9e0ac3e1a6400b193e3b9cc7c14f54f9ed28e4` |
| bootstrap inherited keys | `ALL_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`, `LANG`, `NO_PROXY`, `all_proxy`, `http_proxy`, `https_proxy`, `no_proxy` -- names and SHA-256 value digests only |
| bootstrap service keys | `HOME`, `PATH`, `TMPDIR`, `UV_CACHE_DIR`, `UV_NO_CONFIG`, `UV_PYTHON_DOWNLOADS`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` |
| bootstrap refused keys | `CONDA_EXE`, `GIT_EDITOR` |

The six bound production modules are `serena_light/__init__.py`, `bootstrap.py`,
`build_identity.py`, `workspace/__init__.py`, `workspace/identity.py`, and
`workspace/inventory.py`, each carried in the receipt with its own SHA-256. No proxy value,
CA path, or other environment value appears in plaintext anywhere in the receipt.

### Candidate lock and runtime

A fresh `--exclude-newer 2026-08-12T00:13:38Z` resolved to the same frozen bytes as all three
earlier runs -- lock digest
`6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062`, `pyrefly==1.2.0` (12
lock-set hashes) and `ty==0.0.70` (18), raw-lock witness 2,576 bytes -- so the runtime was
content addressed to the same root, verified in full, and reused rather than rebuilt. No
candidate language server was launched.

| Field | Value |
| --- | --- |
| runtime root | `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062/` |
| `runtime-manifest.json` `sha256` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` (recomputed from disk before PASS, and again after the run) |
| environments | `llm-framework-study` 3.12.13, `ms` 3.12.11 |
| service configs | `pyrefly` `9cbcaf9b…`, `pyright` `eff18e93…`, `ty` `a67784aa…` |

The selected Linux x86_64 wheels and the untouched production `ty 0.0.24` slot are exactly as
recorded further down.

### The measurement window and the corpus

The first capture ran **before** the candidate lock was compiled and before the runtime was
prepared; the second ran **after** preparation and before cleanup and publication.

| Root | Kind | Inventory | Hashed | Remainder | Excluded | Before = after manifest digest |
| --- | --- | --- | --- | --- | --- | --- |
| `/data/CoordExp/.worktrees/research-probes` | git | 1280 | 1281 | 61422 | 27 | `1a36381bf389553202246647de133e06e2c5617dabaaee76aa209e99061ec032` |
| `/data/CoordExp/serena-light` | git | 158 | 159 | 651 | 3 | `8e00ef0c49396e7ed0972b9444fec7b497324381be2907ed239e7f8fbda6ac7d` |
| `/data/ms-swift` | git | 617 | 618 | 1711 | 1 | `f26ed45a75cada25a81255f343dea266f9b679a82a882b53f397a8e0f9443151` |
| `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages` | non_git | 3 | 3 | 0 | 0 | `8af78cfa9a1f4950b9cc38e2084de2b1490dec2145c2c42ae53590a3be6db094` |
| `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` | non_git | 2214 | 2214 | 0 | 0 | `8d9a9884993e7e25373b45a6b0d8fa18c9e80f8f7e115fd290e7d4f708fbaa72` |

Source revisions: `research-probes` `f4b061b7`, `serena-light` `5e7d8ba8`, `ms-swift`
`f2797138`. **Counts.** 68,090 observed = 68,059 in scope + 31 excluded. One delta per root,
each bound to its own before and after manifest digest; every `unexpected`, `declared`, and
`control_changes` list is empty, and every before digest equals its after digest.

All ten manifest digests are byte-identical to the two preceding runs'. The repair changed the
gate's ceiling, identity, and strictness, not what the corpus instrument observes.

### Production identity invariant

`production_identity_before == production_identity_after`, with the published `after` side
captured after evaluation-owned cleanup, and independently re-measured after the run against
`/data/CoordExp/serena-light`: all three readings are equal, and equal to every earlier run's.

| Field | Value |
| --- | --- |
| `pyproject.toml` | `97c8e100f9dd8b0f77a1cbf69a02bea1b1c4ff3d04d168cbf4f4854e914e17d4` |
| `uv.lock` | `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca` |
| `package-lock.json` | `c4f17c7f2e5faf7f69a1d11642792cb8bae5b6502c7288cde3577a8ec3fe0cba` |
| `dependency_lock_digest` | `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941` |
| `compute_build_identity` | `77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` |
| `runtime_paths` | 9 production entries, all below `…/serena-light/deps/eff6ebdf…ab4941/`, unchanged |

### Deadline, process, and cleanup evidence

The 1800 s ceiling now covers resolution, runtime preparation, both captures, cleanup, the
final production identity, the artifact digest, **lock acquisition, and publication**;
collection reserves 300 s so a timeout could still publish a receipt, and publication keeps a
5 s reserve in front of its atomic link. This run used 7 s of the ceiling for its collected
evidence and completed publication inside it -- the run returns its receipt only after the
link and the directory `fsync` have both happened below the ceiling. Every child ran in its
own session with the phase's remaining time as its bound. Cleanup ran once on the passing
path and removed nothing. A post-run scan for `uv`, `ty`, or `pyrefly` processes found none.

### Independent re-verification

From the published bytes alone, after the run: the canonical round trip is byte-identical,
all ten `RootManifest.manifest_digest` values recompute from their own canonical fields, every
delta's before and after digest equals its manifest, the budget set equals
`DEFAULT_PHASE_BUDGETS` exactly, the recorded production closure equals the closure a fresh
in-process binding produces, the runtime manifest digest re-read from disk matches the
receipt, and the live production identity equals both receipt sides. Parsing re-runs the full
tightened PASS invariant set and succeeds.

### Preservation of all three earlier runs

Captured immediately before this run and re-captured immediately after: **46 artifact files,
0 changed** in content, inode, size, `mtime`, or mode. Sixteen files were created, all of them
under `207e7521…81e4/`. The run published under its own evaluation identity and its own
per-run receipt path, so it shared no name with any earlier record.

| Artifact | `sha256` | size | inode |
| --- | --- | --- | --- |
| `36696159…99335/admission-receipt.json` | `de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348` | 2,367,756 | `125834110` |
| `1d00793b…a36297/receipts/c7136711…166767.json` | `29ed04ed65a447100064265b7540dfec1a13bd5174a198d2f526a56971b6f45e` | 39,744,615 | `125834210` |
| `380aaeb4…9147d/receipts/7749b4f9…74be4.json` | `830705ebee286d49d64df18ede84de803d632cadb9292537631b587a212709ae` | 39,744,615 | `125834243` |

### Repository gates at this commit

`pytest -q tests`: 1400 passed, 35 skipped (the 35 skips are the external-root snapshot gates,
unchanged). `ruff check src tests scripts`: clean. `ty check --python <eval venv>`: clean.
`openspec validate --all --strict`: 5 passed, 0 failed. `git diff --check`: clean.


## Superseded but retained (3): the reviewed run

| Field | Value |
| --- | --- |
| `evaluation_identity` | `380aaeb4135c729746e42ff58eecd2f97dd999d90119febd4df5e5e3e6f9147d` |
| `run_identity` | `7749b4f99b43bdb2e1b143147d9c5030f6ef79a3cafcd3fd5acdc391e0074be4` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/receipts/<run-identity>.json` |
| receipt `sha256` | `830705ebee286d49d64df18ede84de803d632cadb9292537631b587a212709ae` (39,744,615 bytes, mode `0600`) |
| `schema_version` / contract | `2` / `python-backend-evaluation-v1` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| window | `started_at=2026-08-11T23:15:23Z`, `ended_at=2026-08-11T23:15:33Z` -- **10 s** of the 1800 s ceiling (12 s process wall) |
| `artifact_tree_digest` | `3a698c3060211f6fe8fc497c18dc3c38a4a432796e3de764e241160a6cb8bfc3` |

### Exact command

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # resolved to 2026-08-11T23:15:22Z
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, empty stderr. Run once, from the clean committed checkout.

**Recorded deviation, unchanged and receipt-bound.** The declared
`conda run -n ms python -m scripts.backend_eval.admission ...` form still fails before the
module is reached: in the `ms` environment `import scripts` resolves to
`/data/verl/scripts/__init__.py`, a regular package that shadows this repository's `scripts`
namespace package. Neither environment was altered and nothing was installed globally. The
CLI host was the parent evaluation `.venv`, whose interpreter is recorded *in the receipt*
and is part of the evaluation identity. The evaluated `ms` `uv` and `ms` interpreter were
passed explicitly through `--uv` and `--python`.

### Evaluator, host, and bootstrap environment

| Field | Value |
| --- | --- |
| evaluator source closure | 10 modules of `scripts/backend_eval`, digest `9c821ff0c7a39d84a4da8ca045cd188e7f99395db7ba070a0763aaa5453d6b37` |
| evaluator source commit | `7d40d411dcdf02da52ae311042b2aad1ded4aa93`, source clean |
| CLI host configured path | `/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python` |
| CLI host realpath / version | `/root/miniconda3/envs/ms/bin/python3.12` / `3.12.11` |
| CLI host `sha256` | `068d88ca469ae96121a1a220eb9e0ac3e1a6400b193e3b9cc7c14f54f9ed28e4` |
| bootstrap inherited keys | `ALL_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY`, `LANG`, `NO_PROXY`, `all_proxy`, `http_proxy`, `https_proxy`, `no_proxy` -- names and SHA-256 value digests only |
| bootstrap service keys | `HOME`, `PATH`, `TMPDIR`, `UV_CACHE_DIR`, `UV_NO_CONFIG`, `UV_PYTHON_DOWNLOADS`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME` |
| bootstrap refused keys | `CONDA_EXE`, `GIT_EDITOR` |

No proxy value, CA path, or other environment value appears in plaintext anywhere in the
receipt.

### Candidate lock and runtime

The fresh `--exclude-newer 2026-08-11T23:15:22Z` resolved to the same frozen bytes as both
earlier runs -- lock digest
`6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062`, `pyrefly==1.2.0` and
`ty==0.0.70`, raw-lock witness 2,576 bytes -- so the runtime was content addressed to the
same root, verified in full, and reused rather than rebuilt. No candidate language server was
launched.

| Field | Value |
| --- | --- |
| runtime root | `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062/` |
| `runtime-manifest.json` `sha256` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` (recomputed from disk before PASS, and again after the run) |
| environments | `llm-framework-study` 3.12.13, `ms` 3.12.11 |
| service configs | `pyrefly`, `pyright`, `ty` (digests unchanged from the record below) |

The selected Linux x86_64 wheels and the untouched production `ty 0.0.24` slot are exactly as
recorded further down.

### The measurement window and the corpus

The first capture ran **before** the candidate lock was compiled and before the runtime was
prepared; the second ran **after** preparation and before cleanup and publication. Every
Phase 1 setup operation is inside the delta.

| Root | Kind | Inventory | Hashed | Remainder | Excluded | Before = after manifest digest |
| --- | --- | --- | --- | --- | --- | --- |
| `/data/CoordExp/.worktrees/research-probes` | git | 1280 | 1281 | 61422 | 27 | `1a36381bf389553202246647de133e06e2c5617dabaaee76aa209e99061ec032` |
| `/data/CoordExp/serena-light` | git | 158 | 159 | 651 | 3 | `8e00ef0c49396e7ed0972b9444fec7b497324381be2907ed239e7f8fbda6ac7d` |
| `/data/ms-swift` | git | 617 | 618 | 1711 | 1 | `f26ed45a75cada25a81255f343dea266f9b679a82a882b53f397a8e0f9443151` |
| `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages` | non_git | 3 | 3 | 0 | 0 | `8af78cfa9a1f4950b9cc38e2084de2b1490dec2145c2c42ae53590a3be6db094` |
| `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` | non_git | 2214 | 2214 | 0 | 0 | `8d9a9884993e7e25373b45a6b0d8fa18c9e80f8f7e115fd290e7d4f708fbaa72` |

**Counts.** 68,090 observed = 68,059 in scope + 31 excluded. Every excluded path is published
in the receipt: `.git` in all three Git roots, `.admission-artifacts` and `.venv` in the
production checkout, and 25 `node_modules` trees below `research-probes/.pi-worker`.
`research-probes/model_cache` is in scope and contributes to that root's 61,422 remainder
records.

**Deltas.** One delta per root, each bound to its own before and after manifest digest; every
`unexpected`, `declared`, and `control_changes` list is empty, and every before digest equals
its after digest.

**Corroboration of the bounded-inventory change.** All ten manifest digests are byte-identical
to the repaired-instrument run's. That run derived the trust inventory from
`git_trust_inventory`; this one derives it from the bounded
`git ls-files --cached --others --exclude-standard -z` bytes through production's pure
helpers. Identical inventory digests, counts, and manifest digests across the two runs are
real-corpus evidence for the equivalence the unit tests assert.

### Production identity invariant

`production_identity_before == production_identity_after`, with the published `after` side
captured after evaluation-owned cleanup, and independently re-measured after the run against
`/data/CoordExp/serena-light`: all three readings are equal.

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

### Deadline, process, and cleanup evidence

**Corrected by the final review.** The ceiling this run enforced covered resolution, runtime
preparation, both captures, cleanup, the final production identity, and the artifact digest,
but *not* publication and *not* lock acquisition: `ended_at` was stamped before the receipt
was serialized, written, `fsync`-ed, and linked, and every `flock` was blocking. The 10 s this
run reports is therefore the pre-publication window, not the whole gate, and its PASS could
not have been refused had publication overrun. Collection did reserve 300 s so a timeout could
still publish a receipt. Every child -- the resolver and every Git invocation of a capture,
nine bounded Git children per Git root capture -- ran in its own session with the phase's
remaining time as its bound. Cleanup ran once on the passing path and removed nothing. A
post-run scan for `uv`, `ty`, or `pyrefly` processes found none.

### Independent re-verification

From the published bytes alone, after the run: the canonical round trip holds, all ten
`RootManifest.manifest_digest` values recompute from their own canonical fields, every delta's
before and after digest equals its manifest, the runtime manifest digest re-read from disk
matches the receipt, and the live production identity equals both receipt sides. Parsing
re-ran the PASS invariant set *of that schema* and succeeded; that invariant set has since
been tightened -- exact budget seconds, and a bound production closure the receipt does not
carry -- so those bytes are retained evidence of what the run observed, not a receipt the
current gate would admit.

### Preservation of both earlier runs at the time of that run

Captured immediately before the run and re-captured immediately after; identical in content
*and* in inode, size, mtime, ctime, and mode:

| Artifact | `sha256` | size | inode |
| --- | --- | --- | --- |
| `36696159…99335/admission-receipt.json` | `de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348` | 2,367,756 | `125834110` |
| `36696159…99335/candidate-requirements.lock` | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` | 2,576 | `125834164` |
| `36696159…99335/candidate-lock-receipt.json` | `dc2bdffeae55ed34e16d91dc9c5dd3112c089f7a95d677fa3a27e41c2ed1e157` | 903 | `125834165` |
| `36696159…99335/candidate-requirements.in` | `677ea5585fb4b5c2dee18b92764ab7ad192572d0a40bf7d6e1336d8c05e4044e` | 11 | `125834143` |
| `1d00793b…a36297/receipts/c7136711…166767.json` | `29ed04ed65a447100064265b7540dfec1a13bd5174a198d2f526a56971b6f45e` | 39,744,615 | `125834210` |
| `1d00793b…a36297/candidate-requirements.lock` | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` | 2,576 | `125834207` |

The run published under its own evaluation identity and its own per-run receipt path, so it
shared no name with either earlier record.

## What the earlier reviews held on

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

## Superseded but retained (1): the attempted run

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

## Superseded but retained (2): the repaired-instrument run

This run demonstrated the repaired instrument on the real corpus and passed. It is **not**
the admitting receipt: `scripts/backend_eval` changed after it -- task 1.16 removed the last
unbounded Git child -- so the evaluation identity it was bound to no longer names the current
evaluator. It is retained in full, and its per-root manifest digests corroborate the
admitting run's.

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
- The evaluator's own checkout must contain the production source it executes. The binding
  refuses a foreign `serena_light`, so a CLI host installed from another worktree fails the
  run closed rather than silently changing semantics; the operator's remedy is to run from a
  host whose module search resolves this checkout, which the package bootstrap arranges.
- The publication reserve is 5 s. A filesystem whose single `link` plus directory `fsync`
  exceeds that would fail closed rather than publish late; that is the intended direction of
  the error, but it is a bound, not an absence of one.
- **The ceiling is cooperative, and one syscall-width window remains.** A `link`, `unlink`,
  or `fsync` already in flight cannot be interrupted from the calling thread, so the final
  receipt name can exist in the directory for the duration of one in-flight post-link
  `fsync` after the ceiling has passed. It is withdrawn at the next boundary. No `pass` is
  ever returned after the ceiling and no final receipt survives an observed overrun, and a
  transient namespace entry is not admitted evidence because every consumer requires a
  successful CLI exit *and* canonical plus artifact-digest verification. Closing the window
  itself would require preempting a durability barrier from another thread, which trades a
  bounded, observable overrun for an unbounded correctness hazard; that trade is declined
  deliberately and is a user-owned decision if it is ever revisited.
- The mode contract is scoped by ownership, not by location. Third-party resolver and
  environment internals -- notably `uv`'s world-writable `.lock` -- keep their tool-defined
  modes. They are confined behind `0700` service-owned ancestors and excluded from the
  artifact-tree digest, so they are outside the evidence a receipt binds, but they are not
  `0600`.
- Every Git child of a corpus capture is now bounded without exception. The evaluation no
  longer calls `git_trust_inventory`; it reads the identical combined
  `git ls-files --cached --others --exclude-standard -z` through the bounded runner and
  reuses production's subprocess-free normalization and inspection helpers, with an
  equivalence test pinning the result to `git_trust_inventory` field by field. One capture
  starts exactly nine bounded Git children and no other process.
