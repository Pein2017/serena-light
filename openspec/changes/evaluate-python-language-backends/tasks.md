## 1. Freeze the evaluation contract and inputs

- [x] 1.1 Add typed manifest and admission-receipt schemas for evaluation identity, evaluation-contract version, source commit, backend lock, interpreter/configuration identity, workspace snapshots, phase/arm budgets, before/after write deltas, and artifact digests; cover canonical serialization and malformed input with unit tests. Later-phase receipt schemas are owned by the task that first freezes their data.
- [x] 1.2 Implement the candidate-lock builder that retains production Pyright `1.1.403`, resolves the highest non-yanked non-PEP-440-prerelease ty and Pyrefly versions once, accepts eligible `0.0.x` ty releases, verifies downloaded file hashes, and refuses ambient executables, editable installs, hash drift, or a second resolution after freeze.
- [x] 1.3 Implement service-owned candidate runtime preparation below `/data/CoordExp/.codex/runtime/serena-light/backend-eval/<candidate-lock-digest>/` from evaluation-only requirements inputs, with a minimal child environment, explicit executable and interpreter, isolated HOME/cache/config, removed proxy variables, and exact process ownership evidence.
- [x] 1.4 Add an invariant test and per-phase assertion that `pyproject.toml`, `uv.lock`, `package-lock.json`, `dependency_lock_digest`, `compute_build_identity`, and production `runtime_paths` remain byte-identical before and after candidate lock, installation, probes, and cleanup.
- [x] 1.5 Implement deterministic Git snapshot manifests for the Serena Light commit, `/data/ms-swift`, and `/data/CoordExp/.worktrees/research-probes`, including tracked/untracked/ignored disposition and fail-closed detection if a source changes while freezing.
- [x] 1.6 Implement bounded non-Git manifests for `ms` transformers and the exact `llm-framework-study` task paths without scanning the full environment.
- [x] 1.7 Implement before/after write detection that hashes the trust-inventory closure and declared fixture paths, metadata-scans the *complete* declared in-scope remainder of every Git root for path membership, type, symlink target, size, `mtime_ns`, and inode, and hashes only changed or created remainder paths; publish the declared exclusions; test declared disposable edits separately from unexpected backend mutation.
- [ ] 1.8 Run the admission command under its 30-minute ceiling, record exact versions, hashes, roots, interpreters, configuration, production identity invariants, and phase budget, and stop for lead disposition if any candidate or required corpus cannot be frozen reproducibly.
> **Task 1.8 is on HOLD and stays unchecked, after a second Sol-xhigh re-review.** That
> re-review overruled a passing Opus review with executable evidence and found three further
> defects, for which tasks 1.13 and 1.15 were reopened: a delayed post-link directory `fsync`
> let a run return `pass` at 1811 s with the final receipt present, cleanup received no
> deadline and could spend the budget, and a retained runtime still carried five
> harness-written files at `0660` from a pre-contract build. All three are repaired and
> covered by regressions, and the design and the spec state the cooperative-enforcement
> boundary explicitly rather than claiming a preemption the kernel does not offer.
>
> **A fourth defect was found while completing that repair, and is fixed here.** The first
> cut of the mode repair opened each harness-written file by its whole relative path under a
> single `O_NOFOLLOW`. That flag constrains only the last component, so a symlinked
> `config/ty` made the repair `fchmod` a file *outside* the runtime root; the escape was
> reproduced (a decoy at `0644` outside the root came back `0600`) before being closed. The
> repair and its verification now open every component from its parent's descriptor, and the
> reproduction is kept as a regression. The post-link publication checkpoints are likewise
> pinned structurally, so no future post-link syscall can be added without a ceiling
> observation after it.
>
> **A fifth defect was found by an Opus-max review after that, and task 1.13 is reopened for
> it.** Every guarded regular-file read opened with `O_RDONLY | O_NOFOLLOW` and no
> `O_NONBLOCK`. `open()` on a FIFO with no writer blocks indefinitely regardless of
> `O_NOFOLLOW`, so a FIFO or other blocking special node left where a regular file was
> expected -- for example a same-name race at `runtime-manifest.json`, or one planted where
> the artifact-tree traversal's own `lstat` gate does not reach -- hung the guarded open
> rather than failing closed, which the frozen ceiling cannot bound because the block is
> inside one uninterruptible syscall in the calling thread. It could not produce a false
> `PASS`, but it could make an otherwise-correct run exceed the whole-phase ceiling with no
> receipt, which is what this review found and what task 1.13 promises does not happen.
> Reproduced at `runtime-manifest.json` and fixed by adding `O_NONBLOCK` to every
> evaluator-owned guarded read in `runtime.py`, `admission.py`, `identity.py`, and
> `source_binding.py` (`candidate_lock.py` already carried it); `O_NONBLOCK` has no effect on
> a regular file's read behaviour, so ordinary reads, `O_NOFOLLOW`, descriptor-relative
> confinement, and the `fstat` regular-file refusal are all unchanged. Every audited directory
> open (`O_DIRECTORY`, which refuses a non-directory node before any type-specific open
> handler runs), `O_CREAT | O_EXCL` create, and `O_RDWR` lock open was confirmed already safe
> by construction and left unchanged. Five adversarial regressions -- one per guarded read
> family, each first proven to hang under a bounded `pytest-timeout` override without the fix
> -- cover the FIFO case at the runtime manifest, the owned-runtime mode-repair walk, the
> admission artifact-tree read, the evaluator source closure, and the bound production helper
> closure, each asserting a typed error well under one second and no leaked descriptor. A
> fresh evidence-only admission run from the repaired, committed evaluator at HEAD `2503f85`
> -- evaluation identity `35b85d4e…d334`, run `ddfe7d49…b8562`, `status=pass`, 12 s of the
> 1800 s ceiling, `runtime_permission_repairs=none` -- supersedes the run below as *the*
> admitting run in `phase-1-acceptance.md`; it is unaffected by the guarded-read fix, which
> changes nothing about ordinary-file behavior, and all five earlier runs remain preserved
> byte-for-byte.
>
> **A sixth family was found by a Sol-xhigh HOLD review after that, and tasks 1.13 and 1.15
> are reopened for it.** The `O_NONBLOCK` repair covered every guarded read the evaluator
> owns, but the audit that produced it was flag-shaped: it searched `os.open` constants, so it
> could not see the accesses that carry no flags. Three families survived. (1) `runtime.py`
> wrote every harness-owned file -- the installed lock snapshot, the three service
> configurations, the manifest temporary -- with a path-based `O_WRONLY | O_CREAT | O_TRUNC |
> O_NOFOLLOW` and created their directories with `Path.mkdir(parents=True)`. `O_NOFOLLOW`
> guards only the last component, so a symlinked `config/ty` carried the write outside the
> root; a FIFO at `config/ty/ty.toml` blocked the open until a reader appeared and then
> received the harness payload; and `O_TRUNC` destroyed an existing target before anything
> proved what it was. Its service-configuration verification was a check followed by
> `Path.read_bytes()`, which a post-check symlink to a file with the expected bytes passes.
> (2) `production_identity.py` read the three declared lock inputs the same check-then-read
> way, and the production helpers it calls -- `dependency_lock_digest`,
> `compute_build_identity`, `runtime_paths` -- do the same inside `src/serena_light`, which the
> evaluation may not edit. (3) `inventory.observe_file_digest` opens `O_RDONLY | O_NOFOLLOW`
> with no `O_NONBLOCK`, so a node substituted after its type was inspected blocks the corpus
> capture.
>
> All three are closed without touching `src/serena_light`. Every harness-owned write and
> verification read below the runtime root now walks out from the already-open root descriptor
> one component at a time, creating and reopening each intermediate directory from its
> parent's descriptor, opening leaves `O_NONBLOCK`, proving them regular by `fstat` on that
> same descriptor, and truncating only after that proof; a FIFO with a live reader is refused
> with not one byte delivered. The three lock inputs are read through one guarded descriptor
> each. The four production helpers run as their exact unmodified bytes in a bounded,
> source-bound, minimal-environment child whose process group the phase deadline kills, with
> canonical digest-bound request and response and every executed helper byte re-read and
> compared by the parent. And the whole surface is now enumerated structurally rather than by
> grep: `tests/backend_eval/test_io_ownership.py` parses every evaluator module, collects every
> filesystem access including `Path.read_bytes`, `Path.write_text`, `Path.mkdir`, and each
> production helper call, and fails until every one appears in a finite table with exactly one
> owner; `docs/backend-eval-io-ownership.md` explains the owner classes and states the residual
> boundaries. The honest limit is stated in both: running production's bytes in a killable
> child *bounds* the helpers' own check-then-reopen race, it does not remove it, and
> `runtime_source_files` still silently skips a non-regular source file, which changes the
> build identity and is refused by the identity guard rather than hidden.
>
> **Two source-binding seams inside that repair were closed before it was committed, after a
> lead pre-review.** (a) The parent's re-read of the helper bytes a child reported opened the
> whole relative path under one `O_NOFOLLOW`, which guards only its last component, so a
> symlinked `src` or `src/serena_light` could have supplied another tree's bytes for the
> parent to "confirm". It now walks every component from an open descriptor on the evaluator
> owner root. (b) The child *program* was handed to the interpreter as a mutable pathname,
> leaving a window between the read that digested those bytes and the `execve` that ran them,
> closed only after the fact by `source_clean`. The program is now read through that same
> confined walk, pinned by digest on first use and re-checked on every later call, and executed
> from a `memfd` sealed `F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL` addressed as
> `/proc/self/fd/<image>` with only that descriptor inherited; a test proves the digest equals
> the one the evaluator identity records for `production_child.py`, and a substitution test
> proves a mid-run swap is refused rather than executed. Closing (b) also removed an unbounded
> child the bounded-runner accounting test then caught: `ctypes.util.find_library("c")` shells
> out to `ldconfig`, so `memfd_create` is resolved from the already-loaded process image
> instead, and the sealed-image primitive now has one owner in `process.py`.
>
> **A seventh defect was found by this repair's own receipt verification, and is fixed here.**
> Moving the dependency-lock digest, the build identity, and the runtime paths into the bounded
> child stopped this process from importing `serena_light.bootstrap` and
> `serena_light.build_identity`, so `sys.modules` no longer saw them and the receipt's bound
> production closure silently narrowed from six files to four -- the receipt stopped naming the
> bytes of helpers whose answers it still published, which the spec's "a phase executes a
> production helper" scenario forbids. Fixed: `source_binding.CHILD_EXECUTED_HELPERS` declares
> the modules the child loads, they are digested from this checkout alongside the in-process
> ones, and a test requires the child's *own reported* closure -- for every operation it
> supports -- to equal that declaration, so a helper that starts importing something new fails a
> test rather than quietly leaving the receipt. The bound closure and its digest
> (`d7ed2395…966b`) are byte-identical to the previous admitting run's again.
>
> **An eighth family was found by two independent exact-target final reviews of the run
> recorded at `49d557f`, and tasks 1.11 and 1.15 were reopened for it.** Both reviewers
> returned HOLD, and both defect families are repaired here; that run is now recorded as
> rejected and superseded evidence in `phase-1-acceptance.md`, never as an admitted PASS.
>
> (a) **The captured identity was not what first use enforced.** `capture_evaluator_identity()`
> recorded `production_child.py` and the executed production-helper closure, and then the first
> `run_production_helper()` called `_PINNED_CHILD_DIGESTS.setdefault(...)`, accepting whatever
> bytes were on disk at *that* moment. A changed child program was reproduced executing
> successfully after identity capture, and `production_child_digest()` -- the only thing tying
> the identity to the executed bytes -- was test-only. The sealed `memfd` proved the child
> *program* bytes; the six `serena_light` helpers were still imported from the mutable `src`
> root and only re-read afterwards by the child and again by the parent, so a transient
> substitution could execute and be restored before either post-hoc read. Closed: a
> `HelperExpectation` derived from the captured `EvaluatorIdentity` is passed explicitly into
> every production-helper call the admission makes -- both corpus captures and their
> enrichment, all three production-identity captures, and the candidate-lock and runtime
> identity brackets -- and compared before any child starts; the verified child program and the
> verified helper bytes are executed and imported from sealed `memfd` images addressed by
> descriptor, with no `src` root on the child's `sys.path`, so the bytes compared are the bytes
> Python compiles; per-operation closure membership is exact and enforced at runtime in the
> child and independently in the parent; the evaluator identity is re-measured after cleanup
> and before publication, so a late evaluator or helper mutation holds the run instead of
> passing it; and `_PINNED_CHILD_DIGESTS` is removed, so two admissions in one process cannot
> contaminate each other's truth.
>
> (b) **Two filesystem-ownership claims were false.** `ProductionAdmissionServices.cleanup()`
> was reproduced following a symlinked ancestor and unlinking a decoy outside the evaluation
> root: it opened `evaluation_root / receipts` as one absolute pathname under `O_NOFOLLOW`,
> which protects only the leaf, and `artifact_tree_digest()` had the same weakness while
> feeding admitted evidence. Closed: both acquire their root by walking every component from
> the declared owner root's own descriptor. The audit is corrected with them: a root open with
> no provable parent is `guarded`, not `confined`; a new `declared-path` class names the
> pathname-shaped observations (`Path.is_file()`, `Path.lstat()`, `os.access`,
> `os.path.realpath`) that are weaker than `guarded` and are only ever used to refuse; the
> structural collector now covers namespace mutation, descriptor byte and durability
> operations, metadata and link operations, descriptor duplication and release, and executable
> discovery, with a finite conservative vocabulary; the `descriptor` class is proven
> mechanically rather than asserted; and the ambient `shutil.which("git")` fallback is replaced
> by one declared `GIT_EXECUTABLE`, proven a regular executable file through a single guarded
> descriptor before any child starts, with its ownership declared. No receipt field changed:
> the receipt contract binds the CLI host interpreter and the candidate executables, not Git.
>
> Prose corrected with the code: design Decision 8d now states that the child program runs from
> a sealed `memfd` `/proc/self/fd/...` rather than an absolute mutable script path and how the
> production helper bytes are executed from the identity-bound source image; the digest-chunk
> claim is replaced by the actual, stronger whole-pass `lstat` bracket; and the closure prose
> says that each operation's reported closure is an exact allowed subset while the union across
> supported operations equals the declared closure, with an unexpected extra module rejected at
> runtime.
>
> **Task 1.8 background from the first re-review.** The final Sol-xhigh review of the earlier
> admitting run (evaluator HEAD `7d40d41`, evaluation identity `380aaeb4…9147d`) found three
> defects that made its PASS untrustworthy as a *gate*, and tasks 1.11, 1.13, and 1.14 were
> reopened and repaired for them: publication and lock acquisition sat outside the 1800 s
> ceiling, the receipt bound only `scripts/backend_eval` while executing `serena_light`
> helpers from an older parent checkout, and strict PASS accepted any positive admission
> budget rather than the frozen seconds. That receipt and both earlier ones are retained
> unchanged as evidence; none of them is the admitting run any more. The admitting run
> recorded in `phase-1-acceptance.md` is the fresh one from the repaired, committed evaluator
> at HEAD `517a451`: evaluation identity `0960ec13…7025`, run `991c9866…cff33`, `status=pass`,
> 0 unexpected paths / 0 declared mutations / 0 changed controls across 68,059 in-scope corpus
> paths in five roots, 7 s of the 1800 s ceiling with publication inside it, equal
> post-cleanup production identity, no leaked process, the five pre-contract runtime files
> repaired from `0660` to `0600` with byte, size, inode, and manifest digest unchanged, and
> all 62 earlier artifact files unchanged. Task 1.8 may be checked only after two independent
> re-reviews approve *that* receipt: a checked box may never stand for an unreviewed run.
>
> Task 1.7 is checked: its write-detection instrument is unchanged by this repair and the
> final review raised no finding against it.

