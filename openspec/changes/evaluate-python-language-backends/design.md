## Context

See `proposal.md` for motivation and scope. Serena Light's production Python path is currently concrete rather than generic: `PyrightFacts` owns command/configuration/provider facts and is constructed unconditionally by `WorkspaceRuntime`; `DiagnosticEngineFacts` rejects Python engines whose name is not `pyright`; Python document-symbol flows always apply a Pyright-specific assignment recovery; Python success metadata is rendered with a hardcoded `pyright` literal; diagnostics/readiness rely on push `publishDiagnostics`; the LSP client contains a bounded three-attempt `ContentModified` retry seam that production leaves disabled; bootstrap pins the npm package; and native-program attribution uses a version-private Pyright probe. At the same time, `WorkspaceRuntime` already accepts injected attributors and adapter factories, and the shared LSP client, executor, document lifecycle, position mapper, freshness coordinator, compact envelopes, and lease model are backend-neutral enough to exercise without first creating a production registry.

The currently pinned baseline is Pyright `1.1.403`. Candidate versions are frozen at evaluation start by the rule in Decision 2 rather than inherited from Serena or the development-only `ty` dependency. Official Serena at pinned reference commit `9a9d07e83d8c` is read-only evidence only. Its Pyrefly adapter writes a fallback `pyrefly.toml` and retries mutation cancellations five times; neither behavior is compatible with Serena Light's workspace and bounded-replay contracts.

The project context still contains historical 12k-LOC and single-transformers trust wording that stable specs and the current runtime have superseded. Evaluation artifacts follow current stable specs and the accepted roadmap: production LOC is informational, arbitrary caller-selected Git and non-Git roots are permitted under the current activation contract, non-Git editing stays forbidden, and host `rg`/`find` remains the lexical-discovery owner.

## Goals / Non-Goals

**Goals:**

- Compare Pyright, ty, and Pyrefly through the same production-shaped semantic boundary.
- Preserve exact workspace, environment, freshness, position, timeout, response-budget, and lifecycle evidence.
- Determine whether candidate-specific closed semantic operations improve real Codex Agent conclusions.
- Produce one replayable recommendation without leaking experimental backend choice into the installed MCP.
- Make the evaluation cheap to stop after any failed gate and cheap to remove after the decision.

**Non-Goals:**

- Generalizing production around a reusable `LanguageServerSpec` or permanent backend registry.
- Migrating the production daemon, changing build identity, or changing client registrations.
- Treating benchmark adapters as production-ready implementations.
- Treating internal hierarchy probes as public hierarchy tools. The current project non-goal remains binding until a later user-approved public-surface change.
- Expanding guarded editing or using candidate code actions, completion, rename, formatting, or workspace edits.
- Replacing shell lexical discovery or claiming complete coverage of dynamic Python.
- Selecting a winner from feature lists, microbenchmarks, or aggregate weighted scores alone.

## Decisions

### Decision 1: Use a three-plane isolated harness

The evaluation has three progressively more expensive planes:

1. **Protocol plane** starts each locked backend directly with service-owned environment/configuration and records initialize, server requests, notifications, diagnostics, cancellation, and process behavior.
2. **Product-seam plane** injects evaluation-only attributors and adapter factories into the existing `WorkspaceRuntime`, reusing production LSP transport, state, documents, freshness, positions, envelopes, leases, and executors.
3. **Agent plane** launches a temporary eval-only MCP with the installed public schemas plus evaluation-only closed query operations. An orchestrator selects the backend before launch; the Agent never receives a backend selector or identity.

Candidate adapters, the eval connector, and the orchestration CLI live under `scripts/backend_eval/`; deterministic tests and fixtures live under `tests/backend_eval/`. No candidate module is imported by `src/serena_light`, packaged in the wheel, listed by production bootstrap, or included in production build identity.

Alternative rejected: add three backend factories to production `WorkspaceRuntime`. It gives a realistic path too early but would generalize diagnostics, status, bootstrap, provenance, attribution, and runtime identity before there is evidence that any candidate should survive.

