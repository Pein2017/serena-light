# Backend-evaluation I/O ownership

Every read and write the evaluation package executes has exactly one owner, and the set of
them is finite and enumerated. `tests/backend_eval/test_io_ownership.py` is the authority:
it parses every module under `scripts/backend_eval/`, collects every call that
opens, creates, enumerates, reads, or writes a filesystem object, and fails until each one
appears in its `OWNERSHIP` table with an owner class. A new access anywhere in the evaluator
fails that test until it is declared; a removed one fails it until the row goes. This
document explains what each owner class guarantees, and states the boundaries that remain.

The enumeration deliberately covers more than `os.open` flags. `Path.read_bytes`,
`Path.write_text`, `Path.mkdir`, and a call into a production helper are each an unguarded
open with no flag to grep for, and each one is how a defect survived an earlier review. The
vocabulary now also covers namespace mutation (`link`, `unlink`, `rename`, `replace`,
`rmdir`, `symlink`), descriptor byte movement and durability (`read`, `write`, `pread`,
`pwrite`, `lseek`, `ftruncate`, `fsync`, `fdatasync`, and the stream `read`/`write`/`flush`
performed through an `os.fdopen` handle), metadata and link inspection (`stat`, `lstat`,
`fstat`, `readlink`, `access`, `chmod`, `fchmod`, `realpath`, and the `pathlib` predicates),
descriptor duplication and release (`dup`, `close`), executable discovery (`shutil.which`,
which the evaluator no longer performs at all), and -- on the parent side of the child
boundary -- every delegation to the bounded child (`delegated.production_child`), so a
production helper is enumerated both where it runs and where it is asked for. Descriptor primitives are enumerated as rows
*and* proven mechanically: a dedicated test walks every call to one and requires its first
argument to be a descriptor-shaped expression, never a constructed pathname, so the
`descriptor` class cannot be used to hide a pathname-shaped access.

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

One open of an absolute pathname with the strongest guard a single open can carry, where no
parent descriptor exists to walk out from. Two shapes:

