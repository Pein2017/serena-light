"""Canonicalization and validation tests for the Phase 2 protocol/capability schema."""

from __future__ import annotations

from typing import Any, cast

import pytest

from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    EVALUATION_CONTRACT_VERSION,
    PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
    CandidateLock,
    CandidatePackage,
    CandidateProtocolOutcome,
    CapabilityEvidence,
    EvaluatorIdentity,
    LifecycleEvidence,
    LockEvidence,
    PathRecord,
    PhaseBudget,
    ProductionIdentity,
    ProtocolPhaseReceipt,
    ResolvedPackage,
    RootManifest,
    RuntimeBinding,
    WriteDelta,
    default_phase_budgets,
)
from serena_light.lsp.adapter import RawLspProviders

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_GIT_REV = "f" * 40


def _capability_evidence(name: str = "definition", **overrides: object) -> CapabilityEvidence:
    fields: dict[str, object] = {
        "name": name,
        "advertised": True,
        "accepted": True,
        "normalized_valid": True,
        "task_utility": CAPABILITY_TASK_UTILITY_DEFERRED,
        "notes": "",
    }
    fields.update(overrides)
    return CapabilityEvidence(**fields)


def _lifecycle_evidence(**overrides: object) -> LifecycleEvidence:
    fields: dict[str, object] = {
        "cold_readiness_seconds": 1.5,
        "diagnostics_mode": "push",
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
    return LifecycleEvidence(**fields)


def _raw_providers(**overrides: object) -> RawLspProviders:
    fields: dict[str, object] = {
        "definition": True,
        "declaration": False,
        "implementation": True,
        "references": True,
        "document_symbols": True,
        "workspace_symbols": True,
    }
    fields.update(overrides)
    return RawLspProviders(**fields)


def _candidate_protocol_outcome(candidate: str = "pyright", **overrides: object) -> CandidateProtocolOutcome:
    fields: dict[str, object] = {
        "candidate": candidate,
        "engine_version": "1.1.403",
        "raw_providers": _raw_providers(),
        "capabilities": (_capability_evidence("definition"), _capability_evidence("references")),
        "lifecycle": _lifecycle_evidence(),
        "gate_disposition": "pass",
        "issues": (),
    }
    fields.update(overrides)
    return CandidateProtocolOutcome(**fields)


def _production_identity(**overrides: object) -> ProductionIdentity:
    fields: dict[str, object] = {
        "pyproject_toml_sha256": _SHA_A,
        "uv_lock_sha256": _SHA_B,
        "package_lock_json_sha256": _SHA_C,
        "dependency_lock_digest": _SHA_D,
        "build_identity": _SHA_E,
        "runtime_paths": (("cli", "/data/x/cli.py"), ("server", "/data/x/server.py")),
    }
    fields.update(overrides)
    return ProductionIdentity(**fields)


def _resolved_package(name: str, **overrides: object) -> ResolvedPackage:
    fields: dict[str, object] = {
        "name": name,
        "version": "0.0.1",
        "requirement": f"{name}==0.0.1",
        "artifact_hashes": (_SHA_A,),
    }
    fields.update(overrides)
    return ResolvedPackage(**fields)


def _candidate_package(name: str = "ty", **overrides: object) -> CandidatePackage:
    fields: dict[str, object] = {
        "name": name,
        "version": "0.0.1",
        "requirement": f"{name}==0.0.1",
        "artifact_hashes": (_SHA_A,),
        "executable_relpath": f"bin/{name}",
    }
    fields.update(overrides)
    return CandidatePackage(**fields)


def _candidate_lock(**overrides: object) -> CandidateLock:
    fields: dict[str, object] = {
        "digest": _SHA_A,
        "exclude_newer": "2026-08-11T00:00:00Z",
        "resolved_packages": (
            _resolved_package("click"),
            _resolved_package("pyrefly"),
            _resolved_package("ty"),
        ),
        "candidates": (_candidate_package("pyrefly"), _candidate_package("ty")),
    }
    fields.update(overrides)
    if "lock_evidence" not in fields:
        fields["lock_evidence"] = LockEvidence.build(
            raw_sha256=cast("str", fields["digest"]),
            raw_size=512,
            resolved_packages=cast("tuple[ResolvedPackage, ...]", fields["resolved_packages"]),
        )
    return CandidateLock(**cast("dict[str, Any]", fields))


def _evaluator_identity() -> EvaluatorIdentity:
    return EvaluatorIdentity.build(
        source_files=(("admission.py", _SHA_A), ("protocol.py", _SHA_B)),
        source_commit=_GIT_REV,
        source_clean=True,
        production_root="/data/CoordExp/serena-light/src",
        production_files=(("src/serena_light/lsp/adapter.py", _SHA_C),),
        production_clean=True,
        host_python_path="/root/miniconda3/envs/ms/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256=_SHA_C,
        host_python_version="3.12.11",
    )


def _runtime_binding() -> RuntimeBinding:
    root = f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{_SHA_A}"
    return RuntimeBinding(
        root=root,
        lock_digest=_SHA_A,
        manifest_path=f"{root}/runtime-manifest.json",
        manifest_sha256=_SHA_D,
    )


def _path_record(path: str = "src/a.py", **overrides: object) -> PathRecord:
    fields: dict[str, object] = {
        "path": path,
        "kind": "file",
        "disposition": "tracked",
        "size": 12,
        "mtime_ns": 1,
        "inode": 1,
        "symlink_target": None,
        "content_sha256": _SHA_A,
    }
    fields.update(overrides)
    return PathRecord(**fields)


def _root_manifest(**overrides: object) -> RootManifest:
    fields: dict[str, object] = {
        "root": "/data/CoordExp/serena-light",
        "kind": "git",
        "source_revision": _GIT_REV,
        "inventory_digest": _SHA_A,
        "inventory_paths": ("src/a.py",),
        "excluded_paths": (".git",),
        "hashed_paths": (_path_record("src/a.py"),),
        "metadata_paths": (),
    }
    fields.update(overrides)
    return RootManifest.build(**cast("dict[str, Any]", fields))


_CORPUS_MANIFEST = _root_manifest()


def _write_delta(**overrides: object) -> WriteDelta:
    fields: dict[str, object] = {
        "root": "/data/CoordExp/serena-light",
        "kind": "git",
        "before_manifest_digest": _CORPUS_MANIFEST.manifest_digest,
        "after_manifest_digest": _CORPUS_MANIFEST.manifest_digest,
        "declared": (),
        "unexpected": (),
        "control_changes": (),
    }
    fields.update(overrides)
    return WriteDelta(**fields)


def _protocol_phase_receipt(*, status: str = "pass", **overrides: object) -> ProtocolPhaseReceipt:
    before = _production_identity()
    fields: dict[str, object] = {
        "schema_version": PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "evaluation_identity": _SHA_A,
        "run_identity": _SHA_B,
        "status": status,
        "started_at": "2026-08-12T00:00:00Z",
        "ended_at": "2026-08-12T00:10:00Z",
        "budgets": (PhaseBudget("protocol", 90 * 60),),
        "evaluator": _evaluator_identity(),
        "production_identity_before": before,
        "production_identity_after": before,
        "candidate_lock": _candidate_lock(),
        "runtime_binding": _runtime_binding(),
        "root_manifests_before": (_root_manifest(),),
        "root_manifests_after": (_root_manifest(),),
        "write_deltas": (_write_delta(),),
        "outcomes": (_candidate_protocol_outcome("pyright"),),
        "issues": (),
        "artifact_tree_digest": _SHA_C,
        "next_action": "begin_product_seam_planning_for_surviving_candidates",
    }
    fields.update(overrides)
    return ProtocolPhaseReceipt(**fields)


# --- CapabilityEvidence -------------------------------------------------------


def test_capability_evidence_fixes_task_utility_and_rejects_override() -> None:
    evidence = CapabilityEvidence(
        name="implementation",
        advertised=False,
        accepted=None,
        normalized_valid=None,
        task_utility=CAPABILITY_TASK_UTILITY_DEFERRED,
        notes="ty 0.x does not advertise textDocument/implementation",
    )
    assert evidence.task_utility == CAPABILITY_TASK_UTILITY_DEFERRED

    with pytest.raises(ValueError, match="task_utility"):
        CapabilityEvidence(
            name="implementation",
            advertised=False,
            accepted=None,
            normalized_valid=None,
            task_utility="improves_task_x",
            notes="",
        )


def test_capability_evidence_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _capability_evidence(name="")


def test_capability_evidence_accepted_and_normalized_valid_may_be_none() -> None:
    evidence = _capability_evidence(accepted=None, normalized_valid=None)
    assert evidence.accepted is None
    assert evidence.normalized_valid is None


# --- LifecycleEvidence ---------------------------------------------------------


def test_lifecycle_evidence_rejects_unknown_diagnostics_mode() -> None:
    with pytest.raises(ValueError, match="diagnostics_mode"):
        _lifecycle_evidence(diagnostics_mode="poll")


def test_lifecycle_evidence_rejects_negative_cold_readiness() -> None:
    with pytest.raises(ValueError, match="cold_readiness_seconds"):
        _lifecycle_evidence(cold_readiness_seconds=-0.1)


def test_lifecycle_evidence_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match="content_modified_count"):
        _lifecycle_evidence(content_modified_count=-1)
    with pytest.raises(ValueError, match="request_cancelled_count"):
        _lifecycle_evidence(request_cancelled_count=-1)


