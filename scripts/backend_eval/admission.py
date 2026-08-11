"""Run the Phase 1 admission gate and publish one immutable admission receipt.

Admission is the only phase that may freeze the candidate resolution and prepare the
service-owned candidate runtime.  It launches no candidate language server, opens no
protocol session, and mutates neither the installed Serena Light registration nor the
canonical production dependency slot.

**One resolution.**  The candidate lock is compiled exactly once per run, below
``<artifact-root>/<evaluation-identity>/``; the runtime is then content addressed by that
lock digest.  A second resolution inside one admission run is a structural error.

**The measurement window brackets every setup operation.**  The corpus is frozen *before*
the candidate lock is compiled and the runtime is prepared, and again after runtime
preparation and before cleanup and receipt publication.  Anything Phase 1 setup could have
written into a corpus root therefore falls inside the delta rather than outside it.  The
second capture is enriched by the two-stage remainder algorithm -- changed or created
remainder files are hashed and the after manifest is rebuilt -- before any ``WriteDelta``
is constructed, so every delta is bound to the two manifest digests it was derived from.

**One ceiling for the whole gate.**  The frozen 1800-second admission budget covers
resolution, runtime preparation, both corpus captures, cleanup, the final production
identity, the artifact digest, and receipt publication.  Collection stops early enough to
leave a reserved finalization window, so a run that reaches the ceiling can still publish a
trustworthy timeout receipt; finalization itself is checked against the same absolute
ceiling and fails closed, without a receipt, rather than publishing evidence it could not
complete.  Every subprocess receives the remaining time and has its process group killed on
expiry.

**Immutable per-execution receipts.**  Each execution has its own ``run_identity`` and
publishes to ``receipts/<run-identity>.json`` with an exclusive link, so a repeated or
concurrent run can never delete or replace another run's receipt.  Publication is
serialized on a per-identity ``O_NOFOLLOW`` lock.

**Fail-closed statuses.**  ``pass`` requires equal production identity before and after
cleanup, one delta per root bound to both manifest digests, no unexpected path, no changed
manifest control, and a cleanup that neither failed nor had to remove anything -- plus the
evaluator, host, bootstrap-environment, and candidate-runtime bindings the receipt model
requires.  ``hold`` is a trustworthy observation of a violation.  ``incomplete`` is an
untrustworthy or unfinished observation.  A receipt is published only when its evidence is
trustworthy: without the production identity on both sides and the frozen candidate lock the
run raises instead of publishing a receipt that would understate what is unknown.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import re
import secrets
import stat
import sys
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
from scripts.backend_eval.identity import (
    IdentityError,
    bootstrap_environment_identity,
    capture_evaluator_identity,
)
from scripts.backend_eval.manifests import ManifestError, default_corpus_requests, freeze_default_corpus
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    EVALUATION_CONTRACT_VERSION,
    NEXT_ACTION_HOLD,
    NEXT_ACTION_PASS,
    AdmissionReceipt,
    BootstrapEnvironmentIdentity,
    CandidateLock,
    EvaluatorIdentity,
    ProductionIdentity,
    RootManifest,
    RuntimeBinding,
    WriteDelta,
    canonical_json,
    default_phase_budgets,
    sha256_bytes,
)
from scripts.backend_eval.process import (
    Clock,
    Deadline,
    DeadlineExceeded,
    monotonic_clock,
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
    runtime_manifest_digest,
)
from scripts.backend_eval.write_guard import (
    WriteGuardError,
    assert_no_unexpected_writes,
    compare_root_manifests,
    enrich_after_manifest,
)

__all__ = [
    "ADMISSION_BUDGET_NAME",
    "ADMISSION_BUDGET_SECONDS",
    "FINALIZATION_RESERVE_SECONDS",
    "MAX_ISSUES",
    "NEXT_ACTION_HOLD",
    "NEXT_ACTION_PASS",
    "PUBLICATION_LOCK_NAME",
    "RECEIPTS_DIR_NAME",
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
    "new_run_identity",
    "run_admission",
]

ADMISSION_BUDGET_NAME = "admission"
ADMISSION_BUDGET_SECONDS = next(
    budget.seconds for budget in default_phase_budgets() if budget.name == ADMISSION_BUDGET_NAME
)
# Collection stops this far before the ceiling so a timeout receipt can still be published.
FINALIZATION_RESERVE_SECONDS = 300
RECEIPTS_DIR_NAME = "receipts"
PUBLICATION_LOCK_NAME = ".admission-publication.lock"
MAX_ISSUES = 20

# Volatile or self-referential artifacts are excluded: the resolver cache is a rebuildable
# download store, the publication lock is a control file, and no receipt can contain the
# digest of a tree that contains it.
_ARTIFACT_DIGEST_EXCLUDED_NAMES = frozenset({CACHE_DIR_NAME, RECEIPTS_DIR_NAME, PUBLICATION_LOCK_NAME})
_EXCLUDE_NEWER_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ABSOLUTE_PATH_RE = re.compile(r"/[^\s'\"]+")
_REDACTED_PATH = "<redacted-path>"
_MAX_DETAIL_CHARACTERS = 160
_TRUNCATION_MARKER = "...(truncated)"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
# The digest traversal checks the ceiling this often rather than on every single file.
_ARTIFACT_CHECK_INTERVAL = 64

Check = Callable[[], None]


def _noop_check() -> None:
    return None


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


def evaluation_identity(
    request: AdmissionRequest, production_identity: ProductionIdentity, evaluator: EvaluatorIdentity
) -> str:
    """The reproducible identity of one admission run, bound to code, host, and production.

    The identity is derived only from inputs that exist before the candidate resolution, so
    the artifact directory can be named before anything is written into it.  It binds the
    evaluator source closure, the CLI host interpreter, and the artifact root, so changed
    evaluator code or a changed host produces a new evaluation identity and therefore new
    artifacts rather than overwriting an earlier run's evidence.
    """

    return sha256_bytes(
        canonical_json(
            {
                "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
                "schema_version": ADMISSION_RECEIPT_SCHEMA_VERSION,
                "repo_root": str(request.repo_root),
                "artifact_root": str(request.artifact_root),
                "runtime_base": str(request.runtime_base),
                "uv": str(request.uv),
                "python": str(request.python),
                "exclude_newer": request.exclude_newer,
                "dependency_lock_digest": production_identity.dependency_lock_digest,
                "build_identity": production_identity.build_identity,
                "evaluator": evaluator.to_dict(),
            }
        )
    )


def new_run_identity(started_at: str) -> str:
    """One immutable identity per execution; two runs never share a receipt path."""

    return sha256_bytes(
        canonical_json({"started_at": started_at, "pid": os.getpid(), "nonce": secrets.token_hex(32)})
    )


def admission_receipt_path(artifact_root: Path, identity: str, run_identity: str) -> Path:
    """The immutable receipt location of one execution of one evaluation identity."""

    return artifact_root / identity / RECEIPTS_DIR_NAME / f"{run_identity}.json"


def _receipt_temporary_name(run_identity: str) -> str:
    return f".{run_identity}.json.tmp"


# --- the service seam ----------------------------------------------------------------


class AdmissionServices(Protocol):
    """The external work admission orchestrates; every member is a Task 1-5 interface."""

    def capture_production_identity(self, repo_root: Path) -> ProductionIdentity: ...

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity: ...

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity: ...

    def compile_candidate_lock(self, request: CandidateLockRequest, deadline: Deadline) -> CandidateLock: ...

    def prepare_candidate_runtime(
        self, lock: CandidateLock, request: RuntimeRequest, deadline: Deadline
    ) -> CandidateRuntime: ...

    def capture_corpus(self, deadline: Deadline) -> tuple[RootManifest, ...]: ...

    def runtime_manifest_digest(self, root: Path) -> str: ...

    def artifact_tree_digest(self, artifact_root: Path, deadline: Deadline) -> str: ...

    def cleanup(self, evaluation_root: Path, run_identity: str, stage: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ProductionAdmissionServices:
    """Bind the admission orchestration to the real Task 1-5 implementations."""

    def capture_production_identity(self, repo_root: Path) -> ProductionIdentity:
        return capture_production_identity(repo_root)

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity:
        return capture_evaluator_identity(deadline=deadline)

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity:
        return bootstrap_environment_identity()

    def compile_candidate_lock(self, request: CandidateLockRequest, deadline: Deadline) -> CandidateLock:
        return compile_candidate_lock(request, deadline=deadline)

    def prepare_candidate_runtime(
        self, lock: CandidateLock, request: RuntimeRequest, deadline: Deadline
    ) -> CandidateRuntime:
        return prepare_candidate_runtime(lock, request, deadline=deadline)

    def capture_corpus(self, deadline: Deadline) -> tuple[RootManifest, ...]:
        return freeze_default_corpus(deadline=deadline)

    def runtime_manifest_digest(self, root: Path) -> str:
        return runtime_manifest_digest(root)

    def artifact_tree_digest(self, artifact_root: Path, deadline: Deadline) -> str:
        return artifact_tree_digest(artifact_root, check=lambda: deadline.check("artifact_tree_digest"))

    def cleanup(self, evaluation_root: Path, run_identity: str, stage: str) -> tuple[str, ...]:
        """Remove exactly the evaluation-owned partial state this module can create.

        The frozen candidate lock, the prepared runtime, and every *other* execution's
        receipt are durable evidence owned elsewhere and are never removed here; the only
        partial state admission itself can leave behind is this run's own interrupted
        receipt temporary.
        """

        del stage
        try:
            dir_fd = os.open(evaluation_root / RECEIPTS_DIR_NAME, _NOFOLLOW_DIRECTORY_FLAGS)
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise _fail(
                "incomplete", "cleanup_failed", f"cannot open {evaluation_root / RECEIPTS_DIR_NAME}: {exc}"
            ) from exc
        temporary = _receipt_temporary_name(run_identity)
        try:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except FileNotFoundError:
                return ()
            except OSError as exc:
                raise _fail(
                    "incomplete",
                    "cleanup_failed",
                    f"cannot remove {evaluation_root / RECEIPTS_DIR_NAME / temporary}: {exc}",
                ) from exc
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return ("removed_temporary_receipt",)


# --- the artifact tree digest ----------------------------------------------------------


def artifact_tree_digest(artifact_root: Path, *, check: Check = _noop_check) -> str:
    """Digest the evaluation-owned artifact tree by content, refusing anything unhashable.

    The traversal is descriptor relative and ``O_NOFOLLOW`` throughout: every directory is
    opened from its parent's descriptor, and every file is validated and read through *one*
    descriptor, so no path is ever reopened after its type was checked.  Only regular files
    are recorded, by relative path, size, and SHA-256; the resolver cache, the receipts
    directory, and the publication lock are excluded.  A symlink or special file anywhere
    below the root fails closed rather than being silently skipped.
    """

    entries: list[dict[str, object]] = []
    try:
        root_fd = os.open(artifact_root, _NOFOLLOW_DIRECTORY_FLAGS)
    except FileNotFoundError:
        return sha256_bytes(canonical_json({"entries": entries}))
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot open {artifact_root}: {exc}") from exc
    try:
        _collect_artifact_entries(root_fd, "", artifact_root, entries, check, top_level=True)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda entry: str(entry["path"]))
    return sha256_bytes(canonical_json({"entries": entries}))


def _collect_artifact_entries(
    dir_fd: int,
    prefix: str,
    artifact_root: Path,
    entries: list[dict[str, object]],
    check: Check,
    *,
    top_level: bool,
) -> None:
    try:
        with os.scandir(dir_fd) as scan:
            names = sorted(entry.name for entry in scan)
    except OSError as exc:
        raise _fail(
            "incomplete", "artifact_digest_failed", f"cannot scan {artifact_root / prefix}: {exc}"
        ) from exc
    for index, name in enumerate(names):
        if index % _ARTIFACT_CHECK_INTERVAL == 0:
            check()
        if top_level and name in _ARTIFACT_DIGEST_EXCLUDED_NAMES:
            continue
        relative = f"{prefix}{name}"
        try:
            observed = os.lstat(name, dir_fd=dir_fd)
        except OSError as exc:
            raise _fail(
                "incomplete", "artifact_digest_failed", f"cannot inspect {artifact_root / relative}: {exc}"
            ) from exc
        if stat.S_ISLNK(observed.st_mode):
            raise _fail(
                "incomplete", "artifact_digest_failed", f"artifact tree contains a symlink: {artifact_root / relative}"
            )
        if stat.S_ISDIR(observed.st_mode):
            try:
                child_fd = os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=dir_fd)
            except OSError as exc:
                raise _fail(
                    "incomplete", "artifact_digest_failed", f"cannot open {artifact_root / relative}: {exc}"
                ) from exc
            try:
                _collect_artifact_entries(
                    child_fd, f"{relative}/", artifact_root, entries, check, top_level=False
                )
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(observed.st_mode):
            raise _fail(
                "incomplete",
                "artifact_digest_failed",
                f"artifact tree contains a special file: {artifact_root / relative}",
            )
        payload = _read_artifact_bytes(dir_fd, name, artifact_root / relative)
        entries.append({"path": relative, "size": len(payload), "sha256": sha256_bytes(payload)})


def _read_artifact_bytes(dir_fd: int, name: str, label: Path) -> bytes:
    """Open, validate, and read one artifact through a single descriptor -- never reopened."""

    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot read {label}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise _fail("incomplete", "artifact_digest_failed", f"artifact is not a regular file: {label}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise _fail("incomplete", "artifact_digest_failed", f"cannot read {label}: {exc}") from exc
    finally:
        os.close(fd)


# --- orchestration ------------------------------------------------------------------


@dataclass(slots=True)
class _Evidence:
    """Everything one admission run has proven so far, in the order it was proven."""

    run_identity: str = ""
    evaluator: EvaluatorIdentity | None = None
    bootstrap: BootstrapEnvironmentIdentity | None = None
    production_identity_before: ProductionIdentity | None = None
    production_identity_after: ProductionIdentity | None = None
    production_identity_final: ProductionIdentity | None = None
    identity: str | None = None
    evaluation_root: Path | None = None
    candidate_lock: CandidateLock | None = None
    runtime: CandidateRuntime | None = None
    runtime_binding: RuntimeBinding | None = None
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
    except DeadlineExceeded as exc:
        raise _fail("incomplete", "admission_deadline_exceeded", str(exc)) from exc
    except ProductionIdentityChanged as exc:
        raise _fail("hold", "production_identity_changed", str(exc)) from exc
    except ProductionIdentityError as exc:
        raise _fail("incomplete", "production_identity_capture_failed", str(exc)) from exc
    except (
        CandidateLockError,
        IdentityError,
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
    the frozen candidate lock, or when finalization itself cannot complete under the
    ceiling, :class:`AdmissionError` is raised instead, so no receipt ever claims more than
    the run actually observed.
    """

    active = ProductionAdmissionServices() if services is None else services
    collect = Deadline.start(clock, ADMISSION_BUDGET_SECONDS, reserve=FINALIZATION_RESERVE_SECONDS)
    finalize = collect.finalization()
    started_at = _utc_now()
    evidence = _Evidence(run_identity=new_run_identity(started_at))
    failure: AdmissionFailure | None = None
    try:
        _collect(request, active, collect, evidence)
    except AdmissionError as exc:
        failure = exc.failure
        evidence.issues.append(_issue(request, failure.code, failure.detail))
    status = "pass" if failure is None else failure.status
    with _translated("incomplete", "admission_deadline_exceeded"):
        finalize.check("cleanup")
    status = _cleanup(request, active, evidence, status)
    _capture_final_production_identity(request, active, evidence, failure)
    status = _bracket_cleanup(request, evidence, status)
    receipt = _build_receipt(
        request, active, evidence, status=status, started_at=started_at, failure=failure, deadline=finalize
    )
    with _translated("incomplete", "admission_deadline_exceeded"):
        finalize.check("publish_receipt")
    _publish_receipt(request, evidence, receipt)
    return receipt


