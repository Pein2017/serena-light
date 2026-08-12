"""Admission orchestration: window, identity, deadline, immutable receipts, and cleanup."""

from __future__ import annotations

import ast
import fcntl
import inspect
import itertools
import json
import os
import shutil
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from scripts.backend_eval.admission import (
    ADMISSION_BUDGET_SECONDS,
    FINALIZATION_RESERVE_SECONDS,
    PUBLICATION_LOCK_NAME,
    RECEIPTS_DIR_NAME,
    AdmissionError,
    AdmissionRequest,
    ProductionAdmissionServices,
    _Publication,
    _read_artifact_bytes,
    admission_receipt_path,
    artifact_tree_digest,
    evaluation_identity,
    main,
    new_run_identity,
    run_admission,
)
from scripts.backend_eval.candidate_lock import CACHE_DIR_NAME, LOCK_FILE_NAME, CandidateLockError, CandidateLockRequest
from scripts.backend_eval.identity import IdentityError
from scripts.backend_eval.manifests import ManifestError
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    EVALUATION_CONTRACT_VERSION,
    NEXT_ACTION_HOLD,
    NEXT_ACTION_PASS,
    AdmissionReceipt,
    BootstrapEnvironmentIdentity,
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    EvaluatorIdentity,
    LockEvidence,
    PathRecord,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    ServiceConfigIdentity,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.process import Deadline, DeadlineExceeded
from scripts.backend_eval.production_identity import ProductionIdentityChanged, ProductionIdentityError
from scripts.backend_eval.runtime import (
    SERVICE_CONFIG_RELPATHS,
    CandidateRuntime,
    RuntimePreparationError,
    RuntimeRequest,
)
from scripts.backend_eval.source_binding import (
    CHILD_EXECUTED_HELPERS,
    PRODUCTION_CHILD_NAME,
    HelperExpectation,
)

LOCK_DIGEST = "1" * 64
MANIFEST_DIGEST = "a" * 64
REVISION = "a" * 40
EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
COLLECT_WINDOW = ADMISSION_BUDGET_SECONDS - FINALIZATION_RESERVE_SECONDS


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


def _evaluator(*, source_digest_seed: str = "9", child_digest_seed: str = "4") -> EvaluatorIdentity:
    """A synthetic identity that can still produce an execution expectation.

    Admission derives the production-helper expectation from this identity before any child
    may run, so the identity has to name the child program and every declared child-executed
    helper -- exactly the completeness the real capture provides.
    """

    return EvaluatorIdentity.build(
        source_files=(
            ("admission.py", source_digest_seed * 64),
            ("models.py", "8" * 64),
            (PRODUCTION_CHILD_NAME, child_digest_seed * 64),
        ),
        source_commit="7" * 40,
        source_clean=True,
        production_root="/data/CoordExp/serena-light/src",
        production_files=tuple((relative, "5" * 64) for relative in CHILD_EXECUTED_HELPERS),
        production_clean=True,
        host_python_path="/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256="6" * 64,
        host_python_version="3.12.11",
    )


def _bootstrap() -> BootstrapEnvironmentIdentity:
    return BootstrapEnvironmentIdentity(
        inherited_keys=("HTTPS_PROXY",),
        inherited_value_digests=(("HTTPS_PROXY", "5" * 64),),
        service_keys=("HOME", "PATH", "TMPDIR"),
        refused_keys=("PIP_INDEX_URL",),
    )


