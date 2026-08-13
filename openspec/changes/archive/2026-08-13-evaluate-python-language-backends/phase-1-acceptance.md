# Phase 1 acceptance: admission gate

**Admission gate disposition: admitted PASS; Task 1.8 is complete.** The clean executed
evaluator target is `2a06e1671c8f66cbe1bd4ece9d79e428e57b8896`, and the admitted evidence
is exactly evaluation `ce005e27…961fa`, run `442851b1…a39a2`. Scoped review had already passed
the sealed evaluator transport at `79c35d5` and the explicit Git trust correction at
`2a06e167`. The required final Sol-xhigh static/correctness and Opus-max runtime/evidence
reviews now both PASS the exact receipt and permit Task 1.8 closure.

## Current admitting run: `ce005e27…961fa` / `442851b1…a39a2`

### The failed canonical attempt that preceded it

The first canonical execution against `79c35d5`, frozen at
`2026-08-12T12:00:24Z`, returned typed `incomplete` in approximately one second with
`corpus_capture_failed`: Git rejected
`/data/CoordExp/.worktrees/research-probes` as dubious ownership. It published **no receipt**
and leaked no process. This execution is failure evidence, not a receipt and not a PASS.

The sealed child intentionally has no `HOME`; older runs had obtained `safe.directory` from
`/root/.gitconfig`, which also contains user identity and credential-helper state and is not
an admissible input. The correction at `2a06e167` gives every evaluator and corpus Git child
no `HOME`, disables system configuration, and supplies a sealed protected global config plus
a matching `-c` argument that trust only the exact declared Git root. No parent, wildcard,
credential helper, user identity, include, or ambient global excludes file is accepted.
Repository-local ignore semantics remain active. The correction neither reads nor passes the
user Git configuration.

### Exact command and terminal result

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="2026-08-12T12:29:00Z"
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -I -S -B scripts/backend_eval_bootstrap.py \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

The command ran once from the clean committed `2a06e167` checkout. It exited `0`; stderr was
empty and stdout was the compact terminal result. The receipt window is
`started_at=2026-08-12T12:29:00Z` through `ended_at=2026-08-12T12:29:17Z`, 17 seconds of the
1800-second admission ceiling.

**Receipt-bound CLI-host deviation.** The command used the sibling evaluation worktree's
configured interpreter path
`/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python`, not a `.venv` below
the `final-fix` checkout. The receipt binds that exact path, its realpath
`/root/miniconda3/envs/ms/bin/python3.12`, version `3.12.11`, and SHA-256
`068d88ca469ae96121a1a220eb9e0ac3e1a6400b193e3b9cc7c14f54f9ed28e4` inside the evaluator
identity. The working directory and bootstrap script remained the clean `final-fix` checkout,
and the semantic evaluator executed from the sealed image recorded below.

| Field | Value |
| --- | --- |
| `evaluation_identity` | `ce005e27b796ef323e3b4fb09c27eff2d1acb8c8b56928ead3628e287eb961fa` |
| `run_identity` | `442851b127d85f568500ccfc1bf3d7ca0214ec78eb1049b3382feb8babda39a2` |
| receipt | `/data/CoordExp/serena-light/.admission-artifacts/backend-eval/ce005e27b796ef323e3b4fb09c27eff2d1acb8c8b56928ead3628e287eb961fa/receipts/442851b127d85f568500ccfc1bf3d7ca0214ec78eb1049b3382feb8babda39a2.json` |
| receipt file | inode `125836138`, mode `0600`, 39,753,917 bytes, SHA-256 `68085049014a900559a9d7c84b328f044be0230f78e09ec312b2b42700f63912` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `artifact_tree_digest` | `e6e10ca2ff9540a83457d361b081bb08eccc07a2662be9d249f4a2417600f7e6` |

The published bytes round-trip to canonical serialization exactly and pass the current strict
schema parser. The evaluation identity, receipt SHA-256, artifact-tree digest, runtime
manifest digest, evaluator and production closure digests, all root-manifest digests, and
both production identity sides were independently recomputed and matched.

### Evaluator, candidate, and runtime identity

| Field | Value |
| --- | --- |
| sealed semantic evaluator closure | clean commit `2a06e1671c8f66cbe1bd4ece9d79e428e57b8896`; 15 source files; digest `8b740633bfc83fee60a22c58ef73b893b5c2f1b3f3e4d7bb8f9375d61c98dcc8` |
| executed production-helper closure | clean; 6 files; digest `d7ed23955949067b932e9b18e5818ca6bece52797cbd2b2241fb84981331966b` |
| candidate lock | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062`, unchanged; `pyrefly==1.2.0`, `ty==0.0.70` |
| runtime manifest | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec`, unchanged |
| runtime permission repair | none |

