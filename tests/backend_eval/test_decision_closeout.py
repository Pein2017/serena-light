"""Evidence-only final decision closeout after the protocol stop gate."""

from __future__ import annotations

import json
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.backend_eval.models import (
    AdmissionReceipt,
    AdmissionRootWitness,
    ProtocolPhaseReceipt,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.publish import PUBLICATION_FAILED, PublicationError
from scripts.backend_eval_closeout import (
    DECISION_RECEIPT_SCHEMA_VERSION,
    AttemptRecord,
    CloseoutInput,
    DecisionReceipt,
    ProtocolDecisionBinding,
    ReviewBinding,
    build_decision_receipt,
    publish_decision_receipt,
)
from tests.backend_eval.test_capability_receipts import (
    _admission_binding,
    _canonical_protocol_outcomes,
    _evaluator_identity,
    _failed_candidate_protocol_outcome,
    _protocol_phase_receipt,
)
from tests.backend_eval.test_models import _admission_receipt

_PROTOCOL_EVALUATION = "1" * 64
_PROTOCOL_RUN = "2" * 64
_PARENT_EVALUATION = "3" * 64
_PARENT_RUN = "4" * 64
_PARENT_ARTIFACT = "5" * 64
_PROTOCOL_ARTIFACT = "6" * 64
_SOURCE_COMMIT = "f" * 40


def _published_protocol_pair(
    tmp_path: Path,
) -> tuple[Path, Path, AdmissionReceipt, ProtocolPhaseReceipt]:
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    parent = _admission_receipt(
        evaluation_identity=_PARENT_EVALUATION,
        run_identity=_PARENT_RUN,
        artifact_tree_digest=_PARENT_ARTIFACT,
    )
    parent_path = (
        artifact_root
        / _PARENT_EVALUATION
        / "receipts"
        / f"{_PARENT_RUN}.json"
    )
    parent_path.parent.mkdir(parents=True)
    parent_bytes = canonical_json(parent.to_dict())
    parent_path.write_bytes(parent_bytes)
    assert parent.runtime_binding is not None
    assert parent.evaluator is not None

    parent_witnesses = tuple(
        AdmissionRootWitness(
            root=manifest.root,
            kind=manifest.kind,
            source_revision=manifest.source_revision,
            manifest_digest=manifest.manifest_digest,
        )
        for manifest in parent.root_manifests_before
    )
    binding = _admission_binding(
        admission_evaluation_identity=_PARENT_EVALUATION,
        admission_run_identity=_PARENT_RUN,
        receipt_path=str(parent_path),
        receipt_sha256=sha256_bytes(parent_bytes),
        artifact_tree_digest=_PARENT_ARTIFACT,
        candidate_lock_digest=parent.candidate_lock.digest,
        runtime_root=parent.runtime_binding.root,
        runtime_manifest_sha256=parent.runtime_binding.manifest_sha256,
        production_root=parent.evaluator.production_root.removesuffix("/src"),
        production_source_revision=parent.evaluator.source_commit,
        production_dependency_lock_digest=parent.production_identity_after.dependency_lock_digest,
        production_build_identity=parent.production_identity_after.build_identity,
        parent_root_manifests=parent_witnesses,
    )
    protocol = _protocol_phase_receipt(
        evaluation_identity=_PROTOCOL_EVALUATION,
        run_identity=_PROTOCOL_RUN,
        admission_binding=binding,
        evaluator=replace(_evaluator_identity(), source_commit=_SOURCE_COMMIT),
        production_identity_before=parent.production_identity_after,
        production_identity_after=parent.production_identity_after,
        candidate_lock=parent.candidate_lock,
        runtime_binding=parent.runtime_binding,
        root_manifests_before=parent.root_manifests_before,
        root_manifests_after=parent.root_manifests_after,
        write_deltas=parent.write_deltas,
        outcomes=_canonical_protocol_outcomes(
            ty=_failed_candidate_protocol_outcome(
                "ty", gate_disposition="seam_incompatible_pull_only"
            )
        ),
        artifact_tree_digest=_PROTOCOL_ARTIFACT,
    )
    protocol_path = (
        artifact_root
        / _PROTOCOL_EVALUATION
        / "protocol-receipts"
        / f"{_PROTOCOL_RUN}.protocol.json"
    )
    protocol_path.parent.mkdir(parents=True)
    protocol_bytes = canonical_json(protocol.to_dict())
    protocol_path.write_bytes(protocol_bytes)
    return artifact_root, protocol_path, parent, protocol


def _closeout_input(tmp_path: Path, **overrides: object) -> tuple[Path, CloseoutInput]:
    artifact_root, protocol_path, parent, protocol = _published_protocol_pair(tmp_path)
    protocol_bytes = protocol_path.read_bytes()
    assert protocol.evaluator is not None
    assert protocol.evaluator.source_commit is not None
    assert protocol.admission_binding is not None
    binding = ProtocolDecisionBinding(
        protocol_evaluation_identity=protocol.evaluation_identity,
        protocol_run_identity=protocol.run_identity,
        protocol_receipt_path=str(protocol_path),
        protocol_receipt_sha256=sha256_bytes(protocol_bytes),
        protocol_artifact_tree_digest=protocol.artifact_tree_digest,
        evaluator_source_commit=protocol.evaluator.source_commit,
        parent_evaluation_identity=parent.evaluation_identity,
        parent_run_identity=parent.run_identity,
        parent_receipt_path=protocol.admission_binding.receipt_path,
        parent_receipt_sha256=protocol.admission_binding.receipt_sha256,
        parent_artifact_tree_digest=parent.artifact_tree_digest,
    )
    fields: dict[str, object] = {
        "schema_version": 1,
        "decision": "retain_pyright",
        "attempt_ledger_complete": True,
        "attempts": (
            AttemptRecord(
                attempt_id="admission-final",
                kind="admission",
                disposition="pass",
                evidence_path=binding.parent_receipt_path,
                evidence_sha256=binding.parent_receipt_sha256,
                measured_seconds=14,
                bounded_upper_seconds=None,
                repair_rerun=False,
            ),
            AttemptRecord(
                attempt_id="protocol-final",
                kind="protocol",
                disposition="pass",
                evidence_path=binding.protocol_receipt_path,
                evidence_sha256=binding.protocol_receipt_sha256,
                measured_seconds=130,
                bounded_upper_seconds=None,
                repair_rerun=True,
            ),
        ),
        "protocol": binding,
        "reviews": (
            ReviewBinding(
                reviewer="sol-max",
                task_id="/root/phase2_final_runtime_solmax",
                disposition="pass",
                protocol_receipt_sha256=binding.protocol_receipt_sha256,
            ),
            ReviewBinding(
                reviewer="sol-xhigh",
                task_id="/root/phase2_task8_orchestrator_sol",
                disposition="pass",
                protocol_receipt_sha256=binding.protocol_receipt_sha256,
            ),
        ),
        "residual_risks": (
            "The current product seam still requires push diagnostics.",
            "The result is scoped to the locked backend versions and frozen corpus.",
        ),
    }
    fields.update(overrides)
    return artifact_root, CloseoutInput(**fields)


def test_valid_retain_decision_is_derived_from_exact_protocol_evidence(
    tmp_path: Path,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)

    receipt = build_decision_receipt(artifact_root, closeout)

    assert receipt.schema_version == DECISION_RECEIPT_SCHEMA_VERSION
    assert receipt.decision == "retain_pyright"
    assert receipt.total_active_upper_seconds == 144
    assert receipt.repair_rerun_upper_seconds == 130
    assert receipt.candidate_dispositions == (
        ("pyrefly", "excluded_protocol_failure"),
        ("pyright", "retained_current_backend"),
        ("ty", "excluded_seam_incompatible_pull_only"),
    )
    assert receipt.phase_dispositions == (
        ("product_seam", "not_required_by_stop_gate"),
        ("feature", "not_required_by_stop_gate"),
        ("agent", "not_required_by_stop_gate"),
    )
    assert receipt.later_ranks == "not_evaluated"
    assert receipt.efficiency == "not_used"
    assert receipt.next_action == "request_explicit_user_acceptance"
    assert DecisionReceipt.from_dict(receipt.to_dict()) == receipt
    assert canonical_json(receipt.to_dict()).endswith(b"\n")


def test_closeout_input_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="unknown"):
        CloseoutInput.from_dict({"schema_version": 1, "unexpected": True})


