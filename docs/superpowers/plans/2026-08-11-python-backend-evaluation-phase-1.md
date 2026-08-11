# Python Backend Evaluation Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement and execute only OpenSpec Phase 1 (tasks 1.1–1.8): freeze typed evaluation contracts, an isolated ty/Pyrefly candidate lock/runtime, bounded corpus manifests, zero-write evidence, and a 30-minute admission receipt without changing Serena Light production identity.

**Architecture:** Evaluation-only modules live under `scripts/backend_eval/` and tests under `tests/backend_eval/`; production code never imports them and this plan does not modify `src/serena_light`, stable specs, project lockfiles, bootstrap, public tools, or client configuration. After one shared model task, two disjoint task pairs may run in parallel in separate Git worktrees (`candidate_lock` with `manifests`, then `runtime` with `write_guard`), followed by one admission integrator. Each task is independently committed and reviewed before integration into the owning feature worktree.

**Tech Stack:** Python 3.12 standard library, existing Serena Light inventory/build-identity APIs, explicit `/root/miniconda3/envs/ms/bin/uv` resolution with recorded executable hash, `uv pip compile`, `uv venv`, `uv pip sync`, Git plumbing, pytest 8.4, Ruff, Ty, OpenSpec.

## Global Constraints

- OpenSpec change `openspec/changes/evaluate-python-language-backends/` is the sole authority for scope and acceptance.
- Use `conda run -n ms <command>` for repository Python commands.
- Create no production backend registry and import no `scripts.backend_eval` module from `src/serena_light`.
- Do not modify `pyproject.toml`, `uv.lock`, `package-lock.json`, `src/serena_light`, `openspec/specs`, production bootstrap, client registrations, canonical Serena, or the installed Serena Light MCP.
- Candidate requirements, compiled lock, virtual environment, HOME, cache, configuration, and receipts live outside production lock inputs under `.admission-artifacts/backend-eval/<evaluation-identity>/` or `/data/CoordExp/.codex/runtime/serena-light/backend-eval/<candidate-lock-digest>/`.
- Pyright remains fixed at production version `1.1.403`; ty and Pyrefly resolve once to the highest non-yanked version that is not a PEP 440 pre-release, with eligible `0.0.x` ty releases allowed.
- Every phase proves `pyproject.toml`, `uv.lock`, `package-lock.json`, `dependency_lock_digest`, `compute_build_identity`, and production `runtime_paths` unchanged.
- Hash the lexical trust-inventory closure and declared fixture/config paths. Metadata-scan only declared remainder roots for path, type, symlink target, size, `mtime_ns`, inode, and hash their content only after metadata changes.
- Admission active wall time is at most 30 minutes; timeout or unstable input fails closed with cleanup and an incomplete receipt.
- Bootstrap download commands may use ambient external-network proxy settings. Candidate backend processes are not launched in this phase; later backend and loopback processes must receive no proxy variables.
- Raw receipts remain ignored under `.admission-artifacts/`; committed evidence contains only bounded summaries and digests.
- No task may install, migrate, release, or publish a production backend. A successful Phase 1 only admits Phase 2 planning.

---

## Execution Topology

```text
Task 1: shared schemas
        |
        +--> Task 2: candidate lock + production identity ---+--> Task 3: runtime prep ---+
        |                                                    |                         |
        +--> Task 4: corpus manifests ------------------------+--> Task 5: write guard ---+
                                                                                       |
                                                                 Task 6: admission CLI
```

- Task 1 is sequential and establishes exact interfaces.
- Tasks 2 and 4 may run in parallel only in separate worktrees because their owned files are disjoint.
- Tasks 3 and 5 may run in parallel only after Tasks 2 and 4 are integrated and in separate worktrees.
- Task 6 is the sole integrator and runs after Tasks 1–5 pass their task reviews.
- Each parallel writer receives the same frozen Task 1 commit. The lead cherry-picks only reviewed commits into the owning feature worktree and runs the cross-lane verifier afterward.

### Task 1: Canonical evaluation models and serialization

**OpenSpec coverage:** 1.1

**Files:**
- Create: `scripts/backend_eval/__init__.py`
- Create: `scripts/backend_eval/models.py`
- Create: `tests/backend_eval/__init__.py`
- Create: `tests/backend_eval/test_models.py`

