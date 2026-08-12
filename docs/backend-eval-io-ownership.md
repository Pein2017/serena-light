# Backend-evaluation I/O ownership

Every filesystem access and Phase 2 lifecycle side effect the evaluation package executes
has exactly one owner, and the set is finite and enumerated.
`tests/backend_eval/test_io_ownership.py` is the authority:
it parses every module under `scripts/backend_eval/`, collects every call that
opens, creates, enumerates, reads, or writes a filesystem object, delegates a stable source
read, observes or signals a candidate process, or temporarily changes the evaluator
environment, and fails until each one
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

Phase 2 adds four non-filesystem classes to the same closed census: delegation to the stable
source reader; candidate PID/create-time/process-group observation; exact process-group
signals; and the poison probe's synchronously restored environment mutation. These rows keep
the lifecycle evidence producer from hiding process or ambient-state effects behind a module
that happens not to open files itself.

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

The Phase 2 source reader uses the same class without writing: it lexically rejects traversal,
opens every workspace and target component from its parent descriptor, opens the leaf
`O_RDONLY | O_NOFOLLOW | O_NONBLOCK`, bounds every chunk by the phase deadline and byte ceiling,
and re-proves the leaf entry, open descriptor, and retained workspace-root identity after the
read. The prepared-runtime loader likewise walks and retains the exact runtime root while it
parses and verifies the canonical manifest and every runtime-owned byte. For each external
tool and interpreter it starts one `guarded` filesystem-root open, then resolves the configured
absolute path component by component in this class, retaining and re-proving every directory
and symlink entry. The final node is opened `O_NONBLOCK`, proven executable and regular, and
its path and digest must equal the manifest-declared endpoint. Both sides of the whole-pass
bracket perform that complete bounded resolution independently; no ambient `PATH` participates.

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

### `candidate-child`

The Phase 2 protocol runner delegates one declared candidate process through
`SubprocessAdapterRuntimeProvider.start()`, which in turn uses production's
`LanguageServerSubprocessLauncher`. The structural collector records that call as
`delegated.candidate_process`; any candidate launch from another function therefore requires
its own explicit ownership row.

This owner class names the process boundary rather than claiming filesystem confinement for
the candidate. The candidate receives the declared workspace as its working directory and an
exact minimal environment binding its service-owned HOME, cache, config, interpreter, PATH,
and TMPDIR. It may still read the evaluated workspace and may attempt writes; the later
Phase 2 write-guard task owns the before/after mutation proof. The shared runner owns bounded
shutdown, process-tree reap, redacted stderr, terminal errors, cleanup errors, and post-stop
exit status. No row here promotes a plumbing probe into evidence of zero workspace mutation.

### `source-read-delegation`

The call delegates source bytes to `manifests.read_stable_source_text`; it does not reopen the
path itself. The delegated reader owns the `guarded` workspace-root open, the component-wise
`confined` walk, the nonblocking descriptor read, the byte ceiling, and the retained-root and
entry re-proofs. The caller owns selecting the declared workspace and relative target and
consuming the decoded text. This class appears on every capability probe and on the lifecycle
cold-readiness scenario, so a new direct or delegated source read cannot be hidden.

### `owned-process-observation`

The evaluator observes candidate identity and cleanup through PID, process create time, and
process group. The lifecycle runner brackets each shared-runner launch with a direct-child
census, accepts exactly one newly created direct child, requires that child to own its process
group, and records `(pid, create_time, pgid)`. Missing and extra children fail closed. Cleanup
checks both that exact PID/create-time identity and every live member of its recorded process
group; inability to inspect either is a failed observation, never evidence that the group was
reaped. Pre-existing `process.py` and `protocol.py` `getpgid` calls are in the same structural
census rather than grandfathered outside it.

### `owned-process-group-signal`

An `os.killpg` that can affect a candidate process tree. The lifecycle crash scenario first
reopens the captured PID through psutil, re-proves its exact create time, re-reads its PGID,
requires both the stored and current group to equal the captured PID, and only then signals
that exact group. A reused PID or changed PGID fails without a signal. The older bounded-runner
and partial-launch cleanup signals are enumerated in this class as well; their existing
process owners and reap paths remain authoritative for those calls.

### `temporary-process-environment`

