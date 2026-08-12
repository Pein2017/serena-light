## Purpose

Defines a reproducible, production-shaped evaluation that selects one fixed Python semantic backend for Serena Light without changing the installed MCP before the evidence supports a separate migration decision.

## ADDED Requirements

### Requirement: Evaluation inputs are immutable and reproducible
The evaluation SHALL bind every result to an immutable manifest composed across phase receipts. Each phase receipt SHALL bind the fields produced by that phase, and the composed manifest SHALL contain the Serena Light source commit, candidate backend versions and executable hashes, selected Conda interpreter paths, service-owned configuration digests, workspace snapshot identities, task corpus version, model route, tool budget, and evaluation-contract version.

#### Scenario: Candidate versions are frozen
- **WHEN** an evaluation run begins
- **THEN** Pyright remains fixed to the production version and bundle hashes, while ty and Pyrefly resolve to exact non-yanked versions whose package metadata is not a PEP 440 pre-release, including eligible `0.0.x` ty releases, before all executable hashes are recorded

#### Scenario: A source root is dirty
- **WHEN** a required Git root contains tracked or untracked changes
- **THEN** the evaluation reads from a frozen snapshot that records the complete input manifest rather than treating the mutable checkout HEAD as the evaluated corpus

#### Scenario: A required input cannot be frozen
- **WHEN** a required root, interpreter, dependency, or snapshot cannot be resolved exactly
- **THEN** the affected phase stops with an incomplete receipt and produces no backend recommendation

### Requirement: Evaluation remains isolated from the production MCP
The evaluation SHALL keep candidate selection, candidate dependencies, temporary registrations, raw receipts, and candidate-specific adapters outside the installed Serena Light runtime and public schema.

#### Scenario: Protocol and product-seam probes run
- **WHEN** ty or Pyrefly is exercised by the evaluation
- **THEN** the installed MCP continues to advertise the same public tools and use the current pinned Pyright backend

#### Scenario: Evaluation dependencies are prepared
- **WHEN** ty or Pyrefly packages are locked or installed for a probe
- **THEN** `pyproject.toml`, `uv.lock`, `package-lock.json`, the production dependency-lock digest, the production build identity, and production runtime paths remain byte-identical to their pre-evaluation values

#### Scenario: An Agent arm is launched
- **WHEN** a temporary Agent comparison exposes an evaluation MCP
- **THEN** every arm receives the same MCP name, tool schemas, initialize guidance, shell allowance, model route, effort, task prompt, and budgets while backend identity remains absent from Agent-visible context

#### Scenario: Service-owned state is written
- **WHEN** the evaluation writes a file, lock, configuration, or receipt it owns, or creates a directory it owns
- **THEN** every component of that path, including each intermediate directory it creates, is opened or created from its parent's descriptor without following a link, the leaf is opened non-blocking and proven a regular file through that same descriptor before any byte moves, no existing file is truncated before its type and ownership are proven, and the resulting regular file is `0600` and directory `0700` regardless of the ambient umask, for every harness-written file and every service-owned ancestor directory

#### Scenario: A harness-owned write target is substituted before the write
- **WHEN** a path the harness is about to write is a symlink, a FIFO, another special node, or lies below a symlinked intermediate component, or the runtime root is renamed and replaced during preparation
- **THEN** the write is refused with a typed error, no byte is written or truncated anywhere outside the open root, no payload reaches a substituted reader, and any write that does proceed lands in the inode the harness opened rather than in whatever the pathname now names

#### Scenario: Published harness-owned state is verified
- **WHEN** the evaluation re-reads a file it owns to verify a published runtime, snapshot, configuration, or manifest digest
- **THEN** it reads through the same component-wise descriptor walk its writes use, so a substituted file holding exactly the expected bytes is refused rather than accepted as the file that was verified

#### Scenario: A third-party tool writes inside a service-owned tree
- **WHEN** a resolver, installer, or candidate creates its own cache or environment files inside a service-owned directory
- **THEN** those files keep their tool-defined modes rather than being recursively rewritten, remain confined behind `0700` service-owned ancestors, and are excluded from the receipt's artifact-tree digest, so they are outside the evidence the receipt binds

#### Scenario: A retained runtime was built before the permission contract
- **WHEN** a published runtime is reused and one of its harness-written files still carries a wider mode from an earlier build
- **THEN** the mode is repaired to `0600` under the same per-digest runtime lock through a descriptor whose every component was opened from its parent without following a link and which is proven regular through that same descriptor, without changing any byte, without altering the published manifest digest, and the whole contract is re-verified before the runtime may be returned