Alternative rejected: build three independent prototype MCPs. That isolates dependencies but duplicates the wrapper, so connector and freshness differences would contaminate the backend comparison.

### Decision 2: Freeze dependencies by resolution rule, not ambient state

Pyright uses the existing production version and exact bundle hashes. For ty and Pyrefly, the manifest builder resolves the highest non-yanked release available from the official package index on the freeze date whose package metadata is not a PEP 440 pre-release; an eligible `0.0.x` ty release is not excluded merely for its version series. It captures package files and SHA-256 digests and writes an immutable candidate lock before running probes. Pre-releases, editable installs, ambient PATH entries, repository checkouts, and automatic upgrades are forbidden.

Candidate executables and caches live below `/data/CoordExp/.codex/runtime/serena-light/backend-eval/<candidate-lock-digest>/`. The candidate lock and its requirements inputs are not `LOCK_INPUTS`: the evaluation may not modify `pyproject.toml`, `uv.lock`, `package-lock.json`, production bootstrap inputs, or the installed dependency slot. Before and after every phase, the harness asserts that `dependency_lock_digest`, `compute_build_identity`, and production `runtime_paths` are byte-identical. Each child receives a service-owned HOME, explicit executable, explicit selected interpreter, minimal allowlisted environment, loopback no-proxy behavior, and no inherited `*_PROXY` values. Bootstrap download steps may use the user's ambient external-network proxy, but the backend and loopback runtime may not.

This rule avoids hard-coding versions before the evaluation begins while making every produced result exactly reproducible. If the resolved package changes before the lock is published, the manifest build fails rather than silently accepting a new hash.

### Decision 3: Freeze real inputs without taking ownership of live worktrees

The corpus contains:

- Serena Light at the exact evaluation commit.
- `/data/ms-swift` at its exact detached commit plus recorded untracked-path disposition.
- `/data/CoordExp/.worktrees/research-probes` at an exact bounded filesystem manifest, not merely its Git HEAD.
- `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers` as the primary external read-only package.
- The `llm-framework-study` interpreter/site-packages as a second environment-resolution case, scoped to the files required by fixed tasks rather than a full unbounded tree scan.

Read-only tasks use immutable snapshots or verified manifests. Full content digests cover the lexical trust-inventory closure and declared fixture paths. The *complete* declared in-scope remainder of every Git root -- ordinary files, symlinks with their targets, directories including empty ones, and any other node, including large ignored subtrees such as `research-probes/model_cache` -- receives a metadata-only path/type/symlink-target/size/`mtime_ns`/inode sweep; content is hashed only for a remainder path whose metadata changed or that did not exist before, and the after manifest and its digest are rebuilt with those hashes before any delta is constructed. A race during that second stage is *incomplete*, never clean.

Exactly four trees are pruned, by name, to keep the sweep bounded: `.git`, the evaluation `.admission-artifacts`, a lane-owned `.venv`, and `node_modules`. Every pruned path is published in the manifest and counted in the acceptance record, so the boundary is evidence rather than a silent hole; a trust-inventory member that happens to live inside a pruned tree is still fully hashed. The remainder is bound by what metadata can express: a rewrite that preserves size, inode, *and* `mtime_ns` is not observable there, which is why the trust-inventory closure and declared configuration paths are content hashed on every capture instead.

The trust inventory itself is production's, without production's unbounded child. Rather
than calling `git_trust_inventory`, which starts its own `git ls-files`, the evaluation reads
that same combined `git ls-files --cached --others --exclude-standard -z` through the bounded
runner and hands the bytes to production's pure candidate normalization and inspection
helpers. Decoding, extension filtering, guarded inspection, rejection reasons, the path
digest, and the query tree are unchanged production code, and an equivalence test pins the
result to `git_trust_inventory` field by field, so every Git child of a capture is bounded
with no exception.