- [x] 1.9 Move the first corpus capture before candidate-lock compilation and runtime preparation and the second after preparation and before cleanup and publication, so the delta brackets every Phase 1 setup operation; treat a created, deleted, or changed inventory member as a write delta rather than an unstable root, and keep an individual freeze that moves while being captured fail-closed.
- [x] 1.10 Implement the spec's two-stage remainder algorithm: compare metadata first, hash only changed or created regular remainder files through guarded no-follow reads, rebuild the after manifest and digest with those hashes before constructing the delta, and treat a race during enrichment as incomplete rather than clean.
- [x] 1.11 Bind every receipt to a typed evaluator identity (executed source closure digest, source commit and cleanliness, CLI host interpreter path/realpath/SHA-256/version), the allowlisted bootstrap environment recorded as key names and value digests only, and the candidate runtime's logical root and canonical `runtime-manifest.json` SHA-256 recomputed before PASS; include the evaluator identity and artifact root in the reproducible evaluation identity. Bind the executed *production* helper closure the same way -- origin root, per-file byte digest, recomputed closure digest, cleanliness at the recorded commit -- resolve `serena_light` from the evaluator's own checkout before any helper import, fail closed by realpath on a shadowed or out-of-owner helper, and prove with an adversarial test that changed or repointed helper bytes change the identity or refuse the run without any change to `scripts/backend_eval`. Make that identity the *execution expectation* rather than a record of it: derive it from the captured `EvaluatorIdentity`, carry it structurally through every production-helper call the admission makes, compare the child program and the exact operation-appropriate helper closure through a confined component-wise read before any child starts, execute and import those verified bytes from sealed in-memory images so the compared bytes are the imported bytes, enforce exact closure membership at runtime in both the child and the parent, re-measure the evaluator identity after the last evaluation-owned action and before publication so a late mutation can never yield PASS, and keep no process-global first-use pin so two admissions in one process cannot contaminate each other.
- [x] 1.12 Give each execution an immutable `run_identity` and publish `receipts/<run-identity>.json` exclusively under a per-identity no-follow lock so a repeated or concurrent run can neither delete nor replace another run's receipt; serialize candidate-lock transactions the same way so a live transaction is never mistaken for an interrupted one.
- [x] 1.13 Make the 1800-second admission ceiling cover resolution, runtime preparation, both captures, cleanup, the final production identity, the artifact digest, and publication; propagate the remaining time to every subprocess and kill its process group on expiry; reserve enough finalization time to publish a trustworthy timeout receipt and fail closed without a receipt when that evidence cannot be completed. Acquire every resolution, runtime, and publication lock non-blockingly against the same monotonic deadline with no background thread, and make publication itself deadline-aware end to end -- chunked deadline-checked writes, a publication reserve before the atomic link, and withdrawal of this run's own link if the ceiling arrives during it -- so no PASS is published or returned after the frozen ceiling and no failure leaves a receipt at the final path. Re-observe the ceiling after *every* post-link mutation and durability barrier, including immediately before returning while withdrawal is still possible, and pass the same deadline into cleanup so a cleanup that spends the budget fails the run closed instead of yielding a later PASS. Bound the production helpers the evaluation executes but may not edit -- the dependency-lock digest, the build identity, the runtime paths, and the trust-inventory file digest -- by running their exact unmodified bytes in a source-bound, minimal-environment child that receives the phase's remaining time and has its whole process group killed on expiry, and propagate that deadline into every production-identity capture and every corpus-capture child. State the cooperative-enforcement boundary, and the fact that this bounds rather than removes the helpers' own check-then-reopen race, honestly rather than claiming preemption.
- [x] 1.14 Make typed PASS structurally strict (exact budgets, ordered timestamps, exact environment and service-config names, one delta per root bound to both manifest digests, no unexpected path, no declared mutation, no changed manifest control, every identity present), recompute `RootManifest.manifest_digest` at construction and parsing, carry an explicit raw-lock digest witness on `CandidateLock`, and cover each case with adversarial parsing tests. Require every phase budget to equal its frozen `DEFAULT_PHASE_BUDGETS` seconds, not merely the frozen names with a positive admission budget, and cover the mutation of each budget's seconds in construction and in parsing.
- [x] 1.15 Close the remaining evidence-integrity defects: a descriptor-relative `O_NOFOLLOW` artifact-tree traversal with no reopen after validation, redaction by path component rather than string prefix, and service-owned files and directories at `0600`/`0700` independent of the ambient umask. Scope that mode contract explicitly to harness-written files, locks, configurations, and receipts plus every service-owned ancestor directory; leave third-party resolver and environment internals at their tool-defined modes behind `0700` ancestors and excluded from the artifact digest, with the boundary pinned by test; and repair a runtime published before the contract under its own per-digest lock without changing bytes or the manifest digest, opening every path component from its parent's descriptor so no repair can follow a link out of the root. Extend that component-wise descriptor discipline from the mode repair to *every* harness-owned write and verification read below the runtime root -- creating each intermediate directory from its parent's descriptor rather than through `mkdir(parents=True)`, opening leaves non-blocking, proving them regular through the same descriptor, and never truncating an existing file before its type and ownership are proven -- and enumerate the evaluator's complete filesystem-access surface structurally in a finite ownership table with one owner per access, so an undeclared read or write fails a test rather than surviving a grep. Extend that enumeration to every evaluator filesystem operation -- namespace mutation, descriptor byte and durability operations, metadata and link operations, descriptor duplication and release, and executable discovery -- keep the vocabulary finite and conservative, and label honestly: a root open with no provable parent descriptor is `guarded`, a pathname-shaped observation used only to refuse is `declared-path`, and the `descriptor` class is proven mechanically. Route evaluation cleanup and artifact-tree-digest root acquisition through component-wise descriptor walks from the declared owner so neither can follow a swapped ancestor, pin both exploits with executable tests, and replace the ambient `shutil.which("git")` fallback with one declared executable validated and owned explicitly.
- [x] 1.16 Remove the last unbounded subprocess from the corpus capture: derive the Git trust inventory from the already bounded combined `git ls-files --cached --others --exclude-standard -z` bytes and reuse production's subprocess-free candidate normalization and inspection, without modifying `src/serena_light`; prove field-by-field equivalence with `git_trust_inventory`, prove capture succeeds with that helper forbidden, and account for every Git child of a capture through the bounded runner.

