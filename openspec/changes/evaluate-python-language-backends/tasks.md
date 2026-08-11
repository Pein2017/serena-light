## 1. Freeze the evaluation contract and inputs

- [x] 1.1 Add typed manifest and admission-receipt schemas for evaluation identity, evaluation-contract version, source commit, backend lock, interpreter/configuration identity, workspace snapshots, phase/arm budgets, before/after write deltas, and artifact digests; cover canonical serialization and malformed input with unit tests. Later-phase receipt schemas are owned by the task that first freezes their data.
- [x] 1.2 Implement the candidate-lock builder that retains production Pyright `1.1.403`, resolves the highest non-yanked non-PEP-440-prerelease ty and Pyrefly versions once, accepts eligible `0.0.x` ty releases, verifies downloaded file hashes, and refuses ambient executables, editable installs, hash drift, or a second resolution after freeze.
- [x] 1.3 Implement service-owned candidate runtime preparation below `/data/CoordExp/.codex/runtime/serena-light/backend-eval/<candidate-lock-digest>/` from evaluation-only requirements inputs, with a minimal child environment, explicit executable and interpreter, isolated HOME/cache/config, removed proxy variables, and exact process ownership evidence.
- [x] 1.4 Add an invariant test and per-phase assertion that `pyproject.toml`, `uv.lock`, `package-lock.json`, `dependency_lock_digest`, `compute_build_identity`, and production `runtime_paths` remain byte-identical before and after candidate lock, installation, probes, and cleanup.
- [x] 1.5 Implement deterministic Git snapshot manifests for the Serena Light commit, `/data/ms-swift`, and `/data/CoordExp/.worktrees/research-probes`, including tracked/untracked/ignored disposition and fail-closed detection if a source changes while freezing.
- [x] 1.6 Implement bounded non-Git manifests for `ms` transformers and the exact `llm-framework-study` task paths without scanning the full environment.
- [x] 1.7 Implement before/after write detection that hashes the trust-inventory closure and declared fixture paths, metadata-scans the remainder for path/type/symlink-target/size/`mtime_ns`/inode changes, and hashes only changed remainder paths; test declared disposable edits separately from unexpected backend mutation.
- [x] 1.8 Run the admission command under its 30-minute ceiling, record exact versions, hashes, roots, interpreters, configuration, production identity invariants, and phase budget, and stop for lead disposition if any candidate or required corpus cannot be frozen reproducibly.

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