Each phase captures the corpus *before* its first setup operation and again *after* the last one and before cleanup and receipt publication, so the delta brackets everything the phase did rather than only its expensive middle. A created, deleted, or changed inventory member between captures is a write delta with an unexpected path, not an unstable-root error; a freeze that moves *while one capture is running* stays fail-closed. Controlled diagnostics, concurrent-change, and guarded-edit tasks use disposable copies under a temporary evaluation root. The manifest distinguishes declared fixture mutations from unexpected backend writes. Missing roots or an inability to freeze dirty state stops the run.

### Decision 4: Separate backend program ownership from test trust

The product-seam plane uses an evaluation-only `ScopeProjection` over the frozen corpus so all backends receive identical trust inventory. This projection is not accepted as a production attribution mechanism. Separately, the protocol plane must demonstrate how each backend selects native configuration, interpreter, included files, excluded files, and external import roots without workspace mutation.

A promotion recommendation must include a viable production attribution design: either stable backend-native configured-program evidence or an explicit service-owned configuration whose scope can be verified against Serena Light's lexical inventory. A candidate that answers the fixed corpus correctly but cannot support trustworthy production attribution may complete exploratory feature probes but cannot be promoted.

This avoids giving Pyright an unfair advantage from its existing private attribution probe while preventing the evaluation projection from laundering an unsafe production design.

Roots without native Python configuration use the common service-owned controlled arm. `research-probes`, which has a native `pyrightconfig.json`, is reported separately: Pyright honours its production-native configuration, while each competitor receives a service-owned translation of the same include/exclude/interpreter intent. Those results are a compatibility/maintenance gate and are not pooled with no-native-config roots. An observation-only native-discovery arm may record divergence but cannot override the controlled comparison.

### Decision 5: Preserve Serena Light freshness and failure semantics

Backends receive the same open/change/close and watched-file notifications from the shared adapter. Every content-bearing call keeps the existing preflight, at-most-one complete replay, byte-witness postflight, and typed second-race failure. The existing `LspClient.set_content_modified_retry_methods` seam remains disabled for all arms, so every request has one backend attempt; the harness may not add hidden retry loops. Protocol receipts separately count `ContentModified` and `RequestCancelled` outcomes.

Protocol receipts retain raw cancellation codes and timing. Product receipts retain normalized outcome, workspace generation, source digest, response length, replay count, typed failure, candidate identity from the orchestrator, the fixed in-response adapter literal, Pyright assignment-recovery fired/unresolved counts, and diagnostics witness mode. A backend that needs unbounded sleeps or retries to appear stable fails lifecycle admission.

The product plane does not relax `DiagnosticEngineFacts` in production. Instead, an evaluation-only diagnostics assembler supplies backend-neutral evidence to the unchanged document witness, readiness, normalization, and compact rendering seams. This deviation is explicit in every receipt and becomes a production-integration requirement if a candidate wins. A pull-only candidate is recorded as `seam_incompatible_pull_only` and non-promotable under the current architecture rather than being scored as a false backend diagnostic result.

### Decision 6: Compare common surface before closed future operations

The common corpus exercises overview, exact and global symbols, declaration, references, diagnostics, implementation support/fallback, compact budgets, Unicode positions, multi-session same-root reuse, concurrent multi-root isolation, cold readiness, external imports, controlled diagnostics changes, and stale-hash guarded replacement.

Only hard-gate survivors receive normalized internal operations for:

- implementation;
- type definition;
- hover;
- incoming and outgoing calls;
- supertypes and subtypes.

The normalization layer accepts only standard LSP results and validated snapshot ranges. It does not expose opaque prepare items to the Agent, infer hierarchy from references, or recover missing operations with AST/text heuristics. Capability advertisement, request success, result correctness, and Agent utility remain four separate receipt fields. Internal call/type-hierarchy probes do not revise the project's public hierarchy-tool non-goal.

### Decision 7: Use backend-blinded, paired Codex tasks