def _collect(
    request: AdmissionRequest, services: AdmissionServices, deadline: Deadline, evidence: _Evidence
) -> None:
    """Perform every external step in canonical order under the monotonic ceiling."""

    evidence.evaluator = _external(
        deadline,
        "capture_evaluator_identity",
        "incomplete",
        "evaluator_identity_capture_failed",
        lambda: services.capture_evaluator_identity(deadline),
    )
    evidence.bootstrap = _external(
        deadline,
        "capture_bootstrap_environment",
        "incomplete",
        "bootstrap_environment_capture_failed",
        services.capture_bootstrap_environment,
    )
    evidence.production_identity_before = _external(
        deadline,
        "capture_production_identity_before",
        "incomplete",
        "production_identity_capture_failed",
        lambda: services.capture_production_identity(request.repo_root),
    )
    evidence.identity = evaluation_identity(
        request, evidence.production_identity_before, evidence.evaluator
    )
    evidence.evaluation_root = request.artifact_root / evidence.identity

    # The first capture happens *before* any Phase 1 setup operation, so the delta brackets
    # the candidate resolution and the runtime preparation as well as the quiet window.
    evidence.manifests_before = _external(
        deadline,
        "capture_corpus_before",
        "incomplete",
        "corpus_capture_failed",
        lambda: services.capture_corpus(deadline),
    )

    if evidence.resolutions:  # pragma: no cover - structural guard
        raise _fail("incomplete", "repeated_candidate_resolution", "the candidate lock may be resolved only once")
    evidence.resolutions += 1
    lock_request = _lock_request(request, evidence.evaluation_root)
    evidence.candidate_lock = _external(
        deadline,
        "compile_candidate_lock",
        "incomplete",
        "candidate_resolution_failed",
        lambda: services.compile_candidate_lock(lock_request, deadline),
    )

    runtime_request = _runtime_request(request, evidence.evaluation_root)
    lock = evidence.candidate_lock
    evidence.runtime = _external(
        deadline,
        "prepare_candidate_runtime",
        "incomplete",
        "runtime_preparation_failed",
        lambda: services.prepare_candidate_runtime(lock, runtime_request, deadline),
    )
    evidence.runtime_binding = _bind_runtime(services, deadline, lock, evidence.runtime)

    evidence.manifests_after = _external(
        deadline,
        "capture_corpus_after",
        "incomplete",
        "corpus_capture_failed",
        lambda: services.capture_corpus(deadline),
    )
    evidence.manifests_after, evidence.write_deltas = _write_deltas(
        evidence.manifests_before, evidence.manifests_after
    )
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