## 2. Build and gate raw protocol probes

- [ ] 2.1 Add the evaluation-only backend protocol interface and shared runner under `scripts/backend_eval/`, reusing production LSP transport and process-launcher behavior without importing evaluation modules from `src/serena_light`.
- [ ] 2.2 Implement the Pyright baseline probe from current production facts and prove initialize providers, configuration requests, position encoding, and shutdown against the frozen manifest.
- [ ] 2.3 Implement the ty probe with locked executable, explicit service-owned configuration and interpreter, and structured initialize/provider evidence.
- [ ] 2.4 Implement the Pyrefly probe with locked executable, explicit external `configPath`/initialization options, workspace diagnostics configuration, and no automatic configuration creation, migration, workspace edit, or hidden retry loop.
- [ ] 2.5 Add typed capability-receipt schemas and receipts that separate initialize advertisement, accepted request, valid normalized result, and real-task utility, including an explicit negative implementation record when the locked ty version does not advertise `textDocument/implementation`.
- [ ] 2.6 Add real subprocess tests for cold readiness, push/pull diagnostics mode, `ContentModified`, `RequestCancelled`, the identically disabled production retry seam, and bounded timeout; use an explicit pytest timeout above the repository's 30-second default for declared real-corpus cases.
- [ ] 2.7 Add real subprocess tests for crash, graceful shutdown, parent/process-tree cleanup, proxy poisoning, minimal environment, and secret/environment redaction.
- [ ] 2.8 Run each protocol probe under bounded write detection and prove zero workspace mutation for Pyright, ty, and Pyrefly.
- [ ] 2.9 Run the complete protocol phase under its 90-minute ceiling, publish typed per-candidate gate outcomes for PASS/FAIL and `seam_incompatible_pull_only` dispositions with artifact-tree digests, and remove failed candidates from later phases.