def _candidate_lock() -> CandidateLock:
    resolved = (
        ResolvedPackage(name="pyrefly", version="1.2.0", requirement="pyrefly==1.2.0", artifact_hashes=("1" * 64,)),
        ResolvedPackage(name="ty", version="0.0.70", requirement="ty==0.0.70", artifact_hashes=("2" * 64,)),
    )
    candidates = tuple(
        CandidatePackage(
            name=package.name,
            version=package.version,
            requirement=package.requirement,
            artifact_hashes=package.artifact_hashes,
            executable_relpath=f"bin/{package.name}",
        )
        for package in resolved
    )
    return CandidateLock(
        digest=LOCK_DIGEST,
        exclude_newer=EXCLUDE_NEWER,
        resolved_packages=resolved,
        candidates=candidates,
        lock_evidence=LockEvidence.build(raw_sha256=LOCK_DIGEST, raw_size=2048, resolved_packages=resolved),
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
        manifest_path=root / "runtime-manifest.json",
        manifest_sha256=MANIFEST_DIGEST,
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path="/root/miniconda3/envs/llm-framework-study/bin/python",
                interpreter_realpath="/root/miniconda3/envs/llm-framework-study/bin/python3.12",
                version="3.12.13",
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


def _record(path: str, *, content: str | None = "9" * 64, kind: str = "file") -> PathRecord:
    return PathRecord(
        path=path,
        kind=kind,
        disposition="tracked",
        size=7,
        mtime_ns=1,
        inode=11,
        symlink_target=None,
        content_sha256=content,
    )


def _manifest(root: str, *, hashed: tuple[PathRecord, ...] = (), inventory_digest: str = "5" * 64) -> RootManifest:
    hashed = hashed or (_record("pyproject.toml"),)
    return RootManifest.build(
        root=root,
        kind="git",
        source_revision=REVISION,
        inventory_digest=inventory_digest,
        inventory_paths=(),
        excluded_paths=(".git",),
        hashed_paths=hashed,
        metadata_paths=(_record("docs", content=None, kind="directory"),),
    )


def _corpus() -> tuple[RootManifest, ...]:
    return (_manifest("/data/CoordExp/serena-light"), _manifest("/data/ms-swift"))


@dataclass(slots=True)
class FakeClock:
    """A monotonic clock the fake services advance by an exact per-step amount.

    ``drift`` makes every *read* cost time, which is how a polling wait is charged against
    the ceiling without spending real wall-clock seconds in a test.
    """

    now: float = 0.0
    drift: float = 0.0

    def __call__(self) -> float:
        self.now += self.drift
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
    cleanup_mutates: ProductionIdentity | None = None
    manifest_digest: str = MANIFEST_DIGEST
    evaluator: EvaluatorIdentity | None = None
    lock_requests: list[CandidateLockRequest] = field(default_factory=list)
    runtime_requests: list[RuntimeRequest] = field(default_factory=list)
    identity_calls: int = 0
    corpus_calls: int = 0
    evaluator_calls: int = 0
    evaluator_final: EvaluatorIdentity | None = None
    expectations: list[tuple[str, HelperExpectation]] = field(default_factory=list)
    cleanup_stages: list[str] = field(default_factory=list)
    digest_calls: int = 0
    order: list[str] = field(default_factory=list)

    @property
    def cleanup_called(self) -> bool:
        return bool(self.cleanup_stages)

    @property
    def resolution_count(self) -> int:
        return len(self.lock_requests)

    def _enter(self, step: str) -> None:
        self.order.append(step)
        self.clock.advance(self.step_seconds.get(step, 1.0))
        failure = self.failures.get(step)
        if failure is not None:
            raise failure

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity:
        assert isinstance(deadline, Deadline)
        # The second capture is the pre-publication bracket, and it is a separate step.
        self.evaluator_calls += 1
        self._enter("evaluator" if self.evaluator_calls == 1 else "evaluator_final")
        if self.evaluator_final is not None and self.evaluator_calls > 1:
            return self.evaluator_final
        return self.evaluator or _evaluator()

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity:
        self._enter("bootstrap")
        return _bootstrap()

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: HelperExpectation
    ) -> ProductionIdentity:
        assert repo_root.is_absolute()
        assert isinstance(deadline, Deadline)
        self.expectations.append(("capture_production_identity", expectation))
        step = ("identity_before", "identity_after", "identity_final")[min(self.identity_calls, 2)]
        self.identity_calls += 1
        self._enter(step)
        index = min(self.identity_calls - 1, len(self.identities) - 1)
        return self.identities[index]

    def compile_candidate_lock(
        self, request: CandidateLockRequest, deadline: Deadline, expectation: HelperExpectation
    ) -> CandidateLock:
        assert isinstance(deadline, Deadline)
        self.expectations.append(("compile_candidate_lock", expectation))
        self.lock_requests.append(request)
        self._enter("candidate_lock")
        assert self.lock is not None
        request.artifact_root.mkdir(parents=True, exist_ok=True)
        (request.artifact_root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.70\n")
        return self.lock

    def prepare_candidate_runtime(
        self,
        lock: CandidateLock,
        request: RuntimeRequest,
        deadline: Deadline,
        expectation: HelperExpectation,
    ) -> CandidateRuntime:
        assert isinstance(deadline, Deadline)
        self.expectations.append(("prepare_candidate_runtime", expectation))
        self.runtime_requests.append(request)
        self._enter("runtime")
        return _candidate_runtime(self.runtime_base)

    def runtime_manifest_digest(self, root: Path) -> str:
        assert root.is_absolute()
        self._enter("runtime_manifest")
        return self.manifest_digest

    def capture_corpus(
        self, deadline: Deadline, expectation: HelperExpectation
    ) -> tuple[RootManifest, ...]:
        assert isinstance(deadline, Deadline)
        self.expectations.append(("capture_corpus", expectation))
        step = "corpus_before" if self.corpus_calls == 0 else "corpus_after"
        self.corpus_calls += 1
        self._enter(step)
        index = min(self.corpus_calls - 1, len(self.corpora) - 1)
        return self.corpora[index]

    def artifact_tree_digest(self, owner_root: Path, evaluation_root: Path, deadline: Deadline) -> str:
        assert isinstance(deadline, Deadline)
        assert evaluation_root.is_relative_to(owner_root)
        self.digest_calls += 1
        self._enter("artifact_digest")
        return artifact_tree_digest(owner_root, evaluation_root)

    def cleanup(
        self,
        owner_root: Path,
        evaluation_root: Path,
        run_identity: str,
        stage: str,
        deadline: Deadline,
    ) -> tuple[str, ...]:
        assert evaluation_root.is_relative_to(owner_root)
        assert len(run_identity) == 64
        assert isinstance(deadline, Deadline)
        self.clock.advance(self.step_seconds.get("cleanup", 0.0))
        self.cleanup_stages.append(stage)
        self.order.append("cleanup")
        if self.cleanup_mutates is not None:
            # A cleanup that reports success but changed production identity anyway.
            self.identities.append(self.cleanup_mutates)
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.cleanup_summary


@dataclass(slots=True)
class _DriftingServices:
    """``FakeServices`` whose clock starts moving on every read once cleanup has run.

    Contention is measured, not slept away: the drift lets a held publication lock consume
    the remaining ceiling in a few polls instead of in real wall-clock seconds.
    """

    inner: FakeServices
    drift: float

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity:
        return self.inner.capture_evaluator_identity(deadline)

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity:
        return self.inner.capture_bootstrap_environment()

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: HelperExpectation
    ) -> ProductionIdentity:
        return self.inner.capture_production_identity(repo_root, deadline, expectation)

    def compile_candidate_lock(
        self, request: CandidateLockRequest, deadline: Deadline, expectation: HelperExpectation
    ) -> CandidateLock:
        return self.inner.compile_candidate_lock(request, deadline, expectation)

    def prepare_candidate_runtime(
        self,
        lock: CandidateLock,
        request: RuntimeRequest,
        deadline: Deadline,
        expectation: HelperExpectation,
    ) -> CandidateRuntime:
        return self.inner.prepare_candidate_runtime(lock, request, deadline, expectation)

    def runtime_manifest_digest(self, root: Path) -> str:
        return self.inner.runtime_manifest_digest(root)

    def capture_corpus(
        self, deadline: Deadline, expectation: HelperExpectation
    ) -> tuple[RootManifest, ...]:
        return self.inner.capture_corpus(deadline, expectation)

    def artifact_tree_digest(self, owner_root: Path, evaluation_root: Path, deadline: Deadline) -> str:
        return self.inner.artifact_tree_digest(owner_root, evaluation_root, deadline)

    def cleanup(
        self,
        owner_root: Path,
        evaluation_root: Path,
        run_identity: str,
        stage: str,
        deadline: Deadline,
    ) -> tuple[str, ...]:
        summary = self.inner.cleanup(owner_root, evaluation_root, run_identity, stage, deadline)
        self.inner.clock.drift = self.drift
        return summary


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