#### Scenario: Evaluation cleanup completes
- **WHEN** a phase or the full evaluation ends
- **THEN** temporary registrations, processes, leases, configuration, and service-owned candidate state are retired without changing canonical Serena or the installed Serena Light registration

### Requirement: Workspace mutation is a non-compensable failure
Candidate language servers SHALL use service-owned HOME, configuration, cache, executable, and minimal environment state, and SHALL NOT create, remove, retarget, or modify any evaluated workspace path outside an explicitly declared disposable mutation fixture.

#### Scenario: A candidate runs on a read-only input
- **WHEN** a protocol, common-surface, feature, cold-start, or warm-query probe completes
- **THEN** a before-and-after lexical manifest fully hashes the trust-inventory closure and declared fixture paths, metadata-scans the complete declared in-scope remainder of every Git root -- files, symlink targets, directories including empty ones, and any other node -- for path membership, file type, symlink target, size, `mtime_ns`, and inode, and hashes any remainder path whose metadata changed or that did not exist before

#### Scenario: The evaluator's own read and write surface is audited
- **WHEN** evaluation code performs any filesystem access -- by descriptor, by pathname, or by delegating to a production helper
- **THEN** that access appears in a finite declared ownership table with exactly one owner, the table is derived structurally from the evaluator source rather than by inspection so an undeclared or removed access fails, and the residual boundaries the table cannot close are stated explicitly rather than described as closed

#### Scenario: A scan boundary is required to keep the sweep bounded
- **WHEN** a Git corpus root contains a service- or repository-owned tree that is not part of the evaluated corpus
- **THEN** only `.git`, the evaluation `.admission-artifacts`, a lane-owned `.venv`, and `node_modules` are pruned, every pruned path is published in the manifest and counted in the acceptance record, and `research-probes/model_cache` remains in scope

#### Scenario: A setup operation could touch a corpus root
- **WHEN** a phase compiles a candidate lock, prepares a candidate runtime, or performs any other setup work
- **THEN** the first capture precedes that work and the second follows it and precedes cleanup and receipt publication, so the delta brackets every operation the phase performed

#### Scenario: The remainder changes between captures
- **WHEN** a remainder path is created, deleted, or changes its metadata, or a trust-inventory member is created or deleted, between the two captures
- **THEN** the comparison reports it as an unexpected path and holds, rather than reporting an unstable root, and the phase publishes the changed manifest controls alongside the delta

#### Scenario: A remainder file changes while its content is being read
- **WHEN** the second stage hashes a changed or created remainder file and the file cannot be read stably, or its metadata moves again during the read
- **THEN** the observation is incomplete and is never reported as clean

#### Scenario: One freeze moves while it is being captured
- **WHEN** a Git revision, trust inventory, or tracked/untracked disposition changes during a single capture
- **THEN** that capture fails closed rather than returning a manifest describing two different filesystem states

#### Scenario: Pyrefly lacks workspace configuration
- **WHEN** Pyrefly evaluates any workspace through the controlled arm
- **THEN** the harness supplies service-owned configuration outside the workspace and fails the candidate if it attempts to create or migrate workspace configuration

#### Scenario: A controlled edit is required
- **WHEN** diagnostics, freshness, or stale-hash behavior requires a source mutation
- **THEN** the mutation occurs only in a disposable snapshot, the intended mutation is declared separately from backend side effects, and the snapshot is destroyed after evidence capture

### Requirement: Every receipt binds its evaluator, host, environment, and runtime
Every phase receipt SHALL be bound to the exact evaluator source closure that produced it, the exact production helpers that closure executed, the CLI host interpreter that executed it, the environment its bootstrap downloads received, and the service-owned candidate runtime it evaluated, and each execution SHALL publish immutable evidence that no later execution can replace.

#### Scenario: A receipt is published
- **WHEN** any phase publishes a receipt
- **THEN** it records the digest of the executed evaluation source closure, the source Git commit and whether that source was clean, the CLI host interpreter's configured path, realpath, SHA-256, and version, and the candidate runtime's logical root and canonical runtime-manifest SHA-256 recomputed from disk before the gate can pass

#### Scenario: A phase executes a production helper
- **WHEN** evaluation code imports and runs a non-stdlib production helper for manifests, write detection, or production identity
- **THEN** the receipt records that helper's origin root, per-file byte digest, recomputed closure digest, and cleanliness at the recorded commit, and both the evaluator and the production closure are part of the reproducible evaluation identity