**Interfaces:**
- Produces: `canonical_json(value: Mapping[str, object]) -> bytes`
- Produces: `sha256_bytes(value: bytes) -> str`
- Produces frozen dataclasses `PhaseBudget`, `ProductionIdentity`, `EnvironmentIdentity`, `ServiceConfigIdentity`, `ResolvedPackage`, `CandidatePackage`, `CandidateLock`, `PathRecord`, `RootManifest`, `WriteDelta`, `AdmissionReceipt`
- Produces: `AdmissionReceipt.to_dict() -> dict[str, object]`
- Produces: `AdmissionReceipt.from_dict(value: Mapping[str, object]) -> AdmissionReceipt`
- All later tasks consume these exact names; no later task may introduce a second receipt/manifest representation.

- [ ] **Step 1: Write failing canonicalization and validation tests**

```python
def test_canonical_json_is_sorted_utf8_and_newline_terminated() -> None:
    assert canonical_json({"z": 1, "é": [True, None]}) == b'{"z":1,"\xc3\xa9":[true,null]}\n'


def test_admission_receipt_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        AdmissionReceipt.from_dict({"schema_version": 999})


def test_phase_budgets_match_openspec() -> None:
    assert DEFAULT_PHASE_BUDGETS == {
        "admission": PhaseBudget("admission", 30 * 60),
        "protocol": PhaseBudget("protocol", 90 * 60),
        "product_seam": PhaseBudget("product_seam", 3 * 60 * 60),
        "feature": PhaseBudget("feature", 2 * 60 * 60),
        "agent": PhaseBudget("agent", 8 * 60 * 60),
        "total": PhaseBudget("total", 16 * 60 * 60),
    }
```

- [ ] **Step 2: Run the tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_models.py`

Expected: FAIL during import because `scripts.backend_eval.models` does not exist.

- [ ] **Step 3: Implement minimal frozen models with strict closed fields**

Use `@dataclass(frozen=True, slots=True)` for every record. `EnvironmentIdentity` contains environment name, configured interpreter path, resolved interpreter realpath, and interpreter version. `ServiceConfigIdentity` contains backend name, absolute service-owned config path and digest, HOME, and cache path. `ResolvedPackage` contains `name`, `version`, `requirement`, and `artifact_hashes` for every package in the compiled lock. `CandidatePackage` contains those same fields plus `executable_relpath`, and is used only for the direct `ty` and `pyrefly` candidates. `CandidateLock` contains the lock digest, resolution cutoff, all resolved packages, and exactly the two direct candidates. `ProductionIdentity` contains all three lockfile SHA-256 values, `dependency_lock_digest`, `build_identity`, and sorted production runtime paths. `PathRecord` includes its lexical disposition (`tracked`, `untracked`, `ignored`, or `declared`). `RootManifest` contains root, kind, Git source revision when applicable, inventory digest/count, fully hashed `PathRecord`s, metadata-only `PathRecord`s, and a manifest digest. A fully hashed record must have `content_sha256`; a metadata record normally omits it but may carry the required after-change digest when the write guard detects changed metadata. `WriteDelta` binds its result to exact before/after manifest digests. `AdmissionReceipt` contains schema version `1`, evaluation-contract version `python-backend-evaluation-v1`, evaluation identity, status, timestamps, canonically ordered budgets, production identity before/after, candidate lock, environment identities, service-config identities, separate canonically ordered before/after root-manifest collections, canonically ordered write deltas/issues, artifact-tree digest, and next action. A PASS requires identical root sets across both manifest collections and deltas, exact digest binding on both sides, and zero unexpected paths; declared disposable edits remain separately recorded.

Reject unknown or missing fields, mutable/non-tuple sequence inputs to frozen records, noncanonical SHA-256 values, duplicate or noncanonical package/path/receipt ordering, non-absolute roots, missing or malformed Git source revisions, invalid per-path dispositions, fully hashed records without content digests, unbound write deltas, a PASS containing unexpected paths, non-positive budgets, and a successful receipt whose before/after production identities differ.

- [ ] **Step 4: Run focused tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_models.py`

Expected: PASS.

- [ ] **Step 5: Run static checks for the new files**

Run: `conda run -n ms ruff check scripts/backend_eval/models.py tests/backend_eval/test_models.py`