The Agent plane runs only when Phase 4 leaves a task-level utility claim requiring Agent demonstration or leaves at least two promotable candidates not yet separated. Otherwise it records `not_required` and stops before model spend. The task set has six fixed families: semantic owner, implementation dispatch, call impact, external type/import, concurrent freshness, and bounded dynamic-Python analysis. Four to six concrete prompts are frozen before the first arm. Each backend receives the same prompt, Codex model, reasoning effort, shell permission, MCP instructions, maximum semantic calls, maximum MCP characters, and a 25-minute wall-time ceiling. The Agent may use shell for `rg`/file discovery, precise reads, Git diffs, and tests, but only its assigned MCP for semantic navigation.

Before any backend result is observed, each fixture freezes its decision-owning evidence as the expected workspace, relative path, symbol identity, accepted range set, and smallest subset sufficient to answer the task. Backend arm order is rotated across tasks. The orchestrator records tool calls, serialized response characters, shell fallback, cold/warm status, time to first decision-owning evidence, time to final answer, and final answer. A task-specific deterministic verifier scores those frozen facts before a lead reviews unsupported claims and evidence boundaries. Infrastructure failures are not silently converted into backend failures; an affected paired task is rerun once from a fresh identical arm or marked unusable for selection.

The Agent surface is a direct evaluation-only stdio MCP, not the production connector. It explicitly classifies every closed evaluation operation as read-only and uses the same typed transport-loss semantics for every arm without modifying `connector.py` or installed registrations.

The small sample is used to detect clear task-level differences, not to estimate population means or rank model providers.

### Decision 8: Select conservatively without a weighted leaderboard

Selection is lexicographic:

1. all hard gates;
2. demonstrated utility of future closed operations;
3. current-surface answer quality;
4. calls, response characters, and time to useful answer;
5. cold/warm latency and RSS;
6. lifecycle and maintenance complexity.

A candidate dominates only when it passes all gates, is no worse on frozen decision-owning correctness, and yields strictly greater task-level future-operation utility than every other candidate that entered the feature phase; candidates eliminated earlier remain explicit gate exclusions. Future-operation utility requires decision-owning evidence unavailable from the current surface. Call/context savings are measured only at the later efficiency rank. Speed breaks a tie only after correctness and utility. If ty and Pyrefly win different tasks without defensible dominance, or only noisy efficiency separates them, the result is `inconclusive_retain_pyright` rather than a subjective weighted score.

The decision receipt enum is closed: `promote_pyrefly`, `promote_ty`, `retain_pyright`, or `inconclusive_retain_pyright`. Every recommendation lists failed/excluded arms, raw artifact digests, residual risks, production attribution feasibility, and the exact next action permitted.

### Decision 8a: Bind every receipt to its evaluator, host, environment, and runtime

A receipt is only reproducible evidence if it names the thing that produced it. Every receipt therefore carries a typed evaluator identity -- the digest of the executed `scripts/backend_eval` source closure measured from the imported bytes, the source Git commit and whether that source was clean, and the CLI host interpreter's configured path, realpath, SHA-256, and version -- plus the candidate runtime's logical root and canonical `runtime-manifest.json` SHA-256, recomputed from disk before the gate may pass. The evaluator identity and the artifact root are part of the reproducible evaluation identity, so changed evaluator code or a changed host publishes new evidence beside the old rather than over it.

`scripts/backend_eval` is not the whole executed evaluator. The corpus manifests, the write guard, and the production-identity capture execute *production* helpers -- the trust-inventory normalization, the guarded directory opener, the dependency-lock digest, the build identity, and the runtime paths -- imported as `serena_light`. A CLI host virtual environment resolves that name through whatever editable `.pth` installed it, so those helper bytes can come from a different checkout than the one whose `scripts/backend_eval` digest the receipt publishes: repointing the `.pth`, or editing the other checkout's `src`, would change the evaluation's semantics while leaving the published identity untouched. The evaluator therefore binds `serena_light` to *its own* checkout's `src` before importing any helper, refuses to run when a loaded `serena_light` module still resolves outside that checkout -- by realpath, so a symlink out of the tree is refused too -- and records the executed production closure as an origin root, a per-file byte digest, a recomputed closure digest, and its own cleanliness at the recorded commit. Both closures feed the evaluation identity. The bound set is complete rather than merely representative: the admission CLI loads no non-stdlib module other than `scripts` and `serena_light`, which a test pins in a fresh interpreter.