* **A regular file outside every owned root** -- the CLI host interpreter, the declared Git
  executable, the caller's declared candidate lock, the evaluator's own source closure -- is
  opened `O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, proven regular by `fstat` on that same
  descriptor, and read from it with no reopen.
* **A declared root directory** that a `confined` walk then starts from -- the production
  repository root, the evaluator owner root, a corpus root, the evaluation artifact owner --
  is opened `O_RDONLY | O_DIRECTORY`, which refuses a non-directory before any type-specific
  open handler runs.

What `guarded` does *not* claim is the point of naming it separately: it closes substitution
of the final component and blocking on a special node, and it says nothing about the
components above. A root open is where confinement starts, not something confinement covers.
Every row that opens a declared root is therefore `guarded` here, even though everything the
walk reaches below it is `confined`.

### `declared-path`

A pathname-shaped *observation* of a caller-declared path, used only to refuse:
`Path.is_file()`, `Path.is_dir()`, `Path.is_symlink()`, `Path.lstat()`, `Path.resolve()`,
`os.access`, `os.path.realpath`. This class is weaker than `guarded` and is named separately
so that it cannot be mistaken for one: it resolves the name again, so a node substituted after
the observation is not caught by it.

Every row in this class only ever *rejects*. None of them authorizes a byte to move: the read
or write that follows an accepted observation is owned by a `confined`, `guarded`, or
`descriptor` row that resolves nothing by name. A request validator that rejects a
non-directory `repo_root`, and the corpus scanner's `lstat` bracket around a hashed path, are
the two shapes that appear.

### `descriptor`

The call operates on a descriptor this process already holds -- `os.fdopen`, `os.scandir(fd)`,
`os.fsync(fd)`, `os.unlink(name, dir_fd=fd)` relative to a proven parent. There is no pathname
left to redirect. Every call in this class is additionally proven structurally to receive a
descriptor-shaped argument rather than a constructed pathname.

### `production-child`

Exact production semantics for the Phase 1 admission gate, executed in
`scripts/backend_eval/production_child.py` under the phase's own monotonic deadline.
**Every production helper the admission gate executes is in this class. The sealed Phase 1
admission/import graph imports no `serena_light` module at all**, which regressions prove in a
fresh interpreter and structurally. Phase 2's protocol plane deliberately imports a frozen,
exact allowlist of production LSP/process primitives in `protocol.py`, plus the corresponding
receipt type in `models.py`; a fail-closed AST regression rejects every other production
import and any silent widening or duplicate of that allowlist.

That completeness is the point, not a tidiness preference. An import compiles whatever bytes
are on disk *at import time*, and the evaluator identity is captured afterwards, so a
production module imported into the evaluator could have been substituted between the two: the
receipt would name one closure while the parent's own evidence was computed by another. Six
helpers are delegated. `dependency_lock_digest`, `compute_build_identity`, `runtime_paths`, and
`observe_file_digest` were always here. `bounded_non_git_trust_inventory` and the pair
`_decode_git_path` / `_inventory_from_candidates` used to run in the corpus capture and moved
here for exactly the reason above; only the evidence a `RootManifest` is built from -- the
resolved root, the kind, the accepted paths, production's digest, and the rejections with
production's own reasons -- crosses back, as canonical JSON validated on arrival. The parent
recomputes production's own `sha256("\0".join(paths))` over the returned path list, so a digest
that does not name the paths beside it is refused; it never recomputes *which* paths are
accepted or why the rest are rejected, because that is production's answer.

A path carrying surrogate-escaped invalid bytes cannot be serialized as canonical JSON, so the
child refuses and the parent reports a typed incomplete capture. That is the pre-existing
fail-closed disposition -- such a path could never have reached a `RootManifest` either -- and
it is left exactly as it was rather than turned into an encoding redesign.

The four originally delegated helpers check a path's type and then reopen it by name -- the
first three through `Path.is_file()` and `Path.read_bytes()`, the fourth through
`O_RDONLY | O_NOFOLLOW` with no `O_NONBLOCK` -- so a node substituted in that window blocks
them. Reimplementing them in the evaluator would drift from the semantics the receipt claims to
bind, so the evaluation runs their exact bytes and bounds the blast radius instead.

The child guarantees:

* **Bounded.** `Deadline.remaining()` is the child's timeout; `run_bounded_bytes` starts it in
  its own session and `SIGKILL`s the whole process group on expiry. A blocked helper costs
  the phase its remaining budget and a typed failure, never an unbounded hang.
* **Bound to an expectation, before it starts.** Every call carries a `HelperExpectation`
  built from the `EvaluatorIdentity` the run captured before any child existed: the expected
  child-program digest and the expected per-file helper closure, both taken from digests the
  receipt itself publishes. There is no process-global first-use pin, so two admissions in one
  process cannot contaminate each other, and a helper substituted between the identity capture
  and the first use cannot execute -- it is compared before anything runs, not recorded after.
* **Source bound, program included.** The child *program* is not named by a mutable pathname.
  `production_child.py` is read through a `confined` walk from an open descriptor on the owner
  root, compared against the expectation, and executed from a `memfd` sealed
  `F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL` addressed as
  `/proc/self/fd/<image>`. The bytes that were compared are the bytes that run.
* **Source bound, helpers included -- by construction, not by report.** The parent reads each
  expected helper file through the same confined walk, refuses any byte that is not the
  expected byte, packs those verified bytes into a second sealed image, and passes it by
  descriptor. `O_NOFOLLOW` on the whole relative path would guard only the last component, so
  a symlinked `src` or `src/serena_light` could hand back another tree's bytes; every
  component is opened from its parent's descriptor instead. The child installs a meta-path
  finder over that image and puts no `src` root on `sys.path` at all, so the bytes compared
  are the bytes Python compiles and executes and an on-disk swap during the import window is
  unreachable rather than merely detected afterwards. Each module keeps the `__file__` an
  ordinary import would have given it, so production semantics that derive a repository root
  from `__file__` are unchanged; origin is proven by *loader identity*, not by that pathname.
* **Exact closure, at runtime.** `OPERATION_HELPER_CLOSURES` declares the exact modules each
  operation may load. Each operation's reported closure is an exact allowed subset of the
  declared union, and the union across the supported operations equals that declaration. The
  child refuses an unexpected extra module, a missing expected module, and any module that
  arrived through a loader other than the image's; the parent independently refuses a reported
  closure that is not exactly the expected one.
* **No ambient shadowing.** `-I -S -B` refuses `PYTHONPATH`, skips `site`, `.pth`,
  `sitecustomize`, site-packages, and the user site directory; the transport environment
  contains only declared proxy, CA, and locale values, and the child strips its own directory from `sys.path`
  before importing anything but the standard library -- so the ambient `scripts` namespace
  package that shadows this repository under a bare `ms` interpreter cannot reach it.
* **Canonically bound I/O.** Request and response are canonical JSON, the response must
  re-serialize to the exact bytes received, and the child echoes the SHA-256 of the request
  bytes it consumed. A response that does not name this request is refused.

Digest batches are chunked (`DIGEST_CHUNK_SIZE`), which keeps each child's argument list and
its own blast radius small. The stability proof is *not* per chunk: the capture `lstat`s every
path before the first chunk starts and again after the last one finishes, so a hashed path
whose identity moved at any point during the whole pass is recorded as unstable rather than as
a clean hash. That is a wider window and a stricter requirement than a per-chunk bracket --
a path that held still for its own chunk but moved during another one is refused here and
would have been accepted there.

### `own-image`

Reads or writes this process's own sealed `memfd`, by descriptor or through `/proc/self/fd`.
The image is created, written, and sealed `F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW |
F_SEAL_SEAL` by this process before it is read back, so no other party can substitute or
change it. The child's read of the production source image belongs here too: the descriptor it
`pread`s was sealed by the parent before the child was started.

The direct bootstrap's evaluator zip is owned here as well. The standard-library transport
reads the complete evaluator package through a descriptor-confined walk, writes and seals one
`memfd`, and starts the semantic evaluator with that descriptor as its zip import root.
`source_image.py` verifies the seal set and hashes the archive from that inherited descriptor;
its `ZipFile` reads operate on an in-memory `BytesIO`, not a filesystem pathname.

## Executables the evaluation runs

`git` is declared, not discovered. `shutil.which("git", ...) or shutil.which("git") or
"/usr/bin/git"` answered from whatever `PATH` the ambient process happened to carry, which is
exactly the ambient control every other input of this evaluation refuses, and it disagreed
with the hard-coded `/usr/bin/git` the evaluator-identity probe already used. Both now use one
`process.GIT_EXECUTABLE`, and `process.bound_executable` proves it is a regular file through
one `O_NOFOLLOW | O_NONBLOCK` descriptor and executable by `os.access` before any child starts;
a missing, redirected, or non-regular Git is a typed failure, never a fallback to a different
program. That binding is `guarded`, not `confined`: `/usr/bin` is outside every root this
evaluation owns. The receipt contract names the CLI host interpreter and the candidate
executables, not this one, so no receipt field changed.

Every Git child receives no `HOME` and disables system config. Its `GIT_CONFIG_GLOBAL` names
a sealed inherited descriptor containing exactly one protected `safe.directory` value for
the declared checkout or corpus root; argv repeats that exact root with `-c
safe.directory=...`. No parent or wildcard is trusted, and no user credentials, identity,
includes, or global excludes are read from ambient system or user config. Repository-local
ignore semantics, including `.gitignore` and `.git/info/exclude`, remain authoritative.

## Audited modules

| Module | What it owns |
| --- | --- |
| `__init__.py` | no filesystem access; both parent-package initializers are inert |
| `admission.py` | artifact tree digest, receipt publication, publication lock, and evaluation directory; disk `__main__` only refuses |
| `backend_eval_bootstrap.py` | the declared standard-library transport trust root: startup validation, source-image construction/sealing, bounded image child, relay, timeout, and reap; no admission semantics |
| `candidate_lock.py` | the frozen candidate-lock transaction below the artifact root |
| `identity.py` | the evaluator's own executed source closure |
| `manifests.py` | the corpus capture: the Git freeze, the remainder scan, the metadata walk, and the delegation of both inventory helpers |
| `production_child.py` | the bounded child that executes production helpers |
| `process.py` | the bounded runner, the sealed-image primitive, the declared-executable binding |
| `production_helper.py` | verifying the expected bytes and starting that child |
| `production_identity.py` | the three declared production lock inputs |
| `protocol.py` | no direct filesystem access; the Phase 2 protocol runner delegates process and transport ownership to its frozen production primitives |
| `runtime.py` | the service-owned candidate runtime |
| `source_binding.py` | the executed production helper closure and the execution expectation |
| `source_image.py` | the sealed evaluator-image descriptor, image-derived source closure, owner root, and loader-origin checks |
| `write_guard.py` | one `lstat` bracket around a changed remainder record |

`models.py` and `protocol.py` execute no direct filesystem access of their own: the former
serializes and validates, while the latter reads only the in-memory process environment and
delegates candidate process/transport ownership to the frozen production primitives.
`process.py`
resolves `memfd_create` from the already-loaded process image rather than through
`ctypes.util.find_library`, which would shell out to `ldconfig` and put an unbounded child
inside a phase whose contract is that every child it starts is bounded and killable.
`write_guard.py` delegates its one content read to `manifests.bounded_file_digests`, which is
`production-child`, and owns only the `declared-path` `lstat` that decides whether a remainder
record needs one.

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
   they read -- a caller's declared lock, an interpreter's realpath, `/usr/bin/git`, a declared
   corpus or artifact root -- have no owning root above them. Cleanup and the artifact-tree
   digest previously *claimed* confinement while opening a whole multi-component absolute path
   under one `O_NOFOLLOW`; both now walk out from the declared owner root's descriptor, and
   only that first root open is `guarded`.
5. **The three production lock inputs are read below the *resolved* repository root.** If
   `repo_root` itself is reached through a symlink, `Path.resolve()` collapses it first and
   the physical path is what is opened; components above the root are not re-proven. The same
   root boundary applies to the production-helper walk. The evaluator-image bootstrap is
   stronger for its own checkout: it starts from one guarded open of `/` and opens every owner
   component below that descriptor with `O_NOFOLLOW`.
6. **The metadata traversal is evaluator-owned, and its root open is only guarded.** It used
   to call production's `open_guarded_directory`; that call is gone, because it would put
   production code back in the evaluator process. The replacement does the same thing --
   declared corpus root opened `O_RDONLY | O_DIRECTORY`, every component below it opened from
   its parent's descriptor with `O_NOFOLLOW` -- and is stated for what it is: a walk, not a
   semantic the receipt binds, with a `guarded` root and `confined` descendants. Nothing above
   the declared root is proven, and the traversal remains bounded by the same stop callback and
   phase deadline as the rest of the capture.
7. **`declared-path` observations can be raced.** They resolve a name a second time, so a node
   substituted after the observation is not caught by it. They are only ever used to refuse,
   never to authorize a byte to move, and the access that follows resolves nothing by name --
   but the observation itself is not a guard and is not described as one.
8. **The expectation binds bytes, not the whole filesystem.** A helper or child program that
   differs from the captured evaluator identity is refused before it can execute, and the
   verified bytes are the imported bytes. That is not a claim that the file on disk cannot
   change: it is a claim that a changed file cannot run, in this run or in any other run in
   the same process, and that a late change is caught by the pre-publication identity
   re-capture rather than published as a `pass`.
9. **The original command process is a transport root of trust.** The closed direct bootstrap
   verifies `-I -S -B`, the effective no-bytecode setting, and the standard-library-only path,
   then itself creates, seals, starts, relays, times out, and reaps the evaluator image. Disk
   admission categorically refuses `__main__`; there is no in-process authentication claim.
   The shim runs no package initializer; the image's inert initializers are bound as sealed entries. Python then compiles enough
   code to create the first immutable source image. That process imports no evaluator semantic
   module: it only confines and reads the closure, creates and seals the image, starts the
   isolated child, relays bytes and exit status, enforces an outer bound, and kills/reaps the
   child group on failure. The receipt exact-byte claim begins at the sealed evaluator image,
   not at self-authentication of the shim's own disk bytes. Receipt construction, helper execution, corpus capture, cleanup,
   and publication all run in the image child whose bytes the identity names.