# --- CandidateProtocolOutcome ---------------------------------------------------


def test_candidate_protocol_outcome_disposition_is_closed() -> None:
    with pytest.raises(ValueError, match="gate_disposition"):
        _candidate_protocol_outcome(gate_disposition="mostly_pass")


def test_candidate_protocol_outcome_rejects_unknown_candidate_name() -> None:
    with pytest.raises(ValueError, match="candidate"):
        _candidate_protocol_outcome(candidate="mypy")


def test_candidate_protocol_outcome_rejects_duplicate_capability_names() -> None:
    with pytest.raises(ValueError, match="capabilities"):
        _candidate_protocol_outcome(
            capabilities=(_capability_evidence("definition"), _capability_evidence("definition"))
        )


def test_candidate_protocol_outcome_rejects_non_raw_providers() -> None:
    with pytest.raises(ValueError, match="raw_providers"):
        _candidate_protocol_outcome(raw_providers=object())


# --- ProtocolPhaseReceipt -------------------------------------------------------


def test_protocol_phase_receipt_pass_requires_frozen_protocol_budget() -> None:
    with pytest.raises(ValueError, match="protocol"):
        _protocol_phase_receipt(status="pass", budgets=(PhaseBudget("protocol", 60 * 60),))


def test_protocol_phase_receipt_pass_requires_evaluator() -> None:
    with pytest.raises(ValueError, match="evaluator"):
        _protocol_phase_receipt(status="pass", evaluator=None)