Alternative rejected: copying the production helpers into evaluation-owned code. That would keep the receipt honest but would fork the very semantics the evaluation exists to measure, and the equivalence claim the corpus capture depends on would decay silently. Binding the real helpers keeps production's code authoritative and makes the *identity* -- not a copy -- carry the proof.

Resolver and installer calls receive an exact environment: the user's external-network proxy, CA bundle, and locale from an allowlist, plus service-owned `HOME`, `TMPDIR`, XDG directories, and uv cache. Ambient `UV_*`, `PIP_*`, `PYTHONPATH`, `PATH`, and other package-manager controls are refused by name, and `UV_NO_CONFIG` keeps an ambient `uv.toml` or `pip` mirror out of the freeze. Receipts record only key names and SHA-256 digests of values, so a proxy URL carrying a credential is never published.

Each execution has its own immutable `run_identity` and publishes `receipts/<run-identity>.json` with an exclusive link under a per-identity `O_NOFOLLOW` lock. Cold and warm runs of one evaluation identity therefore both survive; a repeated or concurrent run can neither delete nor replace another run's receipt. Candidate-lock transactions are serialized the same way, so a live transaction is never mistaken for an interrupted dead one.

Alternative rejected: keep one canonical receipt per evaluation identity and replace it. That loses the cold run's own evidence exactly when the two runs disagree, which is the case the receipt exists to settle.

### Decision 8b: Make the wall-clock ceiling cover the whole gate

A phase's frozen ceiling covers every step it performs -- resolution, runtime preparation, each snapshot capture, cleanup, the final production identity, the artifact digest, and receipt publication. Collection stops early enough to leave a reserved finalization window, so a phase that reaches the ceiling can still publish a trustworthy timeout receipt when it already holds the identities and frozen inputs such a receipt requires; a phase that cannot complete even that evidence fails closed without a receipt. Every subprocess the evaluation starts receives the remaining time as its own bound and runs in its own session, and its whole process group is killed on expiry, so a hung `uv` or Git call can neither outlive the phase nor block cleanup. Long filesystem traversals and finalization check the ceiling cooperatively, so a slow artifact digest stops with a typed incomplete rather than returning a pass.

Three ways of leaving the ceiling remained after that, and all three are closed. **Waiting is a step.** Every `flock` the evaluation takes -- candidate resolution, candidate runtime preparation, and receipt publication -- is acquired non-blockingly and retried against the same monotonic deadline, in the calling thread, with no helper thread, signal, or alarm; a lock another process still holds when the ceiling arrives raises the same typed timeout as any other expired step, and a caller with no phase deadline still gets a bounded wait instead of an indefinite one. **Publication is a step, all the way to the return.** A phase's receipt is tens of megabytes: serializing, writing, `fsync`-ing, linking, and directory-`fsync`-ing it is real time, and recording `ended_at` before that work would let a run that overran its budget still report a pass inside it. Publication therefore checks the ceiling before it starts, writes the payload in deadline-checked chunks, and performs the atomic link only while a small publication reserve is still left. Checking once after the link is not enough: the temporary unlink and the final directory `fsync` come after it, and a check placed before them lets a run earn its pass with work done past the ceiling. Every post-link mutation and every durability barrier is therefore followed by its own checkpoint, including one immediately before the function returns, while withdrawal is still possible; only descriptor closes follow, and they touch neither the namespace nor storage. The first checkpoint that observes expiry withdraws this run's own link and fails closed. Immutability is unaffected: the only name ever unlinked is the one this run's `O_EXCL` temporary and failing `link` prove no other run published.

**Cleanup is a step.** Cleanup receives the same deadline, checks it around each of its own syscalls, and is bracketed on both sides. A ceiling reached in cleanup fails the phase closed rather than being downgraded to an issue on an otherwise passing receipt, so no cleanup implementation -- real or substituted -- can spend the budget and still allow a later pass.