Run: `conda run -n ms ty check scripts/backend_eval/models.py tests/backend_eval/test_models.py`

Expected: both PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add scripts/backend_eval/__init__.py scripts/backend_eval/models.py tests/backend_eval/__init__.py tests/backend_eval/test_models.py
git commit -m "Add backend evaluation receipt models"
```

### Task 2: Candidate lock and production-identity invariant

**OpenSpec coverage:** 1.2, 1.4

**Files:**
- Create: `scripts/backend_eval/production_identity.py`
- Create: `scripts/backend_eval/candidate_lock.py`
- Create: `tests/backend_eval/test_production_identity.py`
- Create: `tests/backend_eval/test_candidate_lock.py`

**Interfaces:**
- Consumes: Task 1 `CandidateLock`, `ResolvedPackage`, `CandidatePackage`, `ProductionIdentity`, `canonical_json`, `sha256_bytes`
- Produces: `capture_production_identity(repo_root: Path) -> ProductionIdentity`
- Produces: `assert_production_identity_unchanged(before: ProductionIdentity, after: ProductionIdentity) -> None`
- Produces: `compile_candidate_lock(request: CandidateLockRequest, *, runner: CommandRunner = subprocess_runner) -> CandidateLock`
- Produces frozen `CandidateLockRequest(repo_root, artifact_root, uv, python, exclude_newer)`
- `CommandRunner` accepts `Sequence[str]`, cwd, env and returns a small `CommandResult(returncode, stdout, stderr)`; this is the only subprocess injection seam.

- [ ] **Step 1: Write failing production-identity tests**

```python
def test_capture_production_identity_matches_runtime_functions(repo_root: Path) -> None:
    identity = capture_production_identity(repo_root)
    assert identity.dependency_lock_digest == dependency_lock_digest(repo_root)
    assert identity.build_identity == compute_build_identity(repo_root)
    assert dict(identity.runtime_paths) == {
        key: str(value) for key, value in sorted(runtime_paths(repo_root).items())
    }


def test_identity_guard_rejects_any_lock_or_runtime_change() -> None:
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        assert_production_identity_unchanged(before, replace(before, uv_lock_sha256="f" * 64))
```

- [ ] **Step 2: Write failing lock-command and parser tests**

Assert the exact command includes:

```text
<explicit uv> pip compile <artifact>/candidate-requirements.in
--output-file <artifact>/candidate-requirements.lock
--generate-hashes --no-annotate --no-header
--resolution highest --prerelease disallow --only-binary :all:
--python <explicit ms python> --no-sources --no-python-downloads
--exclude-newer <UTC timestamp>
```

The input file must contain exactly `ty` and `pyrefly`, one per line. Tests must reject missing hashes, editable/direct-URL requirements, duplicate packages, pre-release versions, unexpected direct packages, changed second freeze output, nonzero command exit, and production identity drift.

- [ ] **Step 3: Run tests and verify missing implementations**

Run: `conda run -n ms pytest -q tests/backend_eval/test_production_identity.py tests/backend_eval/test_candidate_lock.py`

Expected: FAIL because the two modules do not exist.

- [ ] **Step 4: Implement production identity capture using existing APIs**

Import `dependency_lock_digest` and `compute_build_identity` from `serena_light.build_identity`, and `runtime_paths` from `serena_light.bootstrap`. Hash `pyproject.toml`, `uv.lock`, and `package-lock.json` separately with guarded reads. Do not reimplement production identity logic.

- [ ] **Step 5: Implement one-shot lock compilation and parsing**

Write input and output only below the caller's ignored artifact root. Run the exact explicit command with a service-owned cache directory under the artifact root and bootstrap proxy inheritance unchanged. Parse the generated hash-locked requirements into `ResolvedPackage` entries for every resolved distribution. Populate exactly two `CandidatePackage` entries for the direct `ty` and `pyrefly` versions and expected `bin/ty` / `bin/pyrefly` paths. Hash the lock bytes into `CandidateLock.digest`. If a lock already exists, accept it only when recompilation is not requested and its canonical receipt matches exactly.

- [ ] **Step 6: Run focused tests and production-identity regression tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_production_identity.py tests/backend_eval/test_candidate_lock.py tests/unit/test_build_identity.py tests/unit/test_bootstrap.py`

Expected: PASS.

- [ ] **Step 7: Run static checks and commit Task 2**