def _receipt_path(request: AdmissionRequest, receipt: AdmissionReceipt) -> Path:
    return admission_receipt_path(request.artifact_root, receipt.evaluation_identity, receipt.run_identity)


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
    assert not any(delta.unexpected or delta.control_changes for delta in receipt.write_deltas)
    assert receipt.next_action == NEXT_ACTION_PASS
    assert receipt.issues == ()
    assert receipt.candidate_lock.digest == LOCK_DIGEST


def test_admission_pass_binds_the_evaluator_bootstrap_and_runtime_identities(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.evaluator == _evaluator()
    assert receipt.bootstrap_environment == _bootstrap()
    binding = receipt.runtime_binding
    assert binding is not None
    assert binding.root == str(request_.runtime_base / LOCK_DIGEST)
    assert binding.lock_digest == LOCK_DIGEST
    assert binding.manifest_sha256 == MANIFEST_DIGEST
    assert binding.manifest_path == f"{binding.root}/runtime-manifest.json"


def test_the_measurement_window_brackets_every_phase_one_setup_operation(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """The first capture precedes the resolution and the runtime; the second follows them."""

    run_admission(request_, services=services, clock=clock)
    order = services.order
    assert order.index("corpus_before") < order.index("candidate_lock")
    assert order.index("corpus_before") < order.index("runtime")
    assert order.index("candidate_lock") < order.index("corpus_after")
    assert order.index("runtime") < order.index("corpus_after")
    assert order.index("corpus_after") < order.index("cleanup")
    assert order.index("cleanup") < order.index("artifact_digest")


def test_admission_resolves_the_candidate_lock_exactly_once(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"
    assert services.resolution_count == 1
    assert len(services.runtime_requests) == 1
    assert services.corpus_calls == 2
    assert services.identity_calls == 3


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


# --- identity ------------------------------------------------------------------


def test_evaluation_identity_binds_production_the_evaluator_host_and_the_artifact_root(
    request_: AdmissionRequest, tmp_path: Path
) -> None:
    identity = _production_identity()
    evaluator = _evaluator()
    first = evaluation_identity(request_, identity, evaluator)
    assert first == evaluation_identity(request_, identity, evaluator)
    assert len(first) == 64
    assert evaluation_identity(request_, _production_identity(build_identity="7" * 64), evaluator) != first
    assert evaluation_identity(replace(request_, exclude_newer="2026-08-10T00:00:00Z"), identity, evaluator) != first
    # Changed evaluator source, changed CLI host, and a different artifact root each differ.
    assert evaluation_identity(request_, identity, _evaluator(source_digest_seed="3")) != first
    assert (
        evaluation_identity(request_, identity, replace(evaluator, host_python_sha256="0" * 64)) != first
    )
    other_root = request_.repo_root / ".admission-artifacts" / "backend-eval" / "other"
    assert evaluation_identity(replace(request_, artifact_root=other_root), identity, evaluator) != first
    del tmp_path


def test_run_identity_is_unique_per_execution() -> None:
    first = new_run_identity("2026-08-11T00:00:00Z")
    second = new_run_identity("2026-08-11T00:00:00Z")
    assert first != second
    assert len(first) == len(second) == 64


def test_evaluator_identity_failure_is_incomplete_and_publishes_no_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["evaluator"] = IdentityError("imported evaluator module is not part of the closure")
    with pytest.raises(AdmissionError, match="evaluator_identity_capture_failed") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"


# --- immutable receipts ---------------------------------------------------------


def test_admission_publishes_one_immutable_receipt_per_run(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    first = run_admission(request_, services=services, clock=clock)
    path = _receipt_path(request_, first)
    assert path.read_bytes() == canonical_json(first.to_dict())
    assert AdmissionReceipt.from_dict(json.loads(path.read_text())) == first
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700

    second_services = _services(request_, clock)
    second = run_admission(request_, services=second_services, clock=clock)
    assert second.run_identity != first.run_identity
    assert second.evaluation_identity == first.evaluation_identity
    # Both the cold and the warm receipt survive; neither replaced the other.
    assert path.is_file()
    assert _receipt_path(request_, second).is_file()
    assert path.read_bytes() == canonical_json(first.to_dict())
    published = sorted(p.name for p in path.parent.iterdir() if p.suffix == ".json")
    assert published == sorted({f"{first.run_identity}.json", f"{second.run_identity}.json"})


def test_concurrent_runs_cannot_delete_or_replace_another_runs_receipt(
    request_: AdmissionRequest, tmp_path: Path
) -> None:
    del tmp_path

    def _one() -> AdmissionReceipt:
        local_clock = FakeClock()
        return run_admission(request_, services=_services(request_, local_clock), clock=local_clock)

    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = [future.result() for future in [pool.submit(_one) for _ in range(4)]]

    assert len({receipt.run_identity for receipt in receipts}) == 4
    for receipt in receipts:
        path = _receipt_path(request_, receipt)
        assert path.read_bytes() == canonical_json(receipt.to_dict())
    receipts_root = _receipt_path(request_, receipts[0]).parent
    assert len([entry for entry in receipts_root.iterdir() if entry.suffix == ".json"]) == 4
    assert not [entry for entry in receipts_root.iterdir() if entry.name.endswith(".tmp")]


def test_a_receipt_is_never_republished_over_an_existing_run_identity(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    import scripts.backend_eval.admission as admission_module

    first = run_admission(request_, services=services, clock=clock)
    monkeypatch.setattr(admission_module, "new_run_identity", lambda _started: first.run_identity)
    with pytest.raises(AdmissionError, match="already exists and is immutable"):
        run_admission(request_, services=_services(request_, clock), clock=clock)
    assert _receipt_path(request_, first).read_bytes() == canonical_json(first.to_dict())


def test_the_publication_lock_is_a_service_owned_control_file(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    lock_path = request_.artifact_root / receipt.evaluation_identity / PUBLICATION_LOCK_NAME
    assert lock_path.is_file()
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


# --- the artifact tree digest ----------------------------------------------------


def test_artifact_tree_digest_excludes_the_receipts_and_the_resolver_cache(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / CACHE_DIR_NAME).mkdir(parents=True)
    (root / CACHE_DIR_NAME / "blob").write_bytes(b"volatile")
    (root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.70\n")
    baseline = artifact_tree_digest(tmp_path, root)
    (root / RECEIPTS_DIR_NAME).mkdir()
    (root / RECEIPTS_DIR_NAME / "abc.json").write_bytes(b"{}\n")
    (root / PUBLICATION_LOCK_NAME).write_bytes(b"")
    (root / CACHE_DIR_NAME / "blob").write_bytes(b"changed")
    assert artifact_tree_digest(tmp_path, root) == baseline
    (root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.71\n")
    assert artifact_tree_digest(tmp_path, root) != baseline


def test_a_third_party_cache_file_keeps_its_tool_mode_behind_owned_ancestors(tmp_path: Path) -> None:
    """The exact 0600/0700 boundary: harness-owned artifacts versus uv's private cache.

    ``uv`` creates its own world-writable ``.lock`` inside the resolver cache.  The harness
    does not rewrite it: it is third-party, it sits behind a service-owned ``0700`` ancestor,
    and the receipt's artifact-tree digest excludes the cache entirely -- so its mode is
    outside the evidence the receipt binds.  Harness-owned artifacts get no such latitude.
    """

    root = tmp_path / "artifacts"
    (root / CACHE_DIR_NAME / "uv").mkdir(parents=True)
    (root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.70\n")
    baseline = artifact_tree_digest(tmp_path, root)

    tool_lock = root / CACHE_DIR_NAME / "uv" / ".lock"
    tool_lock.write_bytes(b"")
    os.chmod(tool_lock, 0o777)

    assert artifact_tree_digest(tmp_path, root) == baseline
    assert stat.S_IMODE(tool_lock.stat().st_mode) == 0o777


def test_a_published_run_owns_its_artifacts_at_0600_and_its_directories_at_0700(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """Everything the harness writes is 0600/0700, whatever the ambient umask was."""

    receipt = run_admission(request_, services=services, clock=clock)
    evaluation_root = request_.artifact_root / receipt.evaluation_identity

    for path in evaluation_root.rglob("*"):
        if CACHE_DIR_NAME in path.relative_to(evaluation_root).parts:
            continue  # third-party resolver cache: tool-defined modes, excluded from the digest
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected, path
    for ancestor in (request_.artifact_root, evaluation_root, evaluation_root / RECEIPTS_DIR_NAME):
        assert stat.S_IMODE(ancestor.stat().st_mode) == 0o700, ancestor


def test_artifact_tree_digest_refuses_a_symlinked_artifact(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (tmp_path / "outside").write_bytes(b"payload")
    (root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(AdmissionError, match="symlink"):
        artifact_tree_digest(tmp_path, root)


def test_artifact_tree_digest_refuses_a_symlinked_subdirectory(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    (root / "nested").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    (root / "nested" / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdmissionError, match="symlink"):
        artifact_tree_digest(tmp_path, root)


def test_artifact_tree_digest_refuses_a_special_file(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    os.mkfifo(root / "pipe")
    with pytest.raises(AdmissionError, match="special file"):
        artifact_tree_digest(tmp_path, root)


def test_read_artifact_bytes_refuses_a_fifo_promptly(tmp_path: Path) -> None:
    """A regular file swapped for a FIFO between the traversal's ``lstat`` and this open
    must fail fast rather than block on ``O_RDONLY`` with no writer.

    ``_collect_artifact_entries`` already refuses a FIFO it observes directly via ``lstat``,
    which the sibling ``test_artifact_tree_digest_refuses_a_special_file`` covers; this test
    exercises the guarded open itself, which is the only thing that stands between a
    same-name race and an indefinite hang.
    """

    root = tmp_path / "artifacts"
    root.mkdir()
    os.mkfifo(root / "pipe")
    before_fds = len(os.listdir("/proc/self/fd"))
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        started = time.monotonic()
        with pytest.raises(AdmissionError, match="not a regular file"):
            _read_artifact_bytes(dir_fd, "pipe", root / "pipe")
        elapsed = time.monotonic() - started
    finally:
        os.close(dir_fd)

    assert elapsed < 2.0
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_artifact_tree_digest_stops_cooperatively(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    root.mkdir()
    (root / "a").write_bytes(b"a")

    class _Stop(RuntimeError):
        pass

    def _check() -> None:
        raise _Stop("the ceiling was reached during the artifact traversal")

    with pytest.raises(_Stop):
        artifact_tree_digest(tmp_path, root, check=_check)


# --- the deadline ---------------------------------------------------------------


def test_admission_collect_timeout_still_publishes_a_trustworthy_timeout_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["corpus_after"] = float(COLLECT_WINDOW)
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert services.cleanup_called
    assert any(issue.startswith("admission_deadline_exceeded") for issue in receipt.issues)
    assert _receipt_path(request_, receipt).is_file()


def test_admission_checks_the_deadline_before_starting_an_external_step(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["runtime"] = float(COLLECT_WINDOW)
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert services.corpus_calls == 1
    assert any("prepare_candidate_runtime" in issue for issue in receipt.issues)


def test_admission_deadline_stops_before_a_candidate_resolution(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["identity_before"] = float(ADMISSION_BUDGET_SECONDS)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded"):
        run_admission(request_, services=services, clock=clock)
    assert services.resolution_count == 0


def test_a_finalization_that_consumes_the_budget_cannot_return_pass(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """A simulated artifact hash that eats the whole ceiling fails closed, without a receipt."""

    services.step_seconds["artifact_digest"] = float(ADMISSION_BUDGET_SECONDS)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    assert not receipts.exists() or not any(entry.suffix == ".json" for entry in receipts.iterdir())


def test_a_cleanup_that_starts_past_the_ceiling_fails_closed(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.step_seconds["corpus_after"] = float(ADMISSION_BUDGET_SECONDS)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    # The bracket in front of cleanup owns this refusal, so cleanup never starts at all.
    assert "cleanup:before" in error.value.failure.detail
    assert not services.cleanup_called


# --- the ceiling covers publication ------------------------------------------------


# Ten fake steps advance the clock by one second each before the artifact digest runs, and
# nothing after it advances the clock on its own.
_STEPS_BEFORE_ARTIFACT_DIGEST = 10.0


def _publication_window_seconds(remaining: float) -> float:
    """The artifact-digest cost that leaves exactly ``remaining`` seconds for publication."""

    return ADMISSION_BUDGET_SECONDS - remaining - _STEPS_BEFORE_ARTIFACT_DIGEST


def test_a_pass_is_not_published_when_the_ceiling_arrives_before_the_link(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """``ended_at`` is not the end of the run: linking the receipt is inside the ceiling too."""

    services.step_seconds["artifact_digest"] = _publication_window_seconds(3.0)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    assert "publish_receipt:link" in error.value.failure.detail

    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    # Neither a published receipt nor a half-written temporary survives the refusal.
    assert not receipts.exists() or list(receipts.iterdir()) == []


@pytest.mark.parametrize(
    "step", ["link_synced", "temporary_unlinked", "temporary_unlink_synced", "return"]
)
def test_a_pass_published_exactly_at_the_ceiling_is_withdrawn(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, step: str
) -> None:
    """Every post-link checkpoint withdraws this run's own link, the last one included.

    The final checkpoint sits immediately before the return, where only descriptor closes
    remain, so it is the one that decides whether an overrun can still be returned as a
    pass -- it must withdraw exactly like the earlier ones.
    """

    receipt = run_admission(request_, services=services, clock=clock)
    published = _receipt_path(request_, receipt)
    assert published.is_file()

    temporary = published.parent / f".{receipt.run_identity}.json.tmp"
    temporary.write_bytes(b"{}")
    receipts_fd = os.open(published.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        expired = Deadline.start(clock, 10.0)
        clock.advance(11.0)
        publication = _Publication(receipts_fd, published.parent, published.name, temporary.name, expired)
        with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
            publication.checkpoint(step)
    finally:
        os.close(receipts_fd)
    assert error.value.failure.status == "incomplete"
    assert f"publish_receipt:{step}" in error.value.failure.detail
    assert not published.exists()
    assert not temporary.exists()


# The publication steps that move the namespace or force durability after the atomic link.
# Each one has to be *followed* by a ceiling observation, or a run can earn its pass with
# work it did after the ceiling -- which is exactly the defect this pins closed.
_POST_LINK_OPERATIONS = ("_sync_directory", "_replace_temporary")


def _publication_step_order() -> tuple[str, ...]:
    """The post-link operations and checkpoints of ``_publish_receipt``, in source order."""

    import scripts.backend_eval.admission as admission_module

    module = ast.parse(inspect.getsource(admission_module))
    publish = next(
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef) and node.name == "_publish_receipt"
    )
    link = next(
        node.lineno
        for node in ast.walk(publish)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "link"
    )
    steps: list[tuple[int, str]] = []
    for node in ast.walk(publish):
        if not isinstance(node, ast.Call) or node.lineno <= link:
            continue
        if isinstance(node.func, ast.Name) and node.func.id in _POST_LINK_OPERATIONS:
            steps.append((node.lineno, node.func.id))
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "checkpoint":
            steps.append((node.lineno, "checkpoint"))
    return tuple(name for _, name in sorted(steps))


def test_no_post_link_publication_step_is_left_unchecked() -> None:
    """Structural pin: after the link, no mutation or barrier may be the last word.

    Behaviour tests can only slow the barriers that exist today.  This asserts the shape
    the whole argument rests on: every post-link namespace mutation and durability barrier
    is immediately followed by a ceiling observation, and the very last statement before the
    return is one too -- so there is no step whose cost a later pass can absorb.
    """

    steps = _publication_step_order()

    assert steps.count("checkpoint") == 4
    assert steps[-1] == "checkpoint", steps
    assert all(
        later == "checkpoint" for earlier, later in itertools.pairwise(steps) if earlier != "checkpoint"
    ), steps
    assert steps == (
        "_sync_directory",
        "checkpoint",
        "_replace_temporary",
        "checkpoint",
        "_sync_directory",
        "checkpoint",
        "checkpoint",
    )


def test_a_checkpoint_inside_the_ceiling_keeps_the_published_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """The withdrawal is driven by expiry alone; a live deadline never touches the link."""

    receipt = run_admission(request_, services=services, clock=clock)
    published = _receipt_path(request_, receipt)
    receipts_fd = os.open(published.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        live = Deadline.start(clock, 100.0)
        _Publication(receipts_fd, published.parent, published.name, ".absent.tmp", live).checkpoint("return")
    finally:
        os.close(receipts_fd)
    assert published.is_file()


@pytest.mark.parametrize(
    ("boundary", "step"),
    [(1, "link_synced"), (2, "temporary_unlink_synced")],
)
def test_a_slow_post_link_directory_sync_returns_no_pass_and_leaves_none(
    request_: AdmissionRequest,
    services: FakeServices,
    clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    boundary: int,
    step: str,
) -> None:
    """The exact regression: delay a post-link ``fsync`` and the pass must not survive it.

    Both post-link durability barriers are covered -- the one right after the link and the
    one after the temporary is unlinked -- because a check that happens *before* the last
    barrier would let a run return a pass it earned only after the ceiling.
    """

    import scripts.backend_eval.admission as admission_module

    real_sync = admission_module._sync_directory
    calls = {"n": 0}

    def slow_sync(dir_fd: int, evaluation_root: Path) -> None:
        calls["n"] += 1
        # Barrier 1 is the post-link sync; barrier 2 follows the temporary unlink.
        if calls["n"] == boundary:
            clock.advance(float(ADMISSION_BUDGET_SECONDS))
        real_sync(dir_fd, evaluation_root)

    monkeypatch.setattr(admission_module, "_sync_directory", slow_sync)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    assert f"publish_receipt:{step}" in error.value.failure.detail

    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    assert not receipts.exists() or list(receipts.iterdir()) == []


def test_a_slow_post_link_temporary_unlink_returns_no_pass_and_leaves_none(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A namespace mutation after the link is a checkpoint boundary too, not just a sync."""

    import scripts.backend_eval.admission as admission_module

    real_replace = admission_module._replace_temporary
    seen: list[str] = []

    def slow_replace(dir_fd: int, evaluation_root: Path, temporary: str) -> None:
        seen.append(temporary)
        real_replace(dir_fd, evaluation_root, temporary)
        # Only the post-link unlink is slowed; the pre-write one must stay inside the budget.
        if len(seen) == 2:
            clock.advance(float(ADMISSION_BUDGET_SECONDS))

    monkeypatch.setattr(admission_module, "_replace_temporary", slow_replace)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    assert "publish_receipt:temporary_unlinked" in error.value.failure.detail

    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    assert not receipts.exists() or list(receipts.iterdir()) == []


def test_a_cleanup_that_overruns_the_ceiling_returns_no_pass(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """Cleanup is inside the ceiling: spending the budget there cannot yield a later pass."""

    services.step_seconds["cleanup"] = float(ADMISSION_BUDGET_SECONDS)
    with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    assert "cleanup:after" in error.value.failure.detail
    assert services.cleanup_called

    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    assert not receipts.exists() or not any(entry.suffix == ".json" for entry in receipts.iterdir())


def test_the_production_cleanup_receives_and_honours_the_deadline(tmp_path: Path) -> None:
    """The real cleanup implementation checks the ceiling around its own syscalls."""

    evaluation_root = tmp_path / "evaluation"
    (evaluation_root / RECEIPTS_DIR_NAME).mkdir(parents=True)
    run_identity = "b" * 64
    (evaluation_root / RECEIPTS_DIR_NAME / f".{run_identity}.json.tmp").write_bytes(b"{}")

    clock = FakeClock()
    expired = Deadline.start(clock, 10.0)
    clock.advance(11.0)
    with pytest.raises(DeadlineExceeded, match="cleanup:open"):
        ProductionAdmissionServices().cleanup(tmp_path, evaluation_root, run_identity, "pass", expired)
    # Refusing early means the temporary is still there for a run that has budget left.
    assert (evaluation_root / RECEIPTS_DIR_NAME / f".{run_identity}.json.tmp").is_file()

    live = Deadline.start(FakeClock(), 100.0)
    assert ProductionAdmissionServices().cleanup(tmp_path, evaluation_root, run_identity, "pass", live) == (
        "removed_temporary_receipt",
    )
    assert not (evaluation_root / RECEIPTS_DIR_NAME / f".{run_identity}.json.tmp").exists()


def test_a_held_publication_lock_cannot_carry_a_run_past_its_ceiling(
    request_: AdmissionRequest, clock: FakeClock
) -> None:
    """Waiting for another run's publication is bounded by the same ceiling."""

    identity = evaluation_identity(request_, _production_identity(), _evaluator())
    evaluation_root = request_.artifact_root / identity
    evaluation_root.mkdir(parents=True)
    lock_path = evaluation_root / PUBLICATION_LOCK_NAME
    holder = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    services = _DriftingServices(_services(request_, clock), drift=200.0)
    started = time.monotonic()
    try:
        with pytest.raises(AdmissionError, match="admission_deadline_exceeded") as error:
            run_admission(request_, services=services, clock=clock)
    finally:
        os.close(holder)
    assert "publish_receipt:lock" in error.value.failure.detail
    # A blocking flock would have waited for the holder forever.
    assert time.monotonic() - started < 20.0
    receipts = evaluation_root / RECEIPTS_DIR_NAME
    assert not receipts.exists() or not any(entry.suffix == ".json" for entry in receipts.iterdir())


# --- typed failures --------------------------------------------------------------


def test_admission_resolution_failure_raises_without_publishing_a_receipt(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["candidate_lock"] = CandidateLockError("uv pip compile failed: proxy refused")
    with pytest.raises(AdmissionError, match="candidate_resolution_failed") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    assert not any(path.suffix == ".json" for path in request_.artifact_root.rglob("*"))
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
    assert receipt.runtime_binding is None
    assert receipt.root_manifests_after == ()
    assert receipt.write_deltas == ()
    assert any(issue.startswith("runtime_preparation_failed") for issue in receipt.issues)
    assert _receipt_path(request_, receipt).is_file()
    assert receipt.production_identity_after == services.identities[-1]


def test_a_changed_runtime_manifest_is_held(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.manifest_digest = "0" * 64
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    assert any(issue.startswith("runtime_manifest_changed") for issue in receipt.issues)


def test_admission_unstable_corpus_root_is_incomplete(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.corpora = [_corpus(), (_manifest("/data/CoordExp/serena-light"),)]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any(issue.startswith("unstable_corpus_root") for issue in receipt.issues)
    assert receipt.write_deltas == ()


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
    assert _receipt_path(request_, receipt).is_file()


def test_a_changed_manifest_control_is_held_rather_than_called_unstable(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    moved = (
        _manifest("/data/CoordExp/serena-light", inventory_digest="6" * 64),
        _manifest("/data/ms-swift"),
    )
    services.corpora = [_corpus(), moved]
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    controls = {delta.root: delta.control_changes for delta in receipt.write_deltas}
    assert controls["/data/CoordExp/serena-light"] == ("inventory_digest",)
    assert any(issue.startswith("unexpected_evaluation_writes") for issue in receipt.issues)


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


def test_admission_receipt_brackets_cleanup_with_the_final_production_identity(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"
    assert services.identity_calls == 3
    assert services.cleanup_stages == ["pass"]
    assert receipt.production_identity_after == services.identities[-1]


def test_admission_cleanup_that_mutates_production_identity_is_held(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    drifted = _production_identity(build_identity="7" * 64)
    services.cleanup_mutates = drifted
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "hold"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert receipt.production_identity_after == drifted
    assert receipt.production_identity_before != receipt.production_identity_after
    assert [issue for issue in receipt.issues if issue.startswith("production_identity_changed")] == [
        "production_identity_changed: production identity changed: build_identity"
    ]


def test_admission_final_production_identity_failure_fails_closed(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["identity_final"] = ProductionIdentityError("cannot capture production identity")
    with pytest.raises(AdmissionError, match="production_identity_capture_failed") as error:
        run_admission(request_, services=services, clock=clock)
    assert error.value.failure.status == "incomplete"
    identity = evaluation_identity(request_, services.identities[0], _evaluator())
    receipts = request_.artifact_root / identity / RECEIPTS_DIR_NAME
    assert not receipts.exists() or not any(entry.suffix == ".json" for entry in receipts.iterdir())


def test_admission_cleanup_failure_overrides_a_hold(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    dirtied = (
        _manifest("/data/CoordExp/serena-light", hashed=(_record("pyproject.toml", content="8" * 64),)),
        _manifest("/data/ms-swift"),
    )
    services.corpora = [_corpus(), dirtied]
    services.cleanup_error = OSError("cannot remove the partial receipt")
    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "incomplete"
    assert any(issue.startswith("unexpected_evaluation_writes") for issue in receipt.issues)
    assert any(issue.startswith("cleanup_failed") for issue in receipt.issues)


def test_production_cleanup_removes_only_this_runs_temporary_receipt(tmp_path: Path) -> None:
    evaluation_root = tmp_path / "evaluation"
    receipts = evaluation_root / RECEIPTS_DIR_NAME
    receipts.mkdir(parents=True)
    (evaluation_root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.70\n")
    mine = "1" * 64
    other = "2" * 64
    (receipts / f".{mine}.json.tmp").write_bytes(b"partial")
    (receipts / f"{other}.json").write_bytes(b"{}\n")
    (receipts / f".{other}.json.tmp").write_bytes(b"another run's partial state")

    live = Deadline.start(FakeClock(), 100.0)
    summary = ProductionAdmissionServices().cleanup(tmp_path, evaluation_root, mine, "incomplete", live)

    assert summary == ("removed_temporary_receipt",)
    assert not (receipts / f".{mine}.json.tmp").exists()
    # Another execution's receipt and temporary are never touched.
    assert (receipts / f"{other}.json").is_file()
    assert (receipts / f".{other}.json.tmp").is_file()
    assert (evaluation_root / LOCK_FILE_NAME).is_file()
    assert ProductionAdmissionServices().cleanup(tmp_path, evaluation_root, mine, "pass", live) == ()


def test_production_cleanup_tolerates_a_missing_evaluation_root(tmp_path: Path) -> None:
    live = Deadline.start(FakeClock(), 100.0)
    assert ProductionAdmissionServices().cleanup(tmp_path, tmp_path / "absent", "1" * 64, "incomplete", live) == ()


# --- ancestor substitution: the two claims that were false ---------------------------


def _owned_tree(tmp_path: Path) -> tuple[Path, Path, str]:
    """A declared owner root with an evaluation root two components below it."""

    owner = tmp_path / "owner"
    evaluation_root = owner / "artifacts" / "identity"
    (evaluation_root / RECEIPTS_DIR_NAME).mkdir(parents=True)
    return owner, evaluation_root, "c" * 64


def test_cleanup_refuses_a_symlinked_ancestor_and_never_unlinks_outside_the_owner_root(
    tmp_path: Path,
) -> None:
    """The reproduced exploit: cleanup followed a symlinked ancestor and unlinked a decoy.

    ``os.open(evaluation_root / "receipts", O_NOFOLLOW)`` guards only the *last* component, so
    a swapped ``artifacts`` -- or evaluation-identity directory -- pointed the whole walk at
    another tree, and cleanup unlinked this run's temporary name inside it.  The walk now
    starts at the declared owner root's descriptor and opens every component from its parent.
    """

    owner, evaluation_root, run_identity = _owned_tree(tmp_path)
    temporary = f".{run_identity}.json.tmp"

    outside = tmp_path / "outside" / "identity" / RECEIPTS_DIR_NAME
    outside.mkdir(parents=True)
    decoy = outside / temporary
    decoy.write_bytes(b"another owner's file")

    shutil.rmtree(owner / "artifacts")
    (owner / "artifacts").symlink_to(tmp_path / "outside")

    live = Deadline.start(FakeClock(), 100.0)
    with pytest.raises(AdmissionError) as error:
        ProductionAdmissionServices().cleanup(owner, evaluation_root, run_identity, "pass", live)

    assert error.value.failure.code == "cleanup_failed"
    assert error.value.failure.status == "incomplete"
    assert decoy.is_file()
    assert decoy.read_bytes() == b"another owner's file"


def test_cleanup_refuses_a_symlinked_receipts_directory_itself(tmp_path: Path) -> None:
    owner, evaluation_root, run_identity = _owned_tree(tmp_path)
    temporary = f".{run_identity}.json.tmp"
    outside = tmp_path / "outside-receipts"
    outside.mkdir()
    decoy = outside / temporary
    decoy.write_bytes(b"another owner's file")

    (evaluation_root / RECEIPTS_DIR_NAME).rmdir()
    (evaluation_root / RECEIPTS_DIR_NAME).symlink_to(outside)

    live = Deadline.start(FakeClock(), 100.0)
    with pytest.raises(AdmissionError):
        ProductionAdmissionServices().cleanup(owner, evaluation_root, run_identity, "pass", live)

    assert decoy.is_file()


def test_cleanup_still_removes_its_own_temporary_through_the_confined_walk(tmp_path: Path) -> None:
    """The repair does not weaken per-run temporary ownership: the walk still finds it."""

    owner, evaluation_root, run_identity = _owned_tree(tmp_path)
    mine = evaluation_root / RECEIPTS_DIR_NAME / f".{run_identity}.json.tmp"
    mine.write_bytes(b"partial")
    other = evaluation_root / RECEIPTS_DIR_NAME / f".{'d' * 64}.json.tmp"
    other.write_bytes(b"another run's partial state")

    live = Deadline.start(FakeClock(), 100.0)
    summary = ProductionAdmissionServices().cleanup(owner, evaluation_root, run_identity, "pass", live)

    assert summary == ("removed_temporary_receipt",)
    assert not mine.exists()
    assert other.is_file()


def test_the_artifact_tree_digest_refuses_a_substituted_ancestor(tmp_path: Path) -> None:
    """The same defect on the evidence side: the digest could describe another tree entirely.

    ``artifact_tree_digest`` opened the whole absolute evaluation root under one
    ``O_NOFOLLOW``.  A swapped intermediate component therefore made it traverse -- and
    publish the digest of -- a tree the run never wrote.
    """

    owner, evaluation_root, _run_identity = _owned_tree(tmp_path)
    (evaluation_root / LOCK_FILE_NAME).write_bytes(b"ty==0.0.70\n")
    baseline = artifact_tree_digest(owner, evaluation_root)

    other = tmp_path / "other" / "identity"
    other.mkdir(parents=True)
    (other / LOCK_FILE_NAME).write_bytes(b"ty==9.9.99\n")
    shutil.rmtree(owner / "artifacts")
    (owner / "artifacts").symlink_to(tmp_path / "other")

    with pytest.raises(AdmissionError) as error:
        artifact_tree_digest(owner, evaluation_root)

    assert error.value.failure.code == "artifact_digest_failed"
    # The other tree is real and digests differently; the point is that the run never
    # published *its* digest under this evaluation root.
    assert baseline != artifact_tree_digest(tmp_path, other)


def test_the_artifact_tree_digest_refuses_an_evaluation_root_outside_its_owner(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(AdmissionError, match="not below the declared owner root"):
        artifact_tree_digest(tmp_path / "owner", outside)


# --- the evaluator identity is re-measured before publication -------------------------


def test_a_late_evaluator_mutation_cannot_yield_a_pass(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """An evaluator or helper edited after the last ordinary helper call is never a ``pass``.

    The first capture bound every production-helper child of this run; nothing after it
    re-read the evaluator's own bytes, so a late edit would have been published under an
    identity that no longer described the code on disk.
    """

    services.evaluator_final = _evaluator(source_digest_seed="1")

    receipt = run_admission(request_, services=services, clock=clock)

    assert receipt.status == "hold"
    assert receipt.next_action == NEXT_ACTION_HOLD
    assert any(issue.startswith("evaluator_identity_changed") for issue in receipt.issues)
    assert "source_digest" in " ".join(receipt.issues)
    assert services.order.index("cleanup") < services.order.index("evaluator_final")


def test_the_pre_publication_evaluator_capture_is_inside_the_ceiling(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """It is a finalization step like every other one, judged by the same absolute ceiling."""

    services.step_seconds["evaluator_final"] = ADMISSION_BUDGET_SECONDS + 1.0

    with pytest.raises(AdmissionError) as error:
        run_admission(request_, services=services, clock=clock)

    assert error.value.failure.code == "admission_deadline_exceeded"


def test_a_pre_publication_evaluator_capture_that_fails_fails_closed(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    services.failures["evaluator_final"] = IdentityError("the evaluator source closure is empty")

    with pytest.raises(AdmissionError) as error:
        run_admission(request_, services=services, clock=clock)

    assert error.value.failure.code == "evaluator_identity_capture_failed"
    assert error.value.failure.status == "incomplete"


def test_an_unchanged_evaluator_publishes_the_pass_it_earned(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    receipt = run_admission(request_, services=services, clock=clock)

    assert receipt.status == "pass"
    assert services.evaluator_calls == 2
    assert not any(issue.startswith("evaluator_identity_changed") for issue in receipt.issues)


# --- the execution expectation is carried structurally --------------------------------


def test_every_production_helper_call_carries_this_runs_own_expectation(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """No ambient process-global pin decides which bytes a child may execute."""

    receipt = run_admission(request_, services=services, clock=clock)
    assert receipt.status == "pass"

    expected = HelperExpectation.from_identity(_evaluator())
    steps = [step for step, _expectation in services.expectations]
    assert steps.count("capture_production_identity") == 3
    assert steps.count("capture_corpus") == 2
    assert {"compile_candidate_lock", "prepare_candidate_runtime"} <= set(steps)
    assert all(carried == expected for _step, carried in services.expectations)


def test_two_admissions_in_one_process_carry_their_own_expectations(
    request_: AdmissionRequest, clock: FakeClock, services: FakeServices
) -> None:
    """Sequential runs never contaminate each other: each binds only its own identity."""

    first = run_admission(request_, services=services, clock=clock)

    second_clock = FakeClock()
    second_services = replace(services, clock=second_clock)
    second_services.expectations = []
    second_services.order = []
    second_services.identity_calls = 0
    second_services.corpus_calls = 0
    second_services.evaluator_calls = 0
    second_services.cleanup_stages = []
    second_services.lock_requests = []
    second_services.runtime_requests = []
    second_services.evaluator = _evaluator(source_digest_seed="3", child_digest_seed="2")
    second = run_admission(request_, services=second_services, clock=second_clock)

    assert first.status == second.status == "pass"
    assert first.evaluation_identity != second.evaluation_identity
    first_expected = HelperExpectation.from_identity(_evaluator())
    second_expected = HelperExpectation.from_identity(
        _evaluator(source_digest_seed="3", child_digest_seed="2")
    )
    assert first_expected != second_expected
    assert all(carried == second_expected for _step, carried in second_services.expectations)


def test_an_identity_that_cannot_authorize_a_child_holds_the_run(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """An evaluator identity that does not name the child program publishes no ``pass``."""

    incomplete = EvaluatorIdentity.build(
        source_files=(("admission.py", "9" * 64), ("models.py", "8" * 64)),
        source_commit="7" * 40,
        source_clean=True,
        production_root="/data/CoordExp/serena-light/src",
        production_files=tuple((relative, "5" * 64) for relative in CHILD_EXECUTED_HELPERS),
        production_clean=True,
        host_python_path="/data/CoordExp/.worktrees/serena-light-backend-eval/.venv/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256="6" * 64,
        host_python_version="3.12.11",
    )
    services.evaluator = incomplete

    with pytest.raises(AdmissionError) as error:
        run_admission(request_, services=services, clock=clock)

    assert error.value.failure.code == "evaluator_source_binding_failed"
    assert error.value.failure.status == "hold"


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


def test_redaction_uses_path_component_containment_not_a_string_prefix(
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
) -> None:
    """``/data/ms-swift-secret`` is an undeclared sibling of the declared ``/data/ms-swift``."""

    services.failures["runtime"] = RuntimePreparationError(
        "cannot read /data/ms-swift-secret/token while scanning /data/ms-swift/setup.cfg"
    )
    receipt = run_admission(request_, services=services, clock=clock)
    joined = " ".join(receipt.issues)
    assert "/data/ms-swift-secret" not in joined
    assert "<redacted-path>" in joined
    assert "/data/ms-swift/setup.cfg" in joined


def test_admission_issue_order_is_stable_across_identical_runs(request_: AdmissionRequest) -> None:
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
    assert "run_identity=" in out
    assert "evaluator_source_digest=" in out
    assert "runtime_manifest_sha256=" in out
    assert "corpus_in_scope_paths=" in out


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
    request_: AdmissionRequest, services: FakeServices, clock: FakeClock
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