#### Scenario: An executed helper resolves outside the evaluator's own checkout
- **WHEN** a loaded production helper resolves, by realpath, to another checkout, to installed site-packages, or to any path outside the evaluator checkout's own source tree
- **THEN** the phase fails closed without publishing a receipt, rather than executing code whose bytes its identity does not name

#### Scenario: Evaluation code or the CLI host changes
- **WHEN** the evaluation source closure, the CLI host interpreter, or the artifact root differs from an earlier run
- **THEN** the reproducible evaluation identity differs, so new evidence is published beside the earlier evidence rather than over it

#### Scenario: A run is repeated or two runs overlap
- **WHEN** the same evaluation identity is executed again, concurrently or later
- **THEN** each execution carries its own immutable run identity, publishes to its own receipt path under a per-identity lock, and can neither delete nor replace another execution's receipt

#### Scenario: A resolver or installer needs the external network
- **WHEN** a bootstrap download runs
- **THEN** it inherits only the allowlisted external-network proxy, CA, and locale values plus service-owned state, refuses ambient package-index, source, `PATH`, and `PYTHONPATH` controls, and the receipt records only key names and SHA-256 digests of values

#### Scenario: Git observes an evaluator checkout or declared corpus root
- **WHEN** the evaluator runs bounded Git for its own identity or a Git-backed corpus manifest
- **THEN** the child uses the declared absolute Git executable, has no `HOME`, disables system and ambient global/user config, receives one sealed protected config and one `-c` argument that trust only the exact declared Git root as `safe.directory`, and retains repository-local ignore semantics without accepting a parent or wildcard or reading a credential helper, user identity, include, or global excludes file from ambient system or user config

### Requirement: Hard gates precede feature and efficiency comparison
Every candidate SHALL pass correctness, workspace freshness, selected-environment import resolution, zero-write, current-surface compatibility, bounded-response, and lifecycle gates before candidate-specific features or efficiency can influence selection.

#### Scenario: A wrapper coupling is incompatible with a candidate
- **WHEN** a Pyright-specific evaluation wrapper assertion, assignment recovery, adapter literal, or push-diagnostics witness prevents or contaminates a candidate result independently of backend protocol behavior
- **THEN** the receipt records a `wrapper_incompatibility` or `seam_incompatible_pull_only` exclusion separately from backend correctness and never scores the wrapper-caused outcome as a candidate defect

#### Scenario: A candidate returns stale or wrong-root evidence
- **WHEN** a controlled concurrent change, same-root reactivation, multi-root switch, or per-session binding probe returns evidence from the wrong snapshot, generation, or workspace
- **THEN** the candidate fails and no latency, memory, or feature advantage can restore it

#### Scenario: A candidate resolves the wrong Python environment
- **WHEN** `ms`, `llm-framework-study`, or a selected external package is queried
- **THEN** definitions, references, diagnostics, and reported interpreter evidence resolve through the manifest-selected Conda environment or the candidate fails

#### Scenario: A current tool regresses materially
- **WHEN** overview, exact or global symbol lookup, declaration, references, diagnostics, implementation fallback, or guarded symbol replacement is exercised on the common corpus
- **THEN** missing frozen decision-owning evidence, an incorrect body or range, false clean diagnostic result, backend-caused untyped failure, stale-hash write, or unbounded response fails the candidate

### Requirement: Decision-owning evidence is frozen before comparison
Every deterministic fixture and Agent task SHALL declare the smallest evidence set that is sufficient to answer it, expressed as expected workspace, relative path, symbol identity, and accepted range or range set, before any candidate result is observed.

#### Scenario: A fixture is admitted
- **WHEN** a common-surface, feature, or Agent fixture is frozen
- **THEN** its receipt schema contains the complete expected evidence set, the required subset that owns the decision, and the verifier version

#### Scenario: A candidate returns additional results
- **WHEN** a backend returns more locations, symbols, hierarchy items, or diagnostics than the frozen decision-owning subset
- **THEN** the additional result count does not compensate for missing or incorrect decision-owning evidence

### Requirement: Product-shaped lifecycle and concurrency are exercised
The common-surface phase SHALL reuse Serena Light's real LSP transport, document lifecycle, position mapping, freshness admission, timeout translation, compact envelopes, workspace identities, and cleanup behavior rather than comparing backend CLIs alone.