All frozen budgets equal the contract exactly: admission 1800 s, protocol 5400 s,
product-seam 10800 s, feature 7200 s, Agent 28800 s, and total 57600 s.

### Production identity and pre-existing main advance

Production main had independently advanced **before** this run, at 2026-08-12 10:27 UTC, to
`fd6335c5182a56bb266adc6f0ec07bf862bf3117`. The receipt therefore correctly binds that
commit. The evaluator did not cause this pre-existing main update, and equality to the
historical `77e0ff6e…` build identity is not the current invariant.

The current invariant is `production_identity_before == production_identity_after ==` the
independent live capture. `compute_build_identity` is
`6498a4eb68c62e23561aa6b04e167fe54dd55b9d90b80c12bbb6560f078b9c39` on all three sides.
The dependency-lock digest remains
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`, and its input bytes
remain unchanged:

| Lock input | SHA-256 |
| --- | --- |
| `pyproject.toml` | `97c8e100f9dd8b0f77a1cbf69a02bea1b1c4ff3d04d168cbf4f4854e914e17d4` |
| `uv.lock` | `5998451d896430ca4df3cf28f92e6a0bc413bcb840673c5f4db8be64f9a9edca` |
| `package-lock.json` | `c4f17c7f2e5faf7f69a1d11642792cb8bae5b6502c7288cde3577a8ec3fe0cba` |

### Corpus and zero-write result

| Root | Kind | Source revision | In scope | Excluded |
| --- | --- | --- | ---: | ---: |
| `/data/CoordExp/.worktrees/research-probes` | git | `f4b061b73e89e19c19062fac0c9c68030ef00082` | 62,714 | 27 |
| `/data/CoordExp/serena-light` | git | `fd6335c5182a56bb266adc6f0ec07bf862bf3117` | 815 | 3 |
| `/data/ms-swift` | git | `f2797138dba0e224cfff735cd89a528a08d8732a` | 2,329 | 1 |
| `llm-framework-study` declared non-Git paths | non_git | — | 3 | 0 |
| `ms` transformers | non_git | — | 2,214 | 0 |

The totals are 68,075 in-scope paths and 31 declared exclusions. Every before manifest
equals its after manifest. All five deltas contain zero declared mutations, zero unexpected
paths, and zero manifest-control changes.

### Preservation, process ownership, and current code gates

All nine prior receipt files -- eight schema-v2 receipts plus the legacy receipt -- remain
byte-for-byte preserved. This run adds the ninth schema-v2 receipt, for ten receipt files in
total. No evaluator or candidate process leaked. The existing production Serena Pyright
daemon is unrelated to this evaluation and was not stopped, restarted, or otherwise touched.

Current-code evidence at `2a06e167` is intentionally scoped rather than presented as a new
whole-repository test count:

- focused Git trust, dubious-ownership worktree, bounded-runner, identity, and ownership
  tests: 59 passed in 9.06 s;
- full `tests/backend_eval`: 597 passed in 87.73 s;
- repository-wide Ruff and Ty: clean;
- strict validation of `evaluate-python-language-backends`: exit 0;
- `git diff --check`: clean;
- production `src/serena_light`, `pyproject.toml`, `uv.lock`, and `package-lock.json`:
  unchanged by the evaluator repair.

### Final exact-receipt review dispositions

- **Sol-xhigh: PASS**, with no P0, P1, or P2 finding, against the exact receipt and evidence
  record at `992734ba8b2561c3e87c6e67de746500a7fcc0d4`. It explicitly permits Task 1.8 closure
  after the Opus review.
- **Opus-max: PASS** on runtime and evidence. It began against documentation HEAD
  `2d20adc15fe90c20c0df63be1a244c2baaf922a6`; the receipt and executed evaluator bytes did
  not change, and the reviewer re-read the corrected current acceptance text at `992734b`
  during the audit. It independently matched the receipt metadata and SHA-256, canonical and
  strict parse, 15-file evaluator and 6-file production closures, evaluation/artifact/runtime
  and before/after/live production identities, exact budgets, all five roots (68,075 in scope
  plus 31 exclusions) with zero deltas, candidate lock and runtime, all ten receipt files, and
  process cleanup. It explicitly permits Task 1.8 closure after the Sol review.

**Deferred nonblocking Low.** `identity.py` constructs the fixed evaluator-owner
`safe.directory` config without the unsafe-character guard that `manifests.py` applies to a
caller-declared corpus root. The current owner path is a fixed constant with none of those
characters, so this cannot affect this receipt, admit a different root, or produce a false
PASS. Changing evaluator bytes now would invalidate the receipt. The guard is therefore
deferred as static hardening for a future evaluator revision, not a Phase 1 blocker.

Both required final reviews approve the exact current receipt. The scoped evaluator-transport
and Git-trust reviews also remain PASS at their exact code targets; no acceptance work remains
for Task 1.8.

## Historical evidence retained below

The remaining sections are an append-only record of earlier rejected, superseded, and
historical runs and their then-current test counts. Their old dispositions and references to
an "admitting run" describe their own time and are superseded by the current disposition
above; they are not erased or retroactively rewritten.

## The eighth defect family: what rejected run `59a38137…d73f`

1. **The identity captured before first child use was not what first use enforced.**
   `capture_evaluator_identity()` recorded `production_child.py` and the executed
   production-helper closure, and then the first `run_production_helper()` called
   `_PINNED_CHILD_DIGESTS.setdefault(...)` -- accepting whatever bytes were on disk at *that*
   moment instead of requiring the earlier identity. A changed child program was reproduced
   executing successfully after identity capture. The sealed `memfd` proved only that the bytes
   read for `production_child.py` were the bytes executed; the six `serena_light` helpers were
   still imported from the mutable `src` root and only *re-read* afterwards, so a transient
   substitution could execute and be restored before either post-hoc read saw it.

   Repaired: `HelperExpectation`, derived from the captured `EvaluatorIdentity`, is passed
   explicitly into every production-helper call the admission makes and compared before
   anything runs; the verified child program *and* the verified helper bytes are executed and
   imported from sealed `memfd` images addressed by descriptor, with no `src` root on the
   child's `sys.path` at all; closure membership is exact per operation and enforced in the
   child and again in the parent; the evaluator identity is re-measured after cleanup and
   before publication so a late mutation can never yield a `pass`; and `_PINNED_CHILD_DIGESTS`
   is gone, so two admissions in one process cannot contaminate each other.

1b. **Production helpers still executed in the evaluator process.** Found by a focused review
   of the first repair: `manifests.py` imported `bounded_non_git_trust_inventory`,
   `_decode_git_path`, `_inventory_from_candidates`, and `open_guarded_directory` from the
   mutable `src/serena_light` into the *evaluator*. Python compiled those bytes at import time
   and `capture_evaluator_identity()` re-read the same paths afterwards, so a swap between the
   two would have published a receipt naming one closure while the parent's corpus evidence was
   computed by another -- the same defect as (1), one level up.

   Repaired: both inventory helpers execute in the sealed child as two further operations with
   the same exact declared closure, returning only the evidence a `RootManifest` is built from
   as canonical JSON that the parent validates field by field and cross-checks against
   production's own path-digest formula; the metadata traversal is evaluator-owned code with a
   guarded declared-root open and confined descendants; and a fresh-interpreter regression plus
   an AST rule prove that importing the evaluator leaves no `serena_light` module in the parent.

2. **Two filesystem-ownership claims were false.** `ProductionAdmissionServices.cleanup()` was
   reproduced following a symlinked ancestor and unlinking a decoy *outside* the evaluation
   root: it opened `evaluation_root / receipts` as one absolute pathname under `O_NOFOLLOW`,
   which protects only the leaf. `artifact_tree_digest()` had the same ancestor weakness and
   fed admitted evidence. `shutil.which("git")` resolved the corpus scanner's Git from the
   ambient `PATH`.

   Repaired: both acquire their root by walking every component from the declared owner root's
   descriptor; the ownership table names the root opens `guarded` rather than `confined`, adds
   a `declared-path` class for the pathname-shaped observations that are weaker still, expands
   the structural collector to namespace mutation, descriptor byte and durability operations,
   metadata and link operations, descriptor duplication and release, and executable discovery,
   and proves the `descriptor` class mechanically; and Git is one declared absolute executable,
   proven a regular executable file through one descriptor before any child starts.

Every one of these is covered by an adversarial regression that was first shown to fail against
the pre-repair behaviour: the two ancestor-substitution exploits, the child-program and helper
swaps between capture and first use, the helper swap inside the import window, the unexpected
extra and missing expected closure members, the origin escape, the late evaluator mutation, and
two sequential admissions in one process.

## Repository gates at the final-review repair HEAD

`pytest -q tests`: **1511 passed, 35 skipped** (the 35 skips are the external-root snapshot
gates, unchanged; the count rose from 1427 with the new expectation, ownership, and
ancestor-substitution regressions). `ruff check src tests scripts`: clean. `ty check`: clean
(run against this worktree's own `.venv`). `openspec validate --all --strict`: 5 passed, 0
failed. `git diff --check`: clean. Production lock/build/runtime identity invariants: unchanged
and covered by `tests/backend_eval/test_production_identity.py` and
`tests/acceptance/` -- `src/serena_light`, `pyproject.toml`, `uv.lock`, and
`package-lock.json` are byte-identical to the frozen base.

`conda run -n ms pytest -q tests` cannot run in this worktree and could not at the frozen base
either: the shared `ms` environment carries an editable `.pth` for `/data/verl`, whose regular
`scripts` package shadows this repository's `scripts` namespace package, so `tests/conftest.py`
fails at `import scripts.backend_eval`. This is the same ambient shadowing every earlier run
recorded as its CLI-host deviation. The gates above were therefore run with this worktree's own
`.venv` -- the same interpreter `ty check` has always used, built on the `ms` interpreter.

## The rejected run, retained

**The record below is unchanged.** It describes run `59a38137…d73f` exactly as it was
published, including its own then-current gate counts at HEAD `82651d0`. It is evidence of a
rejected run, not an admitted PASS.

**Its previous disposition, superseded.** Phase 1 completion remains on HOLD pending two
independent re-reviews of an admitting receipt that does not yet exist. A second Sol-xhigh re-review
overruled a passing review of an earlier run -- evaluator HEAD `285c203`, evaluation identity
`207e7521…81e4` -- with executable evidence of three further defects, and completing their
repair exposed a fourth. An Opus-max review of the repaired evaluator then found a fifth:
every guarded regular-file read opened `O_RDONLY | O_NOFOLLOW` with no `O_NONBLOCK`, so a
FIFO or other blocking special node left where a regular file was expected -- reproduced at
`runtime-manifest.json` -- hung the open rather than failing closed. It could not produce a
false `PASS`, since the `fstat` regular-file refusal still ran after any open that returned,
but it could exceed the whole-phase ceiling with no receipt. A sixth review then found that
the audit which produced that fix was flag-shaped -- it searched `os.open` constants, which
cannot see `Path.read_bytes`, `Path.write_text`, `Path.mkdir`, or a production helper call --
and three whole families of read and write had survived it: every harness-owned write in
`runtime.py`, the production-identity inputs and the production helpers behind them, and the
corpus content digest. All six are repaired, tasks 1.13 and 1.15 were reopened and closed for
them -- and a seventh, found by this repair's own receipt verification, before the admitting
run. The run recorded below is a fresh run from the repaired, committed evaluator at HEAD
`82651d0`.

**Task 1.8 stays unchecked and on HOLD.** A checked box may never stand for an unreviewed
run, and neither re-review of this receipt has happened. Nothing else blocks it.

**No receipt has been erased.** Eight runs are now on record byte-for-byte: the original
attempted run (instrument-limited), the repaired-instrument run (superseded when the last
unbounded Git child was removed), the reviewed run (superseded by the first repair), the
run admitted by the ceiling-and-mode repair, the run admitted by the guarded-read repair, the
run admitted before the descriptor-ownership repair, the first descriptor-ownership run
(superseded when its own verification exposed the narrowed production closure), and the
admitting run below. All 110 artifact files and 113 artifact directories of the seven earlier
runs were captured immediately before this run and re-captured after it: **110 files, 0
changed** in content, inode, size, `mtime`, or mode, and **113 directories, 0 changed** in
every one of those fields. The only entry that moved at all is the artifact root's own
`mtime`, which advanced because it gained the new evaluation-identity child -- same inode, same
mode; every one of the 16 new files and 17 new directories is under that child alone. All seven
earlier receipt-bearing identities still recompute their recorded `artifact_tree_digest`
exactly.


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
5. **Every guarded regular-file read could hang indefinitely on a FIFO.** `_read_regular_file`
   in `runtime.py`, `admission.py`, `identity.py`, and `source_binding.py` opened
   `O_RDONLY | O_NOFOLLOW` with no `O_NONBLOCK` (`candidate_lock.py` already carried it).
   `open()` on a FIFO with no writer blocks regardless of `O_NOFOLLOW`, and that block is one
   uninterruptible syscall with no cooperative checkpoint inside it -- reproduced at
   `runtime-manifest.json`. Fixed: `O_NONBLOCK` added to every guarded read; it has no effect
   on a regular file's read behavior and changes nothing about `O_NOFOLLOW`, descriptor-relative
   confinement, or the `fstat` regular-file refusal. Five adversarial regressions (one per
   guarded-read family) each proved to hang under a bounded `pytest-timeout` override without
   the fix, then pass in well under a second with it, asserting a typed error and no leaked
   descriptor: the runtime manifest, the owned-runtime mode-repair walk, the admission
   artifact-tree read, the evaluator source closure, and the bound production helper closure.

6. **Three whole families of read and write were outside the previous audit.** The
   `O_NONBLOCK` repair above searched `os.open` constants, which cannot see an access that
   carries no flags. A Sol-xhigh HOLD review and an independent read-only audit found the
   same three families:

   * **Harness-owned writes in `runtime.py`.** The installed lock snapshot, the three service
     configurations, and the manifest temporary were written with a path-based
     `O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW`, and their directories created with
     `Path.mkdir(parents=True)`. `O_NOFOLLOW` guards only the last component, so a symlinked
     `config/ty` carried the write outside the runtime root; a FIFO at `config/ty/ty.toml`
     blocked the open until a reader appeared and then received the harness payload; and
     `O_TRUNC` destroyed an existing target before anything proved what it was. The
     service-configuration verification was a check followed by `Path.read_bytes()`, which a
     post-check symlink to a file holding the expected bytes passes.
   * **The production identity inputs.** `production_identity.py` read `pyproject.toml`,
     `uv.lock`, and `package-lock.json` the same check-then-read way, and the production
     helpers it calls -- `dependency_lock_digest`, `compute_build_identity`, `runtime_paths` --
     do the same inside `src/serena_light`, which the evaluation may not edit.
   * **The corpus digest.** `inventory.observe_file_digest` opens `O_RDONLY | O_NOFOLLOW` with
     no `O_NONBLOCK`, so a node substituted after its type was inspected blocks the capture.

   All three are closed without one byte of change to `src/serena_light`. Every harness-owned
   write and verification read below the runtime root walks out from the already-open root
   descriptor one component at a time, creating and reopening each intermediate directory from
   its parent's descriptor, opening leaves `O_NONBLOCK`, proving them regular by `fstat` on
   that same descriptor, and truncating only after that proof -- so a FIFO with a live reader
   is refused with not one byte delivered, reproduced as a regression. The three lock inputs
   are read through one guarded descriptor each. The four production helpers run as their
   exact unmodified bytes in a bounded, source-bound, minimal-environment child whose process
   group the phase deadline kills, with canonical digest-bound request and response and every
   executed helper byte re-read and compared by the parent; equivalence with the in-process
   helpers is pinned by test on honest inputs. And the surface is now enumerated structurally
   rather than by grep: `tests/backend_eval/test_io_ownership.py` parses every evaluator
   module, collects every filesystem access including `Path.read_bytes`, `Path.write_text`,
   `Path.mkdir`, and each production helper call, and fails until every one appears in a finite
   table with exactly one owner. `docs/backend-eval-io-ownership.md` is its prose companion.

   **What this does not claim.** Running production's bytes in a killable child *bounds* the
   helpers' own check-then-reopen race; it does not remove it. A helper blocked on a
   substituted node costs the phase its remaining budget and a typed failure, not an unbounded
   hang. Separately, `runtime_source_files` still silently skips a non-regular source file:
   that changes the build identity and is refused by the identity guard rather than hidden, and
   it is pinned by test.

   **Two source-binding seams inside this repair were closed before it was committed**, after
   a lead pre-review of the new bounded-child path. The parent's re-read of the helper bytes a
   child reported opened the whole relative path under one `O_NOFOLLOW`, so a symlinked `src`
   or `src/serena_light` could have supplied another tree's bytes for the parent to "confirm";
   it now walks every component from an open descriptor on the evaluator owner root, with an
   intermediate-substitution regression. And the child *program* was handed to the interpreter
   as a mutable pathname, leaving a window between the read that digested those bytes and the
   `execve` that ran them; it is now read through that same confined walk, pinned by digest on
   first use, and executed from a sealed `memfd` addressed as `/proc/self/fd/<image>` with only
   that descriptor inherited, with a test proving the digest equals the one the evaluator
   identity records for `production_child.py` and a substitution test proving a mid-run swap is
   refused rather than executed. Closing the second seam surfaced an unbounded child the
   bounded-runner accounting test then caught -- `ctypes.util.find_library("c")` shells out to
   `ldconfig` -- so `memfd_create` is resolved from the already-loaded process image instead.

7. **The repair's own receipt verification found a seventh defect: the bound production
   closure silently narrowed.** Moving three helpers into the bounded child stopped this
   process from importing `serena_light.bootstrap` and `serena_light.build_identity`, so
   `sys.modules` no longer saw them and the published `production_files` dropped from six
   entries to four. The receipt would have carried those helpers' *answers* while no longer
   naming their *bytes*, which is exactly the binding the "a phase executes a production
   helper" scenario requires. Fixed before the admitting run: `CHILD_EXECUTED_HELPERS` declares
   the modules the child loads, they are digested from this checkout alongside the in-process
   ones, and a test requires the child's own reported closure -- for every operation it
   supports -- to equal that declaration, so a helper that starts importing something new fails
   a test instead of quietly leaving the receipt. The closure and its digest are byte-identical
   to the previous admitting run's again.

### The residual boundaries, stated rather than papered over

**Confinement is claimed only where a root descriptor owns it.** A `guarded` read -- the
caller's declared candidate lock, an interpreter's realpath, the evaluator's own source
closure, the three production lock inputs below the resolved repository root -- closes
final-component substitution and blocking on a special node. It does not re-prove the
components *above* its root, because no root the harness opened owns them. That distinction is
in the ownership table, per access.

**A bounded race is still a race.** The production helpers keep their own check-then-reopen
window. The child bounds the consequence, not the window.

**The ceiling is cooperative.** It is enforced at the boundaries between syscalls, in the
calling thread. A `link`, `unlink`, or `fsync` already in flight is not preemptible, and a watchdog
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


## The rejected run `59a38137…d73f`, recorded exactly as it was published

*This section is the retained record of the run the final review rejected. Every number in it
was true of that run at HEAD `82651d0`. It is not an admitted PASS, and the language below --
including its own use of "admitting run" for itself and for its predecessors -- describes the
disposition that section carried before the rejection, not the current one.*

| Field | Value |
| --- | --- |
| `evaluation_identity` | `59a381372e561da4efe27dbe892c6b3116852d51ec3eef0e0ee22e31e857d73f` |
| `run_identity` | `35701e7bacdd7eb38cc0a1e7974f9cec602bf08bcc15535ce3710dd3ed6e74b5` |
| receipt | `<repo>/.admission-artifacts/backend-eval/<evaluation-identity>/receipts/<run-identity>.json` |
| receipt `sha256` | `809fe9143797a226206e1ead56cea7848fa99b53cac059e77081952a37a0233a` (39,749,313 bytes, inode `125834382`, mode `0600`) |
| `schema_version` / contract | `2` / `python-backend-evaluation-v1` |
| `status` / `next_action` | `pass` / `begin_protocol_probe_planning` |
| `issues` | none |
| window | `started_at=2026-08-12T04:13:54Z`, `ended_at=2026-08-12T04:14:06Z` -- **12 s** of the 1800 s ceiling |
| `artifact_tree_digest` | `2944f88213980a448c6bd8e47e0f79b3001d9df0a994b5ba5de16f10807d5d7b` |
| evaluator source | commit `82651d0dd66a72fc39f31f54c6b9ab03d5be8113`, clean, 13 files, closure `9205f879093d0d3c089386d98df8a89475ae8ad89338984d6bc1b6a8f2a01a26` |
| executed production closure | 6 files, digest `d7ed23955949067b932e9b18e5818ca6bece52797cbd2b2241fb84981331966b`, clean -- byte-identical to the previous admitting run |
| candidate lock | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` -- `pyrefly==1.2.0`, `ty==0.0.70`, unchanged from every earlier run |
| runtime | `runtime_manifest_sha256=e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec`, `runtime_permission_repairs=none` |
| corpus | 68,066 in-scope paths in 5 roots, 31 declared exclusions, 0 unexpected paths, 0 declared mutations, 0 changed controls |
| production identity | build identity and dependency-lock digest identical before and after |