## 3. Compare the current Serena Light product surface

- [ ] 3.1 Implement an evaluation-only fixed-corpus attributor and adapter-factory injection that feeds identical trust inventory to each survivor while keeping native/configured-program feasibility as a separate promotion gate.
- [ ] 3.2 Implement an evaluation-only diagnostics identity/assembly seam that accepts candidate engine evidence without changing `DiagnosticEngineFacts`, production diagnostics code, stable specs, or installed runtime; record this wrapper deviation in every candidate receipt.
- [ ] 3.3 Record candidate identity out-of-band, retain the fixed in-response Python adapter literal for blinding, and count Pyright assignment-recovery fired/unresolved cases so wrapper repair cannot silently become candidate credit or blame.
- [ ] 3.4 Add a common navigation runner for overview, exact/global symbol lookup, declaration, references, implementation support/fallback, compact budgets, Unicode positions, and typed errors through the real `WorkspaceRuntime` seams.
- [ ] 3.5 Add a common diagnostics/guarded-edit runner for disposable snapshots, including changed diagnostics, no false clean, successful expected-hash replacement, stale-hash rejection without write, and post-edit backend reconciliation.
- [ ] 3.6 Freeze typed decision-owning fixture schemas and fixtures for Serena Light and `/data/ms-swift` covering unknown owner discovery, cross-file definitions/references, external imports, and base/derived classes.
- [ ] 3.7 Freeze separately typed and reported fixtures for `research-probes`, `ms` transformers, and `llm-framework-study` covering native config, decorators, registries, pytest fixtures, and external type/import resolution.
- [ ] 3.8 Add real same-root two-session reuse, per-holder release, and same-root reactivation tests for every survivor.
- [ ] 3.9 Add real concurrent multi-root isolation, cold/warm call, immediate release, and orphan-cleanup tests for every survivor, using explicit long-test timeout overrides.
- [ ] 3.10 Add controlled writer-process tests proving freshness preflight/postflight, at-most-one complete replay, typed second-race failure, and no cross-generation success for every survivor.
- [ ] 3.11 Verify controlled service-owned configuration on roots without native config; report `research-probes` separately with production-native Pyright configuration and an equivalent service-owned candidate translation, plus observation-only native-discovery divergence.
- [ ] 3.12 Write each survivor's production attribution feasibility note and mark it non-promotable if trustworthy configured-program ownership requires workspace mutation, ambient discovery, or an unverifiable evaluation projection.
- [ ] 3.13 Run the product-seam phase under its 3-hour ceiling, publish typed gate outcomes, compare frozen decision-owning evidence rather than raw result count, disposition wrapper incompatibilities separately, and stop each materially regressing candidate before feature evaluation.

