# Serena Light Full-Repository Audit Prompt

Use this prompt unchanged for two independent reviews: one by Fable at max
effort and one by Sol at max effort. Keep the reviewers blind to each other's
output. The task is read-only.

---

You are an independent senior architecture, correctness, security, and
integration reviewer. Perform a deep, evidence-based audit of the entire
current Serena Light repository.

## Repository and authority

- Audit root: `/data/CoordExp/serena-light`
- Reference-only Serena checkout: `/data/CoordExp/external/serena`
- Owning change: `openspec/changes/build-serena-light-v1`
- Primary acceptance claim:
  `openspec/changes/build-serena-light-v1/final-acceptance.md`
- Compatibility contract: `docs/compatibility.json`
- Client/lifecycle contract: `docs/client-registration.md`

Treat the current filesystem under the audit root as the review subject. This
repository does not yet have a trustworthy committed baseline, so do not limit
your review to `git diff` or assume that untracked files are irrelevant. Inspect
the complete repository except generated environments, caches, and `.git`.

The OpenSpec proposal, design, specs, tasks, and acceptance reports are the
declared intent, but they are claims to verify rather than proof. Source code,
tests, executable behavior, lock/provenance data, and client configuration must
agree with them. When they disagree, identify the current behavioral owner and
explain the consequence.

## Brief background and incentive

Serena Light is an internal, deliberately minimal semantic MCP for our own
Codex, Claude Code, and CC Agent workflows. We wanted the valuable semantic
parts of Serena—Python and JavaScript/TypeScript navigation, references,
definitions, implementations where supported, diagnostics, and guarded
symbol-body replacement—without Serena's UI, JetBrains integration, memories,
modes, telemetry, broad editing surface, or mutable single-project agent
model.

The main incentive is operational: many agents, sessions, and working
directories should reuse the same expensive language-service daemon without
sharing a mutable active workspace, leaking state across repositories, or
fighting over lifecycle. A connector inherits its startup cwd, while later
cross-root changes are explicit and transactional. Site-packages are readable
for semantic navigation but not editable. The product is independently owned;
the external Serena checkout is a permanent reference, not an upstream to
merge or track automatically.

The accepted deployment is a parallel MCP registration named `serena-light`.
Canonical `serena` must remain unchanged unless a separate decision explicitly
approves a switch.

## Audit objective

Determine whether the repository is genuinely safe and correct enough to rely
on as a shared semantic service, and whether the evidence justifies the v1
PASS claim. Look for concrete defects, silent semantic errors, security or
trust-boundary failures, concurrency races, lifecycle leaks, false readiness,
unsafe edit behavior, protocol mismatches, and tests that pass without proving
the claimed behavior.

Do not reward feature count or propose general product expansion. A missing
Serena feature is not a defect when it is an explicit non-goal. Favor the
smallest correction that restores a stated invariant.

## Required review scope

Review all material files under:

- `src/serena_light/`
- `tests/`, including unit, integration, admission, and real-repository/fault
  acceptance tests
- `scripts/`
- `openspec/changes/build-serena-light-v1/`
- `docs/`, `README.md`, and client registration/configuration artifacts
- `pyproject.toml`, `uv.lock`, `package.json`, and `package-lock.json`
- `third_party/` and `THIRD_PARTY_NOTICES.md`
- repository-local `.codex/`, `.claude/`, ignore files, and packaging/CLI
  entrypoints where present

Trace end-to-end behavior instead of reviewing files in isolation. At minimum,
pressure-test these invariant groups:

1. **Workspace identity and isolation**
   - No daemon-global mutable active workspace.
   - Same-root reuse and cross-root isolation are both real.
   - Cross-root activation acquires and validates the new binding before
     releasing the old one; failed switches preserve the old binding.
   - Startup-cwd auto-binding and later explicit `activate_workspace` behavior
     match the documented client contract.

2. **Trust, scope, and external paths**
   - Git inventory, native configured semantic program, path-scoped document
     availability, and edit authorization remain distinct.
   - Symlink, traversal, nested-repository, ignored/generated-file, linked
     worktree, and non-Git cases fail safely.
   - Definitions may expose only explicitly trusted external roots, with
     correct read-only classification.
   - Conda `ms` site-packages and transformers remain non-editable.
   - `SCOPE_INCOMPATIBLE` fails closed without silently synthesizing an overlay
     or corrupting an existing binding.

3. **Semantic correctness and readiness**
   - UTF-16/UTF-8 offsets, CRLF, BOM, and non-BMP text are handled consistently
     across navigation, snippets, diagnostics, and edits.
   - Global search is limited to the current native configured program and
     verifies candidates rather than returning stale or cross-language data.
   - Program, inventory, document, and index generations cannot falsely report
     readiness after create/change/delete or configuration drift.
   - Diagnostics distinguish findings, clean, not-ready, stale, and timeout;
     TypeScript results remain explicitly advisory.
   - Capability gates reflect actual request semantics, especially definition
     versus declaration and implementation support.