This is the fresh run from the descriptor-ownership repair, produced by the clean committed
checkout `82651d0` with `git status --porcelain` empty before it. It is the first run in which
the production helpers -- the dependency-lock digest, the build identity, the runtime paths,
and every corpus content digest -- executed inside the bounded, source-bound child rather than
in the evaluator process, and in which every harness-owned read below the runtime root went
through the component-wise descriptor walk. Task 1.8 stays unchecked pending the same two
independent re-reviews as before.

**What this run did and did not exercise.** The candidate lock was accepted from its existing
freeze and the runtime at that digest was *reused*, so the run exercised the confined
verification reads, the mode contract, and the independent manifest-digest recomputation --
not the confined *write* path, which only a fresh build reaches. That path was therefore
exercised separately against real `uv venv` and `uv pip sync`, into a throwaway runtime base
outside the artifact tree and every protected root: it built the runtime in 215.6 s with all
five harness-written files at `0600`, all nine service-owned directories at `0700`, no
permission repairs, a manifest digest that recomputes from disk, and byte-identical service
configurations, installed snapshot digest, and candidate executable digests to the published
runtime (`pyrefly 8ff3120d…`, `ty a0f425a3…`). The probe base was removed afterwards, and the
artifact tree was re-captured before and after it: **0 of 110 files and 0 of 113 directories
changed**, and all six receipt-bearing identities still recompute their recorded
`artifact_tree_digest` exactly.