#### Scenario: Cold backend is not ready
- **WHEN** a semantic call arrives before initial analysis is usable
- **THEN** the probe records bounded readiness and typed retry behavior and never converts cold, warming, cancelled, or failed state into an empty success

#### Scenario: Sessions share one root
- **WHEN** two evaluation sessions bind the same root and environment
- **THEN** the evidence distinguishes safe shared backend reuse from caller-specific leases and proves that one release does not invalidate the other holder

#### Scenario: Sessions use different roots
- **WHEN** concurrent sessions bind different Git or non-Git roots
- **THEN** documents, diagnostics, generations, failures, and cleanup remain isolated per physical workspace key

#### Scenario: A file changes during a read
- **WHEN** another process mutates a queried file during freshness preflight, backend execution, or postflight
- **THEN** the complete read replays at most once and a second race returns the existing typed retryable failure rather than stale success

#### Scenario: A stale guarded edit is attempted
- **WHEN** a disposable edit uses a pre-change expected hash
- **THEN** the edit is rejected without a write and backend notification or diagnostic timing cannot bypass the hash guard

### Requirement: Future capabilities are tested as closed Agent operations
Only candidates that pass every hard gate SHALL be tested for closed, normalized implementation, type-definition, hover, incoming-call, outgoing-call, supertype, and subtype operations, and claimed support SHALL require useful results on real repository tasks.

#### Scenario: Initialize advertises a provider
- **WHEN** a candidate advertises a feature in its initialize result
- **THEN** the evaluation records advertised support separately from successful normalized task evidence

#### Scenario: A provider is absent or unstable
- **WHEN** a capability is missing, returns invalid ranges, escapes the trusted scope, repeatedly cancels, or fails its real task
- **THEN** that operation is recorded as unsupported or failed and is never synthesized from references, text search, or a hand-built AST hierarchy

#### Scenario: A feature improves an Agent task
- **WHEN** a closed operation supplies frozen decision-owning evidence that the current surface cannot supply
- **THEN** the receipt records the exact evidence, task outcome, call delta, response-character delta, and recovery behavior as demonstrated utility while call/context savings remain a later efficiency comparison

### Requirement: Agent comparison is backend-blinded and evidence bounded
When Phase 4 leaves a task-level utility claim requiring Agent demonstration or leaves at least two promotable candidates not yet separated, the Agent phase SHALL use four to six fixed decision tasks with the same Codex model and effort, and SHALL record answer correctness before efficiency while allowing ordinary shell only for lexical discovery, precise reads, diffs, and tests. Otherwise the Agent phase SHALL be recorded as not required.

#### Scenario: An Agent explores an unfamiliar task
- **WHEN** an arm runs an owner, implementation, call-impact, external-type, concurrent-change, or dynamic-Python task
- **THEN** it is instructed to prefer the assigned semantic MCP for symbols, references, hierarchy, type information, and diagnostics while using shell for the documented lexical fallback

#### Scenario: An Agent encounters dynamic Python
- **WHEN** decorators, registries, string dispatch, or pytest fixtures exceed backend evidence
- **THEN** a correct bounded conclusion scores above an unsupported claim of complete semantic coverage

#### Scenario: An Agent arm exceeds its budget or infrastructure fails
- **WHEN** the model, temporary MCP, candidate process, or environment does not complete within the declared call, character, and time budgets
- **THEN** the receipt distinguishes candidate behavior from infrastructure failure and does not silently score the arm as an incorrect answer

#### Scenario: The Agent phase cannot change the decision
- **WHEN** no surviving closed operation has a utility claim requiring Agent demonstration or earlier phases already select or eliminate every candidate
- **THEN** the Agent phase is skipped with a decision-relevance receipt rather than spending model budget on a predetermined outcome

### Requirement: Evaluation work is time bounded
The frozen evaluation contract SHALL cap active wall time at 30 minutes for manifest/admission, 90 minutes for protocol probes, 3 hours for product-seam probes, 2 hours for feature probes, 8 hours for the optional Agent phase, 25 minutes per Agent arm, and 16 hours for the complete evaluation before independent review so one hour remains for accepted repairs and reruns.

#### Scenario: A phase exceeds its ceiling
- **WHEN** a phase or Agent arm reaches its frozen active wall-time ceiling without a valid terminal receipt
- **THEN** the phase stops, evaluation-owned processes and leases are released, the timeout is dispositioned as backend, wrapper, or infrastructure behavior, and no further expensive phase starts until the lead determines the decision remains reachable