The lifecycle poison proof temporarily replaces only its finite declared set of proxy and
ambient-runtime keys, starts one candidate through the normal minimal-environment seam, and
restores every prior value in `finally`. A module lock serializes this context across lifecycle
batteries in the evaluator process. This is not a claim that arbitrary unrelated threads can
never read process-global environment during the window; Task 8 must run candidate batteries
sequentially, as the lifecycle runner itself does. It is a bounded proof that poison does not
reach the candidate, not a general environment sandbox.

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
its `ZipFile` reads operate on an in-memory `BytesIO`, not a filesystem pathname. The same
owner covers the protocol image's frozen production Python entries and pure-Python dependency
entries, plus `fstat`/`pread` of the two already-sealed native dependency descriptors. These
reads bind the delayed-import universe before a candidate starts; they do not reopen disk
source or a site-packages pathname.

## Exact parent admission and disposable protocol witness

`protocol_parent.py` has no discovery operation. It derives one receipt pathname from the
caller's exact evaluation and run identities, opens `/` once as the `guarded` filesystem
anchor, then opens every ancestor and the 0600 regular leaf relative to retained descriptors.
Its remaining `open` calls are therefore `confined`; `fstat`, `fdopen`, `read`, and `close`
operate only on descriptors. A missing exact receipt cannot fall through to another run.

`protocol_witness.py` owns a single disposable fixture below an already-created, caller-owned
per-run directory. `_open_owned_run_root` is the only `guarded` absolute open and proves the
root is the expected 0700 directory. Creation, verification, and cleanup then use only
`dir_fd`-relative `mkdir`, `chmod`, `open`, `stat`, `unlink`, and `rmdir` calls (`confined`) or
descriptor-only I/O and durability calls. `_checked_open` closes only the descriptor it just
opened if the shared phase deadline expires after the syscall. The two `Path.resolve`
locations merely reject a definition outside the frozen transformers root; they authorize no
subsequent write and remain `declared-path` observations.

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
| `admission.py` | artifact tree digest and cleanup; disk `__main__` only refuses. Receipt publication is delegated to `publish.py` under admission's own names |
| `backend_eval_bootstrap.py` | the declared standard-library transport trust root: startup validation, source-image construction/sealing, bounded image child, relay, timeout, and reap; no admission semantics |
| `candidate_lock.py` | the frozen candidate-lock transaction below the artifact root |
| `identity.py` | the evaluator's own executed source closure |
| `manifests.py` | the corpus capture plus the bounded descriptor-safe Phase 2 workspace-source reader |
| `production_child.py` | the bounded child that executes production helpers |
| `process.py` | the bounded runner, the sealed-image primitive, the declared-executable binding |
| `production_helper.py` | verifying the expected bytes and starting that child |
| `production_identity.py` | the three declared production lock inputs |
| `protocol.py` | no direct filesystem access; one structurally collected `candidate-child` delegation through the frozen production process/transport primitives |
| `protocol_lifecycle.py` | lifecycle source-read delegation, exact candidate PID/create-time/PGID observation, fail-closed process-group signal and cleanup census, and the synchronously restored ambient poison proof |
| `protocol_parent.py` | exact Phase 1 parent receipt binding: one guarded filesystem-root anchor, no-follow descriptor walk, strict regular-file read, and no discovery fallback |
| `protocol_witness.py` | one deadline-bounded disposable witness below the caller-owned run root: confined creation, verification, durability, cleanup, and read-only external-definition observations |
| `publish.py` | the generic atomic immutable publication: the per-target publication lock, the publication directory and every artifact component below the declared owner root, the `O_EXCL` temporary, the payload write, the atomic link, and the withdrawal |
| `runtime.py` | service-owned candidate runtime preparation and the separate read-only prepared-runtime manifest loader/verifier |
| `pyrefly_probe.py` | no direct filesystem access; delegates workspace manifests and source bytes to `manifests.py`, consumes the caller-bound prepared runtime, and delegates candidate lifecycle to `protocol.py` |
| `pyright_probe.py` | no direct filesystem access; delegates source bytes to `manifests.py`, prepared-runtime verification to `runtime.py`, and candidate lifecycle to `protocol.py` |
| `ty_probe.py` | two `declared-path` workspace-root observations (`Path.resolve()` and `Path.is_dir()`) used only to refuse an invalid caller path; delegates source bytes to `manifests.py`, prepared-runtime verification to `runtime.py`, and candidate lifecycle to `protocol.py` |
| `source_binding.py` | the executed production helper closure and the execution expectation |
| `source_image.py` | sealed evaluator and native-dependency descriptors, image-derived evaluator/production/dependency source closures, owner root, and loader-origin checks |
| `write_guard.py` | one `lstat` bracket around a changed remainder record |

