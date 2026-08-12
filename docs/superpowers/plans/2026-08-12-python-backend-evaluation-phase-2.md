# Python Backend Evaluation Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking. This plan is execution-only: it does not implement
> protocol/backend code itself, does not edit OpenSpec authority (`proposal.md`, `design.md`,
> `specs/`), does not launch a candidate server, and does not mutate any runtime or artifact.
> The implementing agent does both.

**Goal:** Implement and execute only OpenSpec Phase 2 (tasks 2.1–2.9): a shared,
production-shaped protocol-probe harness that starts locked Pyright, ty, and Pyrefly
directly, records initialize/capability/lifecycle/fault evidence through reused production
transport and process-launch code, proves zero workspace mutation, and publishes one
90-minute-bounded protocol-phase receipt with a typed per-candidate gate outcome — without
creating a production backend registry, without touching `src/serena_light`, and without
importing Phase 3's `WorkspaceRuntime`/`LanguageAdapter` scope.

**Precondition this plan does not resolve:** OpenSpec task 1.8 is currently unchecked and
`phase-1-acceptance.md` states Phase 1 stays on HOLD pending two independent re-reviews of
its admitting run (evaluator HEAD `2503f85`, evaluation identity
`35b85d4e…d334`). This plan may be authored and reviewed now, but **no task below may
execute against a real candidate runtime until those two re-reviews close task 1.8** — Task
1 (interfaces/schema) and static-only steps have no such dependency and may proceed, but
every task that launches `scripts.backend_eval.runtime.prepare_candidate_runtime` or a real
subprocess must first confirm task 1.8 is checked. Record this as an explicit precondition
check at the start of Task 2.