### Exact command

```bash
cd /data/CoordExp/.worktrees/serena-light-backend-eval-final-fix
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"   # resolved to 2026-08-12T04:13:54Z
/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Exit status `0`, empty stderr. Run once, from the clean committed checkout `82651d0`, with
`git status --porcelain` empty before the run. No evaluation process, child, or descriptor
outlived it.

**Recorded deviation, unchanged and receipt-bound.** Same as every earlier run: the CLI host
is the sibling worktree's `.venv` (`import scripts` under bare `ms` still shadows this
repository's `scripts` namespace package via `/data/verl/scripts`); the interpreter realpath,
SHA-256, and version are recorded in the receipt and bound into the evaluation identity, and
the evaluator binds `serena_light` to *its own* checkout (`.../final-fix/src`) before any
helper import, which the receipt's `production_root` confirms.

### Evaluator, production helpers, host, and bootstrap environment

| Field | Value |
| --- | --- |
| evaluator source closure | 13 files of `scripts/backend_eval`, digest `9205f879093d0d3c089386d98df8a89475ae8ad89338984d6bc1b6a8f2a01a26` |
| evaluator source commit | `82651d0dd66a72fc39f31f54c6b9ab03d5be8113`, source clean |
| executed production closure | 6 files, digest `d7ed23955949067b932e9b18e5818ca6bece52797cbd2b2241fb84981331966b` (unchanged), clean at that commit -- the three the bounded child executes are declared by `CHILD_EXECUTED_HELPERS` and pinned against the child's own report |
| bounded child program | `production_child.py`, digest `3e48b129b6bf0873697f5155bf8845d180101f32b0db794dd6872e050b60a749`, equal to the digest that run's `production_child_digest()` probe read through the confined walk. *That probe was removed in the final-review repair: the equality is now the execution path itself -- a helper may run only when the program on disk is byte-for-byte the one the captured identity names.* |
| CLI host interpreter | `/root/miniconda3/envs/ms/bin/python3.12`, sha256 `068d88ca469ae96121a1a220eb9e0ac3e1a6400b193e3b9cc7c14f54f9ed28e4` |
| environments | `llm-framework-study` (3.12.13), `ms` (3.12.11) |
| service configurations | `pyrefly`, `pyright`, `ty` |

### Candidate lock and runtime

| Field | Value |
| --- | --- |
| candidate lock digest | `6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` (unchanged) |
| candidates | `pyrefly==1.2.0`, `ty==0.0.70` (production Pyright `1.1.403` retained) |
| runtime root | `/data/CoordExp/.codex/runtime/serena-light/backend-eval/6cd570324d1a35aa0f4c30b60fd3005fe0953e8efe230915fb19ad24184b9062` |
| runtime manifest `sha256` | `e578bf4d6f1d98df96140d6c03b793a26af60658e49ea03b6810581898a6b4ec` (unchanged) |
| `runtime_permission_repairs` | `none` -- the runtime was already `0600`/`0700` from the earlier repair; reuse made no repair |

The lock digest and the runtime manifest digest are byte-identical to every earlier admitting
run: this run *reused* the already-correct runtime through the component-wise descriptor walk,
and found nothing to repair, which is exactly the ordinary-file behavior the repair was
required to preserve.

### The measurement window and the corpus

Five roots, ten manifests (before and after), **68,066 in-scope paths**, 31 declared excluded
paths. Every content digest in those manifests was computed by production's own
`observe_file_digest`, executed in bounded chunks inside the killable child. `unexpected_write_paths=0`,
`manifest_control_changes=0`, declared mutations `0` across all five roots. One write delta
per root, each bound to both of its own manifest digests.

### Production identity invariant

`production_build_identity` and `production_dependency_lock` are equal on both receipt sides
and equal to a fresh live capture taken independently after the run:
`77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa` and
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941` -- both unchanged from the
previous admitting run, since `src/serena_light` was not touched by this repair.

