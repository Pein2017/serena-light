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

#### Scenario: Evaluation cleanup completes
- **WHEN** a phase or the full evaluation ends
- **THEN** temporary registrations, processes, leases, configuration, and service-owned candidate state are retired without changing canonical Serena or the installed Serena Light registration

### Requirement: Workspace mutation is a non-compensable failure
Candidate language servers SHALL use service-owned HOME, configuration, cache, executable, and minimal environment state, and SHALL NOT create, remove, retarget, or modify any evaluated workspace path outside an explicitly declared disposable mutation fixture.

#### Scenario: A candidate runs on a read-only input
- **WHEN** a protocol, common-surface, feature, cold-start, or warm-query probe completes
- **THEN** a before-and-after lexical manifest fully hashes the trust-inventory closure and declared fixture paths, metadata-scans the complete declared in-scope remainder of every Git root -- files, symlink targets, directories including empty ones, and any other node -- for path membership, file type, symlink target, size, `mtime_ns`, and inode, and hashes any remainder path whose metadata changed or that did not exist before

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
Every phase receipt SHALL be bound to the exact evaluator source closure that produced it, the CLI host interpreter that executed it, the environment its bootstrap downloads received, and the service-owned candidate runtime it evaluated, and each execution SHALL publish immutable evidence that no later execution can replace.

#### Scenario: A receipt is published
- **WHEN** any phase publishes a receipt
- **THEN** it records the digest of the executed evaluation source closure, the source Git commit and whether that source was clean, the CLI host interpreter's configured path, realpath, SHA-256, and version, and the candidate runtime's logical root and canonical runtime-manifest SHA-256 recomputed from disk before the gate can pass

#### Scenario: Evaluation code or the CLI host changes
- **WHEN** the evaluation source closure, the CLI host interpreter, or the artifact root differs from an earlier run
- **THEN** the reproducible evaluation identity differs, so new evidence is published beside the earlier evidence rather than over it

#### Scenario: A run is repeated or two runs overlap
- **WHEN** the same evaluation identity is executed again, concurrently or later
- **THEN** each execution carries its own immutable run identity, publishes to its own receipt path under a per-identity lock, and can neither delete nor replace another execution's receipt

#### Scenario: A resolver or installer needs the external network
- **WHEN** a bootstrap download runs
- **THEN** it inherits only the allowlisted external-network proxy, CA, and locale values plus service-owned state, refuses ambient package-index, source, `PATH`, and `PYTHONPATH` controls, and the receipt records only key names and SHA-256 digests of values

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
- **THEN** the ceiling covers every step the phase performs, including resolution, preparation, each snapshot capture, cleanup, final identity checks, artifact digests, and receipt publication, and is not limited to the phase's expensive middle

#### Scenario: A child process does not return
- **WHEN** an evaluation-started subprocess exceeds the phase's remaining time
- **THEN** it receives that remaining time as its own bound, its whole process group is terminated on expiry, and it cannot outlive the phase or block cleanup

#### Scenario: A production helper would start its own child
- **WHEN** a phase needs evidence a production helper computes by spawning its own unbounded process
- **THEN** the phase supplies that helper's input from a bounded invocation of the identical command and reuses only the helper's subprocess-free logic, so no phase step is exempt from the ceiling, and an equivalence test pins the reused result to the production helper's output

#### Scenario: A phase reaches its ceiling with usable evidence
- **WHEN** collection reaches the ceiling but the phase already holds the identities and frozen inputs a timeout receipt requires
- **THEN** a reserved finalization window publishes that timeout receipt, and a phase that cannot complete even that evidence fails closed without publishing a receipt

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
