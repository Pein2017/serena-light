"""Run the sealed, parent-bound Phase 2 protocol gate.

Only :func:`run_protocol_phase` may publish canonical evidence, and it first proves the
sealed ``protocol-phase`` bootstrap.  :func:`evaluate_protocol_phase` is the injected,
non-publishing seam used by deterministic orchestration tests.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol

from scripts.backend_eval.admission import artifact_tree_digest
from scripts.backend_eval.identity import capture_evaluator_identity
from scripts.backend_eval.manifests import freeze_default_corpus
from scripts.backend_eval.models import (
    EVALUATION_CONTRACT_VERSION,
    PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
    PROTOCOL_PHASE_NEXT_ACTION_PASS,
    PROTOCOL_PHASE_NEXT_ACTION_STOP,
    PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
    AdmissionRootWitness,
    CandidateProtocolOutcome,
    EvaluatorIdentity,
    PhaseBudget,
    ProductionIdentity,
    ProtocolPhaseReceipt,
    RootManifest,
    RuntimeBinding,
    bind_candidate_protocol_witness,
    canonical_json,
)
from scripts.backend_eval.process import Clock, Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.production_identity import (
    assert_production_identity_unchanged,
    capture_production_identity,
)
from scripts.backend_eval.protocol_lifecycle import (
    LifecycleBatteryRequest,
    LifecycleBatteryResult,
    run_lifecycle_battery,
)
from scripts.backend_eval.protocol_parent import (
    ParentAdmissionExpectation,
    load_parent_admission,
)
from scripts.backend_eval.protocol_witness import (
    ProtocolBehaviorWitness,
    ProtocolWitnessRequest,
    run_protocol_behavior_witness,
)
from scripts.backend_eval.publish import PublicationRequest, publish_immutable_record
from scripts.backend_eval.pyrefly_probe import (
    pyrefly_protocol_spec,
    run_pyrefly_capability_probe,
)
from scripts.backend_eval.pyright_probe import (
    pyright_protocol_spec,
    run_pyright_capability_probe,
)
from scripts.backend_eval.runtime import CandidateRuntime, load_prepared_candidate_runtime
from scripts.backend_eval.source_binding import HelperExpectation, SourceBindingError
from scripts.backend_eval.source_image import (
    require_protocol_execution,
    source_image_deadline_seconds,
    source_image_started,
)
from scripts.backend_eval.ty_probe import run_ty_capability_probe, ty_protocol_spec
from scripts.backend_eval.write_guard import compare_root_manifests, enrich_after_manifest
from serena_light.lsp.pyright import PyrightFacts

_SHA256_CHARACTERS = frozenset("0123456789abcdef")
_PROTOCOL_SECONDS = 5400.0
_FINALIZATION_RESERVE_SECONDS = 300.0
_EVALUATION_IDENTITY_ALGORITHM_VERSION = 1
_CANDIDATES = ("pyright", "ty", "pyrefly")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_SIDECAR_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW


class ProtocolPhaseError(RuntimeError):
    """The protocol phase cannot produce or publish truthful evidence."""


def _require_absolute(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be absolute")
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent references")


def _require_sha256(value: str, label: str) -> None:
    if len(value) != 64 or set(value) - _SHA256_CHARACTERS:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256")


@dataclass(frozen=True, slots=True)
class ProtocolPhaseRequest:
    """All explicit immutable inputs required by one Phase 2 execution."""

    repo_root: Path
    artifact_root: Path
    runtime_base: Path
    parent_evaluation_identity: str
    parent_run_identity: str
    parent_receipt_sha256: str
    parent_artifact_tree_digest: str
    parent_candidate_lock_digest: str
    parent_runtime_manifest_sha256: str
    parent_production_source_revision: str
    parent_production_dependency_lock_digest: str
    parent_production_build_identity: str
    workspace_root: Path
    target: Path
    symbol_position: tuple[int, int]
    workspace_snapshots: tuple[AdmissionRootWitness, ...]

    def __post_init__(self) -> None:
        for label, path in (
            ("ProtocolPhaseRequest.repo_root", self.repo_root),
            ("ProtocolPhaseRequest.artifact_root", self.artifact_root),
            ("ProtocolPhaseRequest.runtime_base", self.runtime_base),
            ("ProtocolPhaseRequest.workspace_root", self.workspace_root),
            ("ProtocolPhaseRequest.target", self.target),
        ):
            _require_absolute(path, label)
        try:
            self.artifact_root.relative_to(self.repo_root)
        except ValueError as exc:
            raise ValueError(
                "ProtocolPhaseRequest.artifact_root must be inside repo_root"
            ) from exc
        try:
            relative_target = self.target.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError(
                "ProtocolPhaseRequest.target must be inside workspace_root"
            ) from exc
        if not relative_target.parts or any(
            part in {"", ".", ".."} for part in relative_target.parts
        ):
            raise ValueError(
                "ProtocolPhaseRequest.target must name one lexical file inside workspace_root"
            )
        for label, digest in (
            ("parent_evaluation_identity", self.parent_evaluation_identity),
            ("parent_run_identity", self.parent_run_identity),
            ("parent_receipt_sha256", self.parent_receipt_sha256),
            ("parent_artifact_tree_digest", self.parent_artifact_tree_digest),
            ("parent_candidate_lock_digest", self.parent_candidate_lock_digest),
            ("parent_runtime_manifest_sha256", self.parent_runtime_manifest_sha256),
            (
                "parent_production_dependency_lock_digest",
                self.parent_production_dependency_lock_digest,
            ),
            ("parent_production_build_identity", self.parent_production_build_identity),
        ):
            _require_sha256(digest, f"ProtocolPhaseRequest.{label}")
        revision = self.parent_production_source_revision
        if len(revision) not in {40, 64} or set(revision) - _SHA256_CHARACTERS:
            raise ValueError(
                "ProtocolPhaseRequest.parent_production_source_revision must be a Git revision"
            )
        if (
            not isinstance(self.symbol_position, tuple)
            or len(self.symbol_position) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.symbol_position
            )
        ):
            raise ValueError(
                "ProtocolPhaseRequest.symbol_position must contain two non-negative integers"
            )
        snapshots = self.workspace_snapshots
        roots = tuple(snapshot.root for snapshot in snapshots)
        if (
            not isinstance(snapshots, tuple)
            or not snapshots
            or any(
                not isinstance(snapshot, AdmissionRootWitness) for snapshot in snapshots
            )
            or roots != tuple(sorted(set(roots)))
        ):
            raise ValueError(
                "ProtocolPhaseRequest.workspace_snapshots must be a non-empty, root-sorted, unique typed tuple"
            )
        if str(self.workspace_root) not in roots:
            raise ValueError(
                "ProtocolPhaseRequest.workspace_snapshots must contain workspace_root"
            )

    def parent_expectation(self) -> ParentAdmissionExpectation:
        """Build the one legal parent receipt authority without discovery."""

        return ParentAdmissionExpectation(
            artifact_root=self.artifact_root,
            evaluation_identity=self.parent_evaluation_identity,
            run_identity=self.parent_run_identity,
            receipt_sha256=self.parent_receipt_sha256,
            artifact_tree_digest=self.parent_artifact_tree_digest,
            candidate_lock_digest=self.parent_candidate_lock_digest,
            runtime_manifest_sha256=self.parent_runtime_manifest_sha256,
            production_root=self.repo_root,
            production_source_revision=self.parent_production_source_revision,
            production_dependency_lock_digest=self.parent_production_dependency_lock_digest,
            production_build_identity=self.parent_production_build_identity,
        )


class _ProtocolServices(Protocol):
    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity: ...

    def helper_expectation(self, evaluator: EvaluatorIdentity) -> object: ...

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: object
    ) -> ProductionIdentity: ...

    def load_parent(
        self, expectation: ParentAdmissionExpectation, *, deadline: Deadline
    ) -> Any: ...

    def load_runtime(
        self,
        root: Path,
        *,
        expected_lock_digest: str,
        expected_manifest_sha256: str,
        deadline: Deadline,
    ) -> object: ...

    def capture_corpus(
        self, *, deadline: Deadline, expectation: object
    ) -> tuple[RootManifest, ...]: ...

    def create_run_root(
        self,
        repo_root: Path,
        evaluation_root: Path,
        run_identity: str,
        *,
        deadline: Deadline,
    ) -> Path: ...

    def candidate_spec(self, candidate: str, runtime: object, repo_root: Path) -> Any: ...

    def run_capability(
        self,
        candidate: str,
        runtime: object,
        request: ProtocolPhaseRequest,
        *,
        deadline: Deadline,
    ) -> CandidateProtocolOutcome: ...

    def run_lifecycle(
        self, request: LifecycleBatteryRequest, *, deadline: Deadline
    ) -> LifecycleBatteryResult: ...

    def run_witness(
        self, request: ProtocolWitnessRequest, *, deadline: Deadline
    ) -> ProtocolBehaviorWitness: ...

    def write_sidecar(
        self, run_root: Path, name: str, payload: bytes, *, deadline: Deadline
    ) -> str: ...

    def artifact_tree_digest(
        self, repo_root: Path, run_root: Path, *, deadline: Deadline
    ) -> str: ...

    def publish(self, request: PublicationRequest, *, deadline: Deadline) -> Path: ...


class _ProductionServices:
    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity:
        return capture_evaluator_identity(deadline=deadline)

    def helper_expectation(self, evaluator: EvaluatorIdentity) -> HelperExpectation:
        return HelperExpectation.from_identity(evaluator)

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: object
    ) -> ProductionIdentity:
        if not isinstance(expectation, HelperExpectation):
            raise ProtocolPhaseError("production helper expectation is not bound")
        return capture_production_identity(
            repo_root, expectation=expectation, deadline=deadline
        )

    def load_parent(
        self, expectation: ParentAdmissionExpectation, *, deadline: Deadline
    ) -> Any:
        return load_parent_admission(expectation, deadline=deadline)

    def load_runtime(
        self,
        root: Path,
        *,
        expected_lock_digest: str,
        expected_manifest_sha256: str,
        deadline: Deadline,
    ) -> CandidateRuntime:
        deadline.check("load exact parent-bound candidate runtime")
        runtime = load_prepared_candidate_runtime(
            root,
            expected_lock_digest=expected_lock_digest,
            expected_manifest_sha256=expected_manifest_sha256,
        )
        deadline.check("loaded exact parent-bound candidate runtime")
        return runtime

    def capture_corpus(
        self, *, deadline: Deadline, expectation: object
    ) -> tuple[RootManifest, ...]:
        if not isinstance(expectation, HelperExpectation):
            raise ProtocolPhaseError("corpus helper expectation is not bound")
        return freeze_default_corpus(expectation=expectation, deadline=deadline)

    def create_run_root(
        self,
        repo_root: Path,
        evaluation_root: Path,
        run_identity: str,
        *,
        deadline: Deadline,
    ) -> Path:
        deadline.check("create protocol run root")
        parent_fd = _ensure_owned_directory(repo_root, evaluation_root / "protocol-runs")
        try:
            os.mkdir(run_identity, 0o700, dir_fd=parent_fd)
            run_fd = os.open(run_identity, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
            try:
                os.fchmod(run_fd, 0o700)
                os.fsync(run_fd)
            finally:
                os.close(run_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        deadline.check("created protocol run root")
        return evaluation_root / "protocol-runs" / run_identity

    def candidate_spec(
        self, candidate: str, runtime: object, repo_root: Path
    ) -> Any:
        candidate_runtime = _require_runtime(runtime)
        if candidate == "pyright":
            return pyright_protocol_spec(
                candidate_runtime,
                _pyright_facts(candidate_runtime, repo_root),
                production_root=repo_root,
            )
        config = _service_config(candidate_runtime, candidate)
        if candidate == "ty":
            return ty_protocol_spec(candidate_runtime, config)
        if candidate == "pyrefly":
            return pyrefly_protocol_spec(candidate_runtime, config)
        raise ProtocolPhaseError(f"unknown protocol candidate: {candidate}")

    def run_capability(
        self,
        candidate: str,
        runtime: object,
        request: ProtocolPhaseRequest,
        *,
        deadline: Deadline,
    ) -> CandidateProtocolOutcome:
        candidate_runtime = _require_runtime(runtime)
        if candidate == "pyright":
            return run_pyright_capability_probe(
                candidate_runtime,
                _pyright_facts(candidate_runtime, request.repo_root),
                request.workspace_root,
                request.target,
                request.symbol_position,
                production_root=request.repo_root,
                deadline=deadline,
            )
        if candidate == "ty":
            return run_ty_capability_probe(
                candidate_runtime,
                request.workspace_root,
                request.target,
                request.symbol_position,
                deadline=deadline,
            )
        if candidate == "pyrefly":
            return run_pyrefly_capability_probe(
                candidate_runtime,
                request.workspace_root,
                request.target,
                request.symbol_position,
                deadline=deadline,
            )
        raise ProtocolPhaseError(f"unknown protocol candidate: {candidate}")

    def run_lifecycle(
        self, request: LifecycleBatteryRequest, *, deadline: Deadline
    ) -> LifecycleBatteryResult:
        return run_lifecycle_battery(request, deadline=deadline)

    def run_witness(
        self, request: ProtocolWitnessRequest, *, deadline: Deadline
    ) -> ProtocolBehaviorWitness:
        return run_protocol_behavior_witness(request, deadline=deadline)

    def write_sidecar(
        self, run_root: Path, name: str, payload: bytes, *, deadline: Deadline
    ) -> str:
        if "/" in name or name in {"", ".", ".."}:
            raise ProtocolPhaseError("protocol sidecar name is not one component")
        deadline.check(f"write protocol sidecar {name}")
        directory_fd = _open_protocol_run_root(run_root)
        file_fd: int | None = None
        try:
            file_fd = os.open(name, _SIDECAR_FLAGS, 0o600, dir_fd=directory_fd)
            os.fchmod(file_fd, 0o600)
            view = memoryview(payload)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise OSError("protocol sidecar write made no progress")
                view = view[written:]
                deadline.check(f"write protocol sidecar {name}")
            os.fsync(file_fd)
            os.fsync(directory_fd)
        except BaseException:
            if file_fd is not None:
                os.close(file_fd)
                file_fd = None
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory_fd)
            raise
        finally:
            if file_fd is not None:
                os.close(file_fd)
            os.close(directory_fd)
        return sha256(payload).hexdigest()

    def artifact_tree_digest(
        self, repo_root: Path, run_root: Path, *, deadline: Deadline
    ) -> str:
        return artifact_tree_digest(
            repo_root,
            run_root,
            check=lambda: deadline.check("protocol artifact digest"),
        )

    def publish(self, request: PublicationRequest, *, deadline: Deadline) -> Path:
        return publish_immutable_record(request, deadline)


@dataclass(frozen=True, slots=True)
class _ExecutionResult:
    receipt: ProtocolPhaseReceipt
    services: _ProtocolServices
    finalization_deadline: Deadline
    evaluation_root: Path


def _execute_protocol_phase(
    request: ProtocolPhaseRequest,
    *,
    services: _ProtocolServices | None,
    clock: Clock,
) -> _ExecutionResult:
    selected: _ProtocolServices = services or _ProductionServices()
    collection = _protocol_deadline(clock)
    finalization = collection.finalization()
    started_at = _utc_now()
    issues: list[str] = []
    outcomes: list[CandidateProtocolOutcome] = []

    try:
        evaluator_before = selected.capture_evaluator_identity(collection)
        helper_expectation = selected.helper_expectation(evaluator_before)
        production_before = selected.capture_production_identity(
            request.repo_root, collection, helper_expectation
        )
        parent = selected.load_parent(request.parent_expectation(), deadline=collection)
        binding = parent.binding
        candidate_lock = parent.receipt.candidate_lock
        _require_exact_parent_binding(request, binding, candidate_lock)
        runtime_root = Path(binding.runtime_root)
        runtime = selected.load_runtime(
            runtime_root,
            expected_lock_digest=binding.candidate_lock_digest,
            expected_manifest_sha256=binding.runtime_manifest_sha256,
            deadline=collection,
        )
        manifests_before = selected.capture_corpus(
            deadline=collection, expectation=helper_expectation
        )
        _require_exact_parent_corpus(request.workspace_snapshots, manifests_before)
    except BaseException as exc:
        raise ProtocolPhaseError(f"parent/source/identity admission failed: {_detail(exc)}") from exc

    evaluation_identity = _protocol_evaluation_identity(
        binding=binding,
        evaluator=evaluator_before,
        production=production_before,
        artifact_root=request.artifact_root,
    )
    run_identity = _protocol_run_identity(evaluation_identity, started_at)
    evaluation_root = request.artifact_root / evaluation_identity
    try:
        run_root = selected.create_run_root(
            request.repo_root,
            evaluation_root,
            run_identity,
            deadline=collection,
        )
    except BaseException as exc:
        raise ProtocolPhaseError(f"protocol artifact root creation failed: {_detail(exc)}") from exc

    for candidate in _CANDIDATES:
        try:
            collection.check(f"protocol candidate {candidate}")
            spec = selected.candidate_spec(candidate, runtime, request.repo_root)
            capability = selected.run_capability(
                candidate, runtime, request, deadline=collection
            )
            lifecycle = selected.run_lifecycle(
                LifecycleBatteryRequest(
                    candidate=candidate,
                    spec=spec,
                    runtime=runtime,  # type: ignore[arg-type]
                    workspace_root=request.workspace_root,
                    target=request.target,
                    language_id="python",
                    diagnostics_mode=spec.diagnostics_mode,
                ),
                deadline=collection,
            )
            witness = selected.run_witness(
                ProtocolWitnessRequest(
                    candidate=candidate,
                    spec=spec,
                    runtime=runtime,  # type: ignore[arg-type]
                    owned_root=run_root,
                ),
                deadline=collection,
            )
            witness_payload = witness.canonical_bytes()
            witness_sha256 = selected.write_sidecar(
                run_root,
                f"{candidate}-protocol-witness-v1.json",
                witness_payload,
                deadline=collection,
            )
            outcome = _finalize_candidate_outcome(
                capability, lifecycle, witness, witness_sha256
            )
            outcomes.append(outcome)
        except DeadlineExceeded as exc:
            issues.append(f"protocol timeout for {candidate}: {_detail(exc)}")
            break
        except SourceBindingError as exc:
            raise ProtocolPhaseError(
                f"protocol source identity became uncertain: {_detail(exc)}"
            ) from exc
        except BaseException as exc:
            issues.append(f"protocol candidate {candidate} incomplete: {_detail(exc)}")
            break

    try:
        manifests_after_raw = selected.capture_corpus(
            deadline=finalization, expectation=helper_expectation
        )
        manifests_after = _enrich_manifests(
            manifests_before,
            manifests_after_raw,
            expectation=helper_expectation,
            deadline=finalization,
        )
        write_deltas = tuple(
            compare_root_manifests(before, after)
            for before, after in zip(manifests_before, manifests_after, strict=True)
        )
        if any(delta.unexpected or delta.control_changes for delta in write_deltas):
            issues.append("protocol run changed one or more bounded corpus roots")
        production_after = selected.capture_production_identity(
            request.repo_root, finalization, helper_expectation
        )
        assert_production_identity_unchanged(production_before, production_after)
        artifact_digest = selected.artifact_tree_digest(
            request.repo_root, run_root, deadline=finalization
        )
        evaluator_after = selected.capture_evaluator_identity(finalization)
        if evaluator_after != evaluator_before:
            raise ProtocolPhaseError("evaluator identity drifted during protocol phase")
    except ProtocolPhaseError:
        raise
    except BaseException as exc:
        raise ProtocolPhaseError(
            f"protocol finalization identity/source evidence failed: {_detail(exc)}"
        ) from exc

    sorted_outcomes = tuple(sorted(outcomes, key=lambda outcome: outcome.candidate))
    if any(
        outcome.gate_disposition == "configuration_inconclusive"
        for outcome in sorted_outcomes
    ):
        issues.append("one or more candidate configurations remain inconclusive")
    status, next_action = _phase_disposition(sorted_outcomes, write_deltas, issues)
    if status == "pass":
        issues = []
    ended_at = _utc_now()
    runtime_binding = RuntimeBinding(
        root=binding.runtime_root,
        lock_digest=binding.candidate_lock_digest,
        manifest_path=str(Path(binding.runtime_root) / "runtime-manifest.json"),
        manifest_sha256=binding.runtime_manifest_sha256,
    )
    try:
        receipt = ProtocolPhaseReceipt(
            schema_version=PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
            evaluation_contract_version=EVALUATION_CONTRACT_VERSION,
            evaluation_identity=evaluation_identity,
            run_identity=run_identity,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            budgets=(PhaseBudget("protocol", int(_PROTOCOL_SECONDS)),),
            admission_binding=binding,
            evaluator=evaluator_before,
            production_identity_before=production_before,
            production_identity_after=production_after,
            candidate_lock=candidate_lock,
            runtime_binding=runtime_binding,
            root_manifests_before=manifests_before,
            root_manifests_after=manifests_after,
            write_deltas=write_deltas,
            outcomes=sorted_outcomes,
            issues=tuple(sorted(set(issues))),
            artifact_tree_digest=artifact_digest,
            next_action=next_action,
        )
    except BaseException as exc:
        raise ProtocolPhaseError(f"protocol receipt construction failed: {_detail(exc)}") from exc
    return _ExecutionResult(receipt, selected, finalization, evaluation_root)


def evaluate_protocol_phase(
    request: ProtocolPhaseRequest,
    *,
    services: _ProtocolServices | None = None,
    clock: Clock = monotonic_clock,
) -> ProtocolPhaseReceipt:
    """Exercise injected orchestration without publishing a receipt."""

    return _execute_protocol_phase(request, services=services, clock=clock).receipt


def run_protocol_phase(
    request: ProtocolPhaseRequest,
    *,
    services: _ProtocolServices | None = None,
    clock: Clock = monotonic_clock,
) -> ProtocolPhaseReceipt:
    """Run and immutably publish one canonical sealed protocol-phase receipt."""

    try:
        require_protocol_execution()
    except BaseException as exc:
        raise ProtocolPhaseError(
            f"sealed protocol execution is required: {_detail(exc)}"
        ) from exc
    result = _execute_protocol_phase(request, services=services, clock=clock)
    receipt = result.receipt
    publication = PublicationRequest(
        owner_root=request.repo_root,
        target_root=result.evaluation_root,
        directory_name="protocol-receipts",
        lock_name=".protocol-publication.lock",
        identity=receipt.run_identity,
        entry_name=f"{receipt.run_identity}.protocol.json",
        temporary_name=f".{receipt.run_identity}.protocol.json.tmp",
        payload=canonical_json(receipt.to_dict()),
        noun="protocol receipt",
        step_prefix="publish_protocol_receipt",
    )
    try:
        result.services.publish(publication, deadline=result.finalization_deadline)
    except BaseException as exc:
        raise ProtocolPhaseError(f"protocol publication failed: {_detail(exc)}") from exc
    return receipt


def _protocol_deadline(clock: Clock) -> Deadline:
    if clock is monotonic_clock:
        started = source_image_started()
        seconds = source_image_deadline_seconds()
        if started is None or seconds != _PROTOCOL_SECONDS:
            raise ProtocolPhaseError(
                "sealed protocol source did not provide the exact 5400-second deadline"
            )
        return Deadline(
            clock=clock,
            seconds=_PROTOCOL_SECONDS,
            started=started,
            reserve=_FINALIZATION_RESERVE_SECONDS,
        )
    return Deadline.start(
        clock,
        _PROTOCOL_SECONDS,
        reserve=_FINALIZATION_RESERVE_SECONDS,
    )


def _protocol_evaluation_identity(
    *, binding: Any, evaluator: EvaluatorIdentity, production: ProductionIdentity, artifact_root: Path
) -> str:
    payload = {
        "algorithm_version": _EVALUATION_IDENTITY_ALGORITHM_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "protocol_receipt_schema_version": PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
        "parent_admission_binding": binding.to_dict(),
        "evaluator": evaluator.to_dict(),
        "production_identity_before": _production_identity_record(production),
        "artifact_root": str(artifact_root),
    }
    return sha256(canonical_json(payload)).hexdigest()


def _protocol_run_identity(evaluation_identity: str, started_at: str) -> str:
    return sha256(
        canonical_json(
            {
                "evaluation_identity": evaluation_identity,
                "started_at": started_at,
                "pid": os.getpid(),
                "nonce": os.urandom(32).hex(),
            }
        )
    ).hexdigest()


def _production_identity_record(identity: ProductionIdentity) -> dict[str, object]:
    return {
        "pyproject_toml_sha256": identity.pyproject_toml_sha256,
        "uv_lock_sha256": identity.uv_lock_sha256,
        "package_lock_json_sha256": identity.package_lock_json_sha256,
        "dependency_lock_digest": identity.dependency_lock_digest,
        "build_identity": identity.build_identity,
        "runtime_paths": [list(item) for item in identity.runtime_paths],
    }


def _require_exact_parent_binding(
    request: ProtocolPhaseRequest, binding: object, candidate_lock: object
) -> None:
    expected = {
        "admission_evaluation_identity": request.parent_evaluation_identity,
        "admission_run_identity": request.parent_run_identity,
        "receipt_sha256": request.parent_receipt_sha256,
        "artifact_tree_digest": request.parent_artifact_tree_digest,
        "candidate_lock_digest": request.parent_candidate_lock_digest,
        "runtime_manifest_sha256": request.parent_runtime_manifest_sha256,
        "production_root": str(request.repo_root),
        "production_source_revision": request.parent_production_source_revision,
        "production_dependency_lock_digest": request.parent_production_dependency_lock_digest,
        "production_build_identity": request.parent_production_build_identity,
    }
    mismatches = [
        name for name, value in expected.items() if getattr(binding, name, None) != value
    ]
    if getattr(candidate_lock, "digest", None) != request.parent_candidate_lock_digest:
        mismatches.append("candidate_lock.digest")
    expected_runtime = request.runtime_base / request.parent_candidate_lock_digest
    if getattr(binding, "runtime_root", None) != str(expected_runtime):
        mismatches.append("runtime_root")
    if mismatches:
        raise ProtocolPhaseError(
            f"exact parent admission binding mismatch: {', '.join(sorted(mismatches))}"
        )
    if tuple(getattr(binding, "parent_root_manifests", ())) != request.workspace_snapshots:
        raise ProtocolPhaseError("exact parent workspace snapshot binding mismatch")


def _require_exact_parent_corpus(
    expected: tuple[AdmissionRootWitness, ...], manifests: tuple[RootManifest, ...]
) -> None:
    observed = tuple(
        AdmissionRootWitness(
            root=manifest.root,
            kind=manifest.kind,
            source_revision=manifest.source_revision,
            manifest_digest=manifest.manifest_digest,
        )
        for manifest in manifests
    )
    if observed != expected:
        raise ProtocolPhaseError(
            "protocol corpus does not equal the exact parent-bound workspace snapshots"
        )


def _enrich_manifests(
    before: tuple[RootManifest, ...],
    after: tuple[RootManifest, ...],
    *,
    expectation: object,
    deadline: Deadline,
) -> tuple[RootManifest, ...]:
    if tuple(manifest.root for manifest in before) != tuple(
        manifest.root for manifest in after
    ):
        raise ProtocolPhaseError("protocol before/after corpus roots differ")
    if isinstance(expectation, HelperExpectation):
        return tuple(
            enrich_after_manifest(
                prior,
                current,
                expectation=expectation,
                deadline=deadline,
            )
            for prior, current in zip(before, after, strict=True)
        )
    # Injected tests already provide fully-observed manifests and cannot execute helpers.
    return after


def _finalize_candidate_outcome(
    capability: CandidateProtocolOutcome,
    lifecycle: LifecycleBatteryResult,
    witness: ProtocolBehaviorWitness,
    witness_sha256: str,
) -> CandidateProtocolOutcome:
    if capability.candidate != witness.candidate:
        raise ProtocolPhaseError("protocol witness candidate differs from capability evidence")
    issues = set(capability.issues)
    issues.update(lifecycle.issues)
    issues.update(witness.issues)
    conclusive = _configuration_conclusive(witness)
    if not conclusive:
        disposition = "configuration_inconclusive"
        issues.add("configuration attribution is inconclusive")
    elif not witness.passed or capability.gate_disposition == "fail" or lifecycle.issues:
        disposition = "fail"
        if not issues:
            issues.add("protocol behavior witness did not pass")
    elif capability.gate_disposition == "seam_incompatible_pull_only":
        disposition = "seam_incompatible_pull_only"
        issues.add("current product seam requires push diagnostics")
    else:
        disposition = "pass"
        issues.clear()
    intermediate = replace(
        capability,
        lifecycle=lifecycle.lifecycle,
        gate_disposition=disposition,
        issues=tuple(sorted(issues)),
    )
    return bind_candidate_protocol_witness(
        intermediate,
        schema_version=witness.schema_version,
        witness_sha256=witness_sha256,
        passed=witness.passed,
    )


def _configuration_conclusive(witness: ProtocolBehaviorWitness) -> bool:
    expected_transport = {
        "pyright": "workspace_configuration",
        "ty": "did_change_configuration",
        "pyrefly": "initialization_options",
    }[witness.candidate]
    if (
        witness.configuration_transport != expected_transport
        or witness.configuration_interpreter != witness.selected_interpreter
        or witness.configuration_payload_sha256 is None
    ):
        return False
    if witness.candidate == "pyright":
        return witness.configuration_request_count > 0
    return witness.configuration_path is not None


def _phase_disposition(
    outcomes: tuple[CandidateProtocolOutcome, ...],
    deltas: Sequence[object],
    issues: Sequence[str],
) -> tuple[str, str]:
    if any(
        outcome.gate_disposition == "configuration_inconclusive"
        for outcome in outcomes
    ):
        return "hold", PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE
    mutated = any(
        getattr(delta, "unexpected", ()) or getattr(delta, "control_changes", ())
        for delta in deltas
    )
    if mutated:
        return "hold", PROTOCOL_PHASE_NEXT_ACTION_STOP
    if len(outcomes) != len(_CANDIDATES) or issues:
        return "incomplete", PROTOCOL_PHASE_NEXT_ACTION_STOP
    pyright = next(
        (outcome for outcome in outcomes if outcome.candidate == "pyright"), None
    )
    if pyright is None or pyright.gate_disposition != "pass":
        return "hold", PROTOCOL_PHASE_NEXT_ACTION_STOP
    competitor_survives = any(
        outcome.candidate != "pyright" and outcome.gate_disposition == "pass"
        for outcome in outcomes
    )
    return (
        "pass",
        PROTOCOL_PHASE_NEXT_ACTION_PASS
        if competitor_survives
        else PROTOCOL_PHASE_NEXT_ACTION_STOP,
    )


def _require_runtime(runtime: object) -> CandidateRuntime:
    if not isinstance(runtime, CandidateRuntime):
        raise ProtocolPhaseError("candidate runtime is not a verified CandidateRuntime")
    return runtime


def _service_config(runtime: CandidateRuntime, candidate: str) -> Any:
    matches = tuple(
        config for config in runtime.service_configs if config.backend == candidate
    )
    if len(matches) != 1:
        raise ProtocolPhaseError(
            f"candidate runtime does not bind one {candidate} service config"
        )
    return matches[0]


def _pyright_facts(runtime: CandidateRuntime, repo_root: Path) -> PyrightFacts:
    interpreter = next(
        (
            Path(environment.interpreter_path)
            for environment in runtime.environments
            if environment.name == "ms"
        ),
        None,
    )
    if interpreter is None:
        raise ProtocolPhaseError("candidate runtime does not bind the ms interpreter")
    return PyrightFacts.locked(root=repo_root, interpreter=interpreter)


def _ensure_owned_directory(owner_root: Path, target: Path) -> int:
    try:
        relative = target.relative_to(owner_root)
    except ValueError as exc:
        raise ProtocolPhaseError(f"artifact directory escapes owner root: {target}") from exc
    current_fd = _open_protocol_artifact_owner_root(owner_root)
    try:
        for part in relative.parts:
            if part in {"", ".", ".."}:
                raise ProtocolPhaseError("artifact directory contains an unsafe component")
            try:
                next_fd = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=current_fd)
                next_fd = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _open_protocol_artifact_owner_root(owner_root: Path) -> int:
    """Open the one absolute artifact owner before any confined relative walk."""

    try:
        return os.open(owner_root, _NOFOLLOW_DIRECTORY_FLAGS)
    except OSError as exc:
        raise ProtocolPhaseError(
            f"cannot open declared protocol artifact owner root {owner_root}: {exc}"
        ) from exc


def _open_protocol_run_root(run_root: Path) -> int:
    """Open the one absolute run root before sidecar leaf operations become relative."""

    try:
        return os.open(run_root, _NOFOLLOW_DIRECTORY_FLAGS)
    except OSError as exc:
        raise ProtocolPhaseError(
            f"cannot open declared protocol run root {run_root}: {exc}"
        ) from exc


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _detail(error: BaseException) -> str:
    detail = str(error).replace("\n", " ").strip()
    return detail[:500] or type(error).__name__


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the sealed protocol-phase gate")
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--runtime-base", required=True, type=Path)
    parser.add_argument("--parent-evaluation-identity", required=True)
    parser.add_argument("--parent-run-identity", required=True)
    parser.add_argument("--parent-receipt-sha256", required=True)
    parser.add_argument("--parent-artifact-tree-digest", required=True)
    parser.add_argument("--parent-candidate-lock-digest", required=True)
    parser.add_argument("--parent-runtime-manifest-sha256", required=True)
    parser.add_argument("--parent-production-source-revision", required=True)
    parser.add_argument("--parent-production-dependency-lock-digest", required=True)
    parser.add_argument("--parent-production-build-identity", required=True)
    parser.add_argument("--workspace-root", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--line", required=True, type=int)
    parser.add_argument("--character", required=True, type=int)
    parser.add_argument(
        "--workspace-snapshot",
        action="append",
        required=True,
        help="root|kind|source_revision_or_dash|manifest_sha256",
    )
    return parser


def _parse_snapshot(value: str) -> AdmissionRootWitness:
    parts = value.split("|")
    if len(parts) != 4:
        raise ValueError("workspace snapshot must have four pipe-delimited fields")
    root, kind, revision, digest = parts
    return AdmissionRootWitness(
        root=root,
        kind=kind,
        source_revision=None if revision == "-" else revision,
        manifest_digest=digest,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        snapshots = tuple(
            sorted(
                (_parse_snapshot(value) for value in args.workspace_snapshot),
                key=lambda witness: witness.root,
            )
        )
        receipt = run_protocol_phase(
            ProtocolPhaseRequest(
                repo_root=args.repo_root,
                artifact_root=args.artifact_root,
                runtime_base=args.runtime_base,
                parent_evaluation_identity=args.parent_evaluation_identity,
                parent_run_identity=args.parent_run_identity,
                parent_receipt_sha256=args.parent_receipt_sha256,
                parent_artifact_tree_digest=args.parent_artifact_tree_digest,
                parent_candidate_lock_digest=args.parent_candidate_lock_digest,
                parent_runtime_manifest_sha256=args.parent_runtime_manifest_sha256,
                parent_production_source_revision=args.parent_production_source_revision,
                parent_production_dependency_lock_digest=(
                    args.parent_production_dependency_lock_digest
                ),
                parent_production_build_identity=args.parent_production_build_identity,
                workspace_root=args.workspace_root,
                target=args.target,
                symbol_position=(args.line, args.character),
                workspace_snapshots=snapshots,
            )
        )
    except BaseException as exc:
        print(canonical_json({"status": "error", "detail": _detail(exc)}).decode())
        return 2
    print(
        canonical_json(
            {
                "status": receipt.status,
                "evaluation_identity": receipt.evaluation_identity,
                "run_identity": receipt.run_identity,
                "next_action": receipt.next_action,
                "outcomes": [
                    {
                        "candidate": outcome.candidate,
                        "gate_disposition": outcome.gate_disposition,
                    }
                    for outcome in receipt.outcomes
                ],
            }
        ).decode()
    )
    return 0 if receipt.status == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
