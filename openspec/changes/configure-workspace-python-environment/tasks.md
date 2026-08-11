## 1. Contract-first regression coverage

- [x] 1.1 Add unit tests proving any existing absolute non-Git directory, including an enclosing synthetic `site-packages`, activates as the exact `non_git_read_only` identity while relative/missing paths still fail.
- [x] 1.2 Add unit tests for default `ms`, explicit environment selection, invalid-name/missing-interpreter rejection, and registry-key isolation by interpreter; run them against the current implementation and record the expected red failures.
- [x] 1.3 Add service tests proving environment selection is lease-bound, same-root/different-environment runtimes isolate, and a failed environment switch retains the previous binding; record the expected red failures.
- [x] 1.4 Add runtime/Pyright tests proving the selected interpreter reaches attribution, `workspace/configuration`, adapter metadata, status, and rich diagnostics while compact success remains unchanged; record the expected red failures.

## 2. Workspace and environment ownership

- [x] 2.1 Implement a fail-closed Conda environment resolver with `ms` default, safe environment-name validation, exact interpreter validation, and no ambient shell-environment dependency.
- [x] 2.2 Extend workspace identity and physical registry keys with selected environment/interpreter while retaining lease-local working-subdirectory metadata.
- [x] 2.3 Generalize non-Git activation, workspace kind, bounded no-symlink inventory, targeted freshness, and read-only authorization; remove the production transformers-root special case.
- [x] 2.4 Generalize LSP-returned external location classification to existing read-only paths without expanding direct path operands or edit authorization.

## 3. Public activation and Python runtime wiring

- [x] 3.1 Thread optional `python_environment` through the MCP schema, daemon API, connector transport, service resolver, auto-bind default, and acceptance fakes without changing callers that omit it.
- [x] 3.2 Parameterize locked Pyright facts by the binding-selected interpreter and use those facts consistently for program attribution, adapter startup/configuration, assignment recovery, engine metadata, and status.
- [x] 3.3 Preserve transactional activation: validate/acquire/refresh the candidate before swapping the lease, and retire old environment-specific runtimes through existing holders/grace rules.
- [x] 3.4 Update agent instructions, compatibility inventory, README/client registration, and schemas to recommend narrow roots for efficiency without imposing a trust restriction.

## 4. Verification and release evidence

- [x] 4.1 Run focused identity, service, connector, runtime, Pyright, diagnostics, and guarded-edit tests; run Ruff and Ty on the changed closure.
- [ ] 4.2 Run real connector/daemon activation of `llm-framework-study/site-packages` with default `ms` and explicit `llm-framework-study`; prove status interpreter selection, read-only editing, release cleanup, and no orphan processes.
- [ ] 4.3 Run the full pytest suite, strict OpenSpec validation, bootstrap/build-identity, source ownership/provenance, and production LOC informational report.
- [ ] 4.4 Sync stable specs, archive the completed change, verify a fresh client negotiates the new activation schema/build, and publish only after all blockers are closed.
