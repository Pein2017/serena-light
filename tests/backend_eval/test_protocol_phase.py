"""Task 8 protocol-phase orchestration contract."""

from __future__ import annotations

import os
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
    PROTOCOL_PHASE_NEXT_ACTION_PASS,
    PROTOCOL_WITNESS_SCHEMA_VERSION,
    AdmissionBinding,
    AdmissionRootWitness,
    CandidateLock,
    CandidatePackage,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    EvaluatorIdentity,
    LifecycleEvidence,
    LockEvidence,
    PathRecord,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    canonical_json,
)
from scripts.backend_eval.process import Deadline, DeadlineExceeded
from scripts.backend_eval.protocol_lifecycle import LifecycleBatteryResult
from scripts.backend_eval.protocol_parent import (
    ParentAdmissionError,
    ParentAdmissionFailure,
)
from scripts.backend_eval.protocol_phase import ProtocolRunRoot
from scripts.backend_eval.protocol_witness import (
    PROTOCOL_WITNESS_SCHEMA_VERSION as BEHAVIOR_WITNESS_SCHEMA_VERSION,
)
from scripts.backend_eval.protocol_witness import ProtocolBehaviorWitness
from scripts.backend_eval.publish import (
    PUBLICATION_FAILED,
    PublicationError,
    PublicationFailure,
)
from scripts.backend_eval.source_binding import SourceBindingError
from serena_light.lsp.adapter import RawLspProviders

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_SHA_F = "f" * 64
_REVISION = "1" * 40


def test_protocol_phase_module_exposes_the_non_publishing_test_seam() -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    assert callable(evaluate_protocol_phase)


def _request(tmp_path: Path, **overrides: object):
    from scripts.backend_eval.protocol_phase import ProtocolPhaseRequest

    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    target = workspace / "known.py"
    target.write_text("known = 1\n", encoding="utf-8")
    runtime_base = tmp_path / "runtime"
    runtime_base.mkdir(exist_ok=True)
    repo_manifest = _manifest(repo)
    workspace_manifest = _manifest(workspace)
    fields: dict[str, object] = {
        "repo_root": repo,
        "artifact_root": repo / ".admission-artifacts/backend-eval",
        "runtime_base": runtime_base,
        "parent_evaluation_identity": _SHA_A,
        "parent_run_identity": _SHA_B,
        "parent_receipt_sha256": _SHA_C,
        "parent_artifact_tree_digest": _SHA_D,
        "parent_candidate_lock_digest": _SHA_E,
        "parent_runtime_manifest_sha256": _SHA_F,
        "parent_production_source_revision": _REVISION,
        "parent_production_dependency_lock_digest": _SHA_C,
        "parent_production_build_identity": _SHA_D,
        "workspace_root": workspace,
        "target": target,
        "symbol_position": (3, 7),
        "workspace_snapshots": (
            AdmissionRootWitness(
                root=str(repo),
                kind="git",
                source_revision=_REVISION,
                manifest_digest=repo_manifest.manifest_digest,
            ),
            AdmissionRootWitness(
                root=str(workspace),
                kind="git",
                source_revision=_REVISION,
                manifest_digest=workspace_manifest.manifest_digest,
            ),
        ),
    }
    fields.update(overrides)
    return ProtocolPhaseRequest(**fields)