## 4. Probe future closed semantic operations

- [ ] 4.1 Add evaluation-only normalized result models for implementation, type definition, hover, prepared call hierarchy, incoming/outgoing calls, prepared type hierarchy, supertypes, and subtypes using validated snapshot ranges and bounded compact text.
- [ ] 4.2 Implement candidate dispatch for implementation, type-definition, and hover, returning explicit unsupported/failed evidence rather than references, text search, or AST synthesis.
- [ ] 4.3 Implement internal prepare-plus-incoming/outgoing call-hierarchy dispatch without exposing opaque LSP items to the caller.
- [ ] 4.4 Implement internal prepare-plus-supertype/subtype hierarchy dispatch without exposing opaque LSP items to the caller.
- [ ] 4.5 Freeze typed decision-owning fixture schemas and run the concrete subclass/method implementation and class-hierarchy fixtures, verifying exact decision-owning locations, symbol identity, workspace scope, generations, truncation, and unsupported behavior.
- [ ] 4.6 Freeze typed call-impact and external inferred-type fixtures with the same evidence checks, then run them.
- [ ] 4.7 Produce per-operation receipts that distinguish advertisement, protocol success, normalized correctness, frozen task utility, cancellation, calls, response characters, and latency.
- [ ] 4.8 Stop the feature phase at its 2-hour ceiling and emit a typed Agent-phase gate outcome stating either the utility claim requiring Agent demonstration, the at-least-two candidates needing separation, or `not_required`.