Run: `conda run -n ms ruff check scripts/backend_eval/production_identity.py scripts/backend_eval/candidate_lock.py tests/backend_eval/test_production_identity.py tests/backend_eval/test_candidate_lock.py`

Run: `conda run -n ms ty check scripts/backend_eval/production_identity.py scripts/backend_eval/candidate_lock.py tests/backend_eval/test_production_identity.py tests/backend_eval/test_candidate_lock.py`

```bash
git add scripts/backend_eval/production_identity.py scripts/backend_eval/candidate_lock.py tests/backend_eval/test_production_identity.py tests/backend_eval/test_candidate_lock.py
git commit -m "Lock backend evaluation candidates safely"
```

### Task 3: Service-owned candidate runtime preparation

**OpenSpec coverage:** 1.3, 1.4

**Files:**
- Create: `scripts/backend_eval/runtime.py`
- Create: `tests/backend_eval/test_runtime.py`

**Interfaces:**
- Consumes: Task 1 models and Task 2 `capture_production_identity`, `assert_production_identity_unchanged`
- Produces frozen `CandidateRuntime(root, python, ty, pyrefly, lock_digest, executable_hashes, home, cache, config, environments, service_configs)`
- Produces: `prepare_candidate_runtime(lock: CandidateLock, request: RuntimeRequest, *, runner: CommandRunner = subprocess_runner) -> CandidateRuntime`
- Produces: `minimal_backend_environment(runtime: CandidateRuntime, selected_interpreter: Path) -> dict[str, str]`

- [ ] **Step 1: Write failing command, idempotency, and environment tests**

```python
def test_runtime_path_is_candidate_lock_content_addressed(tmp_path: Path) -> None:
    runtime = prepare_candidate_runtime(lock, request, runner=fake_runner)
    assert runtime.root == request.runtime_base / lock.digest


def test_backend_environment_is_minimal_and_proxy_free() -> None:
    env = minimal_backend_environment(runtime, Path("/root/miniconda3/envs/ms/bin/python"))
    assert set(env) == {"HOME", "PATH", "PYTHONPATH", "SERENA_LIGHT_SELECTED_PYTHON", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"}
    assert not any(key.upper().endswith("_PROXY") for key in env)
```

Also assert exact `uv venv` and `uv pip sync --require-hashes` commands, executable hash verification, configured interpreter versus realpath capture for `ms` and `llm-framework-study`, version capture through each explicit interpreter, service-owned config files and digests for `pyright`, `ty`, and `pyrefly`, no ambient PATH lookup, idempotent reuse only after full manifest verification, cleanup of a partially created runtime on failure, and unchanged production identity.

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_runtime.py`

Expected: FAIL because `scripts.backend_eval.runtime` does not exist.

- [ ] **Step 3: Implement runtime preparation**

Use the explicit `uv` and Python paths from `RuntimeRequest`. Create `<runtime-base>/<lock-digest>/venv`, `home`, `cache`, `config`, and `tmp` only. Install the compiled lock with hashes into the evaluation venv; validate `bin/ty` and `bin/pyrefly` are regular files within the runtime root and record their SHA-256 values and `--version` outputs. Resolve the manifest-declared `ms` and `llm-framework-study` interpreters without ambient PATH, retaining both configured path and realpath plus exact version. Materialize deterministic service-owned configuration files for `pyright`, `ty`, and `pyrefly` below the runtime config directory, record their digests and HOME/cache ownership as `ServiceConfigIdentity`, and never write configuration into a corpus root. Publish the runtime manifest with atomic `os.replace` plus directory fsync only after all verification succeeds.

- [ ] **Step 4: Run focused and production-identity tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_runtime.py tests/backend_eval/test_production_identity.py tests/unit/test_build_identity.py tests/unit/test_bootstrap.py`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit Task 3**

Run: `conda run -n ms ruff check scripts/backend_eval/runtime.py tests/backend_eval/test_runtime.py`

Run: `conda run -n ms ty check scripts/backend_eval/runtime.py tests/backend_eval/test_runtime.py`

```bash
git add scripts/backend_eval/runtime.py tests/backend_eval/test_runtime.py
git commit -m "Prepare isolated backend evaluation runtime"
```

### Task 4: Bounded corpus manifests

**OpenSpec coverage:** 1.5, 1.6