def test_protocol_phase_request_requires_explicit_absolute_roots_and_target_scope(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    assert request.target == request.workspace_root / "known.py"

    with pytest.raises(ValueError, match="repo_root.*absolute"):
        _request(tmp_path, repo_root=Path("relative"))
    with pytest.raises(ValueError, match="target.*workspace"):
        _request(tmp_path, target=tmp_path / "outside.py")


def test_protocol_phase_request_rejects_duplicate_or_noncanonical_snapshots(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    duplicate = request.workspace_snapshots[0]
    with pytest.raises(ValueError, match="workspace_snapshots"):
        _request(tmp_path, workspace_snapshots=(duplicate, duplicate))


def _path_record(path: str = "known.py", *, digest: str = _SHA_A) -> PathRecord:
    return PathRecord(
        path=path,
        kind="file",
        disposition="tracked",
        size=10,
        mtime_ns=1,
        inode=1,
        symlink_target=None,
        content_sha256=digest,
    )


def _manifest(root: Path, *, digest: str = _SHA_A) -> RootManifest:
    return RootManifest.build(
        root=str(root),
        kind="git",
        source_revision=_REVISION,
        inventory_digest=_SHA_B,
        inventory_paths=("known.py",),
        excluded_paths=(".git",),
        hashed_paths=(_path_record(digest=digest),),
        metadata_paths=(),
    )


def _production_identity(repo: Path, **overrides: object) -> ProductionIdentity:
    fields: dict[str, object] = {
        "pyproject_toml_sha256": _SHA_A,
        "uv_lock_sha256": _SHA_B,
        "package_lock_json_sha256": _SHA_C,
        "dependency_lock_digest": _SHA_C,
        "build_identity": _SHA_D,
        "runtime_paths": (
            ("cli", str(repo / "src/serena_light/cli.py")),
            ("server", str(repo / "src/serena_light/server.py")),
        ),
    }
    fields.update(overrides)
    return ProductionIdentity(**cast("dict[str, Any]", fields))


def _evaluator_identity(*, source_digest: str = _SHA_A) -> EvaluatorIdentity:
    return EvaluatorIdentity.build(
        source_files=(("protocol_phase.py", source_digest),),
        source_commit=_REVISION,
        source_clean=True,
        production_root="/data/evaluator/src",
        production_files=(("src/serena_light/lsp/adapter.py", _SHA_B),),
        production_clean=True,
        host_python_path="/conda/ms/bin/python",
        host_python_realpath="/conda/ms/bin/python3.12",
        host_python_sha256=_SHA_C,
        host_python_version="3.12.11",
    )


def _resolved_package(name: str) -> ResolvedPackage:
    return ResolvedPackage(
        name=name,
        version="0.0.1",
        requirement=f"{name}==0.0.1",
        artifact_hashes=(_SHA_A,),
    )


def _candidate_package(name: str) -> CandidatePackage:
    return CandidatePackage(
        name=name,
        version="0.0.1",
        requirement=f"{name}==0.0.1",
        artifact_hashes=(_SHA_A,),
        executable_relpath=f"bin/{name}",
    )


def _candidate_lock() -> CandidateLock:
    packages = (
        _resolved_package("click"),
        _resolved_package("pyrefly"),
        _resolved_package("ty"),
    )
    return CandidateLock(
        digest=_SHA_E,
        exclude_newer="2026-08-12T00:00:00Z",
        resolved_packages=packages,
        candidates=(_candidate_package("pyrefly"), _candidate_package("ty")),
        lock_evidence=LockEvidence.build(
            raw_sha256=_SHA_E,
            raw_size=512,
            resolved_packages=packages,
        ),
    )


def _lifecycle(*, diagnostics_mode: str = "push", **overrides: object) -> LifecycleEvidence:
    fields: dict[str, object] = {
        "cold_readiness_seconds": 0.25,
        "diagnostics_mode": diagnostics_mode,
        "content_modified_count": 0,
        "request_cancelled_count": 0,
        "retry_seam_disabled": True,
        "bounded_timeout_observed": True,
        "crash_handled": True,
        "shutdown_clean": True,
        "cleanup_clean": True,
        "proxy_rejected": True,
        "minimal_environment_verified": True,
        "redaction_verified": True,
    }
    fields.update(overrides)
    return LifecycleEvidence(**cast("dict[str, Any]", fields))


def _providers(*, implementation: bool = True) -> RawLspProviders:
    return RawLspProviders(
        definition=True,
        declaration=False,
        implementation=implementation,
        references=True,
        document_symbols=True,
        workspace_symbols=True,
    )


def _outcome(
    candidate: str,
    *,
    failed_capability: str | None = None,
    seam_pull_only: bool = False,
) -> CandidateProtocolOutcome:
    providers = _providers(implementation=not (candidate == "ty" or seam_pull_only))
    capabilities: list[CapabilityEvidence] = []
    for name in (
        "definition",
        "document_symbols",
        "implementation",
        "references",
        "workspace_symbols",
    ):
        advertised = getattr(providers, name)
        failed = name == failed_capability
        capabilities.append(
            CapabilityEvidence(
                name=name,
                advertised=advertised,
                accepted=(None if not advertised else not failed),
                normalized_valid=(None if not advertised else not failed),
                task_utility=CAPABILITY_TASK_UTILITY_DEFERRED,
                notes=(
                    "locked ty version does not advertise textDocument/implementation"
                    if candidate == "ty" and name == "implementation"
                    else ("controlled capability failed" if failed else "")
                ),
            )
        )
    issues = (
        (f"{failed_capability}: controlled capability failed",)
        if failed_capability is not None
        else (
            ("current product seam requires push diagnostics",)
            if seam_pull_only
            else ()
        )
    )
    return CandidateProtocolOutcome(
        candidate=candidate,
        engine_version="1.1.403" if candidate == "pyright" else "0.0.1",
        raw_providers=providers,
        capabilities=tuple(capabilities),
        lifecycle=_lifecycle(
            diagnostics_mode="pull" if candidate == "ty" or seam_pull_only else "push",
            bounded_timeout_observed=False,
            crash_handled=False,
            proxy_rejected=False,
            minimal_environment_verified=False,
            redaction_verified=False,
        ),
        gate_disposition=(
            "fail"
            if failed_capability is not None
            else ("seam_incompatible_pull_only" if seam_pull_only else "pass")
        ),
        issues=issues,
    )


def _witness(
    candidate: str,
    *,
    passed: bool = True,
    configuration_conclusive: bool = True,
) -> ProtocolBehaviorWitness:
    transport = {
        "pyright": "workspace_configuration",
        "ty": "workspace_configuration",
        "pyrefly": "initialization_options",
    }[candidate]
    config_path = None if candidate == "pyright" else f"/runtime/config/{candidate}/config"
    issues = () if passed else ("configuration attribution was not proven",)
    return ProtocolBehaviorWitness(
        schema_version=PROTOCOL_WITNESS_SCHEMA_VERSION,
        candidate=candidate,
        passed=passed,
        fixture_sha256=_SHA_A,
        fixture_mode=0o600,
        fixture_unchanged=True,
        selected_interpreter="/conda/ms/bin/python",
        configuration_transport=(transport if configuration_conclusive else "unobserved"),
        configuration_interpreter=(
            "/conda/ms/bin/python" if configuration_conclusive else None
        ),
        configuration_path=(config_path if configuration_conclusive else None),
        configuration_payload_sha256=(_SHA_B if configuration_conclusive else None),
        configuration_request_count=(
            1 if candidate in {"pyright", "ty"} and configuration_conclusive else 0
        ),
        configuration_application_proven=configuration_conclusive,
        external_definition_relative_path="generation/configuration_utils.py",
        position_encoding="utf-16",
        y_raw_range=(6, 0, 6, 1),
        y_decoded_range=(6, 0, 6, 1),
        push_diagnostics_claimed=candidate != "ty",
        exact_uri_diagnostics=candidate != "ty",
        missing_import_diagnostic=candidate != "ty",
        exact_uri_publish_count=1 if candidate != "ty" else 0,
        exact_uri_diagnostic_count=1 if candidate != "ty" else 0,
        diagnostics_completion_reason=(
            "missing_import_observed" if candidate != "ty" else "not_applicable_pull"
        ),
        first_normalized_capability="definition",
        first_normalized_count=1,
        first_readiness_seconds=0.25,
        raw_providers=tuple(
            sorted(
                {
                    "declaration": False,
                    "definition": True,
                    "document_symbols": True,
                    "implementation": candidate != "ty",
                    "references": True,
                    "workspace_symbols": True,
                }.items()
            )
        ),
        terminal_error_count=0,
        cleanup_error_count=0,
        issues=issues,
    )


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeServices:
    def __init__(self, request: Any, tmp_path: Path) -> None:
        self.request = request
        self.tmp_path = tmp_path
        self.calls: list[tuple[str, object]] = []
        self.published = False
        self.publish_error: PublicationError | None = None
        self.fail_parent = False
        self.drift_evaluator = False
        self.mutate_runtime_file = False
        self.mutate_after = False
        self.drift_production = False
        self.source_uncertain_candidate: str | None = None
        self.timeout_candidate: str | None = None
        self.failed_candidate: str | None = None
        self.seam_candidate: str | None = None
        self.configuration_inconclusive: str | None = None
        self.infrastructure_candidate: str | None = None
        self._evaluator_captures = 0
        self._production_captures = 0
        self._runtime_loads = 0
        self.manifests = (
            _manifest(request.repo_root),
            _manifest(request.workspace_root),
        )
        self.run_roots: list[ProtocolRunRoot] = []
        parent_roots = tuple(
            AdmissionRootWitness(
                root=manifest.root,
                kind=manifest.kind,
                source_revision=manifest.source_revision,
                manifest_digest=manifest.manifest_digest,
            )
            for manifest in self.manifests
        )
        runtime_root = request.runtime_base / _SHA_E
        self.binding = AdmissionBinding(
            admission_evaluation_identity=request.parent_evaluation_identity,
            admission_run_identity=request.parent_run_identity,
            receipt_path=str(
                request.artifact_root
                / request.parent_evaluation_identity
                / "receipts"
                / f"{request.parent_run_identity}.json"
            ),
            receipt_sha256=request.parent_receipt_sha256,
            artifact_tree_digest=request.parent_artifact_tree_digest,
            candidate_lock_digest=request.parent_candidate_lock_digest,
            runtime_root=str(runtime_root),
            runtime_manifest_sha256=request.parent_runtime_manifest_sha256,
            production_root=str(request.repo_root),
            production_source_revision=request.parent_production_source_revision,
            production_dependency_lock_digest=request.parent_production_dependency_lock_digest,
            production_build_identity=request.parent_production_build_identity,
            parent_root_manifests=parent_roots,
        )
        self.lock = _candidate_lock()
        self.runtime_identity_file = tmp_path / "runtime-identity"
        self.runtime_identity_file.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_identity_file.write_bytes(b"runtime-before\n")
        self.runtime = SimpleNamespace(
            root=runtime_root,
            identity_sha256=sha256(self.runtime_identity_file.read_bytes()).hexdigest(),
        )
        self.production = _production_identity(request.repo_root)

    def capture_evaluator_identity(self, deadline: object) -> EvaluatorIdentity:
        self.calls.append(("evaluator", deadline))
        self._evaluator_captures += 1
        return _evaluator_identity(
            source_digest=(
                _SHA_F if self.drift_evaluator and self._evaluator_captures > 1 else _SHA_A
            )
        )

    def helper_expectation(self, evaluator: EvaluatorIdentity) -> object:
        assert evaluator.source_digest
        return object()

    def capture_production_identity(
        self, repo_root: Path, deadline: object, expectation: object
    ) -> ProductionIdentity:
        assert repo_root == self.request.repo_root
        self.calls.append(("production", deadline))
        self._production_captures += 1
        if self.drift_production and self._production_captures > 1:
            return _production_identity(self.request.repo_root, build_identity=_SHA_F)
        return self.production

    def load_parent(self, expectation: Any, *, deadline: object) -> object:
        self.calls.append(("parent", deadline))
        if self.fail_parent:
            raise ParentAdmissionError(
                ParentAdmissionFailure("parent_receipt_mismatch", "injected mismatch")
            )
        assert expectation.evaluation_identity == self.request.parent_evaluation_identity
        assert expectation.run_identity == self.request.parent_run_identity
        return SimpleNamespace(
            binding=self.binding,
            receipt=SimpleNamespace(candidate_lock=self.lock),
        )

    def load_runtime(
        self,
        root: Path,
        *,
        expected_lock_digest: str,
        expected_manifest_sha256: str,
        deadline: object,
    ) -> object:
        self.calls.append(("runtime", deadline))
        self._runtime_loads += 1
        assert root == Path(self.binding.runtime_root)
        assert expected_lock_digest == _SHA_E
        assert expected_manifest_sha256 == _SHA_F
        observed = sha256(self.runtime_identity_file.read_bytes()).hexdigest()
        if observed != self.runtime.identity_sha256:
            return SimpleNamespace(root=root, identity_sha256=observed)
        return self.runtime

    def capture_corpus(self, *, deadline: object, expectation: object) -> tuple[RootManifest, ...]:
        del expectation
        self.calls.append(("corpus", deadline))
        if self.mutate_after and sum(name == "corpus" for name, _ in self.calls) == 2:
            return (self.manifests[0], _manifest(self.request.workspace_root, digest=_SHA_F))
        return self.manifests

    def create_run_root(
        self,
        repo_root: Path,
        evaluation_root: Path,
        run_identity: str,
        *,
        deadline: object,
    ) -> ProtocolRunRoot:
        del repo_root
        self.calls.append(("create_run_root", deadline))
        root = self.tmp_path / "runs" / run_identity
        root.mkdir(parents=True, mode=0o700)
        handle = ProtocolRunRoot(root, os.open(root, os.O_RDONLY | os.O_DIRECTORY))
        self.run_roots.append(handle)
        return handle

    def candidate_spec(self, candidate: str, runtime: object, repo_root: Path) -> object:
        assert runtime is self.runtime
        assert repo_root == self.request.repo_root
        self.calls.append((f"spec:{candidate}", None))
        return SimpleNamespace(name=candidate, diagnostics_mode="pull" if candidate == "ty" else "push")

    def run_capability(
        self,
        candidate: str,
        runtime: object,
        request: object,
        *,
        deadline: object,
    ) -> CandidateProtocolOutcome:
        del runtime, request
        self.calls.append((f"capability:{candidate}", deadline))
        if self.source_uncertain_candidate == candidate:
            raise SourceBindingError("injected source drift")
        if self.timeout_candidate == candidate:
            raise DeadlineExceeded(f"injected timeout for {candidate}")
        return _outcome(
            candidate,
            failed_capability=("references" if self.failed_candidate == candidate else None),
            seam_pull_only=self.seam_candidate == candidate,
        )

    def run_lifecycle(
        self, request: object, *, deadline: object
    ) -> LifecycleBatteryResult:
        candidate = cast("Any", request).candidate
        self.calls.append((f"lifecycle:{candidate}", deadline))
        if self.infrastructure_candidate == candidate:
            raise RuntimeError(
                "minimal backend environment measurement mismatch: changed_keys=[PATH]"
            )
        return LifecycleBatteryResult(
            lifecycle=_lifecycle(diagnostics_mode="pull" if candidate == "ty" else "push"),
            scenarios=(),
            issues=(),
        )

    def run_witness(self, request: object, *, deadline: object) -> ProtocolBehaviorWitness:
        candidate = cast("Any", request).candidate
        self.calls.append((f"witness:{candidate}", deadline))
        inconclusive = self.configuration_inconclusive == candidate
        if self.mutate_runtime_file and candidate == "pyrefly":
            self.runtime_identity_file.write_bytes(b"runtime-after\n")
        return _witness(
            candidate,
            passed=not inconclusive,
            configuration_conclusive=not inconclusive,
        )

    def write_sidecar(
        self, run_root: ProtocolRunRoot, name: str, payload: bytes, *, deadline: object
    ) -> str:
        self.calls.append((f"sidecar:{name}", deadline))
        assert run_root.logical_root.name
        path = run_root.logical_root / name
        path.write_bytes(payload)
        path.chmod(0o600)
        return sha256(payload).hexdigest()

    def artifact_tree_digest(
        self, repo_root: Path, run_root: ProtocolRunRoot, *, deadline: object
    ) -> str:
        del repo_root
        self.calls.append(("artifact_digest", deadline))
        names = sorted(path.name for path in run_root.logical_root.iterdir())
        return sha256(canonical_json({"sidecars": names})).hexdigest()

    def publish(self, request: object, *, deadline: object) -> Path:
        self.calls.append(("publish", deadline))
        if self.publish_error is not None:
            raise self.publish_error
        self.published = True
        return self.tmp_path / "published.json"

    def resolve_candidate_lock(self) -> None:  # pragma: no cover - must never be called
        raise AssertionError("the protocol phase must never resolve a candidate lock")


def _candidate_call_names(services: _FakeServices) -> list[str]:
    return [
        name
        for name, _deadline in services.calls
        if name.startswith(("capability:", "lifecycle:", "witness:"))
    ]


def test_protocol_phase_runs_each_candidate_once_serially_and_brackets_one_manifest_pair(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    assert receipt.status == "pass"
    assert _candidate_call_names(services) == [
        "capability:pyright",
        "lifecycle:pyright",
        "witness:pyright",
        "capability:ty",
        "lifecycle:ty",
        "witness:ty",
        "capability:pyrefly",
        "lifecycle:pyrefly",
        "witness:pyrefly",
    ]
    assert sum(name == "corpus" for name, _deadline in services.calls) == 2
    assert sum(name == "runtime" for name, _deadline in services.calls) == 2
    assert receipt.root_manifests_before == receipt.root_manifests_after
    assert all(not delta.unexpected for delta in receipt.write_deltas)
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_PASS
    assert services.published is False
    assert services.run_roots and all(run.fd == -1 for run in services.run_roots)


def test_protocol_phase_binds_each_witness_to_the_exact_written_sidecar(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    for outcome in receipt.outcomes:
        expected = _witness(outcome.candidate).canonical_bytes()
        assert outcome.witness_schema_version == PROTOCOL_WITNESS_SCHEMA_VERSION
        assert outcome.witness_sha256 == sha256(expected).hexdigest()
        assert outcome.witness_passed is True


def test_protocol_phase_retains_a_failed_candidate_and_excludes_it_from_survivors(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.failed_candidate = "pyrefly"
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    pyrefly = next(outcome for outcome in receipt.outcomes if outcome.candidate == "pyrefly")
    assert receipt.status == "pass"
    assert pyrefly.gate_disposition == "fail"
    assert pyrefly in receipt.outcomes
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_PASS


def test_protocol_phase_never_converts_configuration_uncertainty_into_backend_failure(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.configuration_inconclusive = "pyrefly"
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    pyrefly = next(outcome for outcome in receipt.outcomes if outcome.candidate == "pyrefly")
    assert receipt.status == "hold"
    assert pyrefly.gate_disposition == "configuration_inconclusive"
    assert pyrefly.witness_passed is False
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE


def test_protocol_phase_rejects_sent_only_configuration_without_application_proof(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import _finalize_candidate_outcome

    witness = replace(
        _witness("ty"),
        passed=False,
        configuration_application_proven=False,
        issues=("server-side configuration application was not proven",),
    )
    outcome = _finalize_candidate_outcome(
        _outcome("ty"),
        LifecycleBatteryResult(
            lifecycle=_lifecycle(diagnostics_mode="pull"),
            scenarios=(),
            issues=(),
        ),
        witness,
        _SHA_B,
    )

    assert outcome.gate_disposition == "configuration_inconclusive"


def test_actual_behavior_witness_schema_binds_through_candidate_evidence() -> None:
    """The real witness module and receipt model must share one schema authority."""

    from scripts.backend_eval.protocol_phase import _finalize_candidate_outcome

    witness = replace(
        _witness("pyright"),
        schema_version=BEHAVIOR_WITNESS_SCHEMA_VERSION,
    )

    outcome = _finalize_candidate_outcome(
        _outcome("pyright"),
        LifecycleBatteryResult(
            lifecycle=_lifecycle(diagnostics_mode="push"),
            scenarios=(),
            issues=(),
        ),
        witness,
        _SHA_B,
    )

    assert outcome.witness_schema_version == BEHAVIOR_WITNESS_SCHEMA_VERSION
    assert outcome.witness_sha256 == _SHA_B
    assert outcome.witness_passed is True


def test_protocol_phase_records_environment_measurement_failure_as_incomplete_not_candidate_fail(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.infrastructure_candidate = "ty"

    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    assert receipt.status == "incomplete"
    assert tuple(outcome.candidate for outcome in receipt.outcomes) == ("pyright",)
    assert any(
        "candidate ty incomplete" in issue and "changed_keys=[PATH]" in issue
        for issue in receipt.issues
    )


def test_unresolved_controlled_push_diagnostics_is_inconclusive_not_backend_failure() -> None:
    from scripts.backend_eval.protocol_phase import _finalize_candidate_outcome

    witness = replace(
        _witness("pyrefly"),
        passed=False,
        exact_uri_diagnostics=True,
        missing_import_diagnostic=False,
        exact_uri_publish_count=2,
        exact_uri_diagnostic_count=0,
        diagnostics_completion_reason="bounded_wait_without_required_diagnostic",
        issues=("controlled missing-import diagnostics did not become decision-ready",),
    )

    outcome = _finalize_candidate_outcome(
        _outcome("pyrefly"),
        LifecycleBatteryResult(
            lifecycle=_lifecycle(diagnostics_mode="push"),
            scenarios=(),
            issues=(),
        ),
        witness,
        _SHA_B,
    )

    assert outcome.gate_disposition == "configuration_inconclusive"


def test_protocol_phase_mutation_is_a_hold_with_the_changed_candidate_evidence_retained(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.mutate_after = True
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    assert receipt.status == "hold"
    assert len(receipt.outcomes) == 3
    assert any(delta.unexpected for delta in receipt.write_deltas)


def test_protocol_phase_timeout_finalizes_truthful_partial_evidence_without_retry(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.timeout_candidate = "ty"
    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    assert receipt.status == "incomplete"
    assert tuple(outcome.candidate for outcome in receipt.outcomes) == ("pyright",)
    assert _candidate_call_names(services).count("capability:ty") == 1
    assert "capability:pyrefly" not in _candidate_call_names(services)
    assert any("deadline" in issue or "timeout" in issue for issue in receipt.issues)


def test_protocol_phase_preserves_pull_only_as_a_product_seam_not_backend_failure(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.seam_candidate = "ty"

    receipt = evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    ty = next(outcome for outcome in receipt.outcomes if outcome.candidate == "ty")
    assert ty.gate_disposition == "seam_incompatible_pull_only"
    assert receipt.status == "pass"


def test_protocol_phase_derives_stable_child_evaluation_and_unique_run_identity(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    first = evaluate_protocol_phase(
        request, services=_FakeServices(request, tmp_path / "first"), clock=_FakeClock()
    )
    second = evaluate_protocol_phase(
        request, services=_FakeServices(request, tmp_path / "second"), clock=_FakeClock()
    )

    assert first.evaluation_identity == second.evaluation_identity
    assert first.evaluation_identity != request.parent_evaluation_identity
    assert first.run_identity != second.run_identity

    alternate_target = request.workspace_root / "alternate.py"
    alternate_target.write_text("alternate = 1\n", encoding="utf-8")
    target_changed = evaluate_protocol_phase(
        replace(request, target=alternate_target),
        services=_FakeServices(replace(request, target=alternate_target), tmp_path / "target"),
        clock=_FakeClock(),
    )
    position_request = replace(request, symbol_position=(3, 8))
    position_changed = evaluate_protocol_phase(
        position_request,
        services=_FakeServices(position_request, tmp_path / "position"),
        clock=_FakeClock(),
    )
    assert target_changed.evaluation_identity != first.evaluation_identity
    assert position_changed.evaluation_identity != first.evaluation_identity
    assert first.probe_binding.absolute_target == str(request.target)
    assert first.probe_binding.position == request.symbol_position


def test_protocol_phase_propagates_one_origin_and_reserve_to_all_work(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    deadlines = cast(
        "list[Any]",
        [deadline for _name, deadline in services.calls if deadline is not None],
    )
    assert deadlines
    assert {deadline.started for deadline in deadlines} == {100.0}
    assert {deadline.seconds for deadline in deadlines} == {5400.0}
    assert {deadline.reserve for deadline in deadlines} == {0.0, 300.0}
    candidate_deadlines = [
        deadline
        for name, deadline in services.calls
        if name.startswith(("capability:", "lifecycle:", "witness:"))
    ]
    assert len({id(deadline) for deadline in candidate_deadlines}) == 1


def test_protocol_phase_fatal_source_or_production_identity_uncertainty_has_no_receipt(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError, evaluate_protocol_phase

    request = _request(tmp_path)
    source_services = _FakeServices(request, tmp_path / "source")
    source_services.source_uncertain_candidate = "ty"
    with pytest.raises(ProtocolPhaseError, match="source identity"):
        evaluate_protocol_phase(request, services=source_services, clock=_FakeClock())
    assert source_services.published is False

    production_services = _FakeServices(request, tmp_path / "production")
    production_services.drift_production = True
    with pytest.raises(ProtocolPhaseError, match="identity/source evidence"):
        evaluate_protocol_phase(request, services=production_services, clock=_FakeClock())
    assert production_services.published is False


def test_protocol_phase_run_root_is_unique_private_and_sidecar_only(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import _ProductionServices

    repo = tmp_path / "repo"
    repo.mkdir()
    evaluation_root = repo / ".admission-artifacts/backend-eval" / _SHA_A
    deadline = Deadline.start(_FakeClock(), 60.0)
    services = _ProductionServices()

    run_root = services.create_run_root(
        repo, evaluation_root, _SHA_B, deadline=deadline
    )

    assert run_root.logical_root.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        services.create_run_root(repo, evaluation_root, _SHA_B, deadline=deadline)
    run_root.close()


def test_protocol_absolute_owner_open_is_split_and_typed(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval import protocol_phase
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError

    real_owner = tmp_path / "real-owner"
    real_owner.mkdir()
    redirected_owner = tmp_path / "redirected-owner"
    redirected_owner.symlink_to(real_owner, target_is_directory=True)
    with pytest.raises(ProtocolPhaseError, match="artifact owner root"):
        protocol_phase._open_protocol_artifact_owner_root(redirected_owner)



def test_protocol_run_handle_confines_sidecar_and_witness_after_ancestor_substitution(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval import protocol_witness
    from scripts.backend_eval.protocol_phase import _ProductionServices

    repo = tmp_path / "repo"
    repo.mkdir()
    evaluation_root = repo / ".admission-artifacts/backend-eval" / _SHA_A
    deadline = Deadline.start(_FakeClock(), 60.0)
    services = _ProductionServices()
    run = services.create_run_root(repo, evaluation_root, _SHA_B, deadline=deadline)
    original_runs = evaluation_root / "protocol-runs"
    held_runs = evaluation_root / "protocol-runs-held"
    original_runs.rename(held_runs)
    outside_runs = repo / "outside-runs"
    outside_run = outside_runs / _SHA_B
    outside_run.mkdir(parents=True, mode=0o700)
    original_runs.symlink_to(outside_runs, target_is_directory=True)

    payload = b"bound-sidecar\n"
    services.write_sidecar(
        run,
        f"pyright-protocol-witness-v{PROTOCOL_WITNESS_SCHEMA_VERSION}.json",
        payload,
        deadline=deadline,
    )
    fixture = protocol_witness._DisposableFixture.create(
        run.logical_root,
        "pyright",
        deadline=deadline,
        owned_root_fd=run.fd,
    )
    try:
        assert (
            held_runs
            / _SHA_B
            / f"pyright-protocol-witness-v{PROTOCOL_WITNESS_SCHEMA_VERSION}.json"
        ).read_bytes() == payload
        assert not (
            outside_run
            / f"pyright-protocol-witness-v{PROTOCOL_WITNESS_SCHEMA_VERSION}.json"
        ).exists()
        assert (held_runs / _SHA_B / fixture.directory_name / "witness.py").is_file()
        assert not (outside_run / fixture.directory_name).exists()
        assert str(fixture.directory_path).startswith(f"/proc/{os.getpid()}/fd/")
    finally:
        assert fixture.cleanup(deadline=deadline) is None
        run.close()


def test_protocol_phase_parent_mismatch_fails_before_runtime_or_candidate_work(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError, evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.fail_parent = True
    with pytest.raises(ProtocolPhaseError, match="parent"):
        evaluate_protocol_phase(request, services=services, clock=_FakeClock())

    assert not any(name == "runtime" for name, _deadline in services.calls)
    assert _candidate_call_names(services) == []
    assert services.published is False


def test_protocol_phase_evaluator_identity_drift_refuses_any_receipt(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError, evaluate_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.drift_evaluator = True
    with pytest.raises(ProtocolPhaseError, match="evaluator"):
        evaluate_protocol_phase(request, services=services, clock=_FakeClock())
    assert services.published is False


def test_protocol_phase_runtime_identity_drift_refuses_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.backend_eval import protocol_phase
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.mutate_runtime_file = True
    monkeypatch.setattr(protocol_phase, "require_protocol_execution", lambda: None)

    with pytest.raises(ProtocolPhaseError, match="runtime identity"):
        protocol_phase.run_protocol_phase(
            request, services=services, clock=_FakeClock()
        )

    assert sum(name == "runtime" for name, _deadline in services.calls) == 2
    assert services.runtime_identity_file.read_bytes() == b"runtime-after\n"
    assert services.published is False


def test_run_protocol_phase_refuses_unsealed_execution_before_any_service_call(
    tmp_path: Path,
) -> None:
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError, run_protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    with pytest.raises(ProtocolPhaseError, match="sealed"):
        run_protocol_phase(request, services=services, clock=_FakeClock())
    assert services.calls == []
    assert services.published is False


def test_run_protocol_phase_publication_collision_returns_no_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.backend_eval import protocol_phase
    from scripts.backend_eval.protocol_phase import ProtocolPhaseError

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.publish_error = PublicationError(
        PublicationFailure(PUBLICATION_FAILED, "injected immutable collision")
    )
    monkeypatch.setattr(protocol_phase, "require_protocol_execution", lambda: None)

    with pytest.raises(ProtocolPhaseError, match="publication"):
        protocol_phase.run_protocol_phase(request, services=services, clock=_FakeClock())
    assert services.published is False


def test_run_protocol_phase_publishes_truthful_timeout_evidence_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.backend_eval import protocol_phase

    request = _request(tmp_path)
    services = _FakeServices(request, tmp_path)
    services.timeout_candidate = "ty"
    monkeypatch.setattr(protocol_phase, "require_protocol_execution", lambda: None)

    receipt = protocol_phase.run_protocol_phase(
        request, services=services, clock=_FakeClock()
    )

    assert receipt.status == "incomplete"
    assert services.published is True
    assert sum(name == "publish" for name, _deadline in services.calls) == 1
    assert _candidate_call_names(services).count("capability:ty") == 1