def test_protocol_phase_receipt_pass_requires_runtime_binding() -> None:
    with pytest.raises(ValueError, match="runtime_binding"):
        _protocol_phase_receipt(status="pass", runtime_binding=None)


def test_protocol_phase_receipt_pass_requires_no_issues() -> None:
    with pytest.raises(ValueError, match="issues"):
        _protocol_phase_receipt(status="pass", issues=("something went wrong",))


def test_protocol_phase_receipt_pass_requires_matching_production_identity() -> None:
    with pytest.raises(ValueError, match="production identity"):
        _protocol_phase_receipt(status="pass", production_identity_after=_production_identity(build_identity=_SHA_A))


def test_protocol_phase_receipt_pass_rejects_unexpected_write() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        _protocol_phase_receipt(status="pass", write_deltas=(_write_delta(unexpected=("evil.py",)),))


def test_protocol_phase_receipt_pass_requires_at_least_one_outcome() -> None:
    with pytest.raises(ValueError, match="outcome"):
        _protocol_phase_receipt(status="pass", outcomes=())


def test_protocol_phase_receipt_incomplete_does_not_require_pass_invariants() -> None:
    receipt = _protocol_phase_receipt(status="incomplete", evaluator=None, runtime_binding=None, outcomes=())
    assert receipt.status == "incomplete"


def test_protocol_phase_receipt_rejects_duplicate_outcome_candidates() -> None:
    with pytest.raises(ValueError, match="outcomes"):
        _protocol_phase_receipt(
            status="incomplete",
            evaluator=None,
            runtime_binding=None,
            outcomes=(_candidate_protocol_outcome("pyright"), _candidate_protocol_outcome("pyright")),
        )


def test_protocol_phase_receipt_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        ProtocolPhaseReceipt.from_dict({"schema_version": 999})


def test_protocol_phase_receipt_to_dict_from_dict_round_trips() -> None:
    receipt = _protocol_phase_receipt()
    assert ProtocolPhaseReceipt.from_dict(receipt.to_dict()) == receipt


def test_protocol_phase_receipt_from_dict_rejects_unknown_field() -> None:
    receipt = _protocol_phase_receipt()
    payload = receipt.to_dict()
    payload["extra"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_budgets_frozen_protocol_matches_default() -> None:
    protocol_budget = next(budget for budget in default_phase_budgets() if budget.name == "protocol")
    assert protocol_budget == PhaseBudget("protocol", 90 * 60)