## 5. Run the conditional backend-blinded Codex Agent comparison

- [ ] 5.1 If and only if the Phase 4 entry receipt requires it, freeze typed task-corpus and model-route schemas for four to six concrete prompts, deterministic workspace/path/symbol/range-set verifiers, shell/MCP routing instructions, model and effort, semantic-call and response-character budgets, 25-minute per-arm ceiling, arm rotation, one infrastructure retry, and scoring rubric before the first arm.
- [ ] 5.2 Implement a direct evaluation-only stdio MCP with one identical name and schema across arms, the existing public tools plus only Phase-4-justified normalized operations, explicit read-only transport classification for every closed operation, and orchestrator-owned backend selection absent from Agent-visible instructions and results.
- [ ] 5.3 Implement isolated temporary Codex configuration and instrumentation that records semantic calls, serialized MCP characters, shell fallback, cold/warm status, time to first frozen decision-owning evidence, time to final answer, final response, candidate/runtime identity, and cleanup without modifying production connector code or normal client registrations.
- [ ] 5.4 Run a Pyright dry-run arm to prove prompt solvability, verifier correctness, semantic-MCP assignment, shell allowance, backend blinding, budget enforcement, complete receipts, and zero leaked processes or registrations; revise and refreeze the task corpus before comparative arms if invalid.
- [ ] 5.5 Run every valid paired arm for Pyright and all surviving competitors in the frozen rotation, stop the Agent phase at 8 hours total, and permit exactly one fresh rerun only for a documented infrastructure-invalid arm.
- [ ] 5.6 Verify each Agent answer against frozen facts and review unsupported dynamic-Python claims, evidence boundaries, and shell/MCP routing; exclude unusable pairs rather than converting infrastructure failure into backend error.