#### Scenario: A phase ceiling is measured
- **WHEN** a phase measures its own wall time
- **THEN** the ceiling covers every step the phase performs, including resolution, preparation, each snapshot capture, cleanup, final identity checks, artifact digests, lock acquisition, and receipt publication, and is not limited to the phase's expensive middle

#### Scenario: A child process does not return
- **WHEN** an evaluation-started subprocess exceeds the phase's remaining time
- **THEN** it receives that remaining time as its own bound, its whole process group is terminated on expiry, and it cannot outlive the phase or block cleanup

#### Scenario: A production helper would start its own child
- **WHEN** a phase needs evidence a production helper computes by spawning its own unbounded process
- **THEN** the phase supplies that helper's input from a bounded invocation of the identical command and reuses only the helper's subprocess-free logic, so no phase step is exempt from the ceiling, and an equivalence test pins the reused result to the production helper's output

#### Scenario: A phase reaches its ceiling with usable evidence
- **WHEN** collection reaches the ceiling but the phase already holds the identities and frozen inputs a timeout receipt requires
- **THEN** a reserved finalization window publishes that timeout receipt, and a phase that cannot complete even that evidence fails closed without publishing a receipt

#### Scenario: A lock the phase needs is held by another process
- **WHEN** a phase waits for the candidate-resolution, candidate-runtime, or receipt-publication lock
- **THEN** acquisition is non-blocking and retried against the same monotonic ceiling, and a lock still held at the ceiling raises the phase's typed timeout instead of waiting outside the budget

#### Scenario: Receipt publication itself reaches the ceiling
- **WHEN** serializing, writing, linking, or synchronizing a phase receipt would finish at or after the frozen ceiling
- **THEN** the phase publishes no receipt at the final path and fails closed, withdrawing its own link if the ceiling arrived during it, so no `pass` is published or returned after the ceiling

#### Scenario: A step after the receipt link overruns the ceiling
- **WHEN** any namespace mutation or durability barrier that follows the atomic link -- including the last one before the phase returns -- completes at or after the frozen ceiling
- **THEN** the expiry is observed at the next boundary, the phase withdraws its own link and its own temporary, and it returns no receipt, so a `pass` can never be earned by work done after the ceiling

#### Scenario: The ceiling arrives while a filesystem call is in flight
- **WHEN** a `link`, `unlink`, or `fsync` the phase has already entered is still running when the ceiling passes
- **THEN** the deadline is enforced cooperatively at the next boundary rather than preempting the call, so the linked receipt name may exist transiently while that one call completes; it is withdrawn as soon as the expiry is observed and it is never admitted evidence, because a consumer requires successful command completion and canonical digest verification, neither of which an overrun run supplies

#### Scenario: A guarded read finds a FIFO or other blocking special node
- **WHEN** a guarded read expecting a regular file -- the corpus remainder, the runtime manifest, the owned-runtime mode repair, the evaluator source closure, the bound production helper closure, the declared production lock inputs, or the admission artifact-tree digest -- opens a path that is now a FIFO or other node whose open blocks without a peer
- **THEN** the open returns immediately rather than blocking the calling thread, and the typed domain error the regular-file check already raises is returned promptly, so the FIFO can neither stall the phase past its ceiling nor be read as empty bytes

#### Scenario: A production helper the evaluation may not edit blocks on a substituted node
- **WHEN** a phase needs a value only a production helper can compute, that helper checks a path's type and then reopens it by name, and the node is substituted in that window
- **THEN** the helper runs as its exact unmodified production bytes inside a child that receives the phase's remaining time, runs in its own session, and has its whole process group killed on expiry, so the phase fails typed inside its ceiling instead of hanging, and no evaluation-owned copy of the helper's semantics is used in its place

#### Scenario: The evaluator command starts
- **WHEN** `python -I -S -B scripts/backend_eval_bootstrap.py` starts a receipt-producing execution
- **THEN** the direct shim itself verifies CPython's isolated, no-site, and no-bytecode flags, the effective bytecode setting, and an exact standard-library-only search path, then reads both inert initializer files and the complete `scripts/backend_eval` Python closure through component-wise no-follow descriptors, packs those exact bytes into one sealed immutable source image, and starts the actual evaluator under `-I -S -B`; disk `admission.py` categorically refuses every direct, package `-m`, or generic `runpy` `__main__` execution regardless of forged process state; the child receives only the declared proxy, CA, and locale inputs, derives its owner, deadline origin, and active-image state from the inherited sealed descriptor and bootstrap arguments rather than ambient internal-looking variables, imports no evaluator module from the checkout, ambient `PYTHONPATH`, `.pth`, `sitecustomize`, user site, or site-packages, and derives the published evaluator identity from the exact image bytes it imports

