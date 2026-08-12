"""Load one exact immutable Phase 1 admission receipt for the protocol phase.

There is deliberately no discovery API in this module: no directory scan, no ``latest``
selection, no candidate resolution, and no runtime preparation.  The caller supplies both
immutable parent identities and every digest that owns the continuation decision.  The
loader derives the one legal receipt path, reads it through the existing component-wise
descriptor helpers, verifies its exact canonical bytes, and returns a compact binding for
publication in the Phase 2 receipt alongside the parsed parent authority.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from scripts.backend_eval.process import Deadline

from scripts.backend_eval.models import (
    NEXT_ACTION_PASS,
    AdmissionBinding,
    AdmissionReceipt,
    AdmissionRootWitness,
    canonical_json,
    sha256_bytes,
)

__all__ = [
    "LoadedParentAdmission",
    "ParentAdmissionError",
    "ParentAdmissionExpectation",
    "ParentAdmissionFailure",
    "load_parent_admission",
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_READ_CHUNK_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParentAdmissionFailure:
    """One typed refusal to treat a Phase 1 receipt as continuation authority."""

    code: str
    detail: str

    def __post_init__(self) -> None:
        if not self.code:
            raise ValueError("ParentAdmissionFailure.code must be non-empty")
        if not self.detail:
            raise ValueError("ParentAdmissionFailure.detail must be non-empty")


class ParentAdmissionError(RuntimeError):
    """Raised when exact parent evidence cannot be loaded or does not match."""

    def __init__(self, failure: ParentAdmissionFailure) -> None:
        super().__init__(f"{failure.code}: {failure.detail}")
        self.failure = failure


def _fail(code: str, detail: str) -> ParentAdmissionError:
    return ParentAdmissionError(ParentAdmissionFailure(code=code, detail=detail))


@dataclass(frozen=True, slots=True)
class ParentAdmissionExpectation:
    """Every explicit immutable witness required to admit one exact Phase 1 parent."""

    artifact_root: Path
    evaluation_identity: str
    run_identity: str
    receipt_sha256: str
    artifact_tree_digest: str
    candidate_lock_digest: str
    runtime_manifest_sha256: str
    production_root: Path
    production_source_revision: str
    production_dependency_lock_digest: str
    production_build_identity: str

    def __post_init__(self) -> None:
        _require_absolute(self.artifact_root, "ParentAdmissionExpectation.artifact_root")
        _require_sha256(
            self.evaluation_identity, "ParentAdmissionExpectation.evaluation_identity"
        )
        _require_sha256(self.run_identity, "ParentAdmissionExpectation.run_identity")
        _require_sha256(self.receipt_sha256, "ParentAdmissionExpectation.receipt_sha256")
        _require_sha256(
            self.artifact_tree_digest,
            "ParentAdmissionExpectation.artifact_tree_digest",
        )
        _require_sha256(
            self.candidate_lock_digest,
            "ParentAdmissionExpectation.candidate_lock_digest",
        )
        _require_sha256(
            self.runtime_manifest_sha256,
            "ParentAdmissionExpectation.runtime_manifest_sha256",
        )
        _require_absolute(self.production_root, "ParentAdmissionExpectation.production_root")
        if _GIT_REVISION_RE.fullmatch(self.production_source_revision) is None:
            raise ValueError(
                "ParentAdmissionExpectation.production_source_revision must be a Git commit revision"
            )
        _require_sha256(
            self.production_dependency_lock_digest,
            "ParentAdmissionExpectation.production_dependency_lock_digest",
        )
        _require_sha256(
            self.production_build_identity,
            "ParentAdmissionExpectation.production_build_identity",
        )

    @property
    def receipt_path(self) -> Path:
        """The one legal receipt path; callers cannot redirect this to another authority."""

        return (
            self.artifact_root
            / self.evaluation_identity
            / "receipts"
            / f"{self.run_identity}.json"
        )


@dataclass(frozen=True, slots=True)
class LoadedParentAdmission:
    """The compact publishable binding and full parsed Phase 1 authority."""

    binding: AdmissionBinding
    receipt: AdmissionReceipt


def load_parent_admission(
    expectation: ParentAdmissionExpectation,
    *,
    deadline: Deadline,
) -> LoadedParentAdmission:
    """Load and verify exactly the parent named by ``expectation``.

    Every ancestor is opened from ``/`` one component at a time with ``O_NOFOLLOW`` and
    the leaf is opened with ``O_NOFOLLOW | O_NONBLOCK`` before a regular-file and authority
    check.  This small local guard deliberately avoids importing the Phase 1 runtime or
    admission closures into the sealed Phase 2 image.  The module never lists the receipts
    directory, so a missing exact run cannot fall through to another historical receipt.
    """

    deadline.check("parent admission load before")
    path = expectation.receipt_path
    payload = _read_exact_regular_file(path, deadline=deadline)
    deadline.check("parent admission digest before")

    observed_receipt_sha256 = sha256_bytes(payload)
    deadline.check("parent admission digest after")
    if observed_receipt_sha256 != expectation.receipt_sha256:
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission receipt SHA-256 does not match the exact expected receipt SHA-256",
        )

    deadline.check("parent admission parse before")
    receipt = _parse_canonical_receipt(payload, path)
    deadline.check("parent admission parse after")
    _require_parent_authority(receipt, expectation)
    deadline.check("parent admission authority after")
    binding = _build_binding(receipt, expectation, path, observed_receipt_sha256)
    deadline.check("parent admission load after")
    return LoadedParentAdmission(binding=binding, receipt=receipt)


def _parse_canonical_receipt(payload: bytes, path: Path) -> AdmissionReceipt:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _fail(
            "parent_receipt_malformed",
            f"cannot decode exact parent admission receipt {path}: {exc}",
        ) from exc
    if not isinstance(decoded, dict):
        raise _fail(
            "parent_receipt_malformed",
            f"exact parent admission receipt {path} must be a JSON object",
        )
    try:
        receipt = AdmissionReceipt.from_dict(cast("dict[str, Any]", decoded))
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail(
            "parent_receipt_malformed",
            f"exact parent admission receipt {path} fails its strict schema: {exc}",
        ) from exc
    if canonical_json(receipt.to_dict()) != payload:
        raise _fail(
            "parent_receipt_malformed",
            f"exact parent admission receipt {path} is not canonical",
        )
    return receipt


def _require_parent_authority(
    receipt: AdmissionReceipt,
    expectation: ParentAdmissionExpectation,
) -> None:
    if receipt.evaluation_identity != expectation.evaluation_identity:
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission receipt evaluation identity differs from its exact path",
        )
    if receipt.run_identity != expectation.run_identity:
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission receipt run identity differs from its exact path",
        )
    if receipt.status != "pass":
        raise _fail(
            "parent_receipt_not_admitted",
            f"parent admission receipt status must be 'pass', got {receipt.status!r}",
        )
    if receipt.next_action != NEXT_ACTION_PASS:
        raise _fail(
            "parent_receipt_not_admitted",
            "parent admission receipt does not authorize the protocol probe phase",
        )
    if receipt.evaluator is None or not (
        receipt.evaluator.source_clean and receipt.evaluator.production_clean
    ):
        raise _fail(
            "parent_receipt_not_admitted",
            "parent admission authority must name a clean evaluator and production closure",
        )
    _require_equal(
        receipt.artifact_tree_digest,
        expectation.artifact_tree_digest,
        "artifact tree digest",
    )
    _require_equal(
        receipt.candidate_lock.digest,
        expectation.candidate_lock_digest,
        "candidate lock digest",
    )
    runtime = receipt.runtime_binding
    if runtime is None:
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission receipt has no runtime identity",
        )
    _require_equal(
        runtime.lock_digest,
        expectation.candidate_lock_digest,
        "runtime candidate lock digest",
    )
    _require_equal(
        runtime.manifest_sha256,
        expectation.runtime_manifest_sha256,
        "runtime manifest digest",
    )
    if runtime.root.rsplit("/", 1)[-1] != expectation.candidate_lock_digest:
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission runtime root is not addressed by the expected candidate lock",
        )
    if runtime.manifest_path != f"{runtime.root}/runtime-manifest.json":
        raise _fail(
            "parent_receipt_mismatch",
            "parent admission runtime manifest path is not the canonical runtime-manifest.json",
        )

    production = receipt.production_identity_before
    _require_equal(
        production.dependency_lock_digest,
        expectation.production_dependency_lock_digest,
        "production dependency lock digest",
    )
    _require_equal(
        production.build_identity,
        expectation.production_build_identity,
        "production build identity",
    )
    _require_production_revision(
        receipt,
        str(expectation.production_root),
        expectation.production_source_revision,
    )


def _require_production_revision(
    receipt: AdmissionReceipt,
    production_root: str,
    expected_revision: str,
) -> None:
    for label, manifests in (
        ("before", receipt.root_manifests_before),
        ("after", receipt.root_manifests_after),
    ):
        matches = tuple(manifest for manifest in manifests if manifest.root == production_root)
        if len(matches) != 1 or matches[0].kind != "git":
            raise _fail(
                "parent_receipt_mismatch",
                f"parent admission receipt lacks one Git production root manifest {label}",
            )
        _require_equal(
            matches[0].source_revision,
            expected_revision,
            f"production source revision {label}",
        )


def _build_binding(
    receipt: AdmissionReceipt,
    expectation: ParentAdmissionExpectation,
    path: Path,
    receipt_sha256: str,
) -> AdmissionBinding:
    runtime = receipt.runtime_binding
    if runtime is None:  # guarded above; keeps this function independently total
        raise _fail("parent_receipt_mismatch", "parent admission receipt has no runtime identity")
    return AdmissionBinding(
        admission_evaluation_identity=receipt.evaluation_identity,
        admission_run_identity=receipt.run_identity,
        receipt_path=str(path),
        receipt_sha256=receipt_sha256,
        artifact_tree_digest=receipt.artifact_tree_digest,
        candidate_lock_digest=receipt.candidate_lock.digest,
        runtime_root=runtime.root,
        runtime_manifest_sha256=runtime.manifest_sha256,
        production_root=str(expectation.production_root),
        production_source_revision=expectation.production_source_revision,
        production_dependency_lock_digest=receipt.production_identity_before.dependency_lock_digest,
        production_build_identity=receipt.production_identity_before.build_identity,
        parent_root_manifests=tuple(
            AdmissionRootWitness(
                root=manifest.root,
                kind=manifest.kind,
                source_revision=manifest.source_revision,
                manifest_digest=manifest.manifest_digest,
            )
            for manifest in receipt.root_manifests_before
        ),
    )


def _require_equal(observed: object, expected: object, label: str) -> None:
    if observed != expected:
        raise _fail(
            "parent_receipt_mismatch",
            f"parent admission {label} does not match the exact expected {label}",
        )


def _require_sha256(value: str, label: str) -> None:
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256 digest")


def _require_absolute(path: Path, label: str) -> None:
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or path.anchor != "/"
        or ".." in path.parts
    ):
        raise ValueError(
            f"{label} must be a canonical absolute path without parent references"
        )


def _open_filesystem_root(*, deadline: Deadline) -> int:
    """Open the guarded filesystem anchor before confined descriptor traversal."""

    deadline.check("parent receipt root open before")
    try:
        root_fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail(
            "parent_receipt_unsafe", f"cannot open the filesystem root: {exc}"
        ) from exc
    try:
        deadline.check("parent receipt root open after")
    except BaseException:
        os.close(root_fd)
        raise
    return root_fd


def _close_descriptor(fd: int, *, deadline: Deadline, step: str) -> None:
    """Close one owned descriptor even when the shared deadline has just expired."""

    try:
        deadline.check(f"{step} before")
    finally:
        os.close(fd)
    deadline.check(f"{step} after")


def _read_exact_regular_file(path: Path, *, deadline: Deadline) -> bytes:
    """Read an absolute receipt path once, following no component or blocking special node."""

    current = _open_filesystem_root(deadline=deadline)
    try:
        for part in path.parts[1:-1]:
            deadline.check("parent receipt component open before")
            try:
                child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=current)
            except OSError as exc:
                raise _fail(
                    "parent_receipt_unsafe",
                    f"cannot open exact parent admission receipt {path} without following a link: {exc}",
                ) from exc
            try:
                deadline.check("parent receipt component open after")
            except BaseException:
                os.close(child)
                raise
            previous = current
            current = child
            _close_descriptor(
                previous,
                deadline=deadline,
                step="parent receipt ancestor close",
            )
        deadline.check("parent receipt leaf open before")
        try:
            receipt_fd = os.open(path.name, _READ_FLAGS, dir_fd=current)
        except FileNotFoundError as exc:
            raise _fail(
                "parent_receipt_unavailable",
                f"exact parent admission receipt does not exist: {path}",
            ) from exc
        except OSError as exc:
            raise _fail(
                "parent_receipt_unsafe",
                f"cannot read {path} without following a link; receipt must be a regular file: {exc}",
            ) from exc
        try:
            deadline.check("parent receipt leaf open after")
            deadline.check("parent receipt fstat before")
            observed = os.fstat(receipt_fd)
            deadline.check("parent receipt fstat after")
            if not stat.S_ISREG(observed.st_mode):
                raise _fail(
                    "parent_receipt_unsafe",
                    f"exact parent admission receipt must be a regular file: {path}",
                )
            if stat.S_IMODE(observed.st_mode) != 0o600:
                raise _fail(
                    "parent_receipt_unsafe",
                    f"exact parent admission receipt must have mode 0600: {path}",
                )
            if observed.st_uid != os.geteuid() or observed.st_nlink != 1:
                raise _fail(
                    "parent_receipt_unsafe",
                    f"exact parent admission receipt has unexpected authority: {path}",
                )
            try:
                with os.fdopen(receipt_fd, "rb", closefd=False) as handle:
                    chunks: list[bytes] = []
                    while True:
                        deadline.check("parent receipt read before")
                        chunk = handle.read(_READ_CHUNK_BYTES)
                        deadline.check("parent receipt read after")
                        if not chunk:
                            return b"".join(chunks)
                        chunks.append(chunk)
            except OSError as exc:
                raise _fail(
                    "parent_receipt_unsafe",
                    f"cannot read exact parent admission receipt {path}: {exc}",
                ) from exc
        finally:
            _close_descriptor(
                receipt_fd,
                deadline=deadline,
                step="parent receipt leaf close",
            )
    finally:
        _close_descriptor(
            current,
            deadline=deadline,
            step="parent receipt directory close",
        )
