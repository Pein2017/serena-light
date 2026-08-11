"""Run the Phase 1 admission gate and publish one canonical admission receipt.

Admission is the only phase that may freeze the candidate resolution and prepare the
service-owned candidate runtime.  It launches no candidate language server, opens no
protocol session, and mutates neither the installed Serena Light registration nor the
canonical production dependency slot.

**One resolution.**  The candidate lock is compiled exactly once per run, below
``<artifact-root>/<evaluation-identity>/``; the runtime is then content addressed by that
lock digest.  A second resolution inside one admission run is a structural error.

**A strict monotonic ceiling.**  Every *external* step -- production-identity capture,
candidate resolution, runtime preparation, and each bounded corpus capture -- is bracketed
by a monotonic deadline check before it starts and after it returns, against the frozen
30-minute ``admission`` budget.  Finalization (cleanup, artifact digest, receipt
publication) is evaluation-owned local work that must complete so the run's own evidence
stays trustworthy, so it is deliberately not abandoned at the ceiling.

**Bounded zero-write evidence.**  The corpus is frozen twice around the no-backend
admission operation and *both* canonical manifest collections are retained.  Each root's
before/after pair is compared by the Task 5 write guard, so every delta carries the two
manifest digests it was derived from.  A changed manifest control (revision, inventory
digest, inventory count) or a missing root is an unstable observation, not a clean result.

**The production identity brackets cleanup too.**  Production identity is captured before
any work, again after the last external step under the ceiling, and a third time *after*
evaluation-owned cleanup has run.  The receipt records that final post-cleanup capture, so
a cleanup that reports success while changing a production lock, digest, build identity, or
runtime path cannot hide behind an earlier clean reading.  The final capture is mandatory:
if it fails the run raises instead of publishing, because a receipt whose ``after`` side is
older than the last thing that touched the filesystem would be a false clean bill.

**Fail-closed statuses.**  ``pass`` requires equal production identity before and after
cleanup, one delta per root bound to both manifest digests, no unexpected path, and a
cleanup that neither failed nor had to remove anything.  ``hold`` is a trustworthy
observation of a violation -- unexpected writes, or production drift observed at any of the
three captures.  ``incomplete`` is an untrustworthy or unfinished observation -- the
deadline, a resolution or preparation failure, an unstable root, or a cleanup that failed or
had to remove partial state; a dirty cleanup always ends ``incomplete``, whatever the run
looked like before it, because the run can no longer describe its own side effects.  A
receipt is published only when its evidence is trustworthy: without the production identity
on both sides and the frozen candidate lock the run raises instead of publishing a receipt
that would understate what is unknown.

The receipt is serialized to canonical JSON, written to a same-directory temporary file,
fsynced, ``os.replace``-d over the canonical name, and the directory is fsynced, so a
reader never observes a partial receipt.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from scripts.backend_eval.candidate_lock import (
    ARTIFACT_ROOT_BASE_PARTS,
    CACHE_DIR_NAME,
    LOCK_FILE_NAME,
    CandidateLockError,
    CandidateLockRequest,
    compile_candidate_lock,
)
from scripts.backend_eval.manifests import ManifestError, default_corpus_requests, freeze_default_corpus
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    DEFAULT_PHASE_BUDGETS,
    EVALUATION_CONTRACT_VERSION,
    AdmissionReceipt,
    CandidateLock,
    ProductionIdentity,
    RootManifest,
    WriteDelta,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.production_identity import (
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)
from scripts.backend_eval.runtime import (
    CandidateRuntime,
    RuntimePreparationError,
    RuntimeRequest,
    prepare_candidate_runtime,
)
from scripts.backend_eval.write_guard import WriteGuardError, assert_no_unexpected_writes, compare_root_manifests

__all__ = [
    "ADMISSION_BUDGET_NAME",
    "ADMISSION_RECEIPT_FILE_NAME",
    "MAX_ISSUES",
    "NEXT_ACTION_HOLD",
    "NEXT_ACTION_PASS",
    "AdmissionError",
    "AdmissionFailure",
    "AdmissionRequest",
    "AdmissionServices",
    "Clock",
    "ProductionAdmissionServices",
    "admission_receipt_path",
    "artifact_tree_digest",
    "evaluation_identity",
    "main",
    "monotonic_clock",
    "run_admission",
]

ADMISSION_BUDGET_NAME = "admission"
ADMISSION_RECEIPT_FILE_NAME = "admission-receipt.json"
NEXT_ACTION_PASS = "begin_protocol_probe_planning"
NEXT_ACTION_HOLD = "retain_pyright_and_disposition_admission"
MAX_ISSUES = 20

_RECEIPT_TEMPORARY_NAME = f".{ADMISSION_RECEIPT_FILE_NAME}.tmp"
# Volatile or self-referential artifacts are excluded: the resolver cache is a rebuildable
# download store and the receipt cannot contain the digest of a tree that contains it.
_ARTIFACT_DIGEST_EXCLUDED_NAMES = frozenset({CACHE_DIR_NAME, ADMISSION_RECEIPT_FILE_NAME, _RECEIPT_TEMPORARY_NAME})
_EXCLUDE_NEWER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ABSOLUTE_PATH_RE = re.compile(r"/[^\s'\"]+")
_REDACTED_PATH = "<redacted-path>"
_MAX_DETAIL_CHARACTERS = 160
_TRUNCATION_MARKER = "...(truncated)"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

Clock = Callable[[], float]
monotonic_clock: Clock = time.monotonic


# --- typed failures ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionFailure:
    """The typed disposition of one admission step that did not complete cleanly."""

    status: str
    code: str
    detail: str

    def __post_init__(self) -> None:
        if self.status not in {"hold", "incomplete"}:
            raise ValueError("AdmissionFailure.status must be 'hold' or 'incomplete'")
        if not self.code:
            raise ValueError("AdmissionFailure.code must be a non-empty string")


class AdmissionError(RuntimeError):
    """Raised when admission cannot continue; carries the typed disposition."""

    def __init__(self, failure: AdmissionFailure) -> None:
        super().__init__(f"{failure.code}: {failure.detail}")
        self.failure = failure


def _fail(status: str, code: str, detail: str) -> AdmissionError:
    return AdmissionError(AdmissionFailure(status=status, code=code, detail=detail))


# --- the request ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionRequest:
    """The explicit declared roots and freeze timestamp of one admission run."""

    repo_root: Path
    artifact_root: Path
    runtime_base: Path
    uv: Path
    python: Path
    exclude_newer: str

    def __post_init__(self) -> None:
        _require_declared_path(self.repo_root, "AdmissionRequest.repo_root")
        if not self.repo_root.is_dir():
            raise ValueError(f"AdmissionRequest.repo_root must be an existing directory: {self.repo_root}")
        _require_declared_path(self.artifact_root, "AdmissionRequest.artifact_root")
        base = self.repo_root.joinpath(*ARTIFACT_ROOT_BASE_PARTS)
        if not self.artifact_root.is_relative_to(base):
            raise ValueError(f"AdmissionRequest.artifact_root must be {base} or an evaluation-owned path below it")
        _require_declared_path(self.runtime_base, "AdmissionRequest.runtime_base")
        _require_declared_path(self.uv, "AdmissionRequest.uv")
        _require_declared_path(self.python, "AdmissionRequest.python")
        if _EXCLUDE_NEWER_RE.fullmatch(self.exclude_newer) is None:
            raise ValueError(
                "AdmissionRequest.exclude_newer must be a UTC timestamp such as 2026-08-11T00:00:00Z"
            )

    @property
    def declared_roots(self) -> tuple[str, ...]:
        """Every path prefix this run is allowed to name in published evidence."""

        declared = [self.repo_root, self.artifact_root, self.runtime_base, self.uv, self.python]
        declared.extend(corpus.root for corpus in default_corpus_requests())
        return tuple(sorted({str(path) for path in declared}))


def _require_declared_path(path: Path, label: str) -> None:
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if ".." in path.parts:
        raise ValueError(f"{label} must not contain parent references")


def evaluation_identity(request: AdmissionRequest, production_identity: ProductionIdentity) -> str:
    """The reproducible identity of one admission run, bound to production before any work.

    The identity is derived only from inputs that exist before the candidate resolution, so
    the artifact directory can be named before anything is written into it, and a rerun with
    the same declared roots, freeze timestamp, and production identity reuses the same freeze.
    """

    return sha256_bytes(
        canonical_json(
            {
                "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
                "schema_version": ADMISSION_RECEIPT_SCHEMA_VERSION,
                "repo_root": str(request.repo_root),
                "runtime_base": str(request.runtime_base),
                "uv": str(request.uv),
                "python": str(request.python),
                "exclude_newer": request.exclude_newer,
                "dependency_lock_digest": production_identity.dependency_lock_digest,
                "build_identity": production_identity.build_identity,
            }
        )
    )


def admission_receipt_path(artifact_root: Path, identity: str) -> Path:
    """The canonical receipt location for one evaluation identity."""

    return artifact_root / identity / ADMISSION_RECEIPT_FILE_NAME


# --- the deadline ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Deadline:
    """A strict monotonic ceiling, checked before and after every external step."""

    clock: Clock
    seconds: int
    started: float

    @staticmethod
    def start(clock: Clock, seconds: int) -> _Deadline:
        return _Deadline(clock=clock, seconds=seconds, started=clock())

    def elapsed(self) -> float:
        return self.clock() - self.started

    def check(self, step: str, phase: str) -> None:
        elapsed = self.elapsed()
        if elapsed >= self.seconds:
            raise _fail(
                "incomplete",
                "admission_deadline_exceeded",
                f"step={step} phase={phase} elapsed={elapsed:.3f}s budget={self.seconds}s",
            )


# --- the service seam ----------------------------------------------------------------


class AdmissionServices(Protocol):
    """The external work admission orchestrates; every member is a Task 1-5 interface."""

    def capture_production_identity(self, repo_root: Path) -> ProductionIdentity: ...

    def compile_candidate_lock(self, request: CandidateLockRequest) -> CandidateLock: ...

    def prepare_candidate_runtime(self, lock: CandidateLock, request: RuntimeRequest) -> CandidateRuntime: ...

    def capture_corpus(self) -> tuple[RootManifest, ...]: ...

    def artifact_tree_digest(self, artifact_root: Path) -> str: ...

    def cleanup(self, evaluation_root: Path, stage: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ProductionAdmissionServices:
    """Bind the admission orchestration to the real Task 1-5 implementations."""

    def capture_production_identity(self, repo_root: Path) -> ProductionIdentity:
        return capture_production_identity(repo_root)

    def compile_candidate_lock(self, request: CandidateLockRequest) -> CandidateLock:
        return compile_candidate_lock(request)

    def prepare_candidate_runtime(self, lock: CandidateLock, request: RuntimeRequest) -> CandidateRuntime:
        return prepare_candidate_runtime(lock, request)

    def capture_corpus(self) -> tuple[RootManifest, ...]:
        return freeze_default_corpus()

    def artifact_tree_digest(self, artifact_root: Path) -> str:
        return artifact_tree_digest(artifact_root)

    def cleanup(self, evaluation_root: Path, stage: str) -> tuple[str, ...]:
        """Remove exactly the evaluation-owned partial state this module can create.

        The frozen candidate lock and the prepared runtime are durable evidence owned by
        their own transactional modules and are never removed here; the only partial state
        admission itself can leave behind is an interrupted receipt temporary file.
        """

        try:
            dir_fd = os.open(evaluation_root, _NOFOLLOW_DIRECTORY_FLAGS)
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise _fail("incomplete", "cleanup_failed", f"cannot open {evaluation_root}: {exc}") from exc
        try:
            try:
                os.unlink(_RECEIPT_TEMPORARY_NAME, dir_fd=dir_fd)
            except FileNotFoundError:
                return ()
            except OSError as exc:
                raise _fail(
                    "incomplete", "cleanup_failed", f"cannot remove {evaluation_root / _RECEIPT_TEMPORARY_NAME}: {exc}"
                ) from exc
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return ("removed_temporary_receipt",)


# --- the artifact tree digest ----------------------------------------------------------


def artifact_tree_digest(artifact_root: Path) -> str:
    """Digest the evaluation-owned artifact tree by content, refusing anything unhashable.

    Only regular files are recorded, by relative path, size, and SHA-256; the resolver
    cache and the receipt itself are excluded.  A symlink or special file anywhere below the
    root fails closed rather than being silently skipped.
    """

    entries: list[dict[str, object]] = []
    try:
        dir_fd = os.open(artifact_root, _NOFOLLOW_DIRECTORY_FLAGS)
    except FileNotFoundError:
        dir_fd = -1
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot open {artifact_root}: {exc}") from exc
    if dir_fd >= 0:
        try:
            _collect_artifact_entries(artifact_root, Path("."), entries, top_level=True)
        finally:
            os.close(dir_fd)
    entries.sort(key=lambda entry: str(entry["path"]))
    return sha256_bytes(canonical_json({"entries": entries}))


def _collect_artifact_entries(
    root: Path, relative: Path, entries: list[dict[str, object]], *, top_level: bool
) -> None:
    directory = root if top_level else root / relative
    try:
        children = sorted(os.scandir(directory), key=lambda entry: entry.name)
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot scan {directory}: {exc}") from exc
    for child in children:
        if top_level and child.name in _ARTIFACT_DIGEST_EXCLUDED_NAMES:
            continue
        child_relative = Path(child.name) if top_level else relative / child.name
        if child.is_symlink():
            raise _fail(
                "incomplete", "artifact_digest_failed", f"artifact tree contains a symlink: {directory / child.name}"
            )
        if child.is_dir(follow_symlinks=False):
            _collect_artifact_entries(root, child_relative, entries, top_level=False)
            continue
        if not child.is_file(follow_symlinks=False):
            raise _fail(
                "incomplete",
                "artifact_digest_failed",
                f"artifact tree contains a special file: {directory / child.name}",
            )
        payload = _read_artifact_bytes(Path(child.path))
        entries.append({"path": str(child_relative), "size": len(payload), "sha256": sha256_bytes(payload)})


def _read_artifact_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read()
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot read {path}: {exc}") from exc


# --- orchestration ------------------------------------------------------------------


@dataclass(slots=True)
class _Evidence:
    """Everything one admission run has proven so far, in the order it was proven."""

    production_identity_before: ProductionIdentity | None = None
    production_identity_after: ProductionIdentity | None = None
    production_identity_final: ProductionIdentity | None = None
    identity: str | None = None
    evaluation_root: Path | None = None
    candidate_lock: CandidateLock | None = None
    runtime: CandidateRuntime | None = None
    manifests_before: tuple[RootManifest, ...] = ()
    manifests_after: tuple[RootManifest, ...] = ()
    write_deltas: tuple[WriteDelta, ...] = ()
    resolutions: int = 0
    issues: list[str] = field(default_factory=list)


@contextmanager
def _translated(status: str, code: str) -> Iterator[None]:
    """Convert one Task 1-5 failure into a typed admission disposition.

    Production drift outranks whatever step observed it: it is always a trustworthy
    ``hold`` rather than the step's own disposition.
    """

    try:
        yield
    except AdmissionError:
        raise
    except ProductionIdentityChanged as exc:
        raise _fail("hold", "production_identity_changed", str(exc)) from exc
    except ProductionIdentityError as exc:
        raise _fail("incomplete", "production_identity_capture_failed", str(exc)) from exc
    except (
        CandidateLockError,
        ManifestError,
        RuntimePreparationError,
        WriteGuardError,
        OSError,
        ValueError,
    ) as exc:
        raise _fail(status, code, str(exc)) from exc


def run_admission(
    request: AdmissionRequest,
    *,
    services: AdmissionServices | None = None,
    clock: Clock = monotonic_clock,
) -> AdmissionReceipt:
    """Run the admission gate once and return its published receipt.

    A receipt is returned for every disposition whose evidence is trustworthy enough to
    publish.  When the run cannot even establish the production identity on both sides and
    the frozen candidate lock, :class:`AdmissionError` is raised instead, so no receipt ever
    claims more than the run actually observed.
    """

    active = ProductionAdmissionServices() if services is None else services
    deadline = _Deadline.start(clock, DEFAULT_PHASE_BUDGETS[ADMISSION_BUDGET_NAME].seconds)
    started_at = _utc_now()
    evidence = _Evidence()
    failure: AdmissionFailure | None = None
    try:
        _collect(request, active, deadline, evidence)
    except AdmissionError as exc:
        failure = exc.failure
        evidence.issues.append(_issue(request, failure.code, failure.detail))
    status = "pass" if failure is None else failure.status
    status = _cleanup(request, active, evidence, status)
    _capture_final_production_identity(request, active, evidence, failure)
    status = _bracket_cleanup(request, evidence, status)
    receipt = _build_receipt(request, active, evidence, status=status, started_at=started_at, failure=failure)
    _publish_receipt(request, evidence, receipt)
    return receipt


def _collect(
    request: AdmissionRequest, services: AdmissionServices, deadline: _Deadline, evidence: _Evidence
) -> None:
    """Perform every external step in canonical order under the monotonic ceiling."""

    evidence.production_identity_before = _external(
        deadline,
        "capture_production_identity_before",
        "incomplete",
        "production_identity_capture_failed",
        lambda: services.capture_production_identity(request.repo_root),
    )
    evidence.identity = evaluation_identity(request, evidence.production_identity_before)
    evidence.evaluation_root = request.artifact_root / evidence.identity

    if evidence.resolutions:  # pragma: no cover - structural guard
        raise _fail("incomplete", "repeated_candidate_resolution", "the candidate lock may be resolved only once")
    evidence.resolutions += 1
    lock_request = _lock_request(request, evidence.evaluation_root)
    evidence.candidate_lock = _external(
        deadline,
        "compile_candidate_lock",
        "incomplete",
        "candidate_resolution_failed",
        lambda: services.compile_candidate_lock(lock_request),
    )

    runtime_request = _runtime_request(request, evidence.evaluation_root)
    lock = evidence.candidate_lock
    evidence.runtime = _external(
        deadline,
        "prepare_candidate_runtime",
        "incomplete",
        "runtime_preparation_failed",
        lambda: services.prepare_candidate_runtime(lock, runtime_request),
    )

    evidence.manifests_before = _external(
        deadline, "capture_corpus_before", "incomplete", "corpus_capture_failed", services.capture_corpus
    )
    # Admission performs no backend operation between the two captures: the candidate
    # runtime exists but nothing is launched against the corpus.
    evidence.manifests_after = _external(
        deadline, "capture_corpus_after", "incomplete", "corpus_capture_failed", services.capture_corpus
    )
    evidence.write_deltas = _write_deltas(evidence.manifests_before, evidence.manifests_after)
    _require_no_unexpected_writes(evidence.write_deltas)

    evidence.production_identity_after = _external(
        deadline,
        "capture_production_identity_after",
        "incomplete",
        "production_identity_capture_failed",
        lambda: services.capture_production_identity(request.repo_root),
    )
    with _translated("hold", "production_identity_changed"):
        assert_production_identity_unchanged(evidence.production_identity_before, evidence.production_identity_after)


def _external[T](deadline: _Deadline, step: str, status: str, code: str, call: Callable[[], T]) -> T:
    deadline.check(step, "before")
    with _translated(status, code):
        result = call()
    deadline.check(step, "after")
    return result


def _lock_request(request: AdmissionRequest, evaluation_root: Path) -> CandidateLockRequest:
    with _translated("incomplete", "candidate_resolution_failed"):
        return CandidateLockRequest(
            repo_root=request.repo_root,
            artifact_root=evaluation_root,
            uv=request.uv,
            python=request.python,
            exclude_newer=request.exclude_newer,
        )


def _runtime_request(request: AdmissionRequest, evaluation_root: Path) -> RuntimeRequest:
    with _translated("incomplete", "runtime_preparation_failed"):
        return RuntimeRequest(
            repo_root=request.repo_root,
            runtime_base=request.runtime_base,
            uv=request.uv,
            python=request.python,
            requirements_lock=evaluation_root / LOCK_FILE_NAME,
        )


def _write_deltas(
    before: tuple[RootManifest, ...], after: tuple[RootManifest, ...]
) -> tuple[WriteDelta, ...]:
    """Pair the two canonical manifest collections root by root, refusing an unstable set."""

    before_by_root = {manifest.root: manifest for manifest in before}
    after_by_root = {manifest.root: manifest for manifest in after}
    if not before_by_root or set(before_by_root) != set(after_by_root):
        raise _fail(
            "incomplete",
            "unstable_corpus_root",
            f"corpus roots changed between captures: before={sorted(before_by_root)} after={sorted(after_by_root)}",
        )
    deltas: list[WriteDelta] = []
    for root in sorted(before_by_root):
        with _translated("incomplete", "unstable_corpus_root"):
            deltas.append(compare_root_manifests(before_by_root[root], after_by_root[root]))
    return tuple(deltas)


def _require_no_unexpected_writes(deltas: tuple[WriteDelta, ...]) -> None:
    try:
        assert_no_unexpected_writes(deltas)
    except WriteGuardError as exc:
        raise _fail("hold", "unexpected_evaluation_writes", str(exc)) from exc


def _cleanup(
    request: AdmissionRequest, services: AdmissionServices, evidence: _Evidence, status: str
) -> str:
    """Run the exact evaluation-owned cleanup; a dirty or failed cleanup can never pass."""

    if evidence.evaluation_root is None:
        return status
    try:
        summary = services.cleanup(evidence.evaluation_root, status)
    except AdmissionError as exc:
        evidence.issues.append(_issue(request, "cleanup_failed", exc.failure.detail))
        return "incomplete"
    except (OSError, RuntimeError, ValueError) as exc:
        evidence.issues.append(_issue(request, "cleanup_failed", str(exc)))
        return "incomplete"
    if summary:
        evidence.issues.append(_issue(request, "cleanup_removed_partial_state", ", ".join(sorted(summary))))
        return "incomplete"
    return status


def _capture_final_production_identity(
    request: AdmissionRequest,
    services: AdmissionServices,
    evidence: _Evidence,
    failure: AdmissionFailure | None,
) -> None:
    """Capture the production identity the receipt publishes, after cleanup has run.

    This capture is not optional and not deadline-gated: it is the only reading taken after
    the last thing this run could have changed, so a run that cannot take it has no honest
    ``after`` side to publish and fails closed instead.
    """

    if evidence.production_identity_before is None:
        return
    try:
        evidence.production_identity_final = services.capture_production_identity(request.repo_root)
    except (OSError, RuntimeError, ValueError) as exc:
        context = "" if failure is None else f" (after {failure.code})"
        raise _fail(
            "incomplete",
            "production_identity_capture_failed",
            f"cannot bracket cleanup with a final production identity capture{context}: {exc}",
        ) from exc


def _bracket_cleanup(request: AdmissionRequest, evidence: _Evidence, status: str) -> str:
    """Compare the pre-work identity with the post-cleanup one; drift can never pass.

    Cleanup runs after every other check, so a cleanup that returned success while changing
    production is only visible here.  A prior non-``pass`` disposition is preserved: it
    already describes something the run could not establish.
    """

    before = evidence.production_identity_before
    final = evidence.production_identity_final
    if before is None or final is None:
        return status
    try:
        assert_production_identity_unchanged(before, final)
    except ProductionIdentityChanged as exc:
        evidence.issues.append(_issue(request, "production_identity_changed", str(exc)))
        return "hold" if status == "pass" else status
    return status


def _build_receipt(
    request: AdmissionRequest,
    services: AdmissionServices,
    evidence: _Evidence,
    *,
    status: str,
    started_at: str,
    failure: AdmissionFailure | None,
) -> AdmissionReceipt:
    before = evidence.production_identity_before
    # The published ``after`` side is the post-cleanup capture, never the mid-run one.
    after = evidence.production_identity_final
    lock = evidence.candidate_lock
    if before is None or after is None or lock is None or evidence.identity is None:
        # The run knows less than any publishable receipt would imply: report the original
        # failure when there is one, and otherwise the missing evidence itself.
        if failure is not None:
            raise AdmissionError(failure)
        raise _fail(
            "incomplete",
            "untrustworthy_admission_evidence",
            "no receipt can be published without production identity on both sides and the candidate lock",
        )
    assert evidence.evaluation_root is not None
    digest = services.artifact_tree_digest(evidence.evaluation_root)
    runtime = evidence.runtime
    with _translated("incomplete", "untrustworthy_admission_evidence"):
        return AdmissionReceipt(
            schema_version=ADMISSION_RECEIPT_SCHEMA_VERSION,
            evaluation_contract_version=EVALUATION_CONTRACT_VERSION,
            evaluation_identity=evidence.identity,
            status=status,
            started_at=started_at,
            ended_at=_utc_now(),
            budgets=tuple(sorted(DEFAULT_PHASE_BUDGETS.values(), key=lambda budget: budget.name)),
            production_identity_before=before,
            production_identity_after=after,
            candidate_lock=lock,
            environments=() if runtime is None else tuple(runtime.environments),
            service_configs=() if runtime is None else tuple(runtime.service_configs),
            root_manifests_before=_sorted_manifests(evidence.manifests_before),
            root_manifests_after=_sorted_manifests(evidence.manifests_after),
            write_deltas=tuple(sorted(evidence.write_deltas, key=lambda delta: delta.root)),
            issues=_bounded_issues(evidence.issues),
            artifact_tree_digest=digest,
            next_action=NEXT_ACTION_PASS if status == "pass" else NEXT_ACTION_HOLD,
        )


def _sorted_manifests(manifests: tuple[RootManifest, ...]) -> tuple[RootManifest, ...]:
    return tuple(sorted(manifests, key=lambda manifest: manifest.root))


# --- receipt publication ----------------------------------------------------------------


def _publish_receipt(request: AdmissionRequest, evidence: _Evidence, receipt: AdmissionReceipt) -> Path:
    """Write the canonical receipt bytes atomically and durably, or fail closed."""

    assert evidence.evaluation_root is not None
    payload = canonical_json(receipt.to_dict())
    dir_fd = _open_evaluation_directory(request.repo_root, evidence.evaluation_root)
    try:
        _replace_temporary(dir_fd, evidence.evaluation_root)
        file_fd = os.open(_RECEIPT_TEMPORARY_NAME, _CREATE_FLAGS, 0o600, dir_fd=dir_fd)
        try:
            _write_all(file_fd, payload, evidence.evaluation_root)
            os.fsync(file_fd)
        finally:
            os.close(file_fd)
        try:
            os.replace(
                _RECEIPT_TEMPORARY_NAME, ADMISSION_RECEIPT_FILE_NAME, src_dir_fd=dir_fd, dst_dir_fd=dir_fd
            )
            os.fsync(dir_fd)
        except OSError as exc:
            raise _fail(
                "incomplete",
                "receipt_publication_failed",
                f"cannot publish the receipt below {evidence.evaluation_root}: {exc}",
            ) from exc
    finally:
        os.close(dir_fd)
    return evidence.evaluation_root / ADMISSION_RECEIPT_FILE_NAME


def _replace_temporary(dir_fd: int, evaluation_root: Path) -> None:
    try:
        os.unlink(_RECEIPT_TEMPORARY_NAME, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _fail(
            "incomplete",
            "receipt_publication_failed",
            f"cannot clear the receipt temporary below {evaluation_root}: {exc}",
        ) from exc


def _write_all(file_fd: int, payload: bytes, evaluation_root: Path) -> None:
    written = 0
    while written < len(payload):
        try:
            written += os.write(file_fd, payload[written:])
        except OSError as exc:
            raise _fail(
                "incomplete", "receipt_publication_failed", f"cannot write the receipt below {evaluation_root}: {exc}"
            ) from exc


def _open_evaluation_directory(repo_root: Path, evaluation_root: Path) -> int:
    """Open the evaluation directory, creating and reopening every component with O_NOFOLLOW."""

    relative = evaluation_root.relative_to(repo_root)
    try:
        dir_fd = os.open(repo_root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise _fail("incomplete", "receipt_publication_failed", f"cannot open {repo_root}: {exc}") from exc
    try:
        for part in relative.parts:
            try:
                os.mkdir(part, 0o700, dir_fd=dir_fd)
            except FileExistsError:
                pass
            except OSError as exc:
                raise _fail(
                    "incomplete", "receipt_publication_failed", f"cannot create artifact component {part!r}: {exc}"
                ) from exc
            try:
                child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise _fail(
                    "incomplete",
                    "receipt_publication_failed",
                    f"artifact component {part!r} must be an evaluation-owned directory: {exc}",
                ) from exc
            os.close(dir_fd)
            dir_fd = child
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


# --- issues -------------------------------------------------------------------------


def _issue(request: AdmissionRequest, code: str, detail: str) -> str:
    return f"{code}: {_sanitize(detail, request.declared_roots)}"


def _sanitize(detail: str, declared_roots: tuple[str, ...]) -> str:
    """Redact every absolute path outside the declared roots and bound the sample length."""

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return token if any(token.startswith(root) for root in declared_roots) else _REDACTED_PATH

    redacted = _ABSOLUTE_PATH_RE.sub(_replace, " ".join(detail.split()))
    if len(redacted) > _MAX_DETAIL_CHARACTERS:
        return redacted[:_MAX_DETAIL_CHARACTERS] + _TRUNCATION_MARKER
    return redacted


def _bounded_issues(issues: Sequence[str]) -> tuple[str, ...]:
    unique = sorted(set(issues))
    if len(unique) <= MAX_ISSUES:
        return tuple(unique)
    kept = unique[: MAX_ISSUES - 1]
    kept.append(f"issues_truncated: {len(unique) - len(kept)} additional issues omitted")
    return tuple(sorted(set(kept)))


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the command line -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.backend_eval.admission",
        description="Run the Phase 1 backend-evaluation admission gate and publish its receipt.",
    )
    parser.add_argument("--repo-root", required=True, type=Path)
    parser.add_argument("--artifact-root", required=True, type=Path)
    parser.add_argument("--runtime-base", required=True, type=Path)
    parser.add_argument("--uv", required=True, type=Path)
    parser.add_argument("--python", required=True, type=Path)
    parser.add_argument("--exclude-newer", required=True)
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    services: AdmissionServices | None = None,
    clock: Clock = monotonic_clock,
) -> int:
    """Exit ``0`` only for a canonical PASS; every other disposition exits ``2``."""

    args = _parser().parse_args(argv)
    try:
        request = AdmissionRequest(
            repo_root=args.repo_root,
            artifact_root=args.artifact_root,
            runtime_base=args.runtime_base,
            uv=args.uv,
            python=args.python,
            exclude_newer=args.exclude_newer,
        )
    except ValueError as exc:
        print(f"status=incomplete code=invalid_request detail={exc}")
        return 2
    try:
        receipt = run_admission(request, services=services, clock=clock)
    except AdmissionError as exc:
        print(f"status={exc.failure.status}")
        print(f"issue={_issue(request, exc.failure.code, exc.failure.detail)}")
        print(f"next_action={NEXT_ACTION_HOLD}")
        return 2
    for line in _summary(request, receipt):
        print(line)
    return 0 if receipt.status == "pass" else 2


def _summary(request: AdmissionRequest, receipt: AdmissionReceipt) -> tuple[str, ...]:
    unexpected = sum(len(delta.unexpected) for delta in receipt.write_deltas)
    lines = [
        f"status={receipt.status}",
        f"evaluation_contract_version={receipt.evaluation_contract_version}",
        f"evaluation_identity={receipt.evaluation_identity}",
        f"receipt={admission_receipt_path(request.artifact_root, receipt.evaluation_identity)}",
        f"started_at={receipt.started_at}",
        f"ended_at={receipt.ended_at}",
        f"candidate_lock_digest={receipt.candidate_lock.digest}",
        f"candidate_versions={','.join(f'{p.name}=={p.version}' for p in receipt.candidate_lock.candidates)}",
        f"artifact_tree_digest={receipt.artifact_tree_digest}",
        f"production_build_identity_before={receipt.production_identity_before.build_identity}",
        f"production_build_identity_after={receipt.production_identity_after.build_identity}",
        f"production_dependency_lock_before={receipt.production_identity_before.dependency_lock_digest}",
        f"production_dependency_lock_after={receipt.production_identity_after.dependency_lock_digest}",
        f"environments={','.join(identity.name for identity in receipt.environments)}",
        f"service_configs={','.join(identity.backend for identity in receipt.service_configs)}",
        f"root_manifests={len(receipt.root_manifests_before)}",
        f"unexpected_write_paths={unexpected}",
        f"next_action={receipt.next_action}",
    ]
    lines.extend(f"issue={issue}" for issue in receipt.issues)
    return tuple(lines)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