#### Scenario: Production admission is called outside the sealed command
- **WHEN** a caller imports the evaluator from disk and invokes admission with default services, explicit production services, dependency-injected services, or through `main(..., services=...)`
- **THEN** admission refuses before constructing or invoking those services or creating any receipt path unless the admission module proves its exact loader and origin against the inherited sealed image; dependency-injected unit tests use a separate non-publishing orchestration seam

#### Scenario: A production helper is executed in a child
- **WHEN** the sealed evaluator starts a production-helper child
- **THEN** a `HelperExpectation` derived from that evaluator's captured identity names the exact child program and operation-specific helper closure before the child starts; both are read through component-wise no-follow walks, compared with the expectation, sealed into immutable in-memory images, and executed or imported only from those images in isolated mode with no ambient source root

#### Scenario: The evaluator image, child program, or helper closure is substituted
- **WHEN** an evaluator module, the production-child program, or any production module changes on disk during a run, is transiently restored, or is reached through a symlinked ancestor component
- **THEN** transient evaluator bytes can execute only from the sealed evaluator image and are named by that image-derived identity, while a mismatch against the current checkout makes the source unclean; every production-child call independently enforces its per-run `HelperExpectation` before execution, with no process-global first-use pin and no post-hoc reread accepted as proof of the bytes that ran

#### Scenario: Evaluation-owned cleanup runs
- **WHEN** a phase runs its own cleanup before publishing
- **THEN** cleanup receives the same monotonic deadline, checks it around each of its own syscalls, and is bracketed by a check on both sides, and a ceiling reached in cleanup fails the phase closed rather than being recorded as an issue on an otherwise passing receipt

#### Scenario: A receipt claims a ceiling it was not run under
- **WHEN** a passing phase receipt carries a phase budget whose seconds differ from the frozen contract value, whether it is constructed in memory or parsed from published bytes
- **THEN** the receipt is refused, so a `pass` can never be published against a widened or narrowed ceiling

#### Scenario: The total ceiling is reached
- **WHEN** cumulative active evaluation time reaches 16 hours
- **THEN** the evaluation stops with `inconclusive_retain_pyright` unless existing completed hard-gate evidence already requires `retain_pyright`

### Requirement: Selection uses a conservative lexicographic decision
The final decision SHALL first require all hard gates, then compare demonstrated future-operation utility, current-surface answer quality, end-to-end Agent efficiency, cold and warm resource behavior, lifecycle complexity, and maintenance cost in that order without aggregating them into a compensating weighted score.

#### Scenario: One candidate dominates
- **WHEN** a candidate passes all hard gates, is no worse on frozen decision-owning correctness, and demonstrates strictly greater future-operation utility than every other candidate that entered the feature phase, with earlier eliminations recorded as gate exclusions
- **THEN** the decision may be `promote_pyrefly` or `promote_ty` with per-task evidence and risk disposition

#### Scenario: The competitors fail or do not separate clearly
- **WHEN** ty and Pyrefly fail a hard gate, regress current behavior, trade wins without a defensible task-level dominance, or differ only within noisy small-sample efficiency observations
- **THEN** the decision is `retain_pyright` or `inconclusive_retain_pyright`

#### Scenario: A decision is published
- **WHEN** all required phases and reviews finish
- **THEN** a machine-readable decision receipt and human-readable acceptance record name every gate, exclusion, failure, exact artifact digest, residual risk, and allowed next action

### Requirement: Evaluation cannot authorize migration or public feature expansion
Completing this change SHALL stop at a backend recommendation and SHALL require a separate user-approved OpenSpec change for production integration and another separately reviewable change for each public closed semantic surface.

#### Scenario: A winner is selected
- **WHEN** the final receipt recommends ty or Pyrefly
- **THEN** production continues using Pyright until a separate integration change proves build identity, bootstrap, provenance, compatibility, rollback, fresh-client acceptance, and current safety invariants

#### Scenario: No winner is selected
- **WHEN** the decision retains Pyright or is inconclusive
- **THEN** candidate evaluation processes and dependencies are retired and no permanent multi-backend registry, selector, or dormant public tool remains
