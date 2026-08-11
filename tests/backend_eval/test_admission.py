"""Admission orchestration: deadline, evidence, receipt publication, and cleanup."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from scripts.backend_eval.admission import (
    ADMISSION_RECEIPT_FILE_NAME,
    NEXT_ACTION_HOLD,
    NEXT_ACTION_PASS,
    AdmissionError,
    AdmissionRequest,
    admission_receipt_path,
    artifact_tree_digest,
    evaluation_identity,
    main,
    run_admission,
)
from scripts.backend_eval.candidate_lock import CACHE_DIR_NAME, LOCK_FILE_NAME, CandidateLockError, CandidateLockRequest
from scripts.backend_eval.manifests import ManifestError
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    EVALUATION_CONTRACT_VERSION,
    AdmissionReceipt,
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    PathRecord,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    ServiceConfigIdentity,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.production_identity import ProductionIdentityChanged
from scripts.backend_eval.runtime import (
    SERVICE_CONFIG_RELPATHS,
    CandidateRuntime,
    RuntimePreparationError,
    RuntimeRequest,
)

LOCK_DIGEST = "1" * 64
REVISION = "a" * 40
EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
ADMISSION_BUDGET_SECONDS = 30 * 60


# --- fixtures ------------------------------------------------------------------


def _production_identity(*, build_identity: str = "b" * 64) -> ProductionIdentity:
    return ProductionIdentity(
        pyproject_toml_sha256="c" * 64,
        uv_lock_sha256="d" * 64,
        package_lock_json_sha256="e" * 64,
        dependency_lock_digest="f" * 64,
        build_identity=build_identity,
        runtime_paths=(("python", "/data/runtime/python/bin/python"), ("runtime", "/data/runtime")),
    )


def _candidate_lock() -> CandidateLock:
    resolved = (
        ResolvedPackage(name="pyrefly", version="0.42.0", requirement="pyrefly==0.42.0", artifact_hashes=("1" * 64,)),
        ResolvedPackage(name="ty", version="0.0.24", requirement="ty==0.0.24", artifact_hashes=("2" * 64,)),
    )
    candidates = (
        CandidatePackage(
            name="pyrefly",
            version="0.42.0",
            requirement="pyrefly==0.42.0",
            artifact_hashes=("1" * 64,),
            executable_relpath="bin/pyrefly",
        ),
        CandidatePackage(
            name="ty",
            version="0.0.24",
            requirement="ty==0.0.24",
            artifact_hashes=("2" * 64,),
            executable_relpath="bin/ty",
        ),
    )
    return CandidateLock(
        digest=LOCK_DIGEST,
        exclude_newer=EXCLUDE_NEWER,
        resolved_packages=resolved,
        candidates=candidates,
    )


def _candidate_runtime(runtime_base: Path) -> CandidateRuntime:
    root = runtime_base / LOCK_DIGEST
    config = root / "config"
    home = root / "home"
    cache = root / "cache"
    return CandidateRuntime(
        root=root,
        python=root / "venv" / "bin" / "python",
        ty=root / "venv" / "bin" / "ty",
        pyrefly=root / "venv" / "bin" / "pyrefly",
        lock_digest=LOCK_DIGEST,
        executable_hashes=(("pyrefly", "3" * 64), ("ty", "4" * 64)),
        home=home,
        cache=cache,
        config=config,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path="/root/miniconda3/envs/llm-framework-study/bin/python",
                interpreter_realpath="/root/miniconda3/envs/llm-framework-study/bin/python3.12",
                version="3.12.11",
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path="/root/miniconda3/envs/ms/bin/python",
                interpreter_realpath="/root/miniconda3/envs/ms/bin/python3.12",
                version="3.12.11",
            ),
        ),
        service_configs=tuple(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(config / relpath),
                config_sha256=sha256_bytes(backend.encode()),
                home_path=str(home),
                cache_path=str(cache),
            )
            for backend, relpath in sorted(SERVICE_CONFIG_RELPATHS.items())
        ),
    )


def _record(path: str, *, content: str = "9" * 64, mtime_ns: int = 1) -> PathRecord:
    return PathRecord(
        path=path,
        kind="file",
        disposition="tracked",
        size=7,
        mtime_ns=mtime_ns,
        inode=11,
        symlink_target=None,
        content_sha256=content,
    )


def _manifest(
    root: str,
    *,
    kind: str = "git",
    hashed: tuple[PathRecord, ...] = (),
    inventory_digest: str = "5" * 64,
    inventory_count: int = 1,
) -> RootManifest:
    hashed = hashed or (_record("pyproject.toml"),)
    digest = sha256_bytes(
        canonical_json(
            {
                "root": root,
                "inventory_digest": inventory_digest,
                "inventory_count": inventory_count,
                "hashed": [(record.path, record.content_sha256, record.mtime_ns) for record in hashed],
            }
        )
    )
    return RootManifest(
        root=root,
        kind=kind,
        source_revision=REVISION if kind == "git" else None,
        inventory_digest=inventory_digest,
        inventory_count=inventory_count,
        hashed_paths=hashed,
        metadata_paths=(),
        manifest_digest=digest,
    )


def _corpus() -> tuple[RootManifest, ...]:
    return (
        _manifest("/data/CoordExp/serena-light"),
        _manifest("/data/ms-swift"),
    )


@dataclass(slots=True)
class FakeClock:
    """A monotonic clock the fake services advance by an exact per-step amount."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass(slots=True)