**A fourth way was found later: a guarded read is a syscall too.** Every regular-file read the evaluator performs opens the path `O_RDONLY | O_NOFOLLOW`, `fstat`s the descriptor, and refuses anything that is not a regular file -- the corpus remainder, the runtime manifest, the owned-runtime mode repair, the evaluator source closure, the bound production helper closure, and the admission artifact-tree digest all go through one such guarded read each. `open()` on a FIFO in read-only mode blocks until a writer appears, regardless of `O_NOFOLLOW`, and that block is a single uninterruptible syscall in the calling thread -- the same enforcement boundary already stated above, but with no cooperative checkpoint inside the syscall to observe. A FIFO or other blocking special node left where a regular file is expected -- by a same-name race, or by a node the traversal's own `lstat` gate does not reach -- therefore hung the read rather than failing closed; it could not produce a false `pass`, since the `fstat` refusal still runs after any open that returns, but it could make a run exceed the whole-phase ceiling with no receipt, which is exactly the failure mode this decision exists to close. Every one of those guarded reads now adds `O_NONBLOCK`, which has no effect on a regular file's read behaviour and changes nothing about `O_NOFOLLOW`, descriptor-relative confinement, or the `fstat` refusal; it only makes the open on a FIFO or other blocking special node return immediately, so the refusal that already existed can run promptly instead of never running at all.

**What the ceiling is, precisely.** It is enforced *cooperatively*, at the boundaries between syscalls, in the calling thread. A `link`, `unlink`, or `fsync` already in flight is not preemptible, and introducing a watchdog thread to interrupt one would trade a bounded, observable overrun for an unbounded correctness hazard in the middle of a durability barrier. The consequence is stated rather than papered over: for as long as one in-flight post-link `fsync` takes to complete, the final receipt name can exist in the directory after the ceiling has passed. It is withdrawn at the next boundary, and it is not admitted evidence -- every consumer of this gate requires the command to have exited successfully *and* the receipt to verify canonically against its own digest and artifact-tree digest, and an overrun run supplies neither. The invariants that are actually guaranteed are the two that matter: no `pass` is ever returned after the ceiling, and no final receipt remains once an overrun has been observed.

### Decision 8c: Scope the file-permission contract to what the harness owns

Service-owned state is `0600` for regular files and `0700` for directories, independent of the ambient umask, because the evaluation writes locks, configurations, and receipts that describe interpreters, proxies, and corpus contents. The contract is scoped by *ownership*, not by location: it covers every file this harness writes -- the installed lock snapshot, the published runtime manifest, the three service configurations, the candidate-lock artifacts, the publication and resolution locks, and the receipts -- plus every service-owned ancestor directory.

It deliberately stops at third-party tree interiors. `uv` and `virtualenv` create their own cache and environment files inside those directories, including a world-writable `.lock`. Recursively rewriting them would mean chmod-ing a tool's private cache against its own assumptions, and would buy no confidentiality: they already sit behind `0700` service-owned ancestors, and the receipt's artifact-tree digest excludes the resolver cache entirely, so their modes are outside the evidence the receipt binds. That boundary is pinned by test on both sides rather than left to inspection.

A runtime published before this contract was enforced keeps its old `0660` files, and content addressing means it is reused rather than rebuilt. Reuse already holds the per-digest runtime lock, which is the only safe place to correct them, so reuse repairs them there: `fchmod` on a descriptor whose *every* component was opened `O_NOFOLLOW` from the already-proven open runtime root, and which `fstat` shows to be a regular file through that same descriptor. Naming the whole relative path in a single `open` is not enough and was rejected for a concrete reason: `O_NOFOLLOW` constrains only the last component, so a symlinked `config/ty` would carry the `fchmod` to a file outside the root entirely. Both the repair and the verification therefore walk the path one component at a time. No byte moves, so neither the installed snapshot digest nor the published manifest digest can change; a symlink anywhere along the path, a non-regular harness file, or a widened service-owned directory is refused rather than repaired, so no chmod can escape the root. The contract is then re-verified in full, and a runtime that still violates it is never returned -- which is what makes the repair receipt-bound rather than advisory.

### Decision 9: Keep raw evidence ignored and summaries reviewable