def test_closeout_input_rejects_boolean_schema_version(tmp_path: Path) -> None:
    _, closeout = _closeout_input(tmp_path)
    payload = closeout.to_dict()
    payload["schema_version"] = True

    with pytest.raises(ValueError, match="schema_version"):
        CloseoutInput.from_dict(payload)


@pytest.mark.parametrize(
    "decision",
    ["promote_pyright", "retain_ty", "pass", ""],
)
def test_decision_enum_is_closed(tmp_path: Path, decision: str) -> None:
    with pytest.raises(ValueError, match="decision"):
        _closeout_input(tmp_path, decision=decision)


@pytest.mark.parametrize(
    ("measured_seconds", "bounded_upper_seconds"),
    [
        (None, None),
        (1, 2),
    ],
)
def test_attempt_requires_exactly_one_time_bound(
    measured_seconds: int | None,
    bounded_upper_seconds: int | None,
) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        AttemptRecord(
            attempt_id="bad",
            kind="protocol",
            disposition="pass",
            evidence_path=None,
            evidence_sha256=None,
            measured_seconds=measured_seconds,
            bounded_upper_seconds=bounded_upper_seconds,
            repair_rerun=False,
        )


def test_attempt_evidence_path_and_sha_are_paired() -> None:
    with pytest.raises(ValueError, match="evidence"):
        AttemptRecord(
            attempt_id="bad",
            kind="protocol",
            disposition="pass",
            evidence_path="/evidence/receipt.json",
            evidence_sha256=None,
            measured_seconds=1,
            bounded_upper_seconds=None,
            repair_rerun=False,
        )