class FakeServices:
    clock: FakeClock
    runtime_base: Path
    identities: list[ProductionIdentity] = field(default_factory=list)
    lock: CandidateLock | None = None
    corpora: list[tuple[RootManifest, ...]] = field(default_factory=list)
    step_seconds: dict[str, float] = field(default_factory=dict)
    failures: dict[str, BaseException] = field(default_factory=dict)
    cleanup_summary: tuple[str, ...] = ()
    cleanup_error: BaseException | None = None
    lock_requests: list[CandidateLockRequest] = field(default_factory=list)
    runtime_requests: list[RuntimeRequest] = field(default_factory=list)
    identity_calls: int = 0
    corpus_calls: int = 0
    cleanup_stages: list[str] = field(default_factory=list)
    digest_calls: int = 0

    @property
    def cleanup_called(self) -> bool:
        return bool(self.cleanup_stages)

    @property
    def resolution_count(self) -> int:
        return len(self.lock_requests)

    def _enter(self, step: str) -> None:
        self.clock.advance(self.step_seconds.get(step, 1.0))
        failure = self.failures.get(step)
        if failure is not None:
            raise failure

    def capture_production_identity(self, repo_root: Path) -> ProductionIdentity:
        assert repo_root.is_absolute()
        step = "identity_before" if self.identity_calls == 0 else "identity_after"
        self.identity_calls += 1
        self._enter(step)
        index = min(self.identity_calls - 1, len(self.identities) - 1)
        return self.identities[index]

    def compile_candidate_lock(self, request: CandidateLockRequest) -> CandidateLock:
        self.lock_requests.append(request)
        self._enter("candidate_lock")
        assert self.lock is not None
        request.artifact_root.mkdir(parents=True, exist_ok=True)
        (request.artifact_root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.24\n")
        return self.lock

    def prepare_candidate_runtime(self, lock: CandidateLock, request: RuntimeRequest) -> CandidateRuntime:
        self.runtime_requests.append(request)
        self._enter("runtime")
        return _candidate_runtime(self.runtime_base)

    def capture_corpus(self) -> tuple[RootManifest, ...]:
        step = "corpus_before" if self.corpus_calls == 0 else "corpus_after"
        self.corpus_calls += 1
        self._enter(step)
        index = min(self.corpus_calls - 1, len(self.corpora) - 1)
        return self.corpora[index]

    def artifact_tree_digest(self, artifact_root: Path) -> str:
        self.digest_calls += 1
        self._enter("artifact_digest")
        return artifact_tree_digest(artifact_root)

    def cleanup(self, evaluation_root: Path, stage: str) -> tuple[str, ...]:
        self.cleanup_stages.append(stage)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.cleanup_summary


def _request(tmp_path: Path) -> AdmissionRequest:
    repo_root = tmp_path / "repo"
    (repo_root / "src").mkdir(parents=True)
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    python = tools / "python"
    for executable in (uv, python):
        executable.write_text("#!/bin/sh\nexit 0\n")
        executable.chmod(0o755)
    runtime_base = tmp_path / "runtime-base"
    return AdmissionRequest(
        repo_root=repo_root,
        artifact_root=repo_root / ".admission-artifacts" / "backend-eval",
        runtime_base=runtime_base,
        uv=uv,
        python=python,
        exclude_newer=EXCLUDE_NEWER,
    )


def _services(request: AdmissionRequest, clock: FakeClock) -> FakeServices:
    identity = _production_identity()
    return FakeServices(
        clock=clock,
        runtime_base=request.runtime_base,
        identities=[identity, identity],
        lock=_candidate_lock(),
        corpora=[_corpus(), _corpus()],
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def request_(tmp_path: Path) -> AdmissionRequest:
    return _request(tmp_path)


@pytest.fixture
def services(request_: AdmissionRequest, clock: FakeClock) -> FakeServices:
    return _services(request_, clock)


# --- the passing admission -----------------------------------------------------


def test_admission_pass_requires_equal_production_identity(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"
    assert receipt.production_identity_before == receipt.production_identity_after
    assert receipt.evaluation_contract_version == EVALUATION_CONTRACT_VERSION
    assert receipt.schema_version == ADMISSION_RECEIPT_SCHEMA_VERSION
    assert {identity.name for identity in receipt.environments} == {"llm-framework-study", "ms"}
    assert {identity.backend for identity in receipt.service_configs} == {"pyrefly", "pyright", "ty"}
    assert tuple(manifest.root for manifest in receipt.root_manifests_before) == tuple(
        manifest.root for manifest in receipt.root_manifests_after
    )
    assert not any(delta.unexpected for delta in receipt.write_deltas)
    assert receipt.next_action == NEXT_ACTION_PASS
    assert receipt.issues == ()
    assert receipt.candidate_lock.digest == LOCK_DIGEST


def test_admission_pass_records_every_receipt_array_in_canonical_order(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert [budget.name for budget in receipt.budgets] == sorted(budget.name for budget in receipt.budgets)
    assert [identity.name for identity in receipt.environments] == ["llm-framework-study", "ms"]
    assert [identity.backend for identity in receipt.service_configs] == ["pyrefly", "pyright", "ty"]
    assert [manifest.root for manifest in receipt.root_manifests_before] == sorted(
        manifest.root for manifest in receipt.root_manifests_before
    )
    assert [delta.root for delta in receipt.write_deltas] == sorted(delta.root for delta in receipt.write_deltas)
    budgets = {budget.name: budget.seconds for budget in receipt.budgets}
    assert budgets["admission"] == ADMISSION_BUDGET_SECONDS


def test_admission_binds_write_deltas_to_both_manifest_digests(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    before = {manifest.root: manifest.manifest_digest for manifest in receipt.root_manifests_before}
    after = {manifest.root: manifest.manifest_digest for manifest in receipt.root_manifests_after}
    assert {delta.root for delta in receipt.write_deltas} == set(before)
    for delta in receipt.write_deltas:
        assert delta.before_manifest_digest == before[delta.root]
        assert delta.after_manifest_digest == after[delta.root]


def test_admission_resolves_the_candidate_lock_exactly_once(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"
    assert services.resolution_count == 1
    assert len(services.runtime_requests) == 1
    assert services.corpus_calls == 2
    assert services.identity_calls == 2


def test_admission_confines_every_derived_request_to_the_declared_roots(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    lock_request = services.lock_requests[0]
    runtime_request = services.runtime_requests[0]
    assert lock_request.artifact_root == request_.artifact_root / receipt.evaluation_identity
    assert lock_request.artifact_root.is_relative_to(request_.artifact_root)
    assert lock_request.exclude_newer == EXCLUDE_NEWER
    assert runtime_request.runtime_base == request_.runtime_base
    assert runtime_request.requirements_lock == lock_request.artifact_root / LOCK_FILE_NAME


def test_evaluation_identity_is_deterministic_and_binds_production_identity(request_: AdmissionRequest) -> None:
    identity = _production_identity()
    first = evaluation_identity(request_, identity)
    assert first == evaluation_identity(request_, identity)
    assert len(first) == 64
    other = evaluation_identity(request_, _production_identity(build_identity="7" * 64))
    assert other != first
    assert evaluation_identity(replace(request_, exclude_newer="2026-08-10T00:00:00Z"), identity) != first


# --- receipt publication -------------------------------------------------------


def test_admission_publishes_the_canonical_receipt_atomically(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    path = admission_receipt_path(request_.artifact_root, receipt.evaluation_identity)
    assert path.name == ADMISSION_RECEIPT_FILE_NAME
    assert path.read_bytes() == canonical_json(receipt.to_dict())
    assert AdmissionReceipt.from_dict(json.loads(path.read_text())) == receipt
    leftovers = [entry.name for entry in path.parent.iterdir() if entry.name.startswith(".")]
    assert leftovers == []


def test_admission_republishes_an_identical_receipt_over_the_previous_one(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    first = run_admission(request_, services=services, clock=clock)
    path = admission_receipt_path(request_.artifact_root, first.evaluation_identity)
    inode = path.stat().st_ino
    second_services = _services(request_, clock)
    second = run_admission(request_, services=second_services, clock=clock)
    assert second.status == "pass"
    assert path.stat().st_ino != inode
    assert path.read_bytes() == canonical_json(second.to_dict())


def test_artifact_tree_digest_excludes_the_receipt_and_the_resolver_cache(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / CACHE_DIR_NAME).mkdir(parents=True)
    (root / CACHE_DIR_NAME / "blob").write_bytes(b"volatile")
    (root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.24\n")
    baseline = artifact_tree_digest(root)
    (root / ADMISSION_RECEIPT_FILE_NAME).write_bytes(b"{}\n")
    (root / CACHE_DIR_NAME / "blob").write_bytes(b"changed")
    assert artifact_tree_digest(root) == baseline
    (root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.25\n")
    assert artifact_tree_digest(root) != baseline


def test_artifact_tree_digest_refuses_a_symlinked_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"payload")
    (root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(AdmissionError, match="symlink"):
        artifact_tree_digest(root)


# --- the deadline ---------------------------------------------------------------


def test_admission_timeout_is_incomplete_and_cleans_partial_state(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["corpus_before"] = float(ADMISSION_BUDGET_SECONDS)
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert services.cleanup_called
    assert any(issue.startswith("admission_deadline_exceeded") for issue in receipt.issues)
    assert services.corpus_calls == 1


def test_admission_checks_the_deadline_before_starting_an_external_step(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["runtime"] = float(ADMISSION_BUDGET_SECONDS)
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert services.corpus_calls == 0
    assert any("prepare_candidate_runtime" in issue for issue in receipt.issues)


def test_admission_deadline_stops_before_a_second_candidate_resolution(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["identity_before"] = float(ADMISSION_BUDGET_SECONDS)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded"):
        run_admission(request_, services=services, clock=clock)
    assert services.resolution_count == 0


# --- typed failures --------------------------------------------------------------


def test_admission_resolution_failure_raises_without_publishing_a_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["candidate_lock"] = CandidateLockError("uv pip compile failed: proxy refused")
    with pytest.raises(AdmissionError, match="candidate_resolution_failed") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    assert not (request_.artifact_root).exists() or not any(
        path.name == ADMISSION_RECEIPT_FILE_NAME for path in request_.artifact_root.rglob("*")
    )
    assert services.cleanup_called


def test_admission_runtime_failure_publishes_a_trustworthy_incomplete_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["runtime"] = RuntimePreparationError("uv pip sync failed")
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert receipt.environments == ()
    assert receipt.service_configs == ()
    assert receipt.root_manifests_before == ()
    assert receipt.write_deltas == ()
    assert any(issue.startswith("runtime_preparation_failed") for issue in receipt.issues)
    assert admission_receipt_path(request_.artifact_root, receipt.evaluation_identity).is_file()


def test_admission_unstable_corpus_root_is_incomplete(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    moved = (
        _manifest("/data/CoordExp/serena-light", inventory_digest="6" * 64),
        _manifest("/data/ms-swift"),
    )
    services.corpora = [_corpus(), moved]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any(issue.startswith("unstable_corpus_root") for issue in receipt.issues)
    assert receipt.write_deltas == ()
    assert receipt.root_manifests_after != ()


def test_admission_missing_corpus_root_is_incomplete(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.corpora = [_corpus(), (_manifest("/data/CoordExp/serena-light"),)]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any(issue.startswith("unstable_corpus_root") for issue in receipt.issues)


def test_admission_corpus_capture_failure_is_incomplete(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["corpus_after"] = ManifestError("Git manifest inputs changed while freezing")
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any(issue.startswith("corpus_capture_failed") for issue in receipt.issues)


def test_admission_unexpected_write_is_held_with_bound_manifest_digests(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    dirtied = (
        _manifest("/data/CoordExp/serena-light", hashed=(_record("pyproject.toml", content="8" * 64),)),
        _manifest("/data/ms-swift"),
    )
    services.corpora = [_corpus(), dirtied]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    assert receipt.next_action == NEXT_ACTION_HOLD
    unexpected = {delta.root: delta.unexpected for delta in receipt.write_deltas}
    assert unexpected["/data/CoordExp/serena-light"] == ("pyproject.toml",)
    assert unexpected["/data/ms-swift"] == ()
    assert any(issue.startswith("unexpected_evaluation_writes") for issue in receipt.issues)
    assert admission_receipt_path(request_.artifact_root, receipt.evaluation_identity).is_file()


def test_admission_production_identity_drift_is_held(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.identities = [_production_identity(), _production_identity(build_identity="7" * 64)]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    assert receipt.production_identity_before != receipt.production_identity_after
    assert any(issue.startswith("production_identity_changed") for issue in receipt.issues)


def test_admission_production_identity_drift_inside_a_step_is_held(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["runtime"] = ProductionIdentityChanged("production identity changed: uv.lock")
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    assert any(issue.startswith("production_identity_changed") for issue in receipt.issues)


# --- cleanup ---------------------------------------------------------------------


def test_admission_cleanup_failure_downgrades_a_passing_run(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.cleanup_error = OSError("cannot remove the partial receipt")
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert any(issue.startswith("cleanup_failed") for issue in receipt.issues)


def test_admission_cleanup_that_removed_partial_state_downgrades_a_passing_run(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.cleanup_summary = ("removed_temporary_receipt",)
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any("removed_temporary_receipt" in issue for issue in receipt.issues)


def test_admission_cleanup_runs_once_on_the_passing_path(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"
    assert services.cleanup_stages == ["pass"]


def test_production_cleanup_removes_only_the_temporary_receipt(
    request_: AdmissionRequest, tmp_path: Path
) -> None:
    from scripts.backend_eval.admission import ProductionAdmissionServices

    evaluation_root = tmp_path / "evaluation"
    evaluation_root.mkdir()
    (evaluation_root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.24\n")
    temporary = evaluation_root / f".{ADMISSION_RECEIPT_FILE_NAME}.tmp"
    temporary.write_bytes(b"partial")
    summary = ProductionAdmissionServices().cleanup(evaluation_root, "incomplete")
    assert summary == ("removed_temporary_receipt",)
    assert not temporary.exists()
    assert (evaluation_root / LOCK_FILE_NAME).is_file()
    assert ProductionAdmissionServices().cleanup(evaluation_root, "pass") == ()


def test_production_cleanup_tolerates_a_missing_evaluation_root(tmp_path: Path) -> None:
    from scripts.backend_eval.admission import ProductionAdmissionServices

    assert ProductionAdmissionServices().cleanup(tmp_path / "absent", "incomplete") == ()


# --- issues ------------------------------------------------------------------------


def test_admission_issues_are_sorted_unique_and_bounded(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["runtime"] = RuntimePreparationError("x" * 4000)
    services.cleanup_summary = ("removed_temporary_receipt",)
    receipt = run_admission(request_, services=services, clock=clock)
    assert list(receipt.issues) == sorted(set(receipt.issues))
    assert len(receipt.issues) <= 20
    assert all(len(issue) <= 220 for issue in receipt.issues)


def test_admission_issues_redact_paths_outside_the_declared_roots(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["runtime"] = RuntimePreparationError(
        f"cannot read /home/someone/.netrc while preparing {request_.runtime_base}"
    )
    receipt = run_admission(request_, services=services, clock=clock)
    joined = " ".join(receipt.issues)
    assert "/home/someone/.netrc" not in joined
    assert "<redacted-path>" in joined
    assert str(request_.runtime_base) in joined


def test_admission_issue_order_is_stable_across_identical_runs(
    request_: AdmissionRequest, tmp_path: Path, clock: FakeClock
) -> None:
    def _run() -> tuple[str, ...]:
        local_clock = FakeClock()
        local_services = _services(request_, local_clock)
        local_services.failures["corpus_after"] = ManifestError("Git manifest inputs changed while freezing")
        local_services.cleanup_summary = ("removed_temporary_receipt",)
        return run_admission(request_, services=local_services, clock=local_clock).issues

    assert _run() == _run()


# --- the command line ----------------------------------------------------------------


def test_cli_exits_zero_only_for_a_canonical_pass(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = main(_argv(request_), services=services, clock=clock)
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "status=pass" in out
    assert f"next_action={NEXT_ACTION_PASS}" in out


def test_cli_exits_two_for_a_hold(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    services.identities = [_production_identity(), _production_identity(build_identity="7" * 64)]
    exit_code = main(_argv(request_), services=services, clock=clock)
    assert exit_code == 2
    assert "status=hold" in capsys.readouterr().out


def test_cli_exits_two_when_no_receipt_can_be_published(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    services.failures["candidate_lock"] = CandidateLockError("network is unreachable")
    exit_code = main(_argv(request_), services=services, clock=clock)
    assert exit_code == 2
    out = capsys.readouterr().out
    assert "status=incomplete" in out
    assert "candidate_resolution_failed" in out


def test_cli_rejects_a_malformed_freeze_timestamp(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = _argv(request_)
    argv[argv.index("--exclude-newer") + 1] = "2026-08-11"
    exit_code = main(argv, services=services, clock=clock)
    assert exit_code == 2
    assert services.resolution_count == 0


def _argv(request: AdmissionRequest) -> list[str]:
    return [
        "--repo-root",
        str(request.repo_root),
        "--artifact-root",
        str(request.artifact_root),
        "--runtime-base",
        str(request.runtime_base),
        "--uv",
        str(request.uv),
        "--python",
        str(request.python),
        "--exclude-newer",
        request.exclude_newer,
    ]


def test_admission_request_rejects_an_artifact_root_outside_the_evaluation_area(tmp_path: Path) -> None:
    base = _request(tmp_path)
    with pytest.raises(ValueError, match="artifact_root"):
        replace(base, artifact_root=tmp_path / "elsewhere")


def test_admission_request_rejects_a_relative_root(tmp_path: Path) -> None:
    base = _request(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        replace(base, runtime_base=Path("runtime-base"))


def test_admission_module_never_creates_state_outside_the_declared_roots(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, tmp_path: Path
) -> None:
    before = {entry.name for entry in tmp_path.iterdir()}
    run_admission(request_, services=services, clock=clock)
    assert {entry.name for entry in tmp_path.iterdir()} == before
    assert not (request_.runtime_base).exists()
    assert os.listdir(request_.artifact_root) != []