## 6. Decide, review, and close without migration

- [ ] 6.1 Enforce the 16-hour total active evaluation ceiling, including at most one hour for accepted repairs and reruns, and generate a machine-readable decision receipt plus concise acceptance report choosing exactly one of `promote_pyrefly`, `promote_ty`, `retain_pyright`, or `inconclusive_retain_pyright`.
- [ ] 6.2 Apply the lexicographic rule only to candidates that reached each phase, retaining earlier eliminations as gate exclusions and leaving call/context savings at the efficiency rank rather than future-utility rank.
- [ ] 6.3 Obtain an independent Sol-xhigh static/correctness review and Opus-max runtime/evidence review against the exact evaluation identity, then disposition every blocker without deciding by majority vote.
- [ ] 6.4 Re-run affected probes after any accepted repair, freeze the final artifact-tree digest, validate the change strictly, and present the recommendation, residual risks, and permitted next action to the user for an explicit decision.
- [ ] 6.5 Remove all temporary MCP registrations and evaluation-owned processes; if Pyright is retained, remove candidate runtimes, and if a winner is approved for integration, retain only its immutable lock/evidence until the separate integration change owns it.
- [ ] 6.6 Update the roadmap and acceptance evidence without changing the production backend or public schema, sync/archive this change only after all tasks and reviews are complete, and create no integration or feature change without separate user authorization.