**Files:**
- Create: `scripts/backend_eval/manifests.py`
- Create: `tests/backend_eval/test_manifests.py`

**Interfaces:**
- Consumes: Task 1 `PathRecord`, `RootManifest`, `canonical_json`, `sha256_bytes`
- Produces frozen `RootManifestRequest(root, kind, fully_hashed_paths, metadata_roots, required_config_paths)`
- Produces: `default_corpus_requests() -> tuple[RootManifestRequest, ...]`
- Produces: `capture_root_manifest(request: RootManifestRequest) -> RootManifest`
- Produces: `freeze_default_corpus() -> tuple[RootManifest, ...]`

- [ ] **Step 1: Write failing lexical, symlink, and boundedness tests**

Create disposable Git/non-Git fixtures and assert:

```python
def test_git_manifest_hashes_trust_inventory_but_only_stats_declared_ignored_root(tmp_path: Path) -> None:
    manifest = capture_root_manifest(request)
    assert {record.path for record in manifest.hashed_paths} == {"src/a.py", "pyrightconfig.json"}
    assert {record.path for record in manifest.metadata_paths} == {"model_cache/blob.bin"}


def test_manifest_does_not_follow_symlinked_directory(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="symlink"):
        capture_root_manifest(request_with_symlinked_metadata_root)
```

Also cover byte-stable canonical order, Git HEAD/tracked/untracked/ignored disposition, mid-freeze same-size rewrites, config inclusion, non-Git exact task paths, missing roots, unsupported special files, duplicate paths, and no traversal outside the root.

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_manifests.py`

Expected: FAIL because `scripts.backend_eval.manifests` does not exist.

- [ ] **Step 3: Implement bounded manifest capture**

Use `git_trust_inventory` for Git source closure and `bounded_non_git_trust_inventory` only for the exact transformers root. Add declared native configuration files even when their extensions are not supported source extensions. For research-probes, declare `model_cache` as metadata-only. For `llm-framework-study`, accept only the frozen task path list; never walk full site-packages. Use `lstat`, never follow symlinked directories, and perform two matching guarded content passes for fully hashed files.

- [ ] **Step 4: Run focused inventory regression tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_manifests.py tests/unit/test_workspace_inventory.py tests/unit/test_workspace_identity.py`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit Task 4**

Run: `conda run -n ms ruff check scripts/backend_eval/manifests.py tests/backend_eval/test_manifests.py`

Run: `conda run -n ms ty check scripts/backend_eval/manifests.py tests/backend_eval/test_manifests.py`

```bash
git add scripts/backend_eval/manifests.py tests/backend_eval/test_manifests.py
git commit -m "Add bounded backend evaluation manifests"
```

### Task 5: Zero-write comparison and declared fixture mutations

**OpenSpec coverage:** 1.7

**Files:**
- Create: `scripts/backend_eval/write_guard.py`
- Create: `tests/backend_eval/test_write_guard.py`

**Interfaces:**
- Consumes: Task 1 `RootManifest`, `WriteDelta`; Task 4 `capture_root_manifest`
- Produces: `compare_root_manifests(before: RootManifest, after: RootManifest, *, declared_mutations: frozenset[str] = frozenset()) -> WriteDelta`
- Produces: `assert_no_unexpected_writes(deltas: Sequence[WriteDelta]) -> None`

- [ ] **Step 1: Write failing create/change/delete/symlink tests**

```python
@pytest.mark.parametrize("mutation", ["create", "change", "delete", "symlink_retarget"])
def test_write_guard_reports_unexpected_mutation(mutation: str, fixture_root: Path) -> None:
    before = capture_root_manifest(request)
    apply_mutation(fixture_root, mutation)
    after = capture_root_manifest(request)
    delta = compare_root_manifests(before, after)
    assert delta.unexpected


def test_declared_disposable_edit_is_not_backend_write() -> None:
    delta = compare_root_manifests(before, after, declared_mutations=frozenset({"src/a.py"}))
    assert not delta.unexpected
    assert delta.declared == ("src/a.py",)
    assert delta.before_manifest_digest == before.manifest_digest
    assert delta.after_manifest_digest == after.manifest_digest
```

