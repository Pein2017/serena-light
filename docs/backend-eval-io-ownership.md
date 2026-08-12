# Backend-evaluation I/O ownership

Every read and write the Phase 1 admission gate executes has exactly one owner, and the set
of them is finite and enumerated. `tests/backend_eval/test_io_ownership.py` is the
authority: it parses every module under `scripts/backend_eval/`, collects every call that
opens, creates, enumerates, reads, or writes a filesystem object, and fails until each one
appears in its `OWNERSHIP` table with an owner class. A new access anywhere in the evaluator
fails that test until it is declared; a removed one fails it until the row goes. This
document explains what each owner class guarantees, and states the boundaries that remain.

The enumeration deliberately covers more than `os.open` flags. `Path.read_bytes`,
`Path.write_text`, `Path.mkdir`, and a call into a production helper are each an unguarded
open with no flag to grep for, and each one is how a defect survived an earlier review.

## Why a check followed by an open is not a guard

Two failure modes recur, and both come from resolving the same mutable pathname twice.

**Substitution.** `path.is_file()` and then `path.read_bytes()` are two independent
resolutions. A symlink dropped between them is followed by the second, so the check passes
on one inode and the read lands on another. `O_NOFOLLOW` does not fix this on its own,
because it constrains only the *last* component: `open("config/ty/ty.toml", O_NOFOLLOW)`
still traverses a symlinked `config` or `config/ty`, and an `fchmod` or a write on the
descriptor it returns lands outside the root.

**Blocking.** `open()` on a FIFO with no peer blocks until one appears, regardless of
`O_NOFOLLOW`, and that block is one uninterruptible syscall in the calling thread with no
cooperative checkpoint inside it. A ceiling enforced between syscalls cannot bound it. For
a *write* the older form was worse still: `O_WRONLY | O_CREAT | O_TRUNC` on a FIFO blocked
until a reader appeared and then wrote the harness payload straight into that reader's pipe.

## Owner classes

### `confined`

A component-wise `O_NOFOLLOW` descriptor walk out from an already-proven open root. Every
component -- including every intermediate directory the harness creates -- is opened, or
created and reopened, from its parent's descriptor. The leaf is opened `O_NONBLOCK`, proven
a regular file by `fstat` on that same descriptor, and read or written through it. Nothing
is reopened by pathname, so a rename, a symlinked ancestor, or a swapped root cannot
redirect the access after it was validated.

Writes carry two further rules:

* **No `O_TRUNC`.** The leaf is opened `O_WRONLY | O_NOFOLLOW | O_NONBLOCK` and, only if that
  reports `ENOENT`, created `O_CREAT | O_EXCL`. An existing node keeps every byte until
  `fstat` proves it regular and `fchmod` proves the harness owns it; `ftruncate` comes after
  both. A FIFO with a live reader is therefore refused with not one byte delivered, which is
  pinned by `test_a_harness_owned_write_never_writes_into_a_readable_fifo`.
* **Re-proof after the write.** The descriptor's device, inode, and mode are re-read after
  the write and must be unchanged.

### `guarded`

`O_RDONLY | O_NOFOLLOW | O_NONBLOCK` plus an `fstat` regular-file proof on that same
descriptor, with the bytes read from it and no reopen -- or, for a root a `confined` walk
then starts from, `O_RDONLY | O_DIRECTORY` on an already-resolved absolute path. This is what
a `confined` walk reduces to when there is no owning root descriptor to walk out from: the
caller's declared candidate lock, an executable's realpath outside every root, the evaluator's
own source closure, and the opens of the resolved production repository root and the evaluator
owner root that the confined walks below them begin at. Confinement below a root is not
claimed for these, because no root owns them; substitution of the final component and blocking
on a special node are both closed.

### `descriptor`

The call operates on a descriptor this process already holds -- `os.fdopen`, `os.scandir(fd)`,
`os.open(..., dir_fd=fd)` relative to a proven parent. There is no pathname left to redirect.

### `production-child`

Exact production semantics, executed in `scripts/backend_eval/production_child.py` under the
phase's own monotonic deadline.

`dependency_lock_digest`, `compute_build_identity`, `runtime_paths`, and `observe_file_digest`
live in `src/serena_light`, which is production and is not edited to close an evaluation-only
exposure. All four check a path's type and then reopen it by name -- the first three through
`Path.is_file()` and `Path.read_bytes()`, the fourth through `O_RDONLY | O_NOFOLLOW` with no
`O_NONBLOCK` -- so a node substituted in that window blocks them. Reimplementing them in the
evaluator would drift from the semantics the receipt claims to bind, so the evaluation runs
their exact bytes and bounds the blast radius instead.

The child guarantees:

* **Bounded.** `Deadline.remaining()` is the child's timeout; `run_bounded_bytes` starts it in
  its own session and `SIGKILL`s the whole process group on expiry. A blocked helper costs
  the phase its remaining budget and a typed failure, never an unbounded hang.