4. **Guarded editing**
   - `replace_symbol_body` requires a current whole-file hash and exact,
     unambiguous symbol resolution under the workspace lock.
   - Authorization precedes mutation; external/read-only roots cannot be
     edited.
   - Atomic replacement preserves encoding, BOM, newline style, and mode, and
     cleans temporary files on every pre-install failure.
   - Transport loss, daemon loss, notification failure, or lost responses can
     never cause automatic edit replay. `UNCERTAIN` has honest semantics.
   - Result metadata identifies the actual adapter and language.

5. **Concurrency, transport, and lifecycle**
   - Lease UUID—not MCP HTTP session state—owns binding and lifetime.
   - Authentication, runtime-directory permissions, atomic discovery, daemon
     identity validation, and secret redaction resist local confusion or
     takeover within the stated threat model.
   - Heartbeats, status, release/expiry, ten-minute warm grace, daemon restart,
     connector crash, queue saturation, cancellation, circuit breaker, and
     cooldown cannot deadlock, leak children, reuse stale state, or starve
     unrelated roots.
   - Read-only retry is bounded and safe; edit retry is absent at every layer.
   - Synchronous LSP work cannot block the daemon event loop or create
     unbounded workers/queues.

6. **Dependency, provenance, packaging, and operational truth**
   - Runtime executables derive from the repository lock digest and cannot
     silently fall back to ambient `/root`, pip, npm, Node, or `PATH` state.
   - Copied Serena/SolidLSP mechanisms have accurate MIT provenance and the
     copied-source census/forbidden-import gate covers the real dependency
     closure.
   - Console entrypoints, installation/bootstrap, logs, status envelopes, and
     parallel client registrations agree with documentation.
   - Canonical Serena remains untouched, and rollback targets only the parallel
     Serena Light registration/processes.
   - Production LOC is reported honestly as information, while forbidden
     imports, direct dependency ownership, census/manifest agreement, copied
     hashes, and reference-commit verification remain release gates.

7. **Evidence quality**
   - Tests exercise the production path and consequential failure, not a mock
     or helper that restates the implementation.
   - Real-repository assertions are structural and robust to legitimate source
     evolution; flag hard-coded snapshots that create false failures or false
     confidence.
   - Fault tests prove cleanup/no-replay rather than merely observing an error
     code.
   - Acceptance reports accurately summarize current commands, versions,
     resource behavior, and residual risks.

## Verification

Run read-only checks as useful. The declared complete gate is:

```bash
cd /data/CoordExp/serena-light
uv run pytest -q tests
uv run ruff check src tests scripts
uv run ty check
uv run serena-light-bootstrap --check --json
uv run serena-light-source-budget --json
openspec validate build-serena-light-v1 --strict
```

Do not equate a green suite with a clean audit. Inspect test construction and
trace high-risk paths in source. You may run narrower reproductions or
read-only process/config inspection. Do not mutate shared client configuration,
kill live processes, edit source, stage files, commit, or create cleanup work
for other agents. If a destructive or stateful experiment would be needed,
describe it as a proposed reproduction instead of executing it.

## Finding standard

Report only actionable findings supported by repository evidence. For every
finding include:

- severity: `P0`, `P1`, `P2`, or `P3`;
- a concise defect title;
- exact file and tight line range;
- violated requirement or invariant;
- concrete failure scenario and impact;
- why existing tests do not catch it, if applicable;
- the smallest credible fix direction;
- a deterministic test or reproduction that would prove the fix.

Severity meanings:

- `P0`: immediate destructive compromise or pervasive unrecoverable failure.
- `P1`: common-path correctness/security failure, unsafe edit, cross-workspace
  contamination, or major lifecycle failure that blocks reliance.
- `P2`: real but bounded correctness, robustness, compatibility, or evidence
  gap that should be fixed before broader reliance.
- `P3`: low-impact issue with a concrete operational or maintenance cost.

Do not list style preferences, speculative abstractions, generic hardening, or
intentional non-goals as findings. Deduplicate issues by root cause. If evidence
is incomplete, label the item `QUESTION` rather than inflating severity. Do not
claim a defect merely because a stronger design is imaginable.

## Required output

Return one self-contained Markdown audit with this structure:

1. **Verdict** — `PASS`, `PASS WITH FIXES`, or `FAIL`, plus 3–6 sentences of
   calibrated rationale.
2. **System model checked** — a concise account of the actual architecture and
   the invariants you traced, demonstrating that the whole repository was
   understood.
3. **Findings** — ordered by severity, then confidence. Use `None` explicitly
   if no actionable findings survive scrutiny.
4. **Questions / unproven claims** — uncertainties that could not be resolved
   read-only, clearly separated from defects.
5. **Acceptance evidence assessment** — which current gates are persuasive,
   which are weak or redundant, and which consequential behavior lacks proof.
6. **Minimal remediation and re-acceptance plan** — only for surviving
   findings; name exact tests/gates needed to close them.
7. **Commands and artifacts inspected** — enough detail for another reviewer
   to reproduce your audit.

End with a compact count by severity and a direct answer to both questions:

- Is the parallel `serena-light` deployment safe enough to keep using now?
- Does the current evidence justify calling v1 implementation complete?

Stay read-only. Do not implement fixes. Do not defer the audit to another
agent. Continue until you have inspected the full declared surface and either
produced evidence-backed findings or justified why no actionable finding
remains.