### Deadline, process, and cleanup evidence

The run used 12 s of the 1800 s ceiling and completed publication inside it. Cleanup ran once
on the passing path, under the same deadline, and removed nothing. Every child the run started
-- Git, and now the production-helper children -- went through the one bounded runner, in its
own session, with the phase's remaining time as its bound; a post-run scan found no surviving
`production_child`, `uv`, `ty`, `pyright`, or `pyrefly` process from this run, and no
descriptor leaked.

### Independent re-verification

From the published bytes alone, after the run: the canonical round trip is byte-identical;
strict parsing succeeds and the budget set equals `DEFAULT_PHASE_BUDGETS` exactly;
`artifact_tree_digest` recomputed over the evaluation root equals `2944f882…`; the runtime
manifest digest re-read from disk equals the receipt's `e578bf4d…`; the child program digest
read through the confined walk equals the `production_child.py` entry in the receipt's own
evaluator closure (through the `production_child_digest()` probe that run still had); and the
live production identity equals both receipt sides (above). All
seven receipt-bearing identities -- this one and the six earlier ones -- recompute their
recorded `artifact_tree_digest` exactly.

### Preservation of all seven earlier runs

Captured immediately before this run and re-captured immediately after: **110 artifact files,
0 changed** and **113 artifact directories, 0 changed** in content, inode, size, `mtime`, or
mode. Measured against the baseline taken before the *first* run of this repair cycle, the six
runs that predate it are likewise **94 files, 0 changed** and **96 directories, 0 changed**.
The artifact root directory's own `mtime` advanced because it gained the new
evaluation-identity child -- same inode, same mode -- the behaviour every earlier run showed.
Sixteen files and seventeen directories were created, all of them under `59a38137…d73f/`. The
run published under its own evaluation identity and its own per-run receipt path, so it shared
no name with any earlier record.

