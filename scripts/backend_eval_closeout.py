"""Publish one evidence-only backend decision after a reviewed protocol stop gate.

This module is intentionally outside :mod:`scripts.backend_eval`: adding closeout-only
code must not change the sealed evaluator source closure or invalidate the protocol receipt
it consumes.  It starts no backend, admission run, cleanup, or migration.  Its only mutation
is one immutable decision record published with the evaluator's existing publication
primitive.

The caller supplies a complete attempt ledger.  A decisive retain/promote outcome is refused
unless that ledger is explicitly complete and its derived upper bounds remain within the
frozen total and repair/rerun ceilings.  Missing attempts can therefore produce only
``inconclusive_retain_pyright``; they can never be papered over with published receipt windows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.models import (
    PROTOCOL_PHASE_NEXT_ACTION_STOP,
    AdmissionReceipt,
    ProtocolPhaseReceipt,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.process import Deadline, monotonic_clock
from scripts.backend_eval.publish import PublicationRequest, publish_immutable_record

DECISION_RECEIPT_SCHEMA_VERSION = 1
CLOSEOUT_INPUT_SCHEMA_VERSION = 1
TOTAL_ACTIVE_CEILING_SECONDS = 57_600
REPAIR_RERUN_CEILING_SECONDS = 3_600
DECISION_PUBLICATION_SECONDS = 120

_DECISIONS = frozenset(
    {
        "promote_pyrefly",
        "promote_ty",
        "retain_pyright",
        "inconclusive_retain_pyright",
    }
)
_ATTEMPT_KINDS = frozenset({"admission", "protocol", "repair", "verification"})
_ATTEMPT_DISPOSITIONS = frozenset(
    {
        "pass",
        "fail",
        "incomplete",
        "infrastructure_invalid",
        "classification_invalid",
        "superseded",
    }
)
_REVIEWERS = frozenset({"sol-max", "sol-xhigh"})
_REVIEW_DISPOSITIONS = frozenset({"pass", "hold"})
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PHASE_DISPOSITIONS = (
    ("product_seam", "not_required_by_stop_gate"),
    ("feature", "not_required_by_stop_gate"),
    ("agent", "not_required_by_stop_gate"),
)
_CURRENT_CANDIDATE_DISPOSITIONS = (
    ("pyrefly", "excluded_protocol_failure"),
    ("pyright", "retained_current_backend"),
    ("ty", "excluded_seam_incompatible_pull_only"),
)
_NEXT_ACTION = "request_explicit_user_acceptance"


def _require_keys(value: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    actual = frozenset(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ValueError(f"{label} has unknown fields: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{label} is missing fields: {', '.join(missing)}")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be an object with string keys")
    return cast("Mapping[str, object]", value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    text = _string(value, label)
    if _SHA256_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256 digest")
    return text


def _git_revision(value: object, label: str) -> str:
    text = _string(value, label)
    if _GIT_REVISION_RE.fullmatch(text) is None:
        raise ValueError(f"{label} must be a canonical Git revision")
    return text


def _absolute_path(value: object, label: str) -> str:
    text = _string(value, label)
    path = Path(text)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute path without parent references")
    return text


def _positive_seconds(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer number of seconds")
    return value


def _nonnegative_seconds(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer number of seconds")
    return value


def _tuple_of_mappings(value: object, label: str) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array")
    return tuple(_mapping(item, f"{label}[]") for item in value)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One complete active-evaluation attempt or a conservative bound for one."""

    attempt_id: str
    kind: str
    disposition: str
    evidence_path: str | None
    evidence_sha256: str | None
    measured_seconds: int | None
    bounded_upper_seconds: int | None
    repair_rerun: bool

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, str) or _IDENTIFIER_RE.fullmatch(self.attempt_id) is None:
            raise ValueError("AttemptRecord.attempt_id must be a canonical lowercase identifier")
        if self.kind not in _ATTEMPT_KINDS:
            raise ValueError(f"AttemptRecord.kind must be one of {sorted(_ATTEMPT_KINDS)}")
        if self.disposition not in _ATTEMPT_DISPOSITIONS:
            raise ValueError(
                f"AttemptRecord.disposition must be one of {sorted(_ATTEMPT_DISPOSITIONS)}"
            )
        if (self.evidence_path is None) != (self.evidence_sha256 is None):
            raise ValueError("AttemptRecord evidence_path and evidence_sha256 must be paired")
        if self.evidence_path is not None:
            _absolute_path(self.evidence_path, "AttemptRecord.evidence_path")
            _sha256(self.evidence_sha256, "AttemptRecord.evidence_sha256")
        if (self.measured_seconds is None) == (self.bounded_upper_seconds is None):
            raise ValueError(
                "AttemptRecord requires exactly one of measured_seconds or bounded_upper_seconds"
            )
        if self.measured_seconds is not None:
            _positive_seconds(self.measured_seconds, "AttemptRecord.measured_seconds")
        if self.bounded_upper_seconds is not None:
            _positive_seconds(
                self.bounded_upper_seconds, "AttemptRecord.bounded_upper_seconds"
            )
        if not isinstance(self.repair_rerun, bool):
            raise ValueError("AttemptRecord.repair_rerun must be boolean")

    @property
    def upper_seconds(self) -> int:
        value = (
            self.measured_seconds
            if self.measured_seconds is not None
            else self.bounded_upper_seconds
        )
        assert value is not None
        return value

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_id": self.attempt_id,
            "bounded_upper_seconds": self.bounded_upper_seconds,
            "disposition": self.disposition,
            "evidence_path": self.evidence_path,
            "evidence_sha256": self.evidence_sha256,
            "kind": self.kind,
            "measured_seconds": self.measured_seconds,
            "repair_rerun": self.repair_rerun,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AttemptRecord:
        expected = frozenset(
            {
                "attempt_id",
                "kind",
                "disposition",
                "evidence_path",
                "evidence_sha256",
                "measured_seconds",
                "bounded_upper_seconds",
                "repair_rerun",
            }
        )
        _require_keys(value, expected, "AttemptRecord")
        return cls(
            attempt_id=cast("str", value["attempt_id"]),
            kind=cast("str", value["kind"]),
            disposition=cast("str", value["disposition"]),
            evidence_path=cast("str | None", value["evidence_path"]),
            evidence_sha256=cast("str | None", value["evidence_sha256"]),
            measured_seconds=cast("int | None", value["measured_seconds"]),
            bounded_upper_seconds=cast("int | None", value["bounded_upper_seconds"]),
            repair_rerun=cast("bool", value["repair_rerun"]),
        )