`models.py`, `protocol.py`, `pyrefly_probe.py`, and `pyright_probe.py` execute no direct filesystem
byte access of their own: the first serializes and validates, the second declares its candidate
process/transport delegation and emergency observation/signal surface, and the two candidate
probes declare their stable-source-read delegations to the audited descriptor-confined seam.
`process.py`
resolves `memfd_create` from the already-loaded process image rather than through
`ctypes.util.find_library`, which would shell out to `ldconfig` and put an unbounded child
inside a phase whose contract is that every child it starts is bounded and killable.
`write_guard.py` delegates its one content read to `manifests.bounded_file_digests`, which is
`production-child`, and owns only the `declared-path` `lstat` that decides whether a remainder
record needs one.

`publish.py` is the one filesystem owner that belongs to no single phase. It holds every access
the immutable publication performs -- the `guarded` open of the declared owner root, the
`confined` walk that creates and reopens every artifact component below it, the per-target
`O_NOFOLLOW` publication lock, the `O_EXCL` temporary, the chunked payload write, the atomic
`link`, the directory `fsync` barriers, the withdrawal `unlink`, and every descriptor release --
and it takes the names,
the payload, and the phase deadline from its caller. It refuses a target the declared owner root
does not lexically contain as a typed failure before it opens anything, and every path that
abandons a publication -- the refused link, the exhausted reserve, an interrupted write -- removes
its own temporary and proves the removal durable, because the owning phase's cleanup runs *before*
publication and would never collect it. Its closes are split by what they can still decide:
`_close_payload` is a pre-link step whose refusal discards the temporary and is reported in the
payload's own words, while `_release_descriptor` covers every close whose outcome is already
settled. The publication lock names only its own open, `fchmod`, and `flock`; the body it guards
is outside that handler, so a failure the publication itself raises is never republished as a
locking failure. It carries no phase vocabulary and no
receipt semantics: failures leave it as one of two typed codes, and the calling phase's thin
adapter states what they mean. `admission.py` is that adapter for Phase 1, which is why the
receipts directory, the `.admission-publication.lock` name, and the receipt file names appear
there rather than here.

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
   and no final receipt remains once an overrun *or a post-link failure* is observed. Both are
   enforced in `publish.py` rather than in any one phase, so a second phase inherits them
   instead of restating them.
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
10. **External resolution stability is observational, not preemptive.** Each side of the
    read-only loader's whole-pass bracket independently resolves and re-proves the complete
    bounded tool/interpreter chain, so a persistent intermediate-link retarget across the
    semantic pass is refused. A party that changes and restores every affected entry wholly
    between those observations may remain unobservable; the loader does not claim to prevent
    filesystem mutation or to preempt a syscall already in flight.
11. **A withdrawal that cannot be proven is reported, not assumed.** Every post-link step --
    both durability barriers and the temporary unlink -- runs inside one recovery that removes
    this call's own two names before the failure propagates, so a failed barrier can no longer
    leave a readable canonical entry behind while reporting failure. The original typed failure
    is what the caller gets back whenever the removal is proven. When a name cannot be unlinked,
    or the withdrawal's own `fsync` fails, that is reported instead, because nothing about the
    directory's contents can be claimed at that point. The kernel still offers no way to make
    the removal atomic with the failure that triggered it; what is closed is the silent case.
12. **One close error is deliberately not reported.** Linux frees a descriptor whether or not
    `close` returns an error: the error is a deferred writeback report the kernel already
    recorded, so retrying is unsafe -- the number may already be reused -- and no state is left
    to repair. `_release_descriptor` therefore swallows it, and every call site is one where the
    outcome is already settled: the publication directory and the owner root are read-only
    descriptors whose durability barrier was an explicit `fsync` that already returned, the
    publication lock carries no payload, the walk releases a parent only once its child is open,
    and the payload descriptor arrives there only on a path already failing and already
    discarding the temporary. Propagating it would hand the caller a failure while the durable
    record it denies is on disk, or would mask the failure already being reported. The close
    that *can* still be acted on -- the payload's, before the link -- is not in this class: it
    discards the temporary and fails typed. What is not claimed is that a deferred writeback
    error is invisible; it is claimed that it cannot deny a record the barriers already proved.
