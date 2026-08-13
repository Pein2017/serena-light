"""Canonicalization and validation tests for the Phase 2 protocol/capability schema."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, cast

import pytest

from scripts.backend_eval.models import (
    CAPABILITY_TASK_UTILITY_DEFERRED,
    EVALUATION_CONTRACT_VERSION,
    PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
    PROTOCOL_PHASE_NEXT_ACTION_PASS,
    PROTOCOL_PHASE_NEXT_ACTION_STOP,
    PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION,
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
    PhaseBudget,
    ProductionIdentity,
    ProtocolPhaseReceipt,
    ProtocolProbeBinding,
    ResolvedPackage,
    RootManifest,
    RuntimeBinding,
    WriteDelta,
    bind_candidate_protocol_witness,
    canonical_json,
    default_phase_budgets,
)
from serena_light.lsp.adapter import RawLspProviders

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_GIT_REV = "f" * 40
_PROBED_CAPABILITY_NAMES = (
    "definition",
    "document_symbols",
    "implementation",
    "references",
    "workspace_symbols",
)


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
    raw_providers = _raw_providers()
    fields: dict[str, object] = {
        "candidate": candidate,
        "engine_version": "1.1.403" if candidate == "pyright" else "0.0.1",
        "raw_providers": raw_providers,
        "capabilities": tuple(
            _capability_evidence(name, advertised=getattr(raw_providers, name))
            for name in _PROBED_CAPABILITY_NAMES
        ),
        "lifecycle": _lifecycle_evidence(),
        "gate_disposition": "pass",
        "issues": (),
        "witness_schema_version": PROTOCOL_WITNESS_SCHEMA_VERSION,
        "witness_sha256": _SHA_B,
        "witness_passed": True,
    }
    fields.update(overrides)
    return CandidateProtocolOutcome(**fields)


def _failed_candidate_protocol_outcome(
    candidate: str,
    *,
    gate_disposition: str = "fail",
) -> CandidateProtocolOutcome:
    lifecycle = (
        _lifecycle_evidence(diagnostics_mode="pull")
        if gate_disposition == "seam_incompatible_pull_only"
        else _lifecycle_evidence()
    )
    return _candidate_protocol_outcome(
        candidate,
        gate_disposition=gate_disposition,
        lifecycle=lifecycle,
        witness_passed=gate_disposition != "fail",
        issues=(f"{candidate} did not survive the protocol gate",),
    )


def _canonical_protocol_outcomes(
    *,
    pyright: CandidateProtocolOutcome | None = None,
    pyrefly: CandidateProtocolOutcome | None = None,
    ty: CandidateProtocolOutcome | None = None,
) -> tuple[CandidateProtocolOutcome, ...]:
    return (
        pyrefly or _failed_candidate_protocol_outcome("pyrefly"),
        pyright or _candidate_protocol_outcome("pyright"),
        ty or _failed_candidate_protocol_outcome("ty"),
    )


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


def _admission_binding(**overrides: object) -> AdmissionBinding:
    fields: dict[str, object] = {
        # Parent and child evaluation identities are deliberately different.  Task 8 derives
        # a new child identity from this parent binding plus the current evaluator/artifact
        # authority; it must never collapse both phases into the Phase 1 identity.
        "admission_evaluation_identity": _SHA_B,
        "admission_run_identity": _SHA_C,
        "receipt_path": f"/data/evidence/{_SHA_B}/receipts/{_SHA_C}.json",
        "receipt_sha256": _SHA_D,
        "artifact_tree_digest": _SHA_E,
        "candidate_lock_digest": _SHA_A,
        "runtime_root": f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{_SHA_A}",
        "runtime_manifest_sha256": _SHA_D,
        "production_root": "/data/CoordExp/serena-light",
        "production_source_revision": _GIT_REV,
        "production_dependency_lock_digest": _SHA_D,
        "production_build_identity": _SHA_E,
        "parent_root_manifests": (
            AdmissionRootWitness(
                root="/data/CoordExp/serena-light",
                kind="git",
                source_revision=_GIT_REV,
                manifest_digest=_root_manifest().manifest_digest,
            ),
        ),
    }
    fields.update(overrides)
    return AdmissionBinding(**fields)


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
        "admission_binding": _admission_binding(),
        "evaluator": _evaluator_identity(),
        "production_identity_before": before,
        "production_identity_after": before,
        "candidate_lock": _candidate_lock(),
        "runtime_binding": _runtime_binding(),
        "probe_binding": ProtocolProbeBinding(
            workspace_root="/data/CoordExp/serena-light",
            relative_target="src/a.py",
            absolute_target="/data/CoordExp/serena-light/src/a.py",
            position=(4, 7),
            root_witness=_admission_binding().parent_root_manifests[0],
        ),
        "root_manifests_before": (_root_manifest(),),
        "root_manifests_after": (_root_manifest(),),
        "write_deltas": (_write_delta(),),
        "outcomes": _canonical_protocol_outcomes(),
        "issues": (),
        "artifact_tree_digest": _SHA_C,
        "next_action": PROTOCOL_PHASE_NEXT_ACTION_STOP,
    }
    fields.update(overrides)
    if "probe_binding" not in overrides:
        frozen = cast("tuple[RootManifest, ...]", fields["root_manifests_before"])[0]
        fields["probe_binding"] = ProtocolProbeBinding(
            workspace_root=frozen.root,
            relative_target="src/a.py",
            absolute_target=f"{frozen.root}/src/a.py",
            position=(4, 7),
            root_witness=AdmissionRootWitness(
                root=frozen.root,
                kind=frozen.kind,
                source_revision=frozen.source_revision,
                manifest_digest=frozen.manifest_digest,
            ),
        )
    return ProtocolPhaseReceipt(**fields)


def _protocol_phase_receipt_with_pyright(
    pyright: CandidateProtocolOutcome,
) -> ProtocolPhaseReceipt:
    return _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(
            pyright=pyright,
            ty=_candidate_protocol_outcome("ty"),
        ),
        next_action=PROTOCOL_PHASE_NEXT_ACTION_PASS,
    )


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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_lifecycle_evidence_rejects_non_finite_cold_readiness(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        _lifecycle_evidence(cold_readiness_seconds=value)


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


def test_candidate_protocol_outcome_rejects_unknown_witness_schema() -> None:
    with pytest.raises(ValueError, match="witness_schema_version"):
        _candidate_protocol_outcome(witness_schema_version=999)


def test_candidate_protocol_outcome_rejects_malformed_witness_digest() -> None:
    with pytest.raises(ValueError, match="witness_sha256"):
        _candidate_protocol_outcome(witness_sha256="not-a-digest")


def test_candidate_protocol_outcome_requires_boolean_witness_disposition() -> None:
    with pytest.raises(ValueError, match="all present or all absent"):
        _candidate_protocol_outcome(witness_passed=None)


def test_candidate_protocol_outcome_allows_unbound_intermediate_witness() -> None:
    outcome = _candidate_protocol_outcome(
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    assert outcome.witness_schema_version is None
    assert outcome.witness_sha256 is None
    assert outcome.witness_passed is None


def test_candidate_protocol_outcome_rejects_partial_witness_binding() -> None:
    match = "all present or all absent"
    with pytest.raises(ValueError, match=match):
        _candidate_protocol_outcome(witness_schema_version=None)
    with pytest.raises(ValueError, match=match):
        _candidate_protocol_outcome(witness_sha256=None)
    with pytest.raises(ValueError, match=match):
        _candidate_protocol_outcome(witness_passed=None)


def test_bind_candidate_protocol_witness_returns_new_exactly_bound_outcome() -> None:
    provisional = _candidate_protocol_outcome(
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    bound = bind_candidate_protocol_witness(
        provisional,
        schema_version=PROTOCOL_WITNESS_SCHEMA_VERSION,
        witness_sha256=_SHA_C,
        passed=False,
    )

    assert provisional.witness_schema_version is None
    assert bound.witness_schema_version == PROTOCOL_WITNESS_SCHEMA_VERSION
    assert bound.witness_sha256 == _SHA_C
    assert bound.witness_passed is False


def test_bind_candidate_protocol_witness_rejects_rebinding() -> None:
    with pytest.raises(ValueError, match="already bound"):
        bind_candidate_protocol_witness(
            _candidate_protocol_outcome(),
            schema_version=PROTOCOL_WITNESS_SCHEMA_VERSION,
            witness_sha256=_SHA_C,
            passed=True,
        )


def test_candidate_protocol_outcome_configuration_inconclusive_is_not_backend_failure() -> None:
    outcome = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        witness_passed=False,
        issues=("selected interpreter could not be attributed",),
    )

    assert outcome.gate_disposition == "configuration_inconclusive"
    assert not outcome.witness_passed


def test_candidate_protocol_outcome_configuration_inconclusive_intermediate_may_be_unbound() -> None:
    outcome = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        issues=("selected interpreter could not be attributed",),
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    assert outcome.gate_disposition == "configuration_inconclusive"
    assert outcome.witness_passed is None


def test_candidate_protocol_outcome_configuration_inconclusive_rejects_passing_witness() -> None:
    with pytest.raises(ValueError, match="configuration_inconclusive"):
        _candidate_protocol_outcome(
            "ty",
            gate_disposition="configuration_inconclusive",
            issues=("selected interpreter could not be attributed",),
            witness_passed=True,
        )


# --- ProtocolPhaseReceipt -------------------------------------------------------


def test_protocol_phase_receipt_pass_requires_frozen_protocol_budget() -> None:
    with pytest.raises(ValueError, match="protocol"):
        _protocol_phase_receipt(status="pass", budgets=(PhaseBudget("protocol", 60 * 60),))


def test_protocol_phase_receipt_pass_rejects_extra_budgets_beyond_the_frozen_protocol_entry() -> None:
    """M8: phase-scoped by design -- an otherwise-correct protocol entry plus any extra
    budget (even a legitimately-named one from another phase's receipt) is a defect, not
    additional evidence."""

    with pytest.raises(ValueError, match="budgets"):
        _protocol_phase_receipt(
            status="pass",
            budgets=(PhaseBudget("protocol", 90 * 60), PhaseBudget("admission", 30 * 60)),
        )


def test_protocol_phase_receipt_pass_requires_the_frozen_next_action_literal() -> None:
    with pytest.raises(ValueError, match="next_action"):
        _protocol_phase_receipt(status="pass", next_action="do_something_else")


def test_protocol_phase_receipt_schema_advances_to_v4_for_probe_binding() -> None:
    assert PROTOCOL_PHASE_RECEIPT_SCHEMA_VERSION == 4


def test_protocol_probe_binding_round_trips_exactly_and_is_lexically_closed() -> None:
    binding = _protocol_phase_receipt().probe_binding

    assert ProtocolProbeBinding.from_dict(binding.to_dict()) == binding
    with pytest.raises(ValueError, match="relative_target"):
        replace(binding, relative_target="../outside.py")
    with pytest.raises(ValueError, match="absolute_target"):
        replace(binding, absolute_target="/data/CoordExp/other/a.py")
    with pytest.raises(ValueError, match="position"):
        replace(binding, position=(-1, 0))
    with pytest.raises(ValueError, match="root_witness"):
        replace(
            binding,
            root_witness=replace(binding.root_witness, root="/data/CoordExp/other"),
        )


def test_protocol_phase_receipt_round_trip_carries_exact_probe_binding() -> None:
    receipt = _protocol_phase_receipt()

    assert ProtocolPhaseReceipt.from_dict(receipt.to_dict()) == receipt
    payload = receipt.to_dict()
    probe = cast("dict[str, object]", payload["probe_binding"])
    probe["position"] = [4, -1]
    with pytest.raises(ValueError, match="position"):
        ProtocolPhaseReceipt.from_dict(payload)
    payload = receipt.to_dict()
    probe = cast("dict[str, object]", payload["probe_binding"])
    probe["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_requires_exact_parent_admission_binding() -> None:
    with pytest.raises(ValueError, match="admission_binding"):
        _protocol_phase_receipt(status="pass", admission_binding=None)


def test_protocol_phase_receipt_pass_rejects_a_survivor_without_passing_witness() -> None:
    pyright = _candidate_protocol_outcome("pyright", witness_passed=False)
    with pytest.raises(ValueError, match="witness_passed"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(pyright=pyright))


def test_protocol_phase_receipt_pass_rejects_failed_candidate_without_bound_witness() -> None:
    pyrefly = _failed_candidate_protocol_outcome("pyrefly")
    pyrefly = replace(
        pyrefly,
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    with pytest.raises(ValueError, match="pyrefly.*bound witness"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(pyrefly=pyrefly))


def test_protocol_phase_receipt_from_dict_rejects_failed_candidate_without_bound_witness() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    pyrefly = next(item for item in outcomes if item["candidate"] == "pyrefly")
    pyrefly["witness_schema_version"] = None
    pyrefly["witness_sha256"] = None
    pyrefly["witness_passed"] = None

    with pytest.raises(ValueError, match="pyrefly.*bound witness"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_rejects_all_positive_failed_candidate() -> None:
    all_positive = _candidate_protocol_outcome(
        "pyrefly",
        gate_disposition="fail",
        issues=("unsubstantiated failure",),
    )

    with pytest.raises(ValueError, match="pyrefly.*negative"):
        _protocol_phase_receipt(
            outcomes=_canonical_protocol_outcomes(pyrefly=all_positive)
        )


def test_protocol_phase_receipt_from_dict_rejects_all_positive_failed_candidate() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    pyrefly = next(item for item in outcomes if item["candidate"] == "pyrefly")
    pyrefly["witness_passed"] = True

    with pytest.raises(ValueError, match="pyrefly.*negative"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_rejects_configuration_inconclusive_candidate() -> None:
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        witness_passed=False,
        issues=("configuration attribution is inconclusive",),
    )
    with pytest.raises(ValueError, match="configuration_inconclusive"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=ty))


@pytest.mark.parametrize("status", ["hold", "incomplete"])
def test_protocol_phase_receipt_configuration_inconclusive_requires_conservative_action(
    status: str,
) -> None:
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        witness_passed=False,
        issues=("configuration attribution is inconclusive",),
    )
    receipt = _protocol_phase_receipt(
        status=status,
        outcomes=_canonical_protocol_outcomes(ty=ty),
        issues=("candidate configuration evidence is inconclusive",),
        next_action=PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
    )

    assert receipt.status == status
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE
    assert {outcome.candidate for outcome in receipt.outcomes} == {"pyright", "ty", "pyrefly"}


def test_protocol_phase_receipt_configuration_inconclusive_rejects_definitive_stop() -> None:
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        witness_passed=False,
        issues=("configuration attribution is inconclusive",),
    )
    with pytest.raises(ValueError, match="inconclusive_retain_pyright"):
        _protocol_phase_receipt(
            status="hold",
            outcomes=_canonical_protocol_outcomes(ty=ty),
            issues=("candidate configuration evidence is inconclusive",),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_STOP,
        )


def test_protocol_phase_receipt_configuration_inconclusive_still_requires_pyright_witness() -> None:
    pyright = _candidate_protocol_outcome(
        "pyright",
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        issues=("configuration attribution is inconclusive",),
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    with pytest.raises(ValueError, match="pyright.*bound witness"):
        _protocol_phase_receipt(
            status="hold",
            outcomes=_canonical_protocol_outcomes(pyright=pyright, ty=ty),
            issues=("candidate configuration evidence is inconclusive",),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
        )


def test_protocol_phase_receipt_configuration_inconclusive_requires_failed_bound_witness() -> None:
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        issues=("configuration attribution is inconclusive",),
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )

    with pytest.raises(ValueError, match="ty.*bound witness"):
        _protocol_phase_receipt(
            status="hold",
            outcomes=_canonical_protocol_outcomes(ty=ty),
            issues=("candidate configuration evidence is inconclusive",),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
        )


def test_protocol_phase_receipt_inconclusive_action_requires_configuration_evidence() -> None:
    with pytest.raises(ValueError, match="configuration_inconclusive"):
        _protocol_phase_receipt(
            status="hold",
            issues=("inconclusive",),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
        )


def test_protocol_phase_receipt_child_identity_is_distinct_from_parent_identity() -> None:
    receipt = _protocol_phase_receipt()

    assert receipt.admission_binding is not None
    assert receipt.evaluation_identity != receipt.admission_binding.admission_evaluation_identity


def test_protocol_phase_receipt_rejects_child_identity_equal_to_parent_identity() -> None:
    parent = _admission_binding()

    with pytest.raises(ValueError, match="child evaluation_identity"):
        _protocol_phase_receipt(
            evaluation_identity=parent.admission_evaluation_identity,
            admission_binding=parent,
        )


def test_protocol_phase_receipt_from_dict_rejects_child_identity_equal_to_parent() -> None:
    payload = _protocol_phase_receipt().to_dict()
    admission = cast("dict[str, object]", payload["admission_binding"])
    payload["evaluation_identity"] = admission["admission_evaluation_identity"]

    with pytest.raises(ValueError, match="child evaluation_identity"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_binds_candidate_lock_to_parent_admission() -> None:
    other_root = f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{_SHA_B}"
    with pytest.raises(ValueError, match="candidate_lock.*admission_binding"):
        _protocol_phase_receipt(
            admission_binding=_admission_binding(
                candidate_lock_digest=_SHA_B,
                runtime_root=other_root,
            )
        )


def test_protocol_phase_receipt_pass_binds_runtime_to_parent_admission() -> None:
    with pytest.raises(ValueError, match="runtime_binding.*admission_binding"):
        _protocol_phase_receipt(
            admission_binding=_admission_binding(runtime_manifest_sha256=_SHA_B)
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("production_dependency_lock_digest", _SHA_A, "dependency lock"),
        ("production_build_identity", _SHA_A, "build identity"),
    ],
)
def test_protocol_phase_receipt_pass_binds_production_to_parent_admission(
    field: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _protocol_phase_receipt(admission_binding=_admission_binding(**{field: value}))


def test_protocol_phase_receipt_pass_requires_exact_parent_corpus_roots() -> None:
    unrelated = _root_manifest(root="/data/unrelated")
    unrelated_delta = WriteDelta(
        root=unrelated.root,
        kind=unrelated.kind,
        before_manifest_digest=unrelated.manifest_digest,
        after_manifest_digest=unrelated.manifest_digest,
        declared=(),
        unexpected=(),
        control_changes=(),
    )

    with pytest.raises(ValueError, match="parent corpus roots"):
        _protocol_phase_receipt(
            root_manifests_before=(unrelated,),
            root_manifests_after=(unrelated,),
            write_deltas=(unrelated_delta,),
        )


def test_protocol_phase_receipt_pass_requires_exact_parent_manifest_witness() -> None:
    changed = _root_manifest(inventory_digest=_SHA_B)
    changed_delta = _write_delta(
        before_manifest_digest=changed.manifest_digest,
        after_manifest_digest=changed.manifest_digest,
    )

    with pytest.raises(ValueError, match="parent manifest witness"):
        _protocol_phase_receipt(
            root_manifests_before=(changed,),
            root_manifests_after=(changed,),
            write_deltas=(changed_delta,),
        )


def test_protocol_phase_receipt_from_dict_rejects_unrelated_only_corpus() -> None:
    payload = _protocol_phase_receipt().to_dict()
    unrelated = _root_manifest(root="/data/unrelated")
    unrelated_delta = WriteDelta(
        root=unrelated.root,
        kind=unrelated.kind,
        before_manifest_digest=unrelated.manifest_digest,
        after_manifest_digest=unrelated.manifest_digest,
        declared=(),
        unexpected=(),
        control_changes=(),
    )
    replacement = _protocol_phase_receipt(
        status="incomplete",
        admission_binding=None,
        root_manifests_before=(unrelated,),
        root_manifests_after=(unrelated,),
        write_deltas=(unrelated_delta,),
    ).to_dict()
    payload["root_manifests_before"] = replacement["root_manifests_before"]
    payload["root_manifests_after"] = replacement["root_manifests_after"]
    payload["write_deltas"] = replacement["write_deltas"]

    with pytest.raises(ValueError, match="parent corpus roots"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_requires_evaluator() -> None:
    with pytest.raises(ValueError, match="evaluator"):
        _protocol_phase_receipt(status="pass", evaluator=None)


@pytest.mark.parametrize("field", ["source_clean", "production_clean"])
def test_protocol_phase_receipt_pass_requires_clean_evaluator(field: str) -> None:
    evaluator = _evaluator_identity()

    with pytest.raises(ValueError, match=field):
        _protocol_phase_receipt(evaluator=replace(evaluator, **{field: False}))


@pytest.mark.parametrize("field", ["source_clean", "production_clean"])
def test_protocol_phase_receipt_from_dict_rejects_dirty_evaluator(field: str) -> None:
    payload = _protocol_phase_receipt().to_dict()
    evaluator = cast("dict[str, object]", payload["evaluator"])
    evaluator[field] = False

    with pytest.raises(ValueError, match=field):
        ProtocolPhaseReceipt.from_dict(payload)


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


@pytest.mark.parametrize("field", ["declared", "control_changes"])
def test_protocol_phase_receipt_pass_rejects_every_kind_of_write_delta(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _protocol_phase_receipt(status="pass", write_deltas=(_write_delta(**{field: ("src/a.py",)}),))


def test_protocol_phase_receipt_pass_requires_exactly_all_three_candidate_outcomes() -> None:
    with pytest.raises(ValueError, match="exactly"):
        _protocol_phase_receipt(status="pass", outcomes=())

    with pytest.raises(ValueError, match="exactly"):
        _protocol_phase_receipt(
            status="pass",
            outcomes=(
                _failed_candidate_protocol_outcome("pyrefly"),
                _candidate_protocol_outcome("pyright"),
            ),
        )


@pytest.mark.parametrize("candidate", ["ty", "pyrefly"])
def test_protocol_phase_receipt_pass_binds_candidate_engine_version_to_lock(candidate: str) -> None:
    mismatched = _failed_candidate_protocol_outcome(candidate)
    mismatched = _candidate_protocol_outcome(
        candidate,
        engine_version="9999",
        gate_disposition=mismatched.gate_disposition,
        issues=mismatched.issues,
    )
    outcomes = (
        _canonical_protocol_outcomes(ty=mismatched)
        if candidate == "ty"
        else _canonical_protocol_outcomes(pyrefly=mismatched)
    )
    with pytest.raises(ValueError, match=f"{candidate}.*engine_version"):
        _protocol_phase_receipt(outcomes=outcomes)


@pytest.mark.parametrize("candidate", ["ty", "pyrefly"])
def test_protocol_phase_receipt_from_dict_rejects_candidate_engine_version_not_in_lock(candidate: str) -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    outcome = next(item for item in outcomes if item["candidate"] == candidate)
    outcome["engine_version"] = "9999"
    with pytest.raises(ValueError, match=f"{candidate}.*engine_version"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_pass_requires_pyright_to_survive() -> None:
    failed_pyright = _failed_candidate_protocol_outcome("pyright")
    with pytest.raises(ValueError, match="Pyright"):
        _protocol_phase_receipt(
            outcomes=_canonical_protocol_outcomes(pyright=failed_pyright),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_PASS,
        )


def test_protocol_phase_receipt_pass_derives_stop_action_for_sole_pyright_survivor() -> None:
    receipt = _protocol_phase_receipt()
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_STOP

    with pytest.raises(ValueError, match="next_action"):
        _protocol_phase_receipt(next_action=PROTOCOL_PHASE_NEXT_ACTION_PASS)


def test_protocol_phase_pass_allows_deferred_optional_implementation_negative() -> None:
    pyright = _candidate_protocol_outcome("pyright")
    capabilities = tuple(
        replace(
            capability,
            accepted=True,
            normalized_valid=False,
            notes="normalization returned no evidence",
        )
        if capability.name == "implementation"
        else capability
        for capability in pyright.capabilities
    )

    receipt = _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(
            pyright=replace(pyright, capabilities=capabilities)
        )
    )

    implementation = next(
        capability
        for capability in receipt.outcomes[1].capabilities
        if capability.name == "implementation"
    )
    assert implementation.accepted is True
    assert implementation.normalized_valid is False
    assert implementation.task_utility == CAPABILITY_TASK_UTILITY_DEFERRED


def test_protocol_phase_receipt_pass_derives_product_seam_action_for_competitor_survivor() -> None:
    passing_ty = _candidate_protocol_outcome("ty")
    receipt = _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(ty=passing_ty),
        next_action=PROTOCOL_PHASE_NEXT_ACTION_PASS,
    )
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_PASS

    with pytest.raises(ValueError, match="next_action"):
        _protocol_phase_receipt(
            outcomes=_canonical_protocol_outcomes(ty=passing_ty),
            next_action=PROTOCOL_PHASE_NEXT_ACTION_STOP,
        )


def test_protocol_phase_receipt_seam_incompatible_candidate_is_retained_but_not_a_survivor() -> None:
    pull_only_ty = _failed_candidate_protocol_outcome(
        "ty",
        gate_disposition="seam_incompatible_pull_only",
    )
    receipt = _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=pull_only_ty))
    assert receipt.next_action == PROTOCOL_PHASE_NEXT_ACTION_STOP
    assert receipt.outcomes[-1].gate_disposition == "seam_incompatible_pull_only"


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("required_rejected", "ty.*references.*accepted"),
        ("required_not_normalized", "ty.*references.*normalized"),
        ("lifecycle_false", "ty.*lifecycle.cleanup_clean"),
        ("witness_false", "ty.*witness_passed"),
    ],
)
def test_protocol_phase_receipt_rejects_unproven_pull_only_seam_constructor(
    mutation: str,
    match: str,
) -> None:
    seam = _failed_candidate_protocol_outcome(
        "ty",
        gate_disposition="seam_incompatible_pull_only",
    )
    if mutation.startswith("required_"):
        seam = replace(
            seam,
            capabilities=tuple(
                replace(
                    capability,
                    accepted=mutation != "required_rejected",
                    normalized_valid=False,
                )
                if capability.name == "references"
                else capability
                for capability in seam.capabilities
            ),
        )
    elif mutation == "lifecycle_false":
        seam = replace(
            seam,
            lifecycle=replace(seam.lifecycle, cleanup_clean=False),
        )
    else:
        seam = replace(seam, witness_passed=False)

    with pytest.raises(ValueError, match=match):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=seam))


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ("required_rejected", "ty.*references.*accepted"),
        ("required_not_normalized", "ty.*references.*normalized"),
        ("lifecycle_false", "ty.*lifecycle.cleanup_clean"),
        ("witness_false", "ty.*witness_passed"),
    ],
)
def test_protocol_phase_receipt_rejects_unproven_pull_only_seam_from_dict(
    mutation: str,
    match: str,
) -> None:
    seam = _failed_candidate_protocol_outcome(
        "ty",
        gate_disposition="seam_incompatible_pull_only",
    )
    payload = _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(ty=seam)
    ).to_dict()
    ty = next(
        outcome
        for outcome in cast("list[dict[str, Any]]", payload["outcomes"])
        if outcome["candidate"] == "ty"
    )
    if mutation.startswith("required_"):
        references = next(
            capability
            for capability in cast("list[dict[str, Any]]", ty["capabilities"])
            if capability["name"] == "references"
        )
        references["accepted"] = mutation != "required_rejected"
        references["normalized_valid"] = False
    elif mutation == "lifecycle_false":
        cast("dict[str, Any]", ty["lifecycle"])["cleanup_clean"] = False
    else:
        ty["witness_passed"] = False

    with pytest.raises(ValueError, match=match):
        ProtocolPhaseReceipt.from_dict(payload)


def test_pull_only_seam_allows_deferred_optional_implementation_negative() -> None:
    seam = _failed_candidate_protocol_outcome(
        "ty",
        gate_disposition="seam_incompatible_pull_only",
    )
    seam = replace(
        seam,
        capabilities=tuple(
            replace(
                capability,
                accepted=True,
                normalized_valid=False,
                notes="normalization returned no evidence",
            )
            if capability.name == "implementation"
            else capability
            for capability in seam.capabilities
        ),
    )

    receipt = _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(ty=seam)
    )
    reparsed = ProtocolPhaseReceipt.from_dict(receipt.to_dict())

    assert reparsed.status == "pass"
    assert reparsed.next_action == PROTOCOL_PHASE_NEXT_ACTION_STOP
    assert reparsed.outcomes[-1].gate_disposition == "seam_incompatible_pull_only"


def test_protocol_phase_receipt_seam_incompatible_requires_pull_diagnostics_evidence() -> None:
    unsupported_claim = _candidate_protocol_outcome(
        "ty",
        gate_disposition="seam_incompatible_pull_only",
        issues=("claimed pull-only incompatibility",),
        lifecycle=_lifecycle_evidence(diagnostics_mode="push"),
    )

    with pytest.raises(ValueError, match="ty.*pull"):
        _protocol_phase_receipt(
            outcomes=_canonical_protocol_outcomes(ty=unsupported_claim)
        )


def test_protocol_phase_receipt_from_dict_rejects_seam_claim_with_push_diagnostics() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    ty = next(item for item in outcomes if item["candidate"] == "ty")
    ty["gate_disposition"] = "seam_incompatible_pull_only"
    ty["issues"] = ["claimed pull-only incompatibility"]
    ty["witness_passed"] = True

    with pytest.raises(ValueError, match="ty.*pull"):
        ProtocolPhaseReceipt.from_dict(payload)


@pytest.mark.parametrize("gate_disposition", ["fail", "seam_incompatible_pull_only"])
@pytest.mark.parametrize("issues", [(), ("",), (" ",)])
def test_protocol_phase_receipt_non_survivor_requires_actionable_issues(
    gate_disposition: str,
    issues: tuple[str, ...],
) -> None:
    invalid_ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition=gate_disposition,
        issues=issues,
    )
    with pytest.raises(ValueError, match="ty.*issues"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=invalid_ty))


def test_protocol_phase_receipt_non_survivor_requires_complete_capability_evidence() -> None:
    invalid_pyrefly = _failed_candidate_protocol_outcome("pyrefly")
    invalid_pyrefly = _candidate_protocol_outcome(
        "pyrefly",
        gate_disposition=invalid_pyrefly.gate_disposition,
        issues=invalid_pyrefly.issues,
        capabilities=tuple(
            _capability_evidence(name)
            for name in _PROBED_CAPABILITY_NAMES
            if name != "workspace_symbols"
        ),
    )
    with pytest.raises(ValueError, match="pyrefly.*capabilities.*exactly"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(pyrefly=invalid_pyrefly))


def test_protocol_phase_receipt_non_survivor_binds_advertisement_to_raw_providers() -> None:
    raw_providers = _raw_providers(implementation=False)
    invalid_pyrefly = _candidate_protocol_outcome(
        "pyrefly",
        raw_providers=raw_providers,
        capabilities=tuple(
            _capability_evidence(name, advertised=True)
            for name in _PROBED_CAPABILITY_NAMES
        ),
        gate_disposition="fail",
        issues=("implementation advertisement evidence is inconsistent",),
    )
    with pytest.raises(ValueError, match="pyrefly.*implementation.*advertised"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(pyrefly=invalid_pyrefly))


@pytest.mark.parametrize(
    ("accepted", "normalized_valid"),
    [(None, None), (None, False), (False, True), (True, None)],
)
def test_protocol_phase_receipt_non_survivor_rejects_incoherent_advertised_results(
    accepted: bool | None,
    normalized_valid: bool | None,
) -> None:
    capabilities = tuple(
        _capability_evidence(
            name,
            accepted=accepted if name == "definition" else True,
            normalized_valid=normalized_valid if name == "definition" else True,
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_pyrefly = _candidate_protocol_outcome(
        "pyrefly",
        capabilities=capabilities,
        gate_disposition="fail",
        issues=("definition request failed",),
    )
    with pytest.raises(ValueError, match="pyrefly.*definition"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(pyrefly=invalid_pyrefly))


@pytest.mark.parametrize(
    ("accepted", "normalized_valid"),
    [(False, False), (True, False)],
)
def test_protocol_phase_receipt_non_survivor_allows_coherent_failed_advertised_results(
    accepted: bool,
    normalized_valid: bool,
) -> None:
    capabilities = tuple(
        _capability_evidence(
            name,
            accepted=accepted if name == "definition" else True,
            normalized_valid=normalized_valid if name == "definition" else True,
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    pyrefly = _candidate_protocol_outcome(
        "pyrefly",
        capabilities=capabilities,
        gate_disposition="fail",
        issues=("definition request did not produce normalized evidence",),
    )
    assert _protocol_phase_receipt(
        outcomes=_canonical_protocol_outcomes(pyrefly=pyrefly)
    ).status == "pass"


def test_protocol_phase_receipt_preserves_explicit_ty_negative_implementation_evidence() -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=None if name == "implementation" else True,
            normalized_valid=None if name == "implementation" else True,
            notes=(
                "locked ty version does not advertise textDocument/implementation"
                if name == "implementation"
                else ""
            ),
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    ty = _candidate_protocol_outcome(
        "ty",
        raw_providers=raw_providers,
        capabilities=capabilities,
        gate_disposition="fail",
        witness_passed=False,
        issues=("ty failed a separate protocol gate",),
    )
    receipt = _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=ty))
    implementation = next(
        capability
        for capability in receipt.outcomes[-1].capabilities
        if capability.name == "implementation"
    )
    assert implementation.accepted is None
    assert implementation.normalized_valid is None
    assert "does not advertise" in implementation.notes


def test_protocol_phase_receipt_requires_explicit_ty_negative_implementation_note() -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=None if name == "implementation" else True,
            normalized_valid=None if name == "implementation" else True,
            notes="",
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_ty = _candidate_protocol_outcome(
        "ty",
        raw_providers=raw_providers,
        capabilities=capabilities,
        gate_disposition="fail",
        issues=("ty failed a separate protocol gate",),
    )
    with pytest.raises(ValueError, match="ty.*implementation.*negative"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=invalid_ty))


def test_protocol_phase_receipt_requires_ty_negative_implementation_null_result() -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=name != "implementation",
            normalized_valid=name != "implementation",
            notes=(
                "locked ty version does not advertise textDocument/implementation"
                if name == "implementation"
                else ""
            ),
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_ty = _candidate_protocol_outcome(
        "ty",
        raw_providers=raw_providers,
        capabilities=capabilities,
        gate_disposition="fail",
        issues=("ty failed a separate protocol gate",),
    )
    with pytest.raises(ValueError, match="ty.*implementation.*negative"):
        _protocol_phase_receipt(outcomes=_canonical_protocol_outcomes(ty=invalid_ty))


def test_protocol_phase_receipt_pass_candidate_requires_no_issues() -> None:
    invalid_pyright = _candidate_protocol_outcome("pyright", issues=("hidden failure",))
    with pytest.raises(ValueError, match="pyright.*issues"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


@pytest.mark.parametrize(
    ("accepted", "normalized_valid"),
    [(False, True), (None, True), (True, False), (True, None)],
)
def test_protocol_phase_receipt_pass_candidate_requires_valid_advertised_capabilities(
    accepted: bool | None,
    normalized_valid: bool | None,
) -> None:
    invalid_capability = _capability_evidence(
        "definition",
        accepted=accepted,
        normalized_valid=normalized_valid,
    )
    invalid_pyright = _candidate_protocol_outcome(
        "pyright",
        capabilities=(
            invalid_capability,
            *(
                _capability_evidence(name)
                for name in _PROBED_CAPABILITY_NAMES
                if name != "definition"
            ),
        ),
    )
    with pytest.raises(ValueError, match="pyright.*definition"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


def test_protocol_phase_receipt_pass_candidate_requires_complete_capability_evidence() -> None:
    incomplete_pyright = _candidate_protocol_outcome(
        "pyright",
        capabilities=tuple(
            _capability_evidence(name)
            for name in _PROBED_CAPABILITY_NAMES
            if name != "workspace_symbols"
        ),
    )
    with pytest.raises(ValueError, match="pyright.*capabilities.*exactly"):
        _protocol_phase_receipt_with_pyright(incomplete_pyright)


def test_protocol_phase_receipt_pass_candidate_binds_advertisement_to_raw_providers() -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(name, advertised=True)
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_pyright = _candidate_protocol_outcome(
        "pyright",
        raw_providers=raw_providers,
        capabilities=capabilities,
    )
    with pytest.raises(ValueError, match="pyright.*implementation.*advertised"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


@pytest.mark.parametrize(
    ("accepted", "normalized_valid"),
    [(None, None), (False, False)],
)
def test_protocol_phase_receipt_allows_existing_unadvertised_capability_representations(
    accepted: bool | None,
    normalized_valid: bool | None,
) -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=accepted if name == "implementation" else True,
            normalized_valid=normalized_valid if name == "implementation" else True,
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    pyright = _candidate_protocol_outcome(
        "pyright",
        raw_providers=raw_providers,
        capabilities=capabilities,
    )
    assert _protocol_phase_receipt_with_pyright(pyright).status == "pass"


def test_protocol_phase_receipt_pass_rejects_unadvertised_required_capability() -> None:
    raw_providers = _raw_providers(references=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=name != "references",
            normalized_valid=name != "references",
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_pyright = _candidate_protocol_outcome(
        "pyright",
        raw_providers=raw_providers,
        capabilities=capabilities,
    )

    with pytest.raises(ValueError, match="pyright.*references.*required"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


@pytest.mark.parametrize(
    ("accepted", "normalized_valid"),
    [(True, True), (True, False), (False, True), (None, False), (False, None)],
)
def test_protocol_phase_receipt_rejects_incoherent_unadvertised_capability_representations(
    accepted: bool | None,
    normalized_valid: bool | None,
) -> None:
    raw_providers = _raw_providers(implementation=False)
    capabilities = tuple(
        _capability_evidence(
            name,
            advertised=getattr(raw_providers, name),
            accepted=accepted if name == "implementation" else True,
            normalized_valid=normalized_valid if name == "implementation" else True,
        )
        for name in _PROBED_CAPABILITY_NAMES
    )
    invalid_pyright = _candidate_protocol_outcome(
        "pyright",
        raw_providers=raw_providers,
        capabilities=capabilities,
    )
    with pytest.raises(ValueError, match="pyright.*implementation.*unadvertised"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


@pytest.mark.parametrize(
    "field",
    [
        "retry_seam_disabled",
        "bounded_timeout_observed",
        "crash_handled",
        "shutdown_clean",
        "cleanup_clean",
        "proxy_rejected",
        "minimal_environment_verified",
        "redaction_verified",
    ],
)
def test_protocol_phase_receipt_pass_candidate_requires_complete_lifecycle_evidence(field: str) -> None:
    invalid_pyright = _candidate_protocol_outcome(
        "pyright",
        lifecycle=_lifecycle_evidence(**{field: False}),
    )
    with pytest.raises(ValueError, match=f"pyright.*{field}"):
        _protocol_phase_receipt_with_pyright(invalid_pyright)


def test_protocol_phase_receipt_pass_requires_equal_before_and_after_manifest_digests() -> None:
    changed_manifest = _root_manifest(inventory_digest=_SHA_B)
    delta = _write_delta(after_manifest_digest=changed_manifest.manifest_digest)
    with pytest.raises(ValueError, match="manifest digest changed"):
        _protocol_phase_receipt(
            root_manifests_after=(changed_manifest,),
            write_deltas=(delta,),
        )


@pytest.mark.parametrize(
    ("after_manifest", "delta_kind"),
    [
        (_root_manifest(), "non_git"),
        (_root_manifest(kind="non_git", source_revision=None), "git"),
    ],
)
def test_protocol_phase_receipt_pass_binds_delta_kind_to_both_manifests(
    after_manifest: RootManifest,
    delta_kind: str,
) -> None:
    delta = _write_delta(
        kind=delta_kind,
        after_manifest_digest=after_manifest.manifest_digest,
    )
    with pytest.raises(ValueError, match="kind"):
        _protocol_phase_receipt(
            root_manifests_after=(after_manifest,),
            write_deltas=(delta,),
        )


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

    with pytest.raises(ValueError, match="schema_version"):
        ProtocolPhaseReceipt.from_dict({"schema_version": 1})


def test_protocol_phase_receipt_to_dict_from_dict_round_trips() -> None:
    receipt = _protocol_phase_receipt()
    assert ProtocolPhaseReceipt.from_dict(receipt.to_dict()) == receipt


def test_protocol_phase_receipt_from_dict_round_trips_unbound_intermediate_witness() -> None:
    provisional = _candidate_protocol_outcome(
        witness_schema_version=None,
        witness_sha256=None,
        witness_passed=None,
    )
    receipt = _protocol_phase_receipt(
        status="incomplete",
        evaluator=None,
        runtime_binding=None,
        outcomes=(provisional,),
    )

    assert ProtocolPhaseReceipt.from_dict(receipt.to_dict()) == receipt


def test_protocol_phase_receipt_from_dict_rejects_partial_witness_binding() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    outcomes[0]["witness_sha256"] = None

    with pytest.raises(ValueError, match="all present or all absent"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_missing_witness_field() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    del outcomes[0]["witness_sha256"]

    with pytest.raises(ValueError, match="missing required fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_failed_survivor_witness() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    pyright = next(item for item in outcomes if item["candidate"] == "pyright")
    pyright["witness_passed"] = False

    with pytest.raises(ValueError, match="witness_passed"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_inconclusive_candidate_as_pass() -> None:
    ty = _candidate_protocol_outcome(
        "ty",
        gate_disposition="configuration_inconclusive",
        issues=("configuration attribution is inconclusive",),
        witness_passed=False,
    )
    receipt = _protocol_phase_receipt(
        status="hold",
        outcomes=_canonical_protocol_outcomes(ty=ty),
        issues=("candidate configuration evidence is inconclusive",),
        next_action=PROTOCOL_PHASE_NEXT_ACTION_INCONCLUSIVE,
    )
    payload = receipt.to_dict()
    payload["status"] = "pass"
    payload["issues"] = []
    payload["next_action"] = PROTOCOL_PHASE_NEXT_ACTION_STOP

    with pytest.raises(ValueError, match="configuration_inconclusive"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_canonical_bytes_round_trip_without_drift() -> None:
    receipt = _protocol_phase_receipt()
    canonical = canonical_json(receipt.to_dict())
    decoded = cast("dict[str, object]", json.loads(canonical))
    reparsed = ProtocolPhaseReceipt.from_dict(decoded)
    assert canonical_json(reparsed.to_dict()) == canonical


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_protocol_phase_receipt_from_dict_rejects_non_finite_lifecycle_number(value: float) -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    lifecycle = cast("dict[str, object]", outcomes[1]["lifecycle"])
    lifecycle["cold_readiness_seconds"] = value
    with pytest.raises(ValueError, match="finite"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_unknown_field() -> None:
    receipt = _protocol_phase_receipt()
    payload = receipt.to_dict()
    payload["extra"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_budgets_frozen_protocol_matches_default() -> None:
    protocol_budget = next(budget for budget in default_phase_budgets() if budget.name == "protocol")
    assert protocol_budget == PhaseBudget("protocol", 90 * 60)


# --- M11: nested unknown-field rejection, exercised through the top-level receipt --------


def test_protocol_phase_receipt_from_dict_rejects_unknown_field_in_nested_capability() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    capabilities = cast("list[dict[str, Any]]", outcomes[0]["capabilities"])
    capabilities[0]["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_unknown_field_in_nested_lifecycle() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    lifecycle = cast("dict[str, Any]", outcomes[0]["lifecycle"])
    lifecycle["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_unknown_field_in_nested_raw_providers() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    raw_providers = cast("dict[str, Any]", outcomes[0]["raw_providers"])
    raw_providers["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)


def test_protocol_phase_receipt_from_dict_rejects_unknown_field_in_nested_outcome() -> None:
    payload = _protocol_phase_receipt().to_dict()
    outcomes = cast("list[dict[str, Any]]", payload["outcomes"])
    outcomes[0]["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        ProtocolPhaseReceipt.from_dict(payload)