@dataclass(frozen=True, slots=True)
class ProtocolDecisionBinding:
    """The exact immutable Phase 2 evidence and exact Phase 1 parent it consumed."""

    protocol_evaluation_identity: str
    protocol_run_identity: str
    protocol_receipt_path: str
    protocol_receipt_sha256: str
    protocol_artifact_tree_digest: str
    evaluator_source_commit: str
    parent_evaluation_identity: str
    parent_run_identity: str
    parent_receipt_path: str
    parent_receipt_sha256: str
    parent_artifact_tree_digest: str

    def __post_init__(self) -> None:
        for field in (
            "protocol_evaluation_identity",
            "protocol_run_identity",
            "protocol_receipt_sha256",
            "protocol_artifact_tree_digest",
            "parent_evaluation_identity",
            "parent_run_identity",
            "parent_receipt_sha256",
            "parent_artifact_tree_digest",
        ):
            _sha256(getattr(self, field), f"ProtocolDecisionBinding.{field}")
        _git_revision(
            self.evaluator_source_commit,
            "ProtocolDecisionBinding.evaluator_source_commit",
        )
        _absolute_path(
            self.protocol_receipt_path,
            "ProtocolDecisionBinding.protocol_receipt_path",
        )
        _absolute_path(
            self.parent_receipt_path,
            "ProtocolDecisionBinding.parent_receipt_path",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "evaluator_source_commit": self.evaluator_source_commit,
            "parent_artifact_tree_digest": self.parent_artifact_tree_digest,
            "parent_evaluation_identity": self.parent_evaluation_identity,
            "parent_receipt_path": self.parent_receipt_path,
            "parent_receipt_sha256": self.parent_receipt_sha256,
            "parent_run_identity": self.parent_run_identity,
            "protocol_artifact_tree_digest": self.protocol_artifact_tree_digest,
            "protocol_evaluation_identity": self.protocol_evaluation_identity,
            "protocol_receipt_path": self.protocol_receipt_path,
            "protocol_receipt_sha256": self.protocol_receipt_sha256,
            "protocol_run_identity": self.protocol_run_identity,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ProtocolDecisionBinding:
        expected = frozenset(
            {
                "evaluator_source_commit",
                "parent_artifact_tree_digest",
                "parent_evaluation_identity",
                "parent_receipt_path",
                "parent_receipt_sha256",
                "parent_run_identity",
                "protocol_artifact_tree_digest",
                "protocol_evaluation_identity",
                "protocol_receipt_path",
                "protocol_receipt_sha256",
                "protocol_run_identity",
            }
        )
        _require_keys(value, expected, "ProtocolDecisionBinding")
        return cls(**cast("dict[str, Any]", dict(value)))


@dataclass(frozen=True, slots=True)
class ReviewBinding:
    """One independent review bound to the exact protocol receipt bytes."""

    reviewer: str
    task_id: str
    disposition: str
    protocol_receipt_sha256: str

    def __post_init__(self) -> None:
        if self.reviewer not in _REVIEWERS:
            raise ValueError(f"ReviewBinding.reviewer must be one of {sorted(_REVIEWERS)}")
        _string(self.task_id, "ReviewBinding.task_id")
        if not self.task_id.startswith("/root/"):
            raise ValueError("ReviewBinding.task_id must be a canonical /root task id")
        if self.disposition not in _REVIEW_DISPOSITIONS:
            raise ValueError(
                f"ReviewBinding.disposition must be one of {sorted(_REVIEW_DISPOSITIONS)}"
            )
        _sha256(
            self.protocol_receipt_sha256,
            "ReviewBinding.protocol_receipt_sha256",
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "disposition": self.disposition,
            "protocol_receipt_sha256": self.protocol_receipt_sha256,
            "reviewer": self.reviewer,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReviewBinding:
        expected = frozenset(
            {"reviewer", "task_id", "disposition", "protocol_receipt_sha256"}
        )
        _require_keys(value, expected, "ReviewBinding")
        return cls(**cast("dict[str, Any]", dict(value)))


def _validate_attempts(attempts: tuple[AttemptRecord, ...]) -> None:
    if not isinstance(attempts, tuple) or not attempts:
        raise ValueError("attempts must be a non-empty tuple")
    ids = tuple(attempt.attempt_id for attempt in attempts)
    if len(ids) != len(set(ids)):
        raise ValueError("attempts must have unique attempt_id values")
    if ids != tuple(sorted(ids)):
        raise ValueError("attempts must be sorted by attempt_id")


def _validate_reviews(reviews: tuple[ReviewBinding, ...], protocol_sha256: str) -> None:
    if not isinstance(reviews, tuple):
        raise ValueError("reviews must be a tuple")
    reviewers = tuple(review.reviewer for review in reviews)
    if reviewers != tuple(sorted(_REVIEWERS)):
        raise ValueError("reviews must contain exactly sorted sol-max and sol-xhigh bindings")
    if any(review.disposition != "pass" for review in reviews):
        raise ValueError("reviews must both have disposition='pass'")
    if any(review.protocol_receipt_sha256 != protocol_sha256 for review in reviews):
        raise ValueError("every review must bind the exact protocol receipt SHA-256")


def _validate_residual_risks(risks: tuple[str, ...]) -> None:
    if not isinstance(risks, tuple) or not risks:
        raise ValueError("residual_risks must be a non-empty tuple")
    if any(not isinstance(risk, str) or not risk for risk in risks):
        raise ValueError("residual_risks entries must be non-empty strings")
    if len(risks) != len(set(risks)):
        raise ValueError("residual_risks must not contain duplicates")
    if risks != tuple(sorted(risks)):
        raise ValueError("residual_risks must be sorted")


@dataclass(frozen=True, slots=True)
class CloseoutInput:
    """Caller-authored ledger and review inputs; all evidence identities are explicit."""

    schema_version: int
    decision: str
    attempt_ledger_complete: bool
    attempts: tuple[AttemptRecord, ...]
    protocol: ProtocolDecisionBinding
    reviews: tuple[ReviewBinding, ...]
    residual_risks: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != CLOSEOUT_INPUT_SCHEMA_VERSION
        ):
            raise ValueError(
                f"CloseoutInput.schema_version must be {CLOSEOUT_INPUT_SCHEMA_VERSION}"
            )
        if self.decision not in _DECISIONS:
            raise ValueError(f"CloseoutInput.decision must be one of {sorted(_DECISIONS)}")
        if not isinstance(self.attempt_ledger_complete, bool):
            raise ValueError("CloseoutInput.attempt_ledger_complete must be boolean")
        _validate_attempts(self.attempts)
        _validate_reviews(self.reviews, self.protocol.protocol_receipt_sha256)
        _validate_residual_risks(self.residual_risks)

    def to_dict(self) -> dict[str, object]:
        return {
            "attempt_ledger_complete": self.attempt_ledger_complete,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "decision": self.decision,
            "protocol": self.protocol.to_dict(),
            "residual_risks": list(self.residual_risks),
            "reviews": [review.to_dict() for review in self.reviews],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> CloseoutInput:
        expected = frozenset(
            {
                "schema_version",
                "decision",
                "attempt_ledger_complete",
                "attempts",
                "protocol",
                "reviews",
                "residual_risks",
            }
        )
        _require_keys(value, expected, "CloseoutInput")
        risks = value["residual_risks"]
        if not isinstance(risks, list):
            raise ValueError("CloseoutInput.residual_risks must be an array")
        return cls(
            schema_version=cast("int", value["schema_version"]),
            decision=cast("str", value["decision"]),
            attempt_ledger_complete=cast("bool", value["attempt_ledger_complete"]),
            attempts=tuple(
                AttemptRecord.from_dict(item)
                for item in _tuple_of_mappings(value["attempts"], "CloseoutInput.attempts")
            ),
            protocol=ProtocolDecisionBinding.from_dict(
                _mapping(value["protocol"], "CloseoutInput.protocol")
            ),
            reviews=tuple(
                ReviewBinding.from_dict(item)
                for item in _tuple_of_mappings(value["reviews"], "CloseoutInput.reviews")
            ),
            residual_risks=tuple(cast("list[str]", risks)),
        )


def _decision_payload(
    *,
    decision: str,
    attempt_ledger_complete: bool,
    attempts: tuple[AttemptRecord, ...],
    protocol: ProtocolDecisionBinding,
    total_active_upper_seconds: int,
    repair_rerun_upper_seconds: int,
    candidate_dispositions: tuple[tuple[str, str], ...],
    phase_dispositions: tuple[tuple[str, str], ...],
    later_ranks: str,
    efficiency: str,
    reviews: tuple[ReviewBinding, ...],
    residual_risks: tuple[str, ...],
    next_action: str,
) -> dict[str, object]:
    return {
        "attempt_ledger_complete": attempt_ledger_complete,
        "attempts": [attempt.to_dict() for attempt in attempts],
        "candidate_dispositions": [list(item) for item in candidate_dispositions],
        "decision": decision,
        "efficiency": efficiency,
        "later_ranks": later_ranks,
        "next_action": next_action,
        "phase_dispositions": [list(item) for item in phase_dispositions],
        "protocol": protocol.to_dict(),
        "repair_rerun_upper_seconds": repair_rerun_upper_seconds,
        "residual_risks": list(residual_risks),
        "reviews": [review.to_dict() for review in reviews],
        "schema_version": DECISION_RECEIPT_SCHEMA_VERSION,
        "total_active_upper_seconds": total_active_upper_seconds,
    }


@dataclass(frozen=True, slots=True)
class DecisionReceipt:
    """Canonical closed decision evidence bound to one immutable protocol receipt."""

    schema_version: int
    decision_identity: str
    decision: str
    attempt_ledger_complete: bool
    attempts: tuple[AttemptRecord, ...]
    protocol: ProtocolDecisionBinding
    total_active_upper_seconds: int
    repair_rerun_upper_seconds: int
    candidate_dispositions: tuple[tuple[str, str], ...]
    phase_dispositions: tuple[tuple[str, str], ...]
    later_ranks: str
    efficiency: str
    reviews: tuple[ReviewBinding, ...]
    residual_risks: tuple[str, ...]
    next_action: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != DECISION_RECEIPT_SCHEMA_VERSION
        ):
            raise ValueError(
                f"DecisionReceipt.schema_version must be {DECISION_RECEIPT_SCHEMA_VERSION}"
            )
        if self.decision not in _DECISIONS:
            raise ValueError(f"DecisionReceipt.decision must be one of {sorted(_DECISIONS)}")
        _sha256(self.decision_identity, "DecisionReceipt.decision_identity")
        if not isinstance(self.attempt_ledger_complete, bool):
            raise ValueError("DecisionReceipt.attempt_ledger_complete must be boolean")
        _validate_attempts(self.attempts)
        derived_total = sum(attempt.upper_seconds for attempt in self.attempts)
        derived_repair = sum(
            attempt.upper_seconds for attempt in self.attempts if attempt.repair_rerun
        )
        _nonnegative_seconds(
            self.total_active_upper_seconds,
            "DecisionReceipt.total_active_upper_seconds",
        )
        _nonnegative_seconds(
            self.repair_rerun_upper_seconds,
            "DecisionReceipt.repair_rerun_upper_seconds",
        )
        if self.total_active_upper_seconds != derived_total:
            raise ValueError("DecisionReceipt.total_active_upper_seconds is not derived")
        if self.repair_rerun_upper_seconds != derived_repair:
            raise ValueError("DecisionReceipt.repair_rerun_upper_seconds is not derived")
        decisive = self.decision != "inconclusive_retain_pyright"
        if decisive and not self.attempt_ledger_complete:
            raise ValueError(
                "an incomplete attempt ledger can only choose inconclusive_retain_pyright"
            )
        if decisive and derived_total > TOTAL_ACTIVE_CEILING_SECONDS:
            raise ValueError("decisive outcome exceeds the 57600-second total ceiling")
        if decisive and derived_repair > REPAIR_RERUN_CEILING_SECONDS:
            raise ValueError("decisive outcome exceeds the 3600-second repair/rerun ceiling")
        if self.candidate_dispositions != _CURRENT_CANDIDATE_DISPOSITIONS:
            raise ValueError("DecisionReceipt.candidate_dispositions do not match the stop gate")
        if self.phase_dispositions != _PHASE_DISPOSITIONS:
            raise ValueError("DecisionReceipt.phase_dispositions must be not_required_by_stop_gate")
        if self.later_ranks != "not_evaluated":
            raise ValueError("DecisionReceipt.later_ranks must be 'not_evaluated'")
        if self.efficiency != "not_used":
            raise ValueError("DecisionReceipt.efficiency must be 'not_used'")
        _validate_reviews(self.reviews, self.protocol.protocol_receipt_sha256)
        _validate_residual_risks(self.residual_risks)
        if self.next_action != _NEXT_ACTION:
            raise ValueError(
                "DecisionReceipt.next_action must be 'request_explicit_user_acceptance'"
            )
        expected_identity = sha256_bytes(canonical_json(self._identity_payload()))
        if self.decision_identity != expected_identity:
            raise ValueError("DecisionReceipt.decision_identity does not match canonical evidence")

    def _identity_payload(self) -> dict[str, object]:
        return _decision_payload(
            decision=self.decision,
            attempt_ledger_complete=self.attempt_ledger_complete,
            attempts=self.attempts,
            protocol=self.protocol,
            total_active_upper_seconds=self.total_active_upper_seconds,
            repair_rerun_upper_seconds=self.repair_rerun_upper_seconds,
            candidate_dispositions=self.candidate_dispositions,
            phase_dispositions=self.phase_dispositions,
            later_ranks=self.later_ranks,
            efficiency=self.efficiency,
            reviews=self.reviews,
            residual_risks=self.residual_risks,
            next_action=self.next_action,
        )

    def to_dict(self) -> dict[str, object]:
        value = self._identity_payload()
        value["decision_identity"] = self.decision_identity
        return value

    @classmethod
    def build(
        cls,
        *,
        decision: str,
        attempt_ledger_complete: bool,
        attempts: tuple[AttemptRecord, ...],
        protocol: ProtocolDecisionBinding,
        candidate_dispositions: tuple[tuple[str, str], ...],
        reviews: tuple[ReviewBinding, ...],
        residual_risks: tuple[str, ...],
    ) -> DecisionReceipt:
        total = sum(attempt.upper_seconds for attempt in attempts)
        repair = sum(attempt.upper_seconds for attempt in attempts if attempt.repair_rerun)
        payload = _decision_payload(
            decision=decision,
            attempt_ledger_complete=attempt_ledger_complete,
            attempts=attempts,
            protocol=protocol,
            total_active_upper_seconds=total,
            repair_rerun_upper_seconds=repair,
            candidate_dispositions=candidate_dispositions,
            phase_dispositions=_PHASE_DISPOSITIONS,
            later_ranks="not_evaluated",
            efficiency="not_used",
            reviews=reviews,
            residual_risks=residual_risks,
            next_action=_NEXT_ACTION,
        )
        return cls(
            schema_version=DECISION_RECEIPT_SCHEMA_VERSION,
            decision_identity=sha256_bytes(canonical_json(payload)),
            decision=decision,
            attempt_ledger_complete=attempt_ledger_complete,
            attempts=attempts,
            protocol=protocol,
            total_active_upper_seconds=total,
            repair_rerun_upper_seconds=repair,
            candidate_dispositions=candidate_dispositions,
            phase_dispositions=_PHASE_DISPOSITIONS,
            later_ranks="not_evaluated",
            efficiency="not_used",
            reviews=reviews,
            residual_risks=residual_risks,
            next_action=_NEXT_ACTION,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> DecisionReceipt:
        expected = frozenset(
            {
                "schema_version",
                "decision_identity",
                "decision",
                "attempt_ledger_complete",
                "attempts",
                "protocol",
                "total_active_upper_seconds",
                "repair_rerun_upper_seconds",
                "candidate_dispositions",
                "phase_dispositions",
                "later_ranks",
                "efficiency",
                "reviews",
                "residual_risks",
                "next_action",
            }
        )
        _require_keys(value, expected, "DecisionReceipt")

        def pairs(field: str) -> tuple[tuple[str, str], ...]:
            raw = value[field]
            if not isinstance(raw, list):
                raise ValueError(f"DecisionReceipt.{field} must be an array")
            result: list[tuple[str, str]] = []
            for item in raw:
                if (
                    not isinstance(item, list)
                    or len(item) != 2
                    or not all(isinstance(part, str) for part in item)
                ):
                    raise ValueError(f"DecisionReceipt.{field} entries must be string pairs")
                result.append((cast("str", item[0]), cast("str", item[1])))
            return tuple(result)

        risks = value["residual_risks"]
        if not isinstance(risks, list):
            raise ValueError("DecisionReceipt.residual_risks must be an array")
        return cls(
            schema_version=cast("int", value["schema_version"]),
            decision_identity=cast("str", value["decision_identity"]),
            decision=cast("str", value["decision"]),
            attempt_ledger_complete=cast("bool", value["attempt_ledger_complete"]),
            attempts=tuple(
                AttemptRecord.from_dict(item)
                for item in _tuple_of_mappings(value["attempts"], "DecisionReceipt.attempts")
            ),
            protocol=ProtocolDecisionBinding.from_dict(
                _mapping(value["protocol"], "DecisionReceipt.protocol")
            ),
            total_active_upper_seconds=cast("int", value["total_active_upper_seconds"]),
            repair_rerun_upper_seconds=cast("int", value["repair_rerun_upper_seconds"]),
            candidate_dispositions=pairs("candidate_dispositions"),
            phase_dispositions=pairs("phase_dispositions"),
            later_ranks=cast("str", value["later_ranks"]),
            efficiency=cast("str", value["efficiency"]),
            reviews=tuple(
                ReviewBinding.from_dict(item)
                for item in _tuple_of_mappings(value["reviews"], "DecisionReceipt.reviews")
            ),
            residual_risks=tuple(cast("list[str]", risks)),
            next_action=cast("str", value["next_action"]),
        )


def _read_regular_nofollow(path: Path, label: str) -> bytes:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{label} must be an absolute path without parent references")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"cannot open {label} {path}: {exc}") from exc
    try:
        observed = os.fstat(fd)
        if not stat.S_ISREG(observed.st_mode):
            raise ValueError(f"{label} must be a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _load_canonical_receipt(path: Path, model: type[Any], label: str) -> tuple[Any, bytes]:
    raw = _read_regular_nofollow(path, label)
    try:
        decoded = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} must be canonical JSON: {exc}") from exc
    receipt = model.from_dict(_mapping(decoded, label))
    if canonical_json(receipt.to_dict()) != raw:
        raise ValueError(f"{label} bytes are not the model's exact canonical JSON")
    return receipt, raw


def _verify_protocol_binding(
    artifact_root: Path,
    binding: ProtocolDecisionBinding,
) -> ProtocolPhaseReceipt:
    if not artifact_root.is_absolute() or ".." in artifact_root.parts:
        raise ValueError("artifact_root must be an absolute path without parent references")
    expected_protocol = (
        artifact_root
        / binding.protocol_evaluation_identity
        / "protocol-receipts"
        / f"{binding.protocol_run_identity}.protocol.json"
    )
    if Path(binding.protocol_receipt_path) != expected_protocol:
        raise ValueError("protocol_receipt_path does not match its exact evaluation/run identity")
    protocol, protocol_bytes = _load_canonical_receipt(
        expected_protocol, ProtocolPhaseReceipt, "protocol receipt"
    )
    protocol = cast("ProtocolPhaseReceipt", protocol)
    evaluator = protocol.evaluator
    if evaluator is None:
        raise ValueError("protocol receipt must bind an evaluator identity")
    checks = {
        "protocol_evaluation_identity": protocol.evaluation_identity,
        "protocol_run_identity": protocol.run_identity,
        "protocol_receipt_sha256": sha256_bytes(protocol_bytes),
        "protocol_artifact_tree_digest": protocol.artifact_tree_digest,
        "evaluator_source_commit": evaluator.source_commit,
    }
    for field, observed in checks.items():
        if getattr(binding, field) != observed:
            raise ValueError(f"{field} does not match the exact protocol receipt")
    if protocol.status != "pass" or protocol.next_action != PROTOCOL_PHASE_NEXT_ACTION_STOP:
        raise ValueError("protocol receipt must be the passing retain-and-stop gate")
    if not evaluator.source_clean or not evaluator.production_clean:
        raise ValueError("protocol receipt evaluator source must be exactly clean")

    admission = protocol.admission_binding
    if admission is None:
        raise ValueError("protocol receipt must bind its exact parent admission receipt")
    parent_checks = {
        "parent_evaluation_identity": admission.admission_evaluation_identity,
        "parent_run_identity": admission.admission_run_identity,
        "parent_receipt_path": admission.receipt_path,
        "parent_receipt_sha256": admission.receipt_sha256,
        "parent_artifact_tree_digest": admission.artifact_tree_digest,
    }
    for field, observed in parent_checks.items():
        if getattr(binding, field) != observed:
            raise ValueError(f"{field} does not match the protocol parent binding")
    expected_parent = (
        artifact_root
        / binding.parent_evaluation_identity
        / "receipts"
        / f"{binding.parent_run_identity}.json"
    )
    if Path(binding.parent_receipt_path) != expected_parent:
        raise ValueError("parent_receipt_path does not match its exact evaluation/run identity")
    parent, parent_bytes = _load_canonical_receipt(
        expected_parent, AdmissionReceipt, "parent admission receipt"
    )
    parent = cast("AdmissionReceipt", parent)
    if parent.status != "pass":
        raise ValueError("parent admission receipt must pass")
    if sha256_bytes(parent_bytes) != binding.parent_receipt_sha256:
        raise ValueError("parent_receipt_sha256 does not match parent receipt bytes")
    if (
        parent.evaluation_identity != binding.parent_evaluation_identity
        or parent.run_identity != binding.parent_run_identity
        or parent.artifact_tree_digest != binding.parent_artifact_tree_digest
    ):
        raise ValueError("parent admission identity does not match the exact binding")
    return protocol


def _verify_attempt_evidence(attempt: AttemptRecord) -> None:
    if attempt.evidence_path is None:
        return
    raw = _read_regular_nofollow(Path(attempt.evidence_path), "attempt evidence")
    if sha256_bytes(raw) != attempt.evidence_sha256:
        raise ValueError(f"attempt {attempt.attempt_id} evidence SHA-256 does not match")


def _verify_complete_ledger_bindings(closeout: CloseoutInput) -> None:
    if not closeout.attempt_ledger_complete:
        return
    observed = {
        (
            attempt.kind,
            attempt.disposition,
            attempt.evidence_path,
            attempt.evidence_sha256,
        )
        for attempt in closeout.attempts
    }
    required = (
        (
            "parent",
            (
                "admission",
                "pass",
                closeout.protocol.parent_receipt_path,
                closeout.protocol.parent_receipt_sha256,
            ),
        ),
        (
            "protocol",
            (
                "protocol",
                "pass",
                closeout.protocol.protocol_receipt_path,
                closeout.protocol.protocol_receipt_sha256,
            ),
        ),
    )
    for label, expected in required:
        if expected not in observed:
            raise ValueError(
                f"complete attempt ledger must include the exact passing final {label} receipt"
            )


def _candidate_dispositions(protocol: ProtocolPhaseReceipt, decision: str) -> tuple[tuple[str, str], ...]:
    dispositions = {outcome.candidate: outcome.gate_disposition for outcome in protocol.outcomes}
    if dispositions != {
        "pyrefly": "fail",
        "pyright": "pass",
        "ty": "seam_incompatible_pull_only",
    }:
        raise ValueError("protocol candidate outcomes do not match the reviewed retain stop gate")
    if decision not in {"retain_pyright", "inconclusive_retain_pyright"}:
        raise ValueError("this protocol stop gate cannot support a promotion decision")
    return _CURRENT_CANDIDATE_DISPOSITIONS


def build_decision_receipt(artifact_root: Path, closeout: CloseoutInput) -> DecisionReceipt:
    """Validate exact immutable evidence and derive one closed decision receipt in memory."""

    protocol = _verify_protocol_binding(artifact_root, closeout.protocol)
    for attempt in closeout.attempts:
        _verify_attempt_evidence(attempt)
    _verify_complete_ledger_bindings(closeout)
    return DecisionReceipt.build(
        decision=closeout.decision,
        attempt_ledger_complete=closeout.attempt_ledger_complete,
        attempts=closeout.attempts,
        protocol=closeout.protocol,
        candidate_dispositions=_candidate_dispositions(protocol, closeout.decision),
        reviews=closeout.reviews,
        residual_risks=closeout.residual_risks,
    )


def publish_decision_receipt(
    artifact_root: Path,
    receipt: DecisionReceipt,
    *,
    deadline: Deadline | None = None,
) -> Path:
    """Publish one immutable ``0600`` decision below the final protocol identity."""

    bound = deadline or Deadline.start(monotonic_clock, DECISION_PUBLICATION_SECONDS)
    target_root = artifact_root / receipt.protocol.protocol_evaluation_identity
    request = PublicationRequest(
        owner_root=artifact_root,
        target_root=target_root,
        directory_name="decision-receipts",
        lock_name=".decision-publication.lock",
        identity=receipt.decision_identity,
        entry_name=f"{receipt.decision_identity}.decision.json",
        temporary_name=f".{receipt.decision_identity}.decision.json.tmp",
        payload=canonical_json(receipt.to_dict()),
        noun="decision receipt",
        step_prefix="publish_decision_receipt",
    )
    return publish_immutable_record(request, bound)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Publish an evidence-only backend decision from a complete ledger"
    )
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument(
        "--closeout-input",
        required=True,
        type=Path,
        help="closed JSON containing exact protocol binding, complete attempts, reviews, and risks",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    raw = _read_regular_nofollow(args.closeout_input, "closeout input")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"closeout input must be JSON: {exc}") from exc
    closeout = CloseoutInput.from_dict(_mapping(value, "closeout input"))
    receipt = build_decision_receipt(args.artifact_root, closeout)
    published = publish_decision_receipt(args.artifact_root, receipt)
    print(published)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