Raw manifests, protocol transcripts, process samples, backend responses, and Agent receipts live under `.admission-artifacts/backend-eval/<evaluation-identity>/`, which is already ignored; per-execution receipts live below its `receipts/` directory. The active change's acceptance record contains exact commands, evaluation identity, artifact-tree digest, per-gate disposition, and a concise decision table. No source text, secrets, bearer tokens, or full Agent histories are copied into committed evidence.

### Decision 10: Stop before migration and feature publication

This change completes after the decision receipt is independently reviewed. A winner triggers no automatic install, source import, client restart, or schema change. Production migration requires `integrate-<winner>-python-backend`, including bootstrap/build identity, native attribution, compatibility, rollback, full tests, fresh clients, and independent audits. Closed public query operations are proposed afterward according to measured utility rather than bundled with migration.

## Risks / Trade-offs

- **Evaluation-only projection may hide production attribution difficulty** -> require a separate native/configuration ownership receipt and block promotion without a viable production design.
- **Service-owned configuration may not exactly match each backend's idiomatic project discovery** -> record both the controlled configuration and one observation-only native-discovery arm; selection uses the controlled arm, while divergence is a maintenance risk.
- **Small Agent samples are model-noisy** -> rotate arm order, pair identical tasks, use deterministic fact verifiers, rerun only infrastructure-invalid arms once, and retain Pyright when differences are not clear.
- **Future feature breadth could reward novelty over usefulness** -> count only frozen task-level decision evidence unavailable from the current surface; keep call/context savings in the later efficiency rank and never count advertised features.
- **Candidate releases can move quickly** -> freeze versions and hashes once; later releases require a new evaluation identity rather than mutating the current evidence.
- **Real dirty worktrees can change during setup** -> freeze bounded trust/fixture content manifests plus complete remainder metadata and use snapshots; fail if the source changes while one capture is running, and hold on any change observed between the two captures. A live external worktree that another lane is writing will therefore hold rather than pass; that is the instrument working, not a defect to compensate.
- **A metadata-only remainder cannot see a same-size, same-timestamp rewrite** -> keep the trust-inventory closure and declared configuration paths fully content hashed on every capture, and record the bound explicitly rather than implying complete byte coverage of ignored trees.
- **Protocol logging may capture source or secrets** -> store bounded structured metadata by default, redact environment and bearer values, and keep raw transcripts local and ignored.
- **Temporary MCP configuration could affect normal clients** -> use isolated config roots and names, never modify canonical Serena or installed Serena Light entries, and verify registrations before and after every Agent arm.
- **The harness could become a dormant multi-backend product** -> keep it out of the wheel and production build identity, document its one-decision lifecycle, and remove service-owned candidate runtimes after archive unless an approved integration change needs the winning lock.
- **Evaluation can expand without bound** -> cap active time at 30 minutes for admission, 90 minutes for protocol, 3 hours for product-seam, 2 hours for feature probes, 8 hours for the optional Agent phase, 25 minutes per Agent arm, and 16 hours total including one hour of repair/rerun slack; stop and retain Pyright when the remaining decision is unreachable.

## Migration Plan

There is no production migration in this change.

1. Freeze evaluation source, inputs, candidates, tasks, and budgets.
2. Run protocol gates and stop failed candidates.
3. Run product-seam gates for survivors and stop failed candidates.
4. Run closed feature probes for survivors.
5. Run the backend-blinded Codex tasks only when Phase 4 leaves a utility claim needing Agent demonstration or leaves at least two promotable candidates not yet separated.
6. Produce and review the closed decision receipt.
7. Remove temporary registrations and processes; retain ignored raw evidence until the user accepts the decision.
8. If the user accepts a non-Pyright winner, create a separate integration change. Otherwise remove candidate runtimes and archive this evaluation change.

Rollback during evaluation means stopping the current phase, releasing evaluation leases, killing only PID-and-create-time-verified evaluation-owned processes after holders reach zero, removing isolated temporary registrations/configuration, and leaving the installed Pyright build untouched.