The intermediate run at `7ca37592…1900` is retained unchanged as evidence of the
narrowed-closure defect that its own verification exposed; it is not the admitting run.

| Artifact | `sha256` | size | inode |
| --- | --- | --- | --- |
| `36696159…99335/admission-receipt.json` | `de6d1a93c089f209cdc9e4e618ff0614f55faf3e9d02e31d295c8d295fe9c348` | 2,367,756 | `125834110` |
| `1d00793b…a36297/receipts/c7136711…166767.json` | `29ed04ed65a447100064265b7540dfec1a13bd5174a198d2f526a56971b6f45e` | 39,744,615 | `125834210` |
| `380aaeb4…9147d/receipts/7749b4f9…74be4.json` | `830705ebee286d49d64df18ede84de803d632cadb9292537631b587a212709ae` | 39,744,615 | `125834243` |
| `207e7521…81e4/receipts/2f9e7a08…507b.json` | `3ef7be84035c01538be7ad73722fb82e4373e2464a0cabe9ffa5906a850dcdc9` | 39,745,560 | `125834277` |
| `0960ec13…7025/receipts/991c9866…cff33.json` | `a0b1ff57dde7a8ec1e205793f1a15e4e7bda61a7e65e6e61e565e87b96367dfd` | 39,745,560 | `125834957` |
| `35b85d4e…d334/receipts/ddfe7d49…b8562.json` | `80401b6e7b2e06e3db137d127fed9598868994082dcdeee805efd74bdb8d1fe8` | 39,745,560 | `125835009` |
| `7ca37592…1900/receipts/c9a2456d…d9b63.json` | `8166f6f7a3fc72aa5195b3f9069da98e81a8380022e22227eda6720bf3455c4b` | 39,749,106 | `125835123` |