def _bind_runtime(
    services: AdmissionServices, deadline: Deadline, lock: CandidateLock, runtime: CandidateRuntime
) -> RuntimeBinding:
    """Recompute the candidate runtime manifest digest from disk and bind the receipt to it."""

    observed = _external(
        deadline,
        "verify_runtime_manifest",
        "incomplete",
        "runtime_manifest_verification_failed",
        lambda: services.runtime_manifest_digest(runtime.root),
    )
    if observed != runtime.manifest_sha256:
        raise _fail(
            "hold",
            "runtime_manifest_changed",
            f"the published runtime manifest below {runtime.root} is {observed}, "
            f"not the prepared {runtime.manifest_sha256}",
        )
    with _translated("incomplete", "runtime_manifest_verification_failed"):
        return RuntimeBinding(
            root=str(runtime.root),
            lock_digest=lock.digest,
            manifest_path=str(runtime.manifest_path),
            manifest_sha256=observed,
        )


def _external[T](deadline: Deadline, step: str, status: str, code: str, call: Callable[[], T]) -> T:
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check(f"{step}:before")
    with _translated(status, code):
        result = call()
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check(f"{step}:after")
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
) -> tuple[tuple[RootManifest, ...], tuple[WriteDelta, ...]]:
    """Enrich, then pair, the two canonical manifest collections root by root.

    The returned after manifests are the *enriched* ones, so each delta's
    ``after_manifest_digest`` names exactly the evidence the receipt publishes.
    """

    before_by_root = {manifest.root: manifest for manifest in before}
    after_by_root = {manifest.root: manifest for manifest in after}
    if not before_by_root or set(before_by_root) != set(after_by_root):
        raise _fail(
            "incomplete",
            "unstable_corpus_root",
            f"corpus roots changed between captures: before={sorted(before_by_root)} after={sorted(after_by_root)}",
        )
    enriched: list[RootManifest] = []
    deltas: list[WriteDelta] = []
    for root in sorted(before_by_root):
        with _translated("incomplete", "unstable_corpus_root"):
            manifest = enrich_after_manifest(before_by_root[root], after_by_root[root])
            enriched.append(manifest)
            deltas.append(compare_root_manifests(before_by_root[root], manifest))
    return tuple(enriched), tuple(deltas)


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
        summary = services.cleanup(evidence.evaluation_root, evidence.run_identity, status)
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

    This capture is not optional: it is the only reading taken after the last thing this run
    could have changed, so a run that cannot take it has no honest ``after`` side to publish
    and fails closed instead.
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
    """Compare the pre-work identity with the post-cleanup one; drift can never pass."""

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
    deadline: Deadline,
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
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("artifact_tree_digest:before")
    digest = services.artifact_tree_digest(evidence.evaluation_root, deadline)
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("artifact_tree_digest:after")
    runtime = evidence.runtime
    with _translated("incomplete", "untrustworthy_admission_evidence"):
        return AdmissionReceipt(
            schema_version=ADMISSION_RECEIPT_SCHEMA_VERSION,
            evaluation_contract_version=EVALUATION_CONTRACT_VERSION,
            evaluation_identity=evidence.identity,
            run_identity=evidence.run_identity,
            status=status,
            started_at=started_at,
            ended_at=_utc_now(),
            budgets=default_phase_budgets(),
            evaluator=evidence.evaluator,
            bootstrap_environment=evidence.bootstrap,
            runtime_binding=evidence.runtime_binding,
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
    """Write this run's receipt exclusively and durably, never replacing another run's.

    The temporary is created with ``O_EXCL`` and linked -- not renamed -- onto the canonical
    per-run name, so an existing receipt is never overwritten even by an identical run
    identity.  Publication holds the per-identity ``O_NOFOLLOW`` lock.
    """

    assert evidence.evaluation_root is not None
    payload = canonical_json(receipt.to_dict())
    temporary = _receipt_temporary_name(receipt.run_identity)
    final = f"{receipt.run_identity}.json"
    evaluation_fd = _open_evaluation_directory(request.repo_root, evidence.evaluation_root)
    try:
        with _publication_lock(evaluation_fd, evidence.evaluation_root):
            receipts_fd = _open_owned_child(evaluation_fd, RECEIPTS_DIR_NAME, evidence.evaluation_root)
            try:
                _replace_temporary(receipts_fd, evidence.evaluation_root, temporary)
                file_fd = os.open(temporary, _CREATE_FLAGS, _FILE_MODE, dir_fd=receipts_fd)
                try:
                    os.fchmod(file_fd, _FILE_MODE)
                    _write_all(file_fd, payload, evidence.evaluation_root)
                    os.fsync(file_fd)
                finally:
                    os.close(file_fd)
                try:
                    os.link(temporary, final, src_dir_fd=receipts_fd, dst_dir_fd=receipts_fd, follow_symlinks=False)
                except FileExistsError as exc:
                    raise _fail(
                        "incomplete",
                        "receipt_publication_failed",
                        f"a receipt for run {receipt.run_identity} already exists and is immutable",
                    ) from exc
                except OSError as exc:
                    raise _fail(
                        "incomplete",
                        "receipt_publication_failed",
                        f"cannot publish the receipt below {evidence.evaluation_root}: {exc}",
                    ) from exc
                os.fsync(receipts_fd)
                _replace_temporary(receipts_fd, evidence.evaluation_root, temporary)
                os.fsync(receipts_fd)
            finally:
                os.close(receipts_fd)
    finally:
        os.close(evaluation_fd)
    return admission_receipt_path(request.artifact_root, receipt.evaluation_identity, receipt.run_identity)


@contextmanager
def _publication_lock(evaluation_fd: int, evaluation_root: Path) -> Iterator[None]:
    try:
        fd = os.open(
            PUBLICATION_LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            _FILE_MODE,
            dir_fd=evaluation_fd,
        )
    except OSError as exc:
        raise _fail(
            "incomplete",
            "receipt_publication_failed",
            f"cannot open the publication lock below {evaluation_root}: {exc}",
        ) from exc
    try:
        os.fchmod(fd, _FILE_MODE)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    except OSError as exc:
        raise _fail(
            "incomplete", "receipt_publication_failed", f"cannot lock {evaluation_root}: {exc}"
        ) from exc
    finally:
        os.close(fd)


def _replace_temporary(dir_fd: int, evaluation_root: Path, temporary: str) -> None:
    try:
        os.unlink(temporary, dir_fd=dir_fd)
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
            child = _open_owned_child(dir_fd, part, evaluation_root)
            os.close(dir_fd)
            dir_fd = child
    except BaseException:
        os.close(dir_fd)
        raise
    return dir_fd


def _open_owned_child(parent_fd: int, name: str, evaluation_root: Path) -> int:
    try:
        os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise _fail(
            "incomplete", "receipt_publication_failed", f"cannot create artifact component {name!r}: {exc}"
        ) from exc
    try:
        child = os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise _fail(
            "incomplete",
            "receipt_publication_failed",
            f"artifact component {name!r} must be an evaluation-owned directory: {exc}",
        ) from exc
    # ``mkdir`` is masked by the ambient umask; a service-owned directory is always 0700.
    try:
        os.fchmod(child, _DIRECTORY_MODE)
    except OSError as exc:
        os.close(child)
        raise _fail(
            "incomplete",
            "receipt_publication_failed",
            f"cannot own artifact component {name!r} below {evaluation_root}: {exc}",
        ) from exc
    return child


# --- issues -------------------------------------------------------------------------


def _issue(request: AdmissionRequest, code: str, detail: str) -> str:
    return f"{code}: {_sanitize(detail, request.declared_roots)}"


def _sanitize(detail: str, declared_roots: tuple[str, ...]) -> str:
    """Redact every absolute path outside the declared roots and bound the sample length.

    Containment is by path *component*, not by string prefix, so an undeclared sibling such
    as ``/data/ms-swift-secret`` is redacted even though ``/data/ms-swift`` is declared.
    """

    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        declared = any(token == root or token.startswith(f"{root}/") for root in declared_roots)
        return token if declared else _REDACTED_PATH

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
    controls = sum(len(delta.control_changes) for delta in receipt.write_deltas)
    evaluator = receipt.evaluator
    binding = receipt.runtime_binding
    lines = [
        f"status={receipt.status}",
        f"evaluation_contract_version={receipt.evaluation_contract_version}",
        f"schema_version={receipt.schema_version}",
        f"evaluation_identity={receipt.evaluation_identity}",
        f"run_identity={receipt.run_identity}",
        f"receipt={admission_receipt_path(request.artifact_root, receipt.evaluation_identity, receipt.run_identity)}",
        f"started_at={receipt.started_at}",
        f"ended_at={receipt.ended_at}",
        f"evaluator_source_digest={'-' if evaluator is None else evaluator.source_digest}",
        f"evaluator_source_commit={'-' if evaluator is None else evaluator.source_commit}",
        f"evaluator_source_clean={'-' if evaluator is None else evaluator.source_clean}",
        f"host_python={'-' if evaluator is None else evaluator.host_python_realpath}",
        f"candidate_lock_digest={receipt.candidate_lock.digest}",
        f"candidate_versions={','.join(f'{p.name}=={p.version}' for p in receipt.candidate_lock.candidates)}",
        f"runtime_root={'-' if binding is None else binding.root}",
        f"runtime_manifest_sha256={'-' if binding is None else binding.manifest_sha256}",
        f"artifact_tree_digest={receipt.artifact_tree_digest}",
        f"production_build_identity_before={receipt.production_identity_before.build_identity}",
        f"production_build_identity_after={receipt.production_identity_after.build_identity}",
        f"production_dependency_lock_before={receipt.production_identity_before.dependency_lock_digest}",
        f"production_dependency_lock_after={receipt.production_identity_after.dependency_lock_digest}",
        f"environments={','.join(identity.name for identity in receipt.environments)}",
        f"service_configs={','.join(identity.backend for identity in receipt.service_configs)}",
        f"root_manifests={len(receipt.root_manifests_before)}",
        f"corpus_in_scope_paths={sum(manifest.in_scope_count for manifest in receipt.root_manifests_before)}",
        f"corpus_excluded_paths={sum(manifest.excluded_count for manifest in receipt.root_manifests_before)}",
        f"unexpected_write_paths={unexpected}",
        f"manifest_control_changes={controls}",
        f"next_action={receipt.next_action}",
    ]
    lines.extend(f"issue={issue}" for issue in receipt.issues)
    return tuple(lines)


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