def test_attempt_ids_are_unique(tmp_path: Path) -> None:
    _, closeout = _closeout_input(tmp_path)

    with pytest.raises(ValueError, match="attempt_id"):
        replace(closeout, attempts=(closeout.attempts[0], closeout.attempts[0]))


def test_incomplete_ledger_can_only_retain_inconclusively(tmp_path: Path) -> None:
    artifact_root, closeout = _closeout_input(tmp_path, attempt_ledger_complete=False)
    with pytest.raises(ValueError, match="inconclusive_retain_pyright"):
        build_decision_receipt(artifact_root, closeout)

    receipt = build_decision_receipt(
        artifact_root,
        replace(closeout, decision="inconclusive_retain_pyright"),
    )
    assert receipt.decision == "inconclusive_retain_pyright"


@pytest.mark.parametrize(("omitted_index", "match"), [(0, "parent"), (1, "protocol")])
def test_complete_ledger_must_include_final_parent_and_protocol_attempts(
    tmp_path: Path,
    omitted_index: int,
    match: str,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    attempts = tuple(
        attempt
        for index, attempt in enumerate(closeout.attempts)
        if index != omitted_index
    )

    with pytest.raises(ValueError, match=match):
        build_decision_receipt(artifact_root, replace(closeout, attempts=attempts))


@pytest.mark.parametrize(
    ("extra_attempts", "match"),
    [
        (
            (
                AttemptRecord(
                    attempt_id="too-long",
                    kind="protocol",
                    disposition="pass",
                    evidence_path=None,
                    evidence_sha256=None,
                    measured_seconds=None,
                    bounded_upper_seconds=57_601,
                    repair_rerun=False,
                ),
            ),
            "57600",
        ),
        (
            (
                AttemptRecord(
                    attempt_id="repair-too-long",
                    kind="repair",
                    disposition="pass",
                    evidence_path=None,
                    evidence_sha256=None,
                    measured_seconds=None,
                    bounded_upper_seconds=3_601,
                    repair_rerun=True,
                ),
            ),
            "3600",
        ),
    ],
)
def test_decisive_outcome_rejects_ceiling_overrun(
    tmp_path: Path,
    extra_attempts: tuple[AttemptRecord, ...],
    match: str,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    attempts = tuple(
        sorted((*closeout.attempts, *extra_attempts), key=lambda attempt: attempt.attempt_id)
    )
    with pytest.raises(ValueError, match=match):
        build_decision_receipt(artifact_root, replace(closeout, attempts=attempts))


@pytest.mark.parametrize(
    "field",
    [
        "protocol_receipt_sha256",
        "protocol_artifact_tree_digest",
        "evaluator_source_commit",
        "parent_receipt_sha256",
        "parent_artifact_tree_digest",
    ],
)
def test_exact_protocol_and_parent_digests_and_evaluator_commit_are_required(
    tmp_path: Path,
    field: str,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    wrong = "0" * (40 if field == "evaluator_source_commit" else 64)
    protocol = replace(closeout.protocol, **{field: wrong})
    reviews = closeout.reviews
    if field == "protocol_receipt_sha256":
        reviews = tuple(
            replace(review, protocol_receipt_sha256=wrong) for review in reviews
        )
    closeout = replace(closeout, protocol=protocol, reviews=reviews)

    with pytest.raises(ValueError, match=field):
        build_decision_receipt(artifact_root, closeout)


def test_attempt_evidence_digest_is_verified(tmp_path: Path) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    attempts = (
        replace(closeout.attempts[0], evidence_sha256="0" * 64),
        closeout.attempts[1],
    )

    with pytest.raises(ValueError, match="evidence SHA-256"):
        build_decision_receipt(artifact_root, replace(closeout, attempts=attempts))


def test_review_binding_must_match_exact_protocol_receipt(tmp_path: Path) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    reviews = (
        replace(closeout.reviews[0], protocol_receipt_sha256="0" * 64),
        closeout.reviews[1],
    )
    with pytest.raises(ValueError, match="review.*protocol"):
        build_decision_receipt(artifact_root, replace(closeout, reviews=reviews))


def test_decision_receipt_from_dict_rejects_fixed_disposition_mutation(
    tmp_path: Path,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    payload = build_decision_receipt(artifact_root, closeout).to_dict()
    payload["efficiency"] = "measured"

    with pytest.raises(ValueError, match="efficiency"):
        DecisionReceipt.from_dict(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", True),
        ("total_active_upper_seconds", 144.0),
        ("repair_rerun_upper_seconds", 130.0),
    ],
)
def test_decision_receipt_rejects_non_integer_numeric_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    payload = build_decision_receipt(artifact_root, closeout).to_dict()
    payload[field] = value
    identity_payload = dict(payload)
    identity_payload.pop("decision_identity")
    payload["decision_identity"] = sha256_bytes(canonical_json(identity_payload))

    with pytest.raises(ValueError, match=field):
        DecisionReceipt.from_dict(payload)


def test_decision_publication_is_immutable_and_service_owned(tmp_path: Path) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    receipt = build_decision_receipt(artifact_root, closeout)

    published = publish_decision_receipt(artifact_root, receipt)

    assert published.read_bytes() == canonical_json(receipt.to_dict())
    assert published.parent.name == "decision-receipts"
    assert stat.S_IMODE(published.stat().st_mode) == 0o600
    assert stat.S_IMODE(published.parent.stat().st_mode) == 0o700
    with pytest.raises(PublicationError) as error:
        publish_decision_receipt(artifact_root, receipt)
    assert error.value.failure.code == PUBLICATION_FAILED
    assert published.read_bytes() == canonical_json(receipt.to_dict())


def test_decision_receipt_identity_changes_with_attempt_ledger(tmp_path: Path) -> None:
    artifact_root, closeout = _closeout_input(tmp_path)
    first = build_decision_receipt(artifact_root, closeout)
    changed_attempts = (
        replace(closeout.attempts[0], measured_seconds=15),
        closeout.attempts[1],
    )
    second = build_decision_receipt(
        artifact_root,
        replace(closeout, attempts=changed_attempts),
    )
    assert first.decision_identity != second.decision_identity


def test_closeout_input_canonical_round_trip(tmp_path: Path) -> None:
    _, closeout = _closeout_input(tmp_path)
    payload = json.loads(canonical_json(closeout.to_dict()))
    assert CloseoutInput.from_dict(payload) == closeout