* **Source bound, program included.** The child *program* is not named by a mutable pathname
  either. `production_child.py` is read once through a `confined` walk from an open descriptor
  on the owner root, its digest is pinned on first use and re-checked on every later call, and
  the bytes are executed from a `memfd` sealed `F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW |
  F_SEAL_SEAL` addressed as `/proc/self/fd/<image>` with that one descriptor -- and no other --
  inherited. The bytes that were digested are the bytes that run, and
  `production_child_digest()` is provably the digest the evaluator identity records for
  `production_child.py`. A mid-run substitution is refused by the pin rather than caught
  after the fact by `source_clean`.
* **Source bound, helpers included.** The child reports the byte digest of every
  `serena_light` module it loaded, and the parent re-reads each one -- component by component
  from the same open owner-root descriptor, `O_NOFOLLOW` on every component -- and compares.
  `O_NOFOLLOW` on the whole relative path would guard only the last component, so a symlinked
  `src` or `src/serena_light` could hand back another tree's bytes and have them accepted as
  this checkout's own. A child that ran another checkout's helpers is refused by realpath
  inside the child *and* by the parent's confined re-read.
* **No ambient shadowing.** `-I` refuses `PYTHONPATH` and the user site directory, the
  environment carries no `PATH`, and the child strips its own directory from `sys.path`
  before importing anything but the standard library -- so the ambient `scripts` namespace
  package that shadows this repository under a bare `ms` interpreter cannot reach it.
* **Canonically bound I/O.** Request and response are canonical JSON, the response must
  re-serialize to the exact bytes received, and the child echoes the SHA-256 of the request
  bytes it consumed. A response that does not name this request is refused.

Digest batches are chunked (`DIGEST_CHUNK_SIZE`). The chunk bounds the stability window: the
capture `lstat`s every path before its chunk and again after it, so a hashed path that moved
anywhere inside that window is recorded as unstable rather than as a clean hash.

### `own-image`

Reads this process's own sealed `memfd` through `/proc/self/fd`. The image is created,
written, and sealed `F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL` by this
process before it is read back, so no other party can substitute or change it.

## Audited modules

| Module | What it owns |
| --- | --- |
| `admission.py` | the artifact tree digest, the receipt publication, the publication lock, the evaluation directory |
| `candidate_lock.py` | the frozen candidate-lock transaction below the artifact root |
| `identity.py` | the evaluator's own executed source closure |
| `manifests.py` | the corpus capture: the Git freeze, the remainder scan, the hashed closure |
| `production_child.py` | the bounded child that executes production helpers |
| `production_helper.py` | starting that child and re-binding the bytes it reported |
| `production_identity.py` | the three declared production lock inputs |
| `runtime.py` | the service-owned candidate runtime |
| `source_binding.py` | the executed production helper closure |

`models.py`, `process.py`, and `write_guard.py` execute no filesystem access of their own:
they serialize, bound subprocesses, and compare manifests. `write_guard.py` delegates its one
content read to `manifests.bounded_file_digests`, which is `production-child`. `process.py`
owns the sealed-image primitive and the bounded runner; it resolves `memfd_create` from the
already-loaded process image rather than through `ctypes.util.find_library`, which would shell
out to `ldconfig` and put an unbounded child inside a phase whose contract is that every child
it starts is bounded and killable.

## Residual boundaries

These are stated rather than closed, because closing them would require editing production or
claiming a guarantee the kernel does not offer.

1. **The production helpers still contain a check-then-reopen race.** Running their exact
   bytes preserves it. What the bounded child changes is the consequence: a phase-bounded
   typed failure with the process group killed, instead of an unbounded hang. It is not a
   claim that the race cannot occur.
2. **`runtime_source_files` silently skips a non-regular file.** Production filters by
   `Path.is_file()`, so a FIFO planted below `src/serena_light` is excluded from the build
   identity rather than read. The evaluation does not paper over this: the resulting identity
   simply differs, and `assert_production_identity_unchanged` refuses the run. Pinned by
   `test_a_non_regular_runtime_source_changes_the_identity_rather_than_hanging`.
3. **The ceiling is cooperative.** A `link`, `unlink`, or `fsync` already in flight is not
   preemptible. The two invariants that hold are that no `pass` is returned after the ceiling
   and no final receipt remains once an overrun is observed.
4. **Confinement is claimed only below a root the harness opened.** `guarded` accesses close
   final-component substitution and blocking, not ancestor substitution, because the paths
   they read -- a caller's declared lock, an interpreter's realpath -- have no owning root.
5. **The three production lock inputs are read below the *resolved* repository root.** If
   `repo_root` itself is reached through a symlink, `Path.resolve()` collapses it first and
   the physical path is what is opened; components above the root are not re-proven. The same
   holds for the evaluator owner root the helper re-reads walk out from: it is derived from
   this module's own resolved `__file__`, and its ancestors are not re-proven.
6. **The child program's digest is pinned per process, on first use.** That binds every later
   call in a run to the bytes the first call executed, and the identity-equality test proves
   those are the bytes the receipt names. It does not bind across processes, and it is not a
   claim that the file on disk cannot change -- only that a changed file is refused rather
   than executed.