**Architecture:** Phase 2 code lives under `scripts/backend_eval/` (flat modules, matching
Phase 1's layout) and tests under `tests/backend_eval/`; production code never imports
Phase 2 modules. Phase 2 reuses, unmodified: `serena_light.lsp.client.SyncLspClient`,
`serena_light.lsp.adapter.{RawLspProviders, SubprocessAdapterRuntimeProvider, AdapterRuntime}`,
`serena_light.processes.{LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback}`,
`serena_light.lsp.pyright.PyrightFacts`, and Phase 1's
`scripts.backend_eval.{process.Deadline, runtime.CandidateRuntime, runtime.minimal_backend_environment,
manifests, write_guard, identity.capture_evaluator_identity, production_identity, models}`.
Phase 2 does **not** import `serena_light.workspace.runtime.WorkspaceRuntime` or
`serena_light.lsp.adapter.LanguageAdapter` — those are Phase 3's product-seam scope
(`design.md` Decision 1, plane 2) and importing them here would smuggle document
lifecycle/freshness/lease behavior into a plane whose own requirement (design.md Decision 1,
plane 1) is to start each backend directly.

One shared foundation task (protocol interface + strict capability-receipt schema) is
sequential. A production-shaped Pyright vertical slice is next, sequential, and gates all
broader candidate work — Pyright is production's own already-integrated backend, so proving
the shared runner, receipt schema, and write-guard integration against it first retires the
conclusion-changing risk (a defect in the shared harness itself, not in ty or Pyrefly) before
paying for two new untested candidates. Once that slice is independently reviewed, the ty and
Pyrefly protocol probes proceed in parallel disjoint worktrees, because their owned files do
not overlap. Per-candidate lifecycle/fault-test lanes follow the same disjointness rule, one
per surviving candidate. A single integrator task wires the shared write-guard and the
90-minute whole-phase orchestrator over all three candidates and publishes the Phase 2
receipt.

**Tech Stack:** Python 3.12 standard library, `serena_light.lsp.*`, `serena_light.processes`,
Phase 1 `scripts/backend_eval/*` modules, pytest 8.4 + `pytest-timeout` (repository default
`timeout = 30`; every real-subprocess protocol/lifecycle test declares an explicit
`@pytest.mark.timeout(N)` above that default), the existing `external_repo`
snapshot-marker gate in `tests/conftest.py`, Ruff, Ty, OpenSpec.

## Global Constraints

- OpenSpec change `openspec/changes/evaluate-python-language-backends/` is the sole authority
  for scope and acceptance. This plan changes no file under `openspec/`, `specs/`, or
  `design.md`/`proposal.md`; the only OpenSpec-owned edit permitted anywhere in the
  implementation of this plan is checking OpenSpec checkboxes 2.1–2.9 after a real passing
  receipt, exactly as Phase 1's Task 6 checked 1.1–1.8.
- Use `conda run -n ms <command>` for repository Python commands.
- Create no production backend registry; import no `scripts.backend_eval` module from
  `src/serena_light`; import no `serena_light.workspace.runtime` or
  `serena_light.lsp.adapter.LanguageAdapter` symbol from `scripts/backend_eval` (Phase 3
  scope — see Decision P2-1 below).
- Do not modify `pyproject.toml`, `uv.lock`, `package-lock.json`, `src/serena_light`,
  `openspec/specs`, production bootstrap, client registrations, canonical Serena, or the
  installed Serena Light MCP.
- Do not launch a candidate server outside a test process, do not mutate any evaluated
  workspace path outside a declared disposable fixture, and do not run this plan's real
  subprocess tasks against runtimes/artifacts as this session — that is the implementing
  agent's job, gated by the precondition above.
- Candidate processes receive `minimal_backend_environment(...)`: no ambient `PATH`, no
  inherited `PYTHONPATH`, no `*_PROXY` variable. Reuse it; do not hand-build a second minimal
  environment.
- Every phase proves `pyproject.toml`, `uv.lock`, `package-lock.json`,
  `dependency_lock_digest`, `compute_build_identity`, and production `runtime_paths`
  unchanged, exactly as Phase 1 does via `production_identity.py`.
- Protocol active wall time is at most 90 minutes (`DEFAULT_PHASE_BUDGETS["protocol"]` is
  already frozen at `90 * 60` in `models.py`); timeout or unstable input fails closed with
  cleanup and an incomplete receipt, using the same `Deadline`/reserve/finalization pattern as
  `admission.py`.
- Raw protocol transcripts and process samples remain ignored under
  `.admission-artifacts/backend-eval/<evaluation-identity>/`; committed evidence is a bounded
  summary in `phase-2-acceptance.md` plus receipt digests, never raw source or full
  transcripts.
- No task may install, migrate, release, or publish a production backend. A successful Phase
  2 only admits Phase 3 planning for the candidates that pass; a failed candidate is removed
  from every later phase (OpenSpec 2.9) rather than silently retried.
- Do not invent receipt fields beyond what OpenSpec tasks 2.1–2.9 and the spec's "Hard gates
  precede feature and efficiency comparison" and "Every receipt binds its evaluator, host,
  environment, and runtime" requirements name. In particular, do not fabricate a Phase 4
  "future-operation utility" value here — see Decision P2-3.

---

## Decision P2-1: Protocol plane reuses process/transport primitives, not WorkspaceRuntime

`design.md` Decision 1 defines the protocol plane as starting "each locked backend directly
with service-owned environment/configuration" — a lower plane than the product-seam plane,
which is explicitly the one that "injects evaluation-only attributors and adapter factories
into the existing `WorkspaceRuntime`." The task prompt names
`SubprocessAdapterRuntimeProvider` as reusable infrastructure; it is process-launch and
transport wiring only (`LanguageServerSubprocessLauncher`, `SyncLspClient`, stderr capture,
`terminate_process_tree_with_kill_fallback`) with no document lifecycle, freshness, lease, or
scope-generation state — those live in `LanguageAdapter`/`WorkspaceRuntime`, which are Phase
3's seam. Phase 2 therefore calls `SubprocessAdapterRuntimeProvider.start()`/`.stop()`
directly (through the shared `run_protocol_probe` runner in Task 1) and never constructs a
`LanguageAdapter` or `WorkspaceRuntime`. This satisfies "reuse production LSP transport and
process-launcher behavior" and "Do not import Phase3 WorkspaceRuntime/LanguageAdapter scope
into Phase2" simultaneously, because they name two different layers of the same file.

## Decision P2-2: `RequestCancelled` is observed and counted, never client-issued

Two readings were possible: (a) build raw client-side `$/cancelRequest` because
`SyncLspClient` hides request IDs from callers, or (b) have task 2.6 verify only
server-returned `-32800` with no special retry. This plan adopts (b), for four independently
sufficient reasons visible in the current code and authority:

1. `SyncLspClient` hiding request IDs (`self._next_id` is private, incremented per call, and
   never returned to the caller — `src/serena_light/lsp/client.py:196-210`) is a stated
   design invariant of the shared production client, not an accidental gap. Building a raw
   `$/cancelRequest` path would require either reaching into that private state from
   evaluation code or duplicating request-ID bookkeeping outside the reused client — both are
   exactly the kind of Phase-2-owned fork of production protocol logic the task prompt says to
   avoid ("Reuse production SyncLspClient").
2. `design.md` Decision 5 states only: "The existing `LspClient.set_content_modified_retry_methods`
   seam remains disabled for all arms, so every request has one backend attempt; the harness
   may not add hidden retry loops. Protocol receipts separately count `ContentModified` and
   `RequestCancelled` outcomes." This names counting, not client-initiated cancellation, and
   explicitly forbids adding retry/cancellation machinery beyond what production already has.
3. OpenSpec task 2.6 lists `RequestCancelled` in the same clause as "the identically disabled
   production retry seam" — grouping it with an assert-unchanged-behavior scenario, not with a
   new capability to add.
4. `-32800` is the code a server returns when *it* cancels a request (for example, superseded
   diagnostics or an in-flight request outstanding at shutdown); nothing in the frozen
   contract or spec requires the harness to be the party that triggers cancellation via
   `$/cancelRequest`. CLAUDE.md forbids inventing schema/utility beyond current authority.

Task 2.6 therefore drives real subprocess scenarios that are known to make each candidate
return `-32800` on its own initiative — the two production-shaped triggers are (i) issuing a
request and then `shutdown()`-ing the client while it is still in flight, and (ii) issuing two
overlapping document-symbol requests against the same document version, which Pyright and
similar servers commonly self-cancel the superseded one. The receipt records the observed
`LspResponseError.code == -32800` count per candidate; it never sends `$/cancelRequest` and
never adds a retry.

## Decision P2-3: `task_utility` is a fixed disposition field in Phase 2, not fabricated data

OpenSpec 2.5 requires a capability receipt schema that "separates initialize advertisement,
accepted request, valid normalized result, and real-task utility." Real-task utility is
Phase 4's decision-owning evidence (spec.md "Future capabilities are tested as closed Agent
operations"), which Phase 2 does not run. `CapabilityEvidence.task_utility` is therefore a
frozen literal, `"deferred_to_feature_phase"`, on every Phase 2 record — the field exists (so
the schema is structurally ready for Phase 4 to populate it) but never carries a fabricated
Phase 2 value. `AdmissionReceipt.from_dict` already rejects unknown fields on parsing
(Phase 1 Task 1); the same closed-field discipline applies to every new Phase 2 model.

---

## Execution Topology

```text
Task 1: protocol interface + capability-receipt schema (sequential, foundation)
        |
        v
Task 2: Pyright protocol probe -- production-shaped vertical slice (sequential)
        |  independent task review; STOP here if the vertical slice does not pass
        |
        +--> Task 3: ty protocol probe -----------+--> Task 5: pyright lifecycle/fault --+
        |    (disjoint worktree)                  |    (disjoint worktree)               |
        |                                          |                                       |
        +--> Task 4: pyrefly protocol probe -------+--> Task 6: ty lifecycle/fault --------+--> Task 8: write-guard +
             (disjoint worktree)                        (disjoint worktree)                    90-minute orchestrator
                                                          |                                      (sole integrator)
                                                          +--> Task 7: pyrefly lifecycle/fault --+
                                                               (disjoint worktree)
```

- Task 1 is sequential and freezes the exact interfaces and schema every later task consumes.
- Task 2 is sequential and is the production-shaped vertical slice: it proves the shared
  runner, receipt schema, real-subprocess launch/cleanup, and write-guard integration work
  end to end against Pyright — the one candidate with a known-good production reference —
  before ty and Pyrefly (two new dependencies with unknown runtime behavior) are touched.
  Task 2 requires its own independent task review before Task 3/4 begin.
- Tasks 3 and 4 may run in parallel only in separate worktrees, because `ty_probe.py` and
  `pyrefly_probe.py` are disjoint files that both only import Task 1's frozen interface and
  Task 2's reviewed runner (read-only import, not edited by either).
- Tasks 5, 6, and 7 (per-candidate lifecycle/fault tests) may each start as soon as their own
  candidate's probe task is merged and reviewed: Task 5 may start immediately after Task 2
  merges (in parallel with Tasks 3/4), Task 6 after Task 3 merges, Task 7 after Task 4 merges.
  Each owns a disjoint test file and touches no other candidate's probe module.
- Task 8 is the sole integrator and runs after Tasks 1–7 pass their task reviews. It adds the
  shared write-guard bracketing and the 90-minute orchestrator, and is the only task that
  constructs the final `ProtocolPhaseReceipt`.
- Each parallel writer receives the same frozen Task 1 (and, for Tasks 3–7, Task 2) reviewed
  commit. The lead cherry-picks only reviewed commits into the owning feature worktree
  (`serena-light-backend-eval-phase2`) and runs the cross-lane verifier (Task 8, Step 6) after
  every lane lands.

### Worktree/branch topology

All worktrees branch from the Phase 1-complete commit on `serena-light-backend-eval-phase2`
(the owning feature worktree/branch for this plan's implementation, distinct from this
planning worktree `serena-light-backend-eval-phase2-plan`):

| Task | Worktree | Branch | Depends on (reviewed commit) |
|---|---|---|---|
| 1 | `serena-light-backend-eval-p2-task1` | `codex/backend-eval-p2-task1` | Phase 1 HEAD |
| 2 | `serena-light-backend-eval-p2-task2` | `codex/backend-eval-p2-task2` | Task 1 |
| 3 | `serena-light-backend-eval-p2-task3` | `codex/backend-eval-p2-task3` | Task 2 |
| 4 | `serena-light-backend-eval-p2-task4` | `codex/backend-eval-p2-task4` | Task 2 |
| 5 | `serena-light-backend-eval-p2-task5` | `codex/backend-eval-p2-task5` | Task 2 |
| 6 | `serena-light-backend-eval-p2-task6` | `codex/backend-eval-p2-task6` | Task 3 |
| 7 | `serena-light-backend-eval-p2-task7` | `codex/backend-eval-p2-task7` | Task 4 |
| 8 | `serena-light-backend-eval-phase2` (feature worktree) | `codex/backend-eval-phase2` | Tasks 1–7 |

Each task worktree is deleted after its commit is cherry-picked into
`serena-light-backend-eval-phase2` and its own tests pass there; no worktree is deleted while
its commit is still the only copy.

### Integration order

1. Merge Task 1 into the feature worktree; run `tests/backend_eval/test_protocol.py` and
   `test_capability_receipts.py` there.
2. Merge Task 2; run the Pyright vertical-slice suite there; obtain independent task review
   before continuing.
3. Merge Task 3 and Task 4 (either order — disjoint files); run each candidate's own test
   module after its merge.
4. Merge Task 5 (can happen any time after step 2, even interleaved with step 3); merge Task
   6 after Task 3; merge Task 7 after Task 4.
5. Merge Task 8; run the complete `tests/backend_eval` suite plus the Phase 1 regression
   suite (`tests/unit/test_build_identity.py`, `tests/unit/test_bootstrap.py`,
   `tests/unit/test_workspace_inventory.py`, `tests/unit/test_workspace_identity.py`) to prove
   Phase 2 did not regress Phase 1 invariants.
6. Run the real protocol-phase command (Task 8, Step 6) only after task 1.8 is checked.
7. Phase 2 Final Review (below).

---

### Task 1: Protocol interface, shared runner, and capability-receipt schema

**OpenSpec coverage:** 2.1, 2.5 (schema only; 2.5's real evidence is populated by Tasks 2–4)

**Files:**
- Create: `scripts/backend_eval/protocol.py`
- Modify: `scripts/backend_eval/models.py` (add capability-receipt and protocol-phase models;
  this task owns freezing them, per Phase 1's precedent that the task which first needs a
  receipt shape freezes it)
- Create: `tests/backend_eval/test_protocol.py`
- Create: `tests/backend_eval/test_capability_receipts.py`

**Interfaces:**
- Produces frozen `BackendProtocolSpec(name, build_command, initialize_params, request_handlers, engine, position_encoding, diagnostics_mode)`
  where `build_command: Callable[[CandidateRuntime], tuple[str, ...]]`,
  `initialize_params: Callable[[Path], Mapping[str, object]]`,
  `request_handlers: Mapping[str, Callable[[Any], Any]] | None`,
  `engine: Callable[[CandidateRuntime], serena_light.lsp.adapter.EngineMetadata]`,
  `diagnostics_mode: Literal["push", "pull"]`.
- Produces: `run_protocol_probe(spec: BackendProtocolSpec, runtime: CandidateRuntime, workspace_root: Path, *, deadline: Deadline, session: Callable[[SyncLspClient], T]) -> ProtocolSession[T]`,
  a context-managed wrapper around `SubprocessAdapterRuntimeProvider.start()`/`.stop()` that:
  launches through `LanguageServerSubprocessLauncher.get_instance()`, sends `initialize`
  bounded by `deadline.remaining()`, captures `RawLspProviders.from_initialize_result(...)`,
  sends `initialized`, runs the caller's `session(client)` under the same deadline, then
  always calls `client.shutdown(...)` and `SubprocessAdapterRuntimeProvider.stop(...)` even on
  exception, and never swallows a `deadline.expired()` observation.
- Produces new closed-field frozen dataclasses in `models.py`:
  `CapabilityEvidence(name, advertised, accepted, normalized_valid, task_utility, notes)`
  with `task_utility` fixed to the literal `"deferred_to_feature_phase"` (Decision P2-3);
  `LifecycleEvidence(cold_readiness_seconds, diagnostics_mode, content_modified_count,
  request_cancelled_count, retry_seam_disabled, bounded_timeout_observed, crash_handled,
  shutdown_clean, cleanup_clean, proxy_rejected, minimal_environment_verified, redaction_verified)`;
  `CandidateProtocolOutcome(candidate, engine_version, raw_providers, capabilities, lifecycle, gate_disposition, issues)`
  with `gate_disposition` closed to `{"pass", "fail", "seam_incompatible_pull_only"}`;
  `ProtocolPhaseReceipt(schema_version, evaluation_contract_version, evaluation_identity,
  run_identity, status, started_at, ended_at, budgets, evaluator, production_identity_before,
  production_identity_after, candidate_lock, runtime_binding, root_manifests_before,
  root_manifests_after, write_deltas, outcomes, issues, artifact_tree_digest, next_action)`
  mirroring `AdmissionReceipt`'s `to_dict`/`from_dict`/closed-field/canonical-order discipline
  exactly, reusing `canonical_json`, `sha256_bytes`, `_validate_*` helpers already in
  `models.py` rather than re-implementing validation.
- All later tasks consume these exact names; no later task may introduce a second protocol
  receipt/session representation.

- [ ] **Step 1: Write failing interface and schema tests**

```python
def test_build_command_receives_the_prepared_runtime(fake_runtime: CandidateRuntime) -> None:
    spec = BackendProtocolSpec(
        name="pyright",
        build_command=lambda runtime: (str(runtime.python), "--version"),
        initialize_params=lambda root: {"rootUri": root.as_uri()},
        request_handlers=None,
        engine=lambda runtime: EngineMetadata(name="pyright", version="1.1.403", executable=runtime.python),
        position_encoding=PositionEncoding.UTF16,
        diagnostics_mode="push",
    )
    assert spec.build_command(fake_runtime) == (str(fake_runtime.python), "--version")


def test_capability_evidence_fixes_task_utility_and_rejects_override() -> None:
    evidence = CapabilityEvidence(
        name="implementation", advertised=False, accepted=None, normalized_valid=None,
        task_utility="deferred_to_feature_phase", notes="ty 0.x does not advertise textDocument/implementation",
    )
    assert evidence.task_utility == "deferred_to_feature_phase"
    with pytest.raises(ValueError, match="task_utility"):
        CapabilityEvidence(name="implementation", advertised=False, accepted=None,
                            normalized_valid=None, task_utility="improves_task_x", notes="")


def test_protocol_phase_receipt_pass_requires_frozen_protocol_budget() -> None:
    with pytest.raises(ValueError, match="protocol"):
        ProtocolPhaseReceipt(..., budgets=(PhaseBudget("protocol", 60 * 60),), status="pass", ...)


def test_candidate_protocol_outcome_disposition_is_closed() -> None:
    with pytest.raises(ValueError, match="gate_disposition"):
        CandidateProtocolOutcome(candidate="ty", ..., gate_disposition="mostly_pass")
```

- [ ] **Step 2: Run the tests and verify the missing module/name failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_protocol.py tests/backend_eval/test_capability_receipts.py`

Expected: FAIL because `scripts.backend_eval.protocol` does not exist and `models.py` does
not yet export the new names.

- [ ] **Step 3: Implement the interface and models**

Implement `BackendProtocolSpec` and `run_protocol_probe` in `protocol.py`, importing only
`serena_light.lsp.{client,adapter}`, `serena_light.processes`, and Phase 1's
`scripts.backend_eval.{process,runtime}` — no `serena_light.workspace.*` import (Decision
P2-1; add a static test asserting this, e.g. via `ast`-parsing `protocol.py`'s imports).
Add the new dataclasses to `models.py` beside `AdmissionReceipt`, following its exact
`@dataclass(frozen=True, slots=True)` + `__post_init__` + `to_dict`/`from_dict` pattern; reuse
`_validate_tuple`, `_validate_sorted_unique`, `_validate_sha256`, `_validate_non_empty_str`,
`_validate_utc_timestamp`, `default_phase_budgets` rather than duplicating them.

- [ ] **Step 4: Run focused tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_protocol.py tests/backend_eval/test_capability_receipts.py tests/backend_eval/test_models.py`

Expected: PASS (the existing `test_models.py` regression proves Task 1 did not disturb
Phase 1's frozen models).

- [ ] **Step 5: Run static checks**

Run: `conda run -n ms ruff check scripts/backend_eval/protocol.py scripts/backend_eval/models.py tests/backend_eval/test_protocol.py tests/backend_eval/test_capability_receipts.py`

Run: `conda run -n ms ty check scripts/backend_eval/protocol.py scripts/backend_eval/models.py tests/backend_eval/test_protocol.py tests/backend_eval/test_capability_receipts.py`

- [ ] **Step 6: Commit Task 1**

```bash
git add scripts/backend_eval/protocol.py scripts/backend_eval/models.py \
  tests/backend_eval/test_protocol.py tests/backend_eval/test_capability_receipts.py
git commit -m "Add Phase 2 protocol interface and capability-receipt schema"
```

- [ ] **Step 7: Independent task review**

Dispatch one reviewer over Task 1 alone: confirm no `serena_light.workspace` import, confirm
every new model follows the closed-field/canonical-order contract, confirm `task_utility` is
structurally un-overridable. Do not proceed to Task 2 with an unresolved blocker.

---

### Task 2: Pyright protocol probe — production-shaped vertical slice

**OpenSpec coverage:** 2.2

**Precondition:** OpenSpec task 1.8 must be checked (Phase 1 admitting run independently
re-reviewed) before Step 5 (the only step that launches a real subprocess against a prepared
`CandidateRuntime`); Steps 1–4 and 6–7 do not require it.

**Files:**
- Create: `scripts/backend_eval/pyright_probe.py`
- Create: `tests/backend_eval/test_pyright_probe.py`

**Interfaces:**
- Consumes: Task 1 `BackendProtocolSpec`, `run_protocol_probe`, `CapabilityEvidence`;
  `serena_light.lsp.pyright.PyrightFacts.locked(...)`.
- Produces: `pyright_protocol_spec(facts: PyrightFacts) -> BackendProtocolSpec` — builds the
  spec directly from the already-locked production `PyrightFacts` (`facts.command`,
  `facts.initialize_params`, `facts.workspace_configuration` as the `workspace/configuration`
  request handler, `facts.adapter_language_facts(...).engine`), so Pyright's protocol facts
  are never re-derived by evaluation code.
- Produces: `run_pyright_capability_probe(facts: PyrightFacts, workspace_root: Path, target: Path, symbol_position: tuple[int, int], *, deadline: Deadline) -> CandidateProtocolOutcome`
  — opens the target document, requests `textDocument/definition`, `textDocument/references`,
  `textDocument/implementation`, `textDocument/documentSymbol`, `workspace/symbol`, parses
  each result with production's `serena_light.lsp.normalize` location parsing (reused, not
  reinvented), and returns one `CandidateProtocolOutcome` with `gate_disposition="pass"` only
  if every advertised capability that was exercised returned a normalized-valid result.

- [ ] **Step 1: Write failing fixture-corpus and capability tests**

Reuse the existing fixture pattern from `tests/integration/test_pyright_adapter_real.py`
(`MS_SWIFT`, `TRANSFORMERS_ROOT`, `pytest.mark.external_repo(...)`), scoped to a small,
already-known-good symbol (the same class/function the existing real Pyright test already
resolves, so the assertion is not a new unverified fixture).

```python
pytestmark = [
    pytest.mark.timeout(90),
    pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT"),
]


def test_pyright_capability_probe_reports_advertised_and_normalized_definition() -> None:
    facts = PyrightFacts.locked(interpreter=MS_INTERPRETER)
    outcome = run_pyright_capability_probe(
        facts, MS_SWIFT, target=KNOWN_FILE, symbol_position=KNOWN_POSITION,
        deadline=Deadline.start(monotonic_clock, 90.0),
    )
    definition = next(c for c in outcome.capabilities if c.name == "definition")
    assert definition.advertised is True
    assert definition.accepted is True
    assert definition.normalized_valid is True
    assert outcome.gate_disposition == "pass"


def test_pyright_probe_leaves_no_write(ms_swift_manifest_before) -> None:
    run_pyright_capability_probe(...)
    assert capture_root_manifest(ms_swift_request) == ms_swift_manifest_before
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyright_probe.py`

Expected: FAIL because `scripts.backend_eval.pyright_probe` does not exist.

- [ ] **Step 3: Implement the Pyright spec and capability probe**

Build `BackendProtocolSpec` from `PyrightFacts` without copying its command/initialize-params
logic. Call `run_protocol_probe` from Task 1 for process lifecycle. Parse each LSP result
using production's `serena_light.lsp.normalize` helpers (do not hand-write a second location
parser). Record `LspResponseError` codes so a later lifecycle test can assert on them.

- [ ] **Step 4: Run focused and Phase 1 regression tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyright_probe.py tests/backend_eval/test_protocol.py`

Expected: PASS.

- [ ] **Step 5: Run the real vertical-slice probe once (gated on task 1.8)**

```bash
conda run -n ms python -c "
from pathlib import Path
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.pyright_probe import run_pyright_capability_probe
from serena_light.lsp.pyright import PyrightFacts
from serena_light.workspace.identity import MS_INTERPRETER
outcome = run_pyright_capability_probe(
    PyrightFacts.locked(interpreter=MS_INTERPRETER),
    Path('/data/ms-swift'), target=..., symbol_position=...,
    deadline=Deadline.start(monotonic_clock, 90.0),
)
print(outcome.gate_disposition, [c.name for c in outcome.capabilities if c.advertised])
"
```

Expected: `pass`, and `git status --short` in the repo root shows no change and
`capture_root_manifest` before/after the call is byte-identical. If this step fails, this is
the falsification signal for the whole shared harness — **stop and do not start Task 3/4**;
repair Task 1/2 first, because ty and Pyrefly would inherit the same defect.

- [ ] **Step 6: Run static checks and commit Task 2**

Run: `conda run -n ms ruff check scripts/backend_eval/pyright_probe.py tests/backend_eval/test_pyright_probe.py`

Run: `conda run -n ms ty check scripts/backend_eval/pyright_probe.py tests/backend_eval/test_pyright_probe.py`

```bash
git add scripts/backend_eval/pyright_probe.py tests/backend_eval/test_pyright_probe.py
git commit -m "Add Pyright protocol probe as the Phase 2 vertical slice"
```

- [ ] **Step 7: Independent task review — required before Task 3/4 start**

Dispatch one reviewer confirming: the real Step 5 run is genuine (not mocked), zero workspace
mutation, correct reuse of `PyrightFacts`/`normalize`/`run_protocol_probe`, and no
`WorkspaceRuntime`/`LanguageAdapter` import. Task 3 and Task 4 worktrees may only branch from
this reviewed commit.

---

### Task 3: ty protocol probe

**OpenSpec coverage:** 2.3

**Files:**
- Create: `scripts/backend_eval/ty_probe.py`
- Create: `tests/backend_eval/test_ty_probe.py`

**Interfaces:**
- Consumes: Task 1 interfaces; Task 2's `run_protocol_probe` usage pattern (read-only
  reference, not a shared import beyond Task 1); Phase 1 `CandidateRuntime.ty`,
  `minimal_backend_environment`.
- Produces: `ty_protocol_spec(runtime: CandidateRuntime, service_config: ServiceConfigIdentity) -> BackendProtocolSpec`
  — command is `(str(runtime.ty), "server")` (or ty's documented LSP subcommand; confirmed
  against the locked `ty --help`/`--version` output captured in Phase 1's runtime manifest,
  not assumed), explicit service-owned config path and interpreter from `runtime.config` /
  `runtime.python`, no `workspace/configuration` handler unless ty's initialize result
  requests one (verified, not assumed).
- Produces: `run_ty_capability_probe(runtime: CandidateRuntime, workspace_root: Path, target: Path, symbol_position: tuple[int, int], *, deadline: Deadline) -> CandidateProtocolOutcome`
  — identical shape to Task 2's Pyright probe, plus one explicit negative record: if
  `raw_providers.implementation` is `False`, append a `CapabilityEvidence(name="implementation",
  advertised=False, accepted=None, normalized_valid=None, task_utility="deferred_to_feature_phase",
  notes="locked ty version does not advertise textDocument/implementation")` rather than
  omitting the capability (OpenSpec 2.5's explicit negative-record requirement).

- [ ] **Step 1: Write failing capability and negative-implementation tests**

```python
def test_ty_probe_records_explicit_negative_implementation_when_unadvertised(locked_ty_runtime) -> None:
    outcome = run_ty_capability_probe(locked_ty_runtime, MS_SWIFT, KNOWN_FILE, KNOWN_POSITION, deadline=...)
    implementation = next(c for c in outcome.capabilities if c.name == "implementation")
    if not outcome.raw_providers.implementation:
        assert implementation.advertised is False
        assert implementation.accepted is None
        assert "does not advertise" in implementation.notes


def test_ty_probe_uses_service_owned_config_and_minimal_environment(locked_ty_runtime) -> None:
    spec = ty_protocol_spec(locked_ty_runtime, service_config=...)
    env = minimal_backend_environment(locked_ty_runtime, locked_ty_runtime.python)
    assert not any(k.upper().endswith("_PROXY") for k in env)
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_ty_probe.py`

Expected: FAIL because `scripts.backend_eval.ty_probe` does not exist.

- [ ] **Step 3: Implement the ty spec and capability probe**

Confirm ty's actual LSP invocation and initialize capabilities empirically against the
Phase-1-prepared runtime (`runtime.ty --help`, captured once, bounded by `run_bounded_bytes`)
before hard-coding the command; do not assume Pyright's `--stdio` flag applies.

- [ ] **Step 4: Run focused tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_ty_probe.py tests/backend_eval/test_protocol.py`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit Task 3**

Run: `conda run -n ms ruff check scripts/backend_eval/ty_probe.py tests/backend_eval/test_ty_probe.py`

Run: `conda run -n ms ty check scripts/backend_eval/ty_probe.py tests/backend_eval/test_ty_probe.py`

```bash
git add scripts/backend_eval/ty_probe.py tests/backend_eval/test_ty_probe.py
git commit -m "Add ty protocol probe"
```

- [ ] **Step 6: Independent task review**

---

### Task 4: Pyrefly protocol probe

**OpenSpec coverage:** 2.4

**Files:**
- Create: `scripts/backend_eval/pyrefly_probe.py`
- Create: `tests/backend_eval/test_pyrefly_probe.py`

**Interfaces:**
- Consumes: Task 1 interfaces; Phase 1 `CandidateRuntime.pyrefly`, `ServiceConfigIdentity`.
- Produces: `pyrefly_protocol_spec(runtime: CandidateRuntime, service_config: ServiceConfigIdentity) -> BackendProtocolSpec`
  — explicit external `configPath` in `initializationOptions` pointing at the service-owned
  config below `runtime.config`, workspace diagnostics configuration set to pull mode if that
  is what Pyrefly's initialize result advertises (verified against the real initialize
  response, recorded in `LifecycleEvidence.diagnostics_mode`), and no code path that lets
  Pyrefly write a fallback `pyrefly.toml` into the workspace (per `design.md` §Context: "Its
  Pyrefly adapter writes a fallback `pyrefly.toml` and retries mutation cancellations five
  times; neither behavior is compatible").
- Produces: `run_pyrefly_capability_probe(...) -> CandidateProtocolOutcome`, same shape as
  Task 2/3, plus an explicit assertion that no `pyrefly.toml` (or any file) was created inside
  `workspace_root` during the probe (spec.md "Pyrefly lacks workspace configuration" scenario).

- [ ] **Step 1: Write failing configuration-ownership and zero-write tests**

```python
def test_pyrefly_probe_supplies_external_config_and_never_writes_workspace(locked_pyrefly_runtime, ms_swift_manifest_before) -> None:
    outcome = run_pyrefly_capability_probe(locked_pyrefly_runtime, MS_SWIFT, KNOWN_FILE, KNOWN_POSITION, deadline=...)
    assert not (MS_SWIFT / "pyrefly.toml").exists()
    assert capture_root_manifest(ms_swift_request) == ms_swift_manifest_before


def test_pyrefly_probe_fails_the_candidate_if_it_attempts_config_creation(locked_pyrefly_runtime) -> None:
    with pytest.raises(PyreflyWorkspaceMutation):
        run_pyrefly_capability_probe(locked_pyrefly_runtime, hostile_workspace_root, ...)
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyrefly_probe.py`

Expected: FAIL because `scripts.backend_eval.pyrefly_probe` does not exist.

- [ ] **Step 3: Implement the Pyrefly spec and capability probe**

Confirm Pyrefly's real `initializationOptions.configPath` field name and diagnostics mode
empirically (do not assume LSP-community conventions); wrap the probe in a before/after
`capture_root_manifest` on `workspace_root` and raise a typed `PyreflyWorkspaceMutation` if
anything changed, independent of the shared write-guard integrated later in Task 8 (defense
in depth for the one candidate design.md already flags as workspace-mutation-prone).

- [ ] **Step 4: Run focused tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyrefly_probe.py tests/backend_eval/test_protocol.py`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit Task 4**

Run: `conda run -n ms ruff check scripts/backend_eval/pyrefly_probe.py tests/backend_eval/test_pyrefly_probe.py`

Run: `conda run -n ms ty check scripts/backend_eval/pyrefly_probe.py tests/backend_eval/test_pyrefly_probe.py`

```bash
git add scripts/backend_eval/pyrefly_probe.py tests/backend_eval/test_pyrefly_probe.py
git commit -m "Add Pyrefly protocol probe with workspace-config isolation"
```

- [ ] **Step 6: Independent task review**

---

### Task 5: Pyright lifecycle and fault tests

**OpenSpec coverage:** 2.6, 2.7 (Pyright only)

**Files:**
- Create: `tests/backend_eval/test_pyright_lifecycle.py`

**Interfaces:**
- Consumes: Task 2's `pyright_protocol_spec`, `run_protocol_probe` (Task 1), Task 1's
  `LifecycleEvidence`.
- Produces: no new production interface — this task is real-subprocess test coverage only,
  reusing Task 1/2's runner.

- [ ] **Step 1: Write failing lifecycle/fault tests**

```python
pytestmark = [pytest.mark.timeout(120), pytest.mark.external_repo(root=str(MS_SWIFT), snapshot_env="SERENA_LIGHT_MS_SWIFT_SNAPSHOT")]


def test_cold_readiness_never_reports_empty_success_as_ready() -> None: ...
def test_diagnostics_mode_is_recorded_push_for_pyright() -> None: ...
def test_content_modified_returns_the_documented_code_with_no_retry() -> None:
    # asserts client._retry_methods is empty (set_content_modified_retry_methods never called)
    ...
def test_request_cancelled_is_observed_via_shutdown_in_flight_and_overlapping_requests() -> None:
    # Decision P2-2: server-triggered only, no $/cancelRequest sent
    ...
def test_bounded_request_timeout_raises_typed_timeout_and_cleans_up() -> None: ...
def test_crash_is_detected_and_process_tree_is_fully_reaped() -> None: ...
def test_graceful_shutdown_leaves_no_process() -> None: ...
def test_proxy_variables_are_never_present_in_the_child_environment() -> None: ...
def test_minimal_environment_matches_minimal_backend_environment() -> None: ...
def test_stderr_and_environment_are_redacted_in_the_recorded_evidence() -> None: ...
```

- [ ] **Step 2: Run tests and verify each scenario currently fails or is unimplemented**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyright_lifecycle.py`

Expected: FAIL for any scenario not yet exercisable through Task 1/2's runner (for example, if
`LifecycleEvidence` capture is not yet wired into `run_protocol_probe`'s return value); if a
scenario needs a small runner addition (e.g., a bounded overlapping-request helper), add it to
`protocol.py` in this task since it is lifecycle-test infrastructure, not a new production
interface, and note the addition in the commit message.

- [ ] **Step 3: Make the scenarios pass against a real Pyright subprocess**

- [ ] **Step 4: Run focused and Task 1/2 regression tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_pyright_lifecycle.py tests/backend_eval/test_pyright_probe.py tests/backend_eval/test_protocol.py`

- [ ] **Step 5: Run static checks and commit Task 5**

Run: `conda run -n ms ruff check tests/backend_eval/test_pyright_lifecycle.py`

Run: `conda run -n ms ty check tests/backend_eval/test_pyright_lifecycle.py`

```bash
git add tests/backend_eval/test_pyright_lifecycle.py
git commit -m "Add Pyright protocol lifecycle and fault coverage"
```

- [ ] **Step 6: Independent task review**

---

### Task 6: ty lifecycle and fault tests

**OpenSpec coverage:** 2.6, 2.7 (ty only)

**Files:**
- Create: `tests/backend_eval/test_ty_lifecycle.py`

Same structure, steps, and scenario list as Task 5, built against Task 3's `ty_protocol_spec`.
One ty-specific addition: assert the explicit negative `implementation` capability record
(Task 3) is present in `LifecycleEvidence`'s paired `CandidateProtocolOutcome` when exercised
through a full lifecycle run, not only the isolated capability probe.

- [ ] **Step 1: Write failing lifecycle/fault tests** (mirrors Task 5, `ty` substituted)
- [ ] **Step 2: Run and verify failure** — `conda run -n ms pytest -q tests/backend_eval/test_ty_lifecycle.py`
- [ ] **Step 3: Make scenarios pass against a real ty subprocess**
- [ ] **Step 4: Run focused and Task 1/3 regression tests**
- [ ] **Step 5: Static checks and commit**

```bash
git add tests/backend_eval/test_ty_lifecycle.py
git commit -m "Add ty protocol lifecycle and fault coverage"
```

- [ ] **Step 6: Independent task review**

---

### Task 7: Pyrefly lifecycle and fault tests

**OpenSpec coverage:** 2.6, 2.7 (Pyrefly only)

**Files:**
- Create: `tests/backend_eval/test_pyrefly_lifecycle.py`

Same structure as Task 5/6, built against Task 4's `pyrefly_protocol_spec`. One
Pyrefly-specific addition, per `design.md` §Context and the spec's "Pyrefly lacks workspace
configuration" scenario: a fault test that the harness's service-owned `configPath` is
honored and that a deliberately hostile initialize (omitting `configPath`) is refused rather
than silently allowing Pyrefly to create its own config — this is a fault-injection test, not
new production interface.

- [ ] **Step 1: Write failing lifecycle/fault tests** (mirrors Task 5, `pyrefly` substituted, plus the config-omission fault case)
- [ ] **Step 2: Run and verify failure** — `conda run -n ms pytest -q tests/backend_eval/test_pyrefly_lifecycle.py`
- [ ] **Step 3: Make scenarios pass against a real Pyrefly subprocess**
- [ ] **Step 4: Run focused and Task 1/4 regression tests**
- [ ] **Step 5: Static checks and commit**

```bash
git add tests/backend_eval/test_pyrefly_lifecycle.py
git commit -m "Add Pyrefly protocol lifecycle and fault coverage"
```

- [ ] **Step 6: Independent task review**

---

### Task 8: Shared write-guard integration, 90-minute orchestrator, and Phase 2 gate

**OpenSpec coverage:** 2.8, 2.9

**Files:**
- Create: `scripts/backend_eval/protocol_phase.py`
- Create: `tests/backend_eval/test_protocol_phase.py`
- Create after a real run: `openspec/changes/evaluate-python-language-backends/phase-2-acceptance.md`
- Modify after verification: `openspec/changes/evaluate-python-language-backends/tasks.md`
  (check only 2.1–2.9)

**Interfaces:**
- Consumes: all of Tasks 1–7; Phase 1's `manifests.{default_corpus_requests,capture_root_manifest}`,
  `write_guard.{compare_root_manifests,assert_no_unexpected_writes}`,
  `identity.capture_evaluator_identity`, `production_identity.{capture_production_identity,assert_production_identity_unchanged}`,
  `process.{Deadline,DeadlineExceeded,acquire_exclusive_lock,monotonic_clock}`,
  `runtime.{prepare_candidate_runtime,CandidateRuntime}` (reused as *already prepared* by the
  Phase 1 admission run this Phase 2 run is chained from — Task 8 does not call
  `prepare_candidate_runtime` again if a valid runtime for the same lock digest already
  exists; it verifies and reuses it exactly as `admission.py`'s reuse path does).
- Produces: `run_protocol_phase(request: ProtocolPhaseRequest, *, clock: Clock = monotonic_clock) -> ProtocolPhaseReceipt`
- Produces CLI: `python -m scripts.backend_eval.protocol_phase --repo-root ABS --artifact-root ABS --runtime-base ABS --evaluation-identity SHA256`
  (the evaluation identity from the admitting Phase 1 receipt — Task 8 never resolves a
  second candidate lock; a second resolution inside protocol phase is a structural error,
  exactly as in Phase 1).
- Exit `0` only for canonical PASS-with-at-least-one-surviving-candidate; exit `2` for
  incomplete/hold; never print secrets or full source paths beyond declared roots.

- [ ] **Step 1: Write failing orchestration tests**

```python
def test_protocol_phase_brackets_the_whole_run_with_one_manifest_pair() -> None:
    receipt = run_protocol_phase(request, services=fakes_for_three_passing_candidates)
    assert tuple(m.root for m in receipt.root_manifests_before) == tuple(m.root for m in receipt.root_manifests_after)
    assert not any(delta.unexpected for delta in receipt.write_deltas)


def test_protocol_phase_removes_a_failed_candidate_from_the_outcome_but_not_the_receipt() -> None:
    receipt = run_protocol_phase(request, services=fakes_with_pyrefly_failing)
    pyrefly = next(o for o in receipt.outcomes if o.candidate == "pyrefly")
    assert pyrefly.gate_disposition == "fail"
    assert receipt.next_action == "begin_product_seam_planning_for_surviving_candidates"


def test_protocol_phase_seam_incompatible_pull_only_is_not_scored_as_failure_cause() -> None:
    receipt = run_protocol_phase(request, services=fakes_with_ty_pull_only)
    ty = next(o for o in receipt.outcomes if o.candidate == "ty")
    assert ty.gate_disposition == "seam_incompatible_pull_only"


def test_protocol_phase_90_minute_ceiling_publishes_a_trustworthy_timeout_receipt() -> None:
    receipt = run_protocol_phase(request, services=services_exceeding_deadline)
    assert receipt.status == "incomplete"
    assert services_exceeding_deadline.cleanup_called


def test_protocol_phase_rejects_a_second_candidate_lock_resolution() -> None: ...


def test_protocol_phase_receipt_pass_requires_the_frozen_90_minute_budget() -> None:
    with pytest.raises(ValueError, match="protocol"):
        ProtocolPhaseReceipt(..., budgets=(PhaseBudget("protocol", 60 * 60),), status="pass", ...)
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_protocol_phase.py`

Expected: FAIL because `scripts.backend_eval.protocol_phase` does not exist.

- [ ] **Step 3: Implement the orchestrator**

Capture production identity; verify (not re-prepare) the existing candidate runtime for the
given evaluation identity; capture the bounded corpus once before any candidate probe and
once after the last candidate's lifecycle/fault coverage, before cleanup and publication —
same before/after bracketing discipline as `admission.py`, reusing
`default_corpus_requests()`/`capture_root_manifest`/`compare_root_manifests` unmodified. Run
each candidate's capability probe (Tasks 2–4) then its lifecycle/fault battery (Tasks 5–7) in
turn, propagating `deadline.remaining()` into each; a candidate whose lifecycle battery
reveals a hard-gate violation (wrong-workspace evidence, stale diagnostics, unbounded
response, workspace mutation) is marked `"fail"` and removed from the `next_action`'s
implied Phase 3 candidate set, but its `CandidateProtocolOutcome` remains in the receipt
(falsifiability: a failed candidate's evidence is retained, never deleted). Construct the
final receipt with the same atomic-publish, per-run-identity-lock, deadline-checked-chunk
discipline as `admission.py`'s `_publish_receipt` (reuse that pattern; do not reimplement a
second publication primitive — if it is not already a shared helper, this task's first
sub-step is extracting it from `admission.py` into `scripts/backend_eval/publish.py` with a
regression test proving `admission.py`'s own publish path is unchanged byte-for-byte).

- [ ] **Step 4: Run the complete Phase 2 suite and static checks**

Run: `conda run -n ms pytest -q tests/backend_eval`

Run: `conda run -n ms ruff check scripts/backend_eval tests/backend_eval`

Run: `conda run -n ms ty check scripts/backend_eval tests/backend_eval`

Expected: all PASS, including every Phase 1 test file (`test_models.py`,
`test_production_identity.py`, `test_candidate_lock.py`, `test_runtime.py`,
`test_manifests.py`, `test_write_guard.py`, `test_admission.py`) unchanged and green — this is
the full cross-lane verification step.

- [ ] **Step 5: Run the Phase 1 regression suite**

Run: `conda run -n ms pytest -q tests/unit/test_build_identity.py tests/unit/test_bootstrap.py tests/unit/test_workspace_inventory.py tests/unit/test_workspace_identity.py`

Expected: PASS — proves Phase 2 did not disturb production identity invariants.

- [ ] **Step 6: Run the real protocol-phase command (gated on task 1.8)**

```bash
conda run -n ms python -m scripts.backend_eval.protocol_phase \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --evaluation-identity <the admitting Phase 1 evaluation_identity>
```

Expected: exit `0`, `status=pass`, three `CandidateProtocolOutcome` entries (pyright, ty,
pyrefly) each with a `gate_disposition`, no unexpected write deltas, production identities
equal, `next_action=begin_product_seam_planning_for_surviving_candidates` (or the equivalent
stop action if all non-Pyright candidates fail). If any step reaches the 90-minute ceiling
with usable evidence, expect `status=incomplete` with a published timeout receipt instead —
this is not a plan failure, it is the falsification/stop-rule path: do not retry inside the
same budget, disposition the timeout as backend/wrapper/infrastructure per spec.md, and stop
before Phase 3 planning until the lead determines the decision remains reachable.

- [ ] **Step 7: Verify repository and production identity after the real run**

Run: `git status --short`

Run: `conda run -n ms python -c 'from pathlib import Path; from serena_light.build_identity import compute_build_identity, dependency_lock_digest; print(dependency_lock_digest(Path.cwd())); print(compute_build_identity(Path.cwd()))'`

Compare both digests with the receipt's pre/post-evaluation values. Verify no
evaluation-owned child process remains (`ps` filtered by the recorded PIDs) and canonical
Serena/installed Serena Light registrations were untouched.

- [ ] **Step 8: Record bounded acceptance and check OpenSpec Phase 2 tasks**

Write `phase-2-acceptance.md` with: exact command, evaluation identity, per-candidate gate
dispositions and capability summaries (advertised/accepted/normalized counts, not raw
transcripts), `RequestCancelled`/`ContentModified` observed counts per candidate,
`seam_incompatible_pull_only` dispositions if any, artifact-tree digest, production identity
before/after, root manifest digests/counts, write-delta digests, elapsed time against the
5400-second budget, cleanup summary, and PASS/HOLD/incomplete disposition. Do not embed raw
protocol transcripts or source. Change only OpenSpec checkboxes 2.1–2.9 to `[x]` when the real
receipt passes with a trustworthy per-candidate disposition for every candidate; if any
candidate's gate cannot be trustworthy resolved, leave that checkbox unchecked and record why,
exactly as Phase 1's task 1.8 stayed on HOLD rather than being checked speculatively.

- [ ] **Step 9: Run the phase gate and commit Task 8**

Run: `openspec validate evaluate-python-language-backends --strict`

Run: `git diff --check`

Run: `conda run -n ms pytest -q tests/backend_eval`

```bash
git add scripts/backend_eval/protocol_phase.py scripts/backend_eval/publish.py \
  tests/backend_eval/test_protocol_phase.py \
  openspec/changes/evaluate-python-language-backends/phase-2-acceptance.md \
  openspec/changes/evaluate-python-language-backends/tasks.md
git commit -m "Complete backend evaluation protocol-phase gate"
```

---

## Phase 2 Final Review

- [ ] Generate one review package from the Phase 2 merge base (Phase 1 HEAD) through Task 8
  HEAD.
- [ ] Dispatch an Opus/xhigh lifecycle reviewer over shared-runner correctness, per-candidate
  process ownership and cleanup, the `RequestCancelled`/`ContentModified` decision (Decision
  P2-2), zero-write coverage across all three candidates, deadline/cleanup semantics, and
  receipt truthfulness including the failed-candidate-retained-not-deleted invariant.
- [ ] Dispatch a Sol/xhigh semantic reviewer over the capability-receipt schema
  (advertisement/accepted/normalized/utility separation and the fixed `task_utility`
  literal), the Pyright vertical-slice evidence, the ty negative-implementation record, the
  Pyrefly workspace-isolation evidence, and any false-`pass` path.
- [ ] If either review finds a blocker, run one bounded fix wave and one scoped re-review. Do
  not begin Phase 3 planning with an unresolved load-bearing finding.
- [ ] When both reviews pass, update the SDD ledger and create the Phase 3 implementation
  plan; do not install or expose a production candidate backend, and do not import Phase 3
  scope into this plan's own code.