Cover same-size/mtime replacement on fully hashed paths, metadata change followed by content hash on remainder paths, unexpected new paths, root/kind mismatch, special-file failure, and canonical delta ordering.

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_write_guard.py`

Expected: FAIL because `scripts.backend_eval.write_guard` does not exist.

- [ ] **Step 3: Implement fail-closed comparison**

Compare path membership and every recorded field, and bind every `WriteDelta` to `before.manifest_digest` and `after.manifest_digest`. A declared mutation suppresses only the exact named path, never its parent, sibling, or symlink target. When a metadata-only record changes, require `after.content_sha256` before classification; absence is an incomplete observation and must fail closed. `assert_no_unexpected_writes` raises one bounded error containing counts, digest, and at most 50 sample paths.

- [ ] **Step 4: Run focused and manifest regression tests**

Run: `conda run -n ms pytest -q tests/backend_eval/test_write_guard.py tests/backend_eval/test_manifests.py`

Expected: PASS.

- [ ] **Step 5: Run static checks and commit Task 5**

Run: `conda run -n ms ruff check scripts/backend_eval/write_guard.py tests/backend_eval/test_write_guard.py`

Run: `conda run -n ms ty check scripts/backend_eval/write_guard.py tests/backend_eval/test_write_guard.py`

```bash
git add scripts/backend_eval/write_guard.py tests/backend_eval/test_write_guard.py
git commit -m "Detect backend evaluation workspace writes"
```

### Task 6: Admission CLI, receipt, and real Phase 1 gate

**OpenSpec coverage:** 1.8

**Files:**
- Create: `scripts/backend_eval/admission.py`
- Create: `tests/backend_eval/test_admission.py`
- Create after a real run: `openspec/changes/evaluate-python-language-backends/phase-1-acceptance.md`
- Modify after verification: `openspec/changes/evaluate-python-language-backends/tasks.md` (check only 1.1–1.8)

**Interfaces:**
- Consumes all Task 1–5 interfaces.
- Produces: `run_admission(request: AdmissionRequest, *, clock: Clock = monotonic_clock) -> AdmissionReceipt`
- Produces CLI: `python -m scripts.backend_eval.admission --repo-root ABS --artifact-root ABS --runtime-base ABS --uv ABS --python ABS --exclude-newer RFC3339`
- Exit `0` only for canonical PASS; exit `2` for typed admission HOLD/incomplete; never print secrets or full source paths beyond the declared roots.

- [ ] **Step 1: Write failing orchestration tests**

Use fake lock/runtime/manifest functions to cover PASS, 30-minute deadline, unstable root, resolution failure, runtime preparation failure, production identity drift, unexpected write, cleanup failure, deterministic issue ordering, bounded error samples, atomic receipt publication, and no second candidate resolution.

```python
def test_admission_pass_requires_equal_production_identity() -> None:
    receipt = run_admission(request, services=passing_services)
    assert receipt.status == "pass"
    assert receipt.production_identity_before == receipt.production_identity_after
    assert receipt.evaluation_contract_version == "python-backend-evaluation-v1"
    assert {identity.name for identity in receipt.environments} == {"llm-framework-study", "ms"}
    assert {identity.backend for identity in receipt.service_configs} == {"pyrefly", "pyright", "ty"}
    assert tuple(manifest.root for manifest in receipt.root_manifests_before) == tuple(
        manifest.root for manifest in receipt.root_manifests_after
    )
    assert not any(delta.unexpected for delta in receipt.write_deltas)
    assert receipt.next_action == "begin_protocol_probe_planning"


def test_admission_timeout_is_incomplete_and_cleans_partial_state() -> None:
    receipt = run_admission(request, services=services_exceeding_deadline)
    assert receipt.status == "incomplete"
    assert receipt.next_action == "retain_pyright_and_disposition_admission"
    assert services_exceeding_deadline.cleanup_called