### Superseded but retained (6) and (7): the two runs of this repair cycle

| Run | Identity / receipt | Why it is not the admitting run |
| --- | --- | --- |
| 6 | `35b85d4e…d334` / `ddfe7d49…b8562`, `pass` in 12 s | Produced by HEAD `2503f85`, before the descriptor-ownership repair; superseded by it. |
| 7 | `7ca37592…1900` / `c9a2456d…d9b63`, `pass` in 11 s | Produced by HEAD `40eb2af`. Its own receipt verification exposed the narrowed production closure (defect 7): it published `production_files` with four entries where six helpers were executed. **Retained as evidence of that defect, and explicitly not admitted.** The admitting run above is from HEAD `82651d0`, which restores the six-file closure and its digest `d7ed2395…966b`. |

Both are byte-identical to when they were written, and both still recompute their own recorded
`artifact_tree_digest`.

### Repository gates at this commit

`pytest -q tests`: 1427 passed, 35 skipped (the 35 skips are the external-root snapshot gates,
unchanged; the count rose from 1422 with the five new FIFO regressions). `ruff check src tests
scripts`: clean. `ty check`: clean (run against this worktree's own `.venv`). `openspec
validate --all --strict`: 5 passed, 0 failed. `git diff --check`: clean.

*These are the counts at HEAD `82651d0`, the rejected run's own commit. The current gate counts
are at the top of this record.*


## Superseded but retained (5): the reviewed run that the second re-review overruled

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