```

- [ ] **Step 2: Run tests and verify the missing module failure**

Run: `conda run -n ms pytest -q tests/backend_eval/test_admission.py`

Expected: FAIL because `scripts.backend_eval.admission` does not exist.

- [ ] **Step 3: Implement orchestration and atomic receipt publication**

Capture production identity; enforce the monotonic deadline before and after every external step; compile/freeze the candidate lock once; prepare/verify runtime; capture the exact environment and service-config identities from that runtime; capture the bounded corpus twice around the no-backend admission operation and retain both canonical manifest collections; compare zero-write evidence with bound before/after manifest digests; reject PASS when any unexpected path exists; capture production identity again; construct all receipt-level arrays in their canonical order; write canonical JSON to a temporary file, fsync, `os.replace`, and fsync the artifact directory. On failure, publish an incomplete receipt only when its evidence is trustworthy and perform exact evaluation-owned partial cleanup.

- [ ] **Step 4: Run Phase 1 focused suite and static checks**

Run: `conda run -n ms pytest -q tests/backend_eval tests/unit/test_build_identity.py tests/unit/test_bootstrap.py tests/unit/test_workspace_inventory.py tests/unit/test_workspace_identity.py`

Run: `conda run -n ms ruff check scripts/backend_eval tests/backend_eval`

Run: `conda run -n ms ty check scripts/backend_eval tests/backend_eval`

Expected: all PASS.

- [ ] **Step 5: Run the real admission command**

Resolve a fresh UTC freeze timestamp once, print it into the command transcript, and pass that exact value to the admission CLI:

```bash
backend_eval_freeze_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
test -n "$backend_eval_freeze_at"
echo "$backend_eval_freeze_at"
conda run -n ms python -m scripts.backend_eval.admission \
  --repo-root /data/CoordExp/serena-light \
  --artifact-root /data/CoordExp/serena-light/.admission-artifacts/backend-eval \
  --runtime-base /data/CoordExp/.codex/runtime/serena-light/backend-eval \
  --uv /root/miniconda3/envs/ms/bin/uv \
  --python /root/miniconda3/envs/ms/bin/python \
  --exclude-newer "$backend_eval_freeze_at"
```

Expected: exit `0`, `status=pass`, candidate lock/runtime hashes present, bounded manifests present, no unexpected write deltas, production identities equal, and `next_action=begin_protocol_probe_planning`. If network/proxy resolution fails, stop with the typed incomplete receipt; do not silently fall back to ambient packages.

- [ ] **Step 6: Verify the repository and production identity after the real run**

Run: `git status --short`

Run: `conda run -n ms python -c 'from pathlib import Path; from serena_light.build_identity import compute_build_identity, dependency_lock_digest; print(dependency_lock_digest(Path.cwd())); print(compute_build_identity(Path.cwd()))'`

Compare both digests with the receipt's pre-evaluation values. Verify no evaluation-owned child process remains and canonical Serena/installed Serena Light registrations were not touched.

- [ ] **Step 7: Record bounded acceptance and check OpenSpec Phase 1 tasks**

Write `phase-1-acceptance.md` with exact command, evaluation identity and contract version, candidate lock digest, interpreter configured/real paths and versions, service-config paths/digests, artifact-tree digest, production identity before/after, root manifest digests/counts, bound before/after write-delta digests, elapsed time, cleanup summary, and PASS/HOLD disposition. Do not embed raw transcripts or source.

Change only OpenSpec checkboxes 1.1–1.8 to `[x]` when the real receipt passes. Leave every Phase 2–6 checkbox unchecked.

- [ ] **Step 8: Run the phase gate and commit Task 6**

Run: `openspec validate evaluate-python-language-backends --strict`

Run: `git diff --check`

Run: `conda run -n ms pytest -q tests/backend_eval`

Expected: all PASS.

```bash
git add scripts/backend_eval/admission.py tests/backend_eval/test_admission.py \
  openspec/changes/evaluate-python-language-backends/phase-1-acceptance.md \
  openspec/changes/evaluate-python-language-backends/tasks.md
git commit -m "Complete backend evaluation admission gate"
```

## Phase 1 Final Review

- [ ] Generate one review package from the Phase 1 merge base through Task 6 HEAD.
- [ ] Dispatch an Opus/xhigh lifecycle reviewer over production-identity isolation, runtime ownership, zero-write coverage, deadline/cleanup semantics, and receipt truthfulness.
- [ ] Dispatch a Sol/xhigh semantic reviewer over snapshot correctness, decision-owning evidence schema, manifest/write-delta logic, and false-PASS paths.
- [ ] If either review finds a blocker, run one bounded fix wave and one scoped re-review. Do not begin Phase 2 planning with an unresolved load-bearing finding.
- [ ] When both reviews pass, update the SDD ledger and create the Phase 2 implementation plan; do not install or expose a production candidate backend.
