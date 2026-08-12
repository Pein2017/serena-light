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
the pre-import evaluator-image capture and child startup, resolution, runtime preparation,
both corpus captures, cleanup, the final production
identity, the artifact digest, and receipt publication -- *including* the waiting and the
I/O publication itself performs.  Collection stops early enough to leave a reserved
finalization window, so a run that reaches the ceiling can still publish a trustworthy
timeout receipt; finalization itself is checked against the same absolute ceiling and fails
closed, without a receipt, rather than publishing evidence it could not complete.  Every
subprocess receives the remaining time and has its process group killed on expiry, and every
lock is acquired non-blockingly against the same deadline, so no step can wait past the
ceiling on another process.  Cleanup is not an exception: it receives the same deadline and
checks it around each of its own syscalls.

**Immutable per-execution receipts.**  Each execution has its own ``run_identity`` and
publishes to ``receipts/<run-identity>.json`` with an exclusive link, so a repeated or
concurrent run can never delete or replace another run's receipt.  Publication is
serialized on a per-identity ``O_NOFOLLOW`` lock, writes the payload in deadline-checked
chunks, links only while a publication reserve remains, and re-observes the ceiling after
every later mutation and durability barrier -- including immediately before it returns --
withdrawing its own link on the first observed expiry.  A run that exceeded its budget never
returns a ``pass`` and leaves no receipt at the final path.  The enforcement is cooperative,
between syscalls: :func:`_publish_receipt` documents the one in-flight-``fsync`` window that
cannot be preempted, and why that window is not admitted evidence.

**Fail-closed statuses.**  ``pass`` requires equal production identity before and after
cleanup, one delta per root bound to both manifest digests, no unexpected path, no changed
manifest control, and a cleanup that neither failed nor had to remove anything -- plus the
evaluator, host, bootstrap-environment, and candidate-runtime bindings the receipt model
requires.  ``hold`` is a trustworthy observation of a violation.  ``incomplete`` is an
untrustworthy or unfinished observation.  A receipt is published only when its evidence is
trustworthy: without the production identity on both sides and the frozen candidate lock the
run raises instead of publishing a receipt that would understate what is unknown.
"""

# ruff: noqa: E402

from __future__ import annotations

# ``python -m scripts.backend_eval.admission`` reaches this guard before any evaluator
# semantic import below.  The parent is deliberately only a transport bootstrap: it freezes
# the complete evaluator package into one sealed zip image and runs ``main`` from that image.
# Regular imports (including tests) continue below and preserve the module's existing API.
import ctypes as _bootstrap_ctypes
import fcntl as _bootstrap_fcntl
import io as _bootstrap_io
import os as _bootstrap_os
import signal as _bootstrap_signal
import stat as _bootstrap_stat
import subprocess as _bootstrap_subprocess
import sys as _bootstrap_sys
import time as _bootstrap_time
import zipfile as _bootstrap_zipfile
from collections.abc import Mapping as _BootstrapMapping
from collections.abc import Sequence as _BootstrapSequence
from contextlib import suppress as _bootstrap_suppress
from pathlib import Path as _BootstrapPath

_SOURCE_IMAGE_ACTIVE_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE"
_SOURCE_IMAGE_FD_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_FD"
_SOURCE_IMAGE_PATH_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_PATH"
_SOURCE_IMAGE_OWNER_KEY = "SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT"
_SOURCE_IMAGE_STARTED_KEY = "SERENA_LIGHT_BACKEND_EVAL_STARTED_MONOTONIC"
_SOURCE_IMAGE_ACTIVE_VALUE = "1"
_EVALUATOR_BOOTSTRAP_SECONDS = 1800.0
_EVALUATOR_BOOTSTRAP_REAP_SECONDS = 20.0
_EVALUATOR_BOOTSTRAP_GRACE_SECONDS = 5.0
_BOOTSTRAP_DIRECTORY_FLAGS = _bootstrap_os.O_RDONLY | _bootstrap_os.O_DIRECTORY
_BOOTSTRAP_NOFOLLOW_DIRECTORY_FLAGS = _BOOTSTRAP_DIRECTORY_FLAGS | _bootstrap_os.O_NOFOLLOW
_BOOTSTRAP_READ_FLAGS = _bootstrap_os.O_RDONLY | _bootstrap_os.O_NOFOLLOW | _bootstrap_os.O_NONBLOCK
_BOOTSTRAP_MFD_FLAGS = 0x0001 | 0x0002  # MFD_CLOEXEC | MFD_ALLOW_SEALING
_BOOTSTRAP_ADD_SEALS = 1033
_BOOTSTRAP_GET_SEALS = 1034
_BOOTSTRAP_ALL_SEALS = 0x1 | 0x2 | 0x4 | 0x8
_BOOTSTRAP_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_BOOTSTRAP_ZIP_MODE = 0o100600 << 16
_IMAGE_MAIN = b"""\
import importlib
import os
import sys

owner_root, image_fd = sys.argv[1:3]
del sys.argv[1:3]
os.environ["SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE"] = "1"
os.environ["SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_FD"] = image_fd
os.environ["SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_PATH"] = sys.path[0]
os.environ["SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT"] = owner_root
module = importlib.import_module("scripts.backend_eval.admission")
raise SystemExit(module.main())
"""


class EvaluatorBootstrapError(RuntimeError):
    """The immutable evaluator image cannot be built or started."""


class EvaluatorBootstrapTimeout(EvaluatorBootstrapError):
    """The sealed evaluator exceeded its outer safety bound and was killed."""


def _command_owner_root() -> _BootstrapPath:
    """The checkout that supplied this command module, before semantic imports run."""

    return _BootstrapPath(_bootstrap_os.path.abspath(__file__)).parent.parent.parent


def _open_absolute_directory(path: _BootstrapPath) -> int:
    """Open every absolute component without following a substituted ancestor."""

    if not path.is_absolute():
        raise EvaluatorBootstrapError(f"the evaluator owner root must be absolute: {path}")
    fd = _open_filesystem_root(path)
    try:
        for part in path.parts[1:]:
            child = _bootstrap_os.open(part, _BOOTSTRAP_NOFOLLOW_DIRECTORY_FLAGS, dir_fd=fd)
            _bootstrap_os.close(fd)
            fd = child
        return fd
    except OSError as error:
        with _bootstrap_suppress(OSError):
            _bootstrap_os.close(fd)
        raise EvaluatorBootstrapError(f"cannot open evaluator owner root {path}: {error}") from error


def _open_filesystem_root(label: _BootstrapPath) -> int:
    """The one guarded pathname open from which the bootstrap confines every descendant."""

    try:
        return _bootstrap_os.open("/", _BOOTSTRAP_DIRECTORY_FLAGS)
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot open filesystem root for {label}: {error}") from error


def _open_relative_directory(parent_fd: int, parts: _BootstrapSequence[str], label: str) -> int:
    fd = _bootstrap_os.dup(parent_fd)
    try:
        for part in parts:
            child = _bootstrap_os.open(part, _BOOTSTRAP_NOFOLLOW_DIRECTORY_FLAGS, dir_fd=fd)
            _bootstrap_os.close(fd)
            fd = child
        return fd
    except OSError as error:
        _bootstrap_os.close(fd)
        raise EvaluatorBootstrapError(f"cannot open evaluator directory {label}: {error}") from error


def _read_relative_file(parent_fd: int, parts: _BootstrapSequence[str], label: str) -> bytes:
    directory = _open_relative_directory(parent_fd, parts[:-1], label)
    try:
        fd = _bootstrap_os.open(parts[-1], _BOOTSTRAP_READ_FLAGS, dir_fd=directory)
    except OSError as error:
        _bootstrap_os.close(directory)
        raise EvaluatorBootstrapError(f"cannot open evaluator source {label}: {error}") from error
    try:
        if not _bootstrap_stat.S_ISREG(_bootstrap_os.fstat(fd).st_mode):
            raise EvaluatorBootstrapError(f"evaluator source {label} must be a regular file")
        chunks: list[bytes] = []
        while chunk := _bootstrap_os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot read evaluator source {label}: {error}") from error
    finally:
        _bootstrap_os.close(fd)
        _bootstrap_os.close(directory)


def _zip_entry(name: str, payload: bytes) -> tuple[_bootstrap_zipfile.ZipInfo, bytes]:
    info = _bootstrap_zipfile.ZipInfo(name, _BOOTSTRAP_ZIP_TIME)
    info.compress_type = _bootstrap_zipfile.ZIP_STORED
    info.external_attr = _BOOTSTRAP_ZIP_MODE
    info.create_system = 3
    return info, payload


def _build_evaluator_source_image(owner_root: _BootstrapPath) -> bytes:
    """Read the complete evaluator closure once and pack those exact bytes deterministically."""

    if not owner_root.is_absolute():
        raise EvaluatorBootstrapError(f"the evaluator owner root must be absolute: {owner_root}")
    owner_fd = _open_absolute_directory(owner_root)
    package_fd = _open_relative_directory(owner_fd, ("scripts", "backend_eval"), "scripts/backend_eval")
    try:
        try:
            names = sorted(
                entry.name
                for entry in _bootstrap_os.scandir(package_fd)
                if entry.name.endswith(".py")
            )
        except OSError as error:
            raise EvaluatorBootstrapError(f"cannot enumerate evaluator source closure: {error}") from error
        if not names:
            raise EvaluatorBootstrapError("the evaluator source closure is empty")
        entries = [
            _zip_entry("__main__.py", _IMAGE_MAIN),
            _zip_entry(
                "scripts/__init__.py",
                _read_relative_file(owner_fd, ("scripts", "__init__.py"), "scripts/__init__.py"),
            ),
        ]
        entries.extend(
            _zip_entry(
                f"scripts/backend_eval/{name}",
                _read_relative_file(
                    owner_fd,
                    ("scripts", "backend_eval", name),
                    f"scripts/backend_eval/{name}",
                ),
            )
            for name in names
        )
    finally:
        _bootstrap_os.close(package_fd)
        _bootstrap_os.close(owner_fd)
    buffer = _bootstrap_io.BytesIO()
    with _bootstrap_zipfile.ZipFile(buffer, "w") as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    return buffer.getvalue()


def _sealed_evaluator_image(payload: bytes) -> int:
    fd = -1
    try:
        handle = _bootstrap_ctypes.CDLL(None, use_errno=True)
        if not hasattr(handle, "memfd_create"):
            raise EvaluatorBootstrapError("this platform cannot provide a sealed evaluator image")
        create = handle.memfd_create
        create.argtypes = [_bootstrap_ctypes.c_char_p, _bootstrap_ctypes.c_uint]
        create.restype = _bootstrap_ctypes.c_int
        fd = create(b"backend-eval-source-image", _BOOTSTRAP_MFD_FLAGS)
        if fd < 0:
            raise OSError(
                _bootstrap_ctypes.get_errno(),
                _bootstrap_os.strerror(_bootstrap_ctypes.get_errno()),
            )
        written = 0
        while written < len(payload):
            written += _bootstrap_os.write(fd, payload[written:])
        if (
            _bootstrap_fcntl.fcntl(fd, _BOOTSTRAP_ADD_SEALS, _BOOTSTRAP_ALL_SEALS) != 0
            or _bootstrap_fcntl.fcntl(fd, _BOOTSTRAP_GET_SEALS) != _BOOTSTRAP_ALL_SEALS
            or _bootstrap_os.pread(fd, len(payload) + 1, 0) != payload
        ):
            raise EvaluatorBootstrapError("cannot seal the evaluator source image")
        return fd
    except (AttributeError, OSError) as error:
        if fd >= 0:
            with _bootstrap_suppress(OSError):
                _bootstrap_os.close(fd)
        raise EvaluatorBootstrapError(f"cannot build the sealed evaluator source image: {error}") from error


def _kill_evaluator_group(process: _bootstrap_subprocess.Popen[bytes]) -> None:
    try:
        _bootstrap_os.killpg(_bootstrap_os.getpgid(process.pid), _bootstrap_signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.communicate(timeout=_EVALUATOR_BOOTSTRAP_REAP_SECONDS)
    except _bootstrap_subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL always reaps
        process.kill()
        process.wait(timeout=_EVALUATOR_BOOTSTRAP_REAP_SECONDS)


def _run_sealed_evaluator(
    owner_root: _BootstrapPath,
    argv: _BootstrapSequence[str],
    *,
    timeout: float,
    environ: _BootstrapMapping[str, str],
) -> tuple[int, bytes, bytes]:
    """Execute admission from one sealed source image with exact stdout/exit passthrough."""

    started = _bootstrap_time.monotonic()
    image = _build_evaluator_source_image(owner_root)
    remaining = timeout - (_bootstrap_time.monotonic() - started)
    if remaining <= 0:
        raise EvaluatorBootstrapTimeout("the evaluator source image exhausted the command deadline")
    image_fd = _sealed_evaluator_image(image)
    child_environment = dict(environ)
    child_environment[_SOURCE_IMAGE_STARTED_KEY] = repr(started)
    command = (
        _bootstrap_sys.executable,
        "-I",
        "-B",
        f"/proc/self/fd/{image_fd}",
        str(owner_root),
        str(image_fd),
        *argv,
    )
    try:
        process = _bootstrap_subprocess.Popen(
            command,
            cwd=owner_root,
            env=child_environment,
            stdin=_bootstrap_subprocess.DEVNULL,
            stdout=_bootstrap_subprocess.PIPE,
            stderr=_bootstrap_subprocess.PIPE,
            pass_fds=(image_fd,),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except _bootstrap_subprocess.TimeoutExpired as error:
            _kill_evaluator_group(process)
            raise EvaluatorBootstrapTimeout(
                f"the sealed evaluator exceeded its {timeout:g}s outer bound and its process group was killed"
            ) from error
        except BaseException:
            _kill_evaluator_group(process)
            raise
        return process.returncode, stdout, stderr
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot start the sealed evaluator: {error}") from error
    finally:
        _bootstrap_os.close(image_fd)


def _bootstrap_command() -> int:
    try:
        returncode, stdout, stderr = _run_sealed_evaluator(
            _command_owner_root(),
            tuple(_bootstrap_sys.argv[1:]),
            timeout=_EVALUATOR_BOOTSTRAP_SECONDS + _EVALUATOR_BOOTSTRAP_GRACE_SECONDS,
            environ=_bootstrap_os.environ,
        )
    except EvaluatorBootstrapError as error:
        _bootstrap_sys.stdout.write("status=incomplete\n")
        _bootstrap_sys.stdout.write(f"issue=evaluator_bootstrap_failed: {error}\n")
        _bootstrap_sys.stdout.write("next_action=hold\n")
        return 2
    _bootstrap_sys.stdout.buffer.write(stdout)
    _bootstrap_sys.stderr.buffer.write(stderr)
    return returncode


if __name__ == "__main__":
    raise SystemExit(_bootstrap_command())

import argparse
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
from typing import NoReturn, Protocol

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
    acquire_exclusive_lock,
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
from scripts.backend_eval.source_binding import HelperExpectation, SourceBindingError
from scripts.backend_eval.source_image import SourceImageError, source_image_active, source_image_started
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
    "PUBLICATION_RESERVE_SECONDS",
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
# The window the atomic link and its directory ``fsync`` must still have in front of them.
PUBLICATION_RESERVE_SECONDS = 5.0
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
# One receipt is tens of megabytes; writing it in chunks keeps the ceiling observable.
_WRITE_CHUNK_BYTES = 4 * 1024 * 1024
# A regular file's read behaviour is unaffected by O_NONBLOCK; a FIFO or other blocking
# special node left in the tree by a race or a swap returns immediately instead of hanging
# the guarded open, so the fstat regular-file check below can refuse it promptly.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
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

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: HelperExpectation
    ) -> ProductionIdentity: ...

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity: ...

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity: ...

    def compile_candidate_lock(
        self, request: CandidateLockRequest, deadline: Deadline, expectation: HelperExpectation
    ) -> CandidateLock: ...

    def prepare_candidate_runtime(
        self,
        lock: CandidateLock,
        request: RuntimeRequest,
        deadline: Deadline,
        expectation: HelperExpectation,
    ) -> CandidateRuntime: ...

    def capture_corpus(
        self, deadline: Deadline, expectation: HelperExpectation
    ) -> tuple[RootManifest, ...]: ...

    def runtime_manifest_digest(self, root: Path) -> str: ...

    def artifact_tree_digest(self, owner_root: Path, evaluation_root: Path, deadline: Deadline) -> str: ...

    def cleanup(
        self,
        owner_root: Path,
        evaluation_root: Path,
        run_identity: str,
        stage: str,
        deadline: Deadline,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class ProductionAdmissionServices:
    """Bind the admission orchestration to the real Task 1-5 implementations.

    ``runtime_permission_repairs`` is the one piece of state this binding keeps: the
    harness-owned runtime files whose mode the serialized reuse had to correct.  The repair
    itself is verified inside the runtime lock -- a runtime that still violates the contract
    is never returned, so no run can pass on one -- and this list only carries the record out
    to the command's summary.
    """

    runtime_permission_repairs: list[str] = field(default_factory=list)

    def capture_production_identity(
        self, repo_root: Path, deadline: Deadline, expectation: HelperExpectation
    ) -> ProductionIdentity:
        return capture_production_identity(repo_root, expectation=expectation, deadline=deadline)

    def capture_evaluator_identity(self, deadline: Deadline) -> EvaluatorIdentity:
        return capture_evaluator_identity(deadline=deadline)

    def capture_bootstrap_environment(self) -> BootstrapEnvironmentIdentity:
        return bootstrap_environment_identity()

    def compile_candidate_lock(
        self, request: CandidateLockRequest, deadline: Deadline, expectation: HelperExpectation
    ) -> CandidateLock:
        return compile_candidate_lock(request, expectation=expectation, deadline=deadline)

    def prepare_candidate_runtime(
        self,
        lock: CandidateLock,
        request: RuntimeRequest,
        deadline: Deadline,
        expectation: HelperExpectation,
    ) -> CandidateRuntime:
        runtime = prepare_candidate_runtime(lock, request, expectation=expectation, deadline=deadline)
        self.runtime_permission_repairs.extend(runtime.permission_repairs)
        return runtime

    def capture_corpus(
        self, deadline: Deadline, expectation: HelperExpectation
    ) -> tuple[RootManifest, ...]:
        return freeze_default_corpus(expectation=expectation, deadline=deadline)

    def runtime_manifest_digest(self, root: Path) -> str:
        return runtime_manifest_digest(root)

    def artifact_tree_digest(self, owner_root: Path, evaluation_root: Path, deadline: Deadline) -> str:
        return artifact_tree_digest(
            owner_root, evaluation_root, check=lambda: deadline.check("artifact_tree_digest")
        )

    def cleanup(
        self,
        owner_root: Path,
        evaluation_root: Path,
        run_identity: str,
        stage: str,
        deadline: Deadline,
    ) -> tuple[str, ...]:
        """Remove exactly the evaluation-owned partial state this module can create.

        The frozen candidate lock, the prepared runtime, and every *other* execution's
        receipt are durable evidence owned elsewhere and are never removed here; the only
        partial state admission itself can leave behind is this run's own interrupted
        receipt temporary.

        **The receipts directory is reached component by component, never by pathname.**
        ``os.open(evaluation_root / "receipts", O_NOFOLLOW)`` guards only the last component,
        so a symlinked ancestor -- ``.admission-artifacts``, ``backend-eval``, or the
        evaluation-identity directory itself -- redirected this unlink outside the evaluation
        root entirely; that escape was reproduced against a decoy outside the root before it
        was closed.  The walk now starts at the declared owner root's own descriptor and opens
        every component from its parent with ``O_NOFOLLOW | O_DIRECTORY``, so no swapped
        ancestor can move the target.  Per-run temporary-name ownership is unchanged: the only
        name unlinked is this run's own.

        Cleanup runs inside the phase ceiling like every other step: it receives the same
        monotonic deadline and checks it cooperatively around each syscall, so a slow or
        wedged filesystem stops the run rather than carrying it past its budget.
        """

        del stage
        deadline.check("cleanup:open")
        relative = _owned_relative_parts(owner_root, evaluation_root, "cleanup_failed")
        dir_fd = _open_owned_walk(
            owner_root, (*relative, RECEIPTS_DIR_NAME), "cleanup_failed", deadline, "cleanup:walk"
        )
        if dir_fd is None:
            return ()
        temporary = _receipt_temporary_name(run_identity)
        try:
            deadline.check("cleanup:unlink")
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
            deadline.check("cleanup:sync")
            os.fsync(dir_fd)
            deadline.check("cleanup:synced")
        finally:
            os.close(dir_fd)
        return ("removed_temporary_receipt",)


# --- the artifact tree digest ----------------------------------------------------------


def artifact_tree_digest(
    owner_root: Path, artifact_root: Path, *, check: Check = _noop_check
) -> str:
    """Digest the evaluation-owned artifact tree by content, refusing anything unhashable.

    **The root itself is acquired component by component.**  Opening the whole absolute
    ``artifact_root`` under one ``O_NOFOLLOW`` guarded only its last component, so a swapped
    intermediate directory made this function digest another tree and publish that digest as
    the run's admitted evidence.  Acquisition now walks out from the declared owner root's own
    descriptor, ``O_NOFOLLOW | O_DIRECTORY`` on every component, so a substituted ancestor
    fails closed instead of redirecting the evidence.

    The traversal below it is descriptor relative and ``O_NOFOLLOW`` throughout: every
    directory is opened from its parent's descriptor, and every file is validated and read
    through *one* descriptor, so no path is ever reopened after its type was checked.  Only
    regular files are recorded, by relative path, size, and SHA-256; the resolver cache, the
    receipts directory, and the publication lock are excluded.  A symlink or special file
    anywhere below the root fails closed rather than being silently skipped.
    """

    entries: list[dict[str, object]] = []
    relative = _owned_relative_parts(owner_root, artifact_root, "artifact_digest_failed")
    root_fd = _open_owned_walk(owner_root, relative, "artifact_digest_failed", None, "artifact_tree_digest")
    if root_fd is None:
        return sha256_bytes(canonical_json({"entries": entries}))
    try:
        _collect_artifact_entries(root_fd, "", artifact_root, entries, check, top_level=True)
    finally:
        os.close(root_fd)
    entries.sort(key=lambda entry: str(entry["path"]))
    return sha256_bytes(canonical_json({"entries": entries}))


def _owned_relative_parts(owner_root: Path, target: Path, code: str) -> tuple[str, ...]:
    """The components between the declared owner root and one path it must contain."""

    try:
        relative = target.relative_to(owner_root)
    except ValueError as exc:
        raise _fail("incomplete", code, f"{target} is not below the declared owner root {owner_root}") from exc
    parts = tuple(part for part in relative.parts if part not in ("", "."))
    if ".." in parts:
        raise _fail("incomplete", code, f"{target} is not an owned path below {owner_root}")
    return parts


def _open_declared_root(owner_root: Path, code: str) -> int | None:
    """Open the caller-declared owner root itself, the one open with no parent to walk from.

    This is a *guarded* open, not a confined one, and the ownership document says so: the
    declared root is where confinement starts, so nothing above it has been proven.
    ``O_DIRECTORY`` still refuses a non-directory before any type-specific open handler runs,
    and every component below is opened from this descriptor.
    """

    try:
        return os.open(owner_root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _fail("incomplete", code, f"cannot open the declared owner root {owner_root}: {exc}") from exc


def _open_owned_walk(
    owner_root: Path, parts: tuple[str, ...], code: str, deadline: Deadline | None, step: str
) -> int | None:
    """Open one owned directory by walking every component from the owner root's descriptor.

    ``None`` means the directory does not exist, which every caller here treats as "there was
    nothing of ours to act on".  Anything else -- a symlinked component, a non-directory, a
    permission failure -- is a typed failure rather than a silent redirection.
    """

    if deadline is not None:
        deadline.check(step)
    current = _open_declared_root(owner_root, code)
    if current is None:
        return None
    walked = owner_root
    try:
        for part in parts:
            if deadline is not None:
                deadline.check(step)
            walked = walked / part
            try:
                child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=current)
            except FileNotFoundError:
                os.close(current)
                return None
            except OSError as exc:
                raise _fail(
                    "incomplete",
                    code,
                    f"cannot open the evaluation-owned component {walked} without following a link: {exc}",
                ) from exc
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


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
    expectation: HelperExpectation | None = None
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
    except SourceBindingError as exc:
        raise _fail("hold", "evaluator_source_binding_failed", str(exc)) from exc
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

    if services is None and not source_image_active():
        raise _fail(
            "incomplete",
            "unsealed_evaluator_entrypoint",
            "production admission must run through python -m scripts.backend_eval.admission "
            "so its complete evaluator closure executes from one sealed source image",
        )
    active = ProductionAdmissionServices() if services is None else services
    try:
        bootstrap_started = source_image_started() if clock is monotonic_clock else None
    except SourceImageError as exc:
        raise _fail("incomplete", "evaluator_source_binding_failed", str(exc)) from exc
    collect = (
        Deadline.start(clock, ADMISSION_BUDGET_SECONDS, reserve=FINALIZATION_RESERVE_SECONDS)
        if bootstrap_started is None
        else Deadline(
            clock=clock,
            seconds=ADMISSION_BUDGET_SECONDS,
            started=bootstrap_started,
            reserve=FINALIZATION_RESERVE_SECONDS,
        )
    )
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
    status = _cleanup(request, active, evidence, status, finalize)
    _capture_final_production_identity(request, active, evidence, failure, finalize)
    status = _bracket_cleanup(request, evidence, status)
    status = _bracket_evaluator_identity(request, active, evidence, status, finalize)
    receipt = _build_receipt(
        request, active, evidence, status=status, started_at=started_at, failure=failure, deadline=finalize
    )
    with _translated("incomplete", "admission_deadline_exceeded"):
        finalize.check("publish_receipt")
    _publish_receipt(request, evidence, receipt, finalize)
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
    # The expectation is derived from the identity that was just captured, before any child
    # has run, and is then the only thing that decides which bytes any child may execute.
    with _translated("hold", "evaluator_source_binding_failed"):
        evidence.expectation = HelperExpectation.from_identity(evidence.evaluator)
    expectation = evidence.expectation
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
        lambda: services.capture_production_identity(request.repo_root, deadline, expectation),
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
        lambda: services.capture_corpus(deadline, expectation),
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
        lambda: services.compile_candidate_lock(lock_request, deadline, expectation),
    )

    runtime_request = _runtime_request(request, evidence.evaluation_root)
    lock = evidence.candidate_lock
    evidence.runtime = _external(
        deadline,
        "prepare_candidate_runtime",
        "incomplete",
        "runtime_preparation_failed",
        lambda: services.prepare_candidate_runtime(lock, runtime_request, deadline, expectation),
    )
    evidence.runtime_binding = _bind_runtime(services, deadline, lock, evidence.runtime)

    evidence.manifests_after = _external(
        deadline,
        "capture_corpus_after",
        "incomplete",
        "corpus_capture_failed",
        lambda: services.capture_corpus(deadline, expectation),
    )
    evidence.manifests_after, evidence.write_deltas = _write_deltas(
        evidence.manifests_before, evidence.manifests_after, expectation, deadline
    )
    _require_no_unexpected_writes(evidence.write_deltas)

    evidence.production_identity_after = _external(
        deadline,
        "capture_production_identity_after",
        "incomplete",
        "production_identity_capture_failed",
        lambda: services.capture_production_identity(request.repo_root, deadline, expectation),
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
    before: tuple[RootManifest, ...],
    after: tuple[RootManifest, ...],
    expectation: HelperExpectation,
    deadline: Deadline,
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
            manifest = enrich_after_manifest(
                before_by_root[root], after_by_root[root], expectation=expectation, deadline=deadline
            )
            enriched.append(manifest)
            deltas.append(compare_root_manifests(before_by_root[root], manifest))
    return tuple(enriched), tuple(deltas)


def _require_no_unexpected_writes(deltas: tuple[WriteDelta, ...]) -> None:
    try:
        assert_no_unexpected_writes(deltas)
    except WriteGuardError as exc:
        raise _fail("hold", "unexpected_evaluation_writes", str(exc)) from exc


def _cleanup(
    request: AdmissionRequest,
    services: AdmissionServices,
    evidence: _Evidence,
    status: str,
    deadline: Deadline,
) -> str:
    """Run the exact evaluation-owned cleanup; a dirty or failed cleanup can never pass.

    Cleanup is bracketed by the ceiling on both sides and receives the deadline itself, so
    an implementation that blocks, traverses, or otherwise spends the remaining budget stops
    the run.  A ceiling reached here is never downgraded to an issue on an otherwise passing
    receipt: it raises, and the run publishes nothing.
    """

    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("cleanup:before")
    if evidence.evaluation_root is None:
        return status
    try:
        summary = services.cleanup(
            request.repo_root, evidence.evaluation_root, evidence.run_identity, status, deadline
        )
    except DeadlineExceeded as exc:
        raise _fail("incomplete", "admission_deadline_exceeded", f"step=cleanup {exc}") from exc
    except AdmissionError as exc:
        evidence.issues.append(_issue(request, "cleanup_failed", exc.failure.detail))
        return "incomplete"
    except (OSError, RuntimeError, ValueError) as exc:
        evidence.issues.append(_issue(request, "cleanup_failed", str(exc)))
        return "incomplete"
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("cleanup:after")
    if summary:
        evidence.issues.append(_issue(request, "cleanup_removed_partial_state", ", ".join(sorted(summary))))
        return "incomplete"
    return status


def _capture_final_production_identity(
    request: AdmissionRequest,
    services: AdmissionServices,
    evidence: _Evidence,
    failure: AdmissionFailure | None,
    deadline: Deadline,
) -> None:
    """Capture the production identity the receipt publishes, after cleanup has run.

    This capture is not optional: it is the only reading taken after the last thing this run
    could have changed, so a run that cannot take it has no honest ``after`` side to publish
    and fails closed instead.
    """

    if evidence.production_identity_before is None or evidence.expectation is None:
        return
    try:
        evidence.production_identity_final = services.capture_production_identity(
            request.repo_root, deadline, evidence.expectation
        )
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


def _bracket_evaluator_identity(
    request: AdmissionRequest,
    services: AdmissionServices,
    evidence: _Evidence,
    status: str,
    deadline: Deadline,
) -> str:
    """Re-measure the evaluator after the last evaluation-owned action, before publication.

    The first capture is what every production-helper child was bound to; it happened before
    any of them ran.  Nothing after it re-read the evaluator's own bytes, so an evaluator
    module or a production helper edited late in the run -- after the last ordinary helper
    call, while the receipt was being assembled -- would have been published under an identity
    that no longer described the code on disk.  This capture closes that window: the identity
    is measured again after cleanup and the final production identity, and a receipt whose
    evaluator moved can never be a ``pass``.

    It is inside the same absolute ceiling as every other finalization step, and a capture
    that cannot be completed at all fails the run closed rather than publishing a receipt
    whose evaluator was never re-checked.
    """

    if evidence.evaluator is None:
        return status
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("recapture_evaluator_identity:before")
    try:
        observed = services.capture_evaluator_identity(deadline)
    except DeadlineExceeded as exc:
        raise _fail("incomplete", "admission_deadline_exceeded", f"step=recapture_evaluator_identity {exc}") from exc
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(
            "incomplete",
            "evaluator_identity_capture_failed",
            f"cannot re-measure the evaluator identity before publication: {exc}",
        ) from exc
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("recapture_evaluator_identity:after")
    if observed == evidence.evaluator:
        return status
    changed = _changed_identity_fields(evidence.evaluator, observed)
    evidence.issues.append(_issue(request, "evaluator_identity_changed", ", ".join(changed)))
    return "hold" if status == "pass" else status


def _changed_identity_fields(before: EvaluatorIdentity, after: EvaluatorIdentity) -> tuple[str, ...]:
    """Which parts of the evaluator identity moved, named without publishing their bytes."""

    fields = (
        "source_digest",
        "source_commit",
        "source_clean",
        "production_root",
        "production_digest",
        "production_clean",
        "host_python_path",
        "host_python_realpath",
        "host_python_sha256",
        "host_python_version",
    )
    changed = tuple(name for name in fields if getattr(before, name) != getattr(after, name))
    return changed or ("evaluator_identity",)


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
    digest = services.artifact_tree_digest(request.repo_root, evidence.evaluation_root, deadline)
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


def _publish_receipt(
    request: AdmissionRequest, evidence: _Evidence, receipt: AdmissionReceipt, deadline: Deadline
) -> Path:
    """Write this run's receipt exclusively and durably, never replacing another run's.

    The temporary is created with ``O_EXCL`` and linked -- not renamed -- onto the canonical
    per-run name, so an existing receipt is never overwritten even by an identical run
    identity.  Publication holds the per-identity ``O_NOFOLLOW`` lock.

    **Publication is inside the ceiling, not after it.**  A 39.7 MB receipt takes real time
    to serialize, write, ``fsync``, and link, and the publication lock can be held by another
    run.  Every one of those steps is therefore checked against the same absolute deadline:
    acquisition polls without blocking, the payload is written in deadline-checked chunks,
    and the atomic ``link`` runs only while a small publication reserve is still left.

    After the link, every remaining namespace mutation and every durability barrier is
    followed by a :class:`_Publication` checkpoint, including one immediately before this
    function returns, while withdrawal is still possible; only descriptor closes follow it,
    and they touch neither the namespace nor storage.  Any checkpoint that observes expiry
    withdraws this run's own link and fails, so no ``pass`` is ever returned after the
    ceiling and no final receipt is left behind once an overrun has been observed.

    **What that does and does not promise.**  The deadline is enforced *cooperatively*, at
    the boundaries between syscalls; a filesystem call already in flight is not preemptible.
    Between ``link`` returning and the next checkpoint -- that is, for as long as one
    in-flight ``fsync`` takes to complete -- the final name exists in the directory even if
    the ceiling has already passed.  That entry is withdrawn as soon as expiry is observed,
    and it is not admitted evidence: a consumer of this gate requires the command to have
    completed successfully and the receipt to verify canonically against its own digest, and
    an overrun run supplies neither.  This is a kernel boundary, not a guarantee of zero
    transient visibility.

    Immutability is unaffected: the only name ever unlinked is this run's own, which
    ``O_EXCL`` and the failing ``link`` prove no other run published.
    """

    assert evidence.evaluation_root is not None
    with _translated("incomplete", "admission_deadline_exceeded"):
        deadline.check("publish_receipt:serialize")
    payload = canonical_json(receipt.to_dict())
    temporary = _receipt_temporary_name(receipt.run_identity)
    final = f"{receipt.run_identity}.json"
    evaluation_fd = _open_evaluation_directory(request.repo_root, evidence.evaluation_root)
    try:
        with _publication_lock(evaluation_fd, evidence.evaluation_root, deadline):
            receipts_fd = _open_owned_child(evaluation_fd, RECEIPTS_DIR_NAME, evidence.evaluation_root)
            try:
                _replace_temporary(receipts_fd, evidence.evaluation_root, temporary)
                file_fd = os.open(temporary, _CREATE_FLAGS, _FILE_MODE, dir_fd=receipts_fd)
                try:
                    os.fchmod(file_fd, _FILE_MODE)
                    _write_all(file_fd, payload, evidence.evaluation_root, deadline)
                    os.fsync(file_fd)
                except BaseException:
                    # A half-written receipt is never left behind, not even under a dot name.
                    os.close(file_fd)
                    _replace_temporary(receipts_fd, evidence.evaluation_root, temporary)
                    _sync_directory(receipts_fd, evidence.evaluation_root)
                    raise
                else:
                    os.close(file_fd)
                _require_publication_window(receipts_fd, evidence.evaluation_root, temporary, deadline)
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
                # From here the final name exists.  Every remaining namespace mutation and
                # every durability barrier is followed by an expiry observation, and each
                # observation can still withdraw this run's own link, so no step after the
                # link can carry a published pass past the ceiling.
                published = _Publication(receipts_fd, evidence.evaluation_root, final, temporary, deadline)
                _sync_directory(receipts_fd, evidence.evaluation_root)
                published.checkpoint("link_synced")
                _replace_temporary(receipts_fd, evidence.evaluation_root, temporary)
                published.checkpoint("temporary_unlinked")
                _sync_directory(receipts_fd, evidence.evaluation_root)
                published.checkpoint("temporary_unlink_synced")
                # Immediately before returning, while withdrawal is still possible: only
                # descriptor closes remain, and they touch neither the namespace nor storage.
                published.checkpoint("return")
            finally:
                os.close(receipts_fd)
    finally:
        os.close(evaluation_fd)
    return admission_receipt_path(request.artifact_root, receipt.evaluation_identity, receipt.run_identity)


def _require_publication_window(
    receipts_fd: int, evaluation_root: Path, temporary: str, deadline: Deadline
) -> None:
    """Refuse to link a receipt that cannot be completed inside the ceiling."""

    if deadline.remaining() > PUBLICATION_RESERVE_SECONDS:
        return
    _replace_temporary(receipts_fd, evaluation_root, temporary)
    _sync_directory(receipts_fd, evaluation_root)
    raise _fail(
        "incomplete",
        "admission_deadline_exceeded",
        f"step=publish_receipt:link elapsed={deadline.elapsed():.3f}s budget={deadline.seconds:g}s "
        f"reserve={PUBLICATION_RESERVE_SECONDS:g}s; the receipt was not published",
    )


@dataclass(frozen=True, slots=True)
class _Publication:
    """One linked receipt this run owns, and the ceiling every later step is judged by.

    A checkpoint is the only thing that happens between two post-link operations.  It asks
    the same monotonic deadline whether the ceiling has arrived and, if it has, withdraws
    the very link this run created before failing, so a run that overran leaves no receipt
    and returns none.
    """

    receipts_fd: int
    evaluation_root: Path
    final: str
    temporary: str
    deadline: Deadline

    def checkpoint(self, step: str) -> None:
        if not self.deadline.expired():
            return
        self.withdraw(step)

    def withdraw(self, step: str) -> NoReturn:
        """Remove this run's own names -- and only its own -- then fail closed."""

        for name in (self.final, self.temporary):
            try:
                os.unlink(name, dir_fd=self.receipts_fd)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _fail(
                    "incomplete",
                    "receipt_publication_failed",
                    f"the ceiling was reached during publication and {name} below "
                    f"{self.evaluation_root} could not be withdrawn: {exc}",
                ) from exc
        _sync_directory(self.receipts_fd, self.evaluation_root)
        raise _fail(
            "incomplete",
            "admission_deadline_exceeded",
            f"step=publish_receipt:{step} elapsed={self.deadline.elapsed():.3f}s "
            f"budget={self.deadline.seconds:g}s; the receipt was withdrawn and none was published",
        )


def _sync_directory(dir_fd: int, evaluation_root: Path) -> None:
    """One durability barrier for a receipts-directory mutation.

    Named rather than inlined so that a test can make exactly this barrier slow and prove
    the checkpoint after it still refuses -- and withdraws -- a receipt that crossed the
    ceiling while the barrier was running.
    """

    try:
        os.fsync(dir_fd)
    except OSError as exc:
        raise _fail(
            "incomplete",
            "receipt_publication_failed",
            f"cannot synchronize the receipts directory below {evaluation_root}: {exc}",
        ) from exc


@contextmanager
def _publication_lock(evaluation_fd: int, evaluation_root: Path, deadline: Deadline) -> Iterator[None]:
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
        with _translated("incomplete", "admission_deadline_exceeded"):
            acquire_exclusive_lock(fd, deadline=deadline, step="publish_receipt:lock")
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


def _write_all(file_fd: int, payload: bytes, evaluation_root: Path, deadline: Deadline) -> None:
    """Write the payload in bounded chunks, checking the ceiling between each one."""

    written = 0
    while written < len(payload):
        with _translated("incomplete", "admission_deadline_exceeded"):
            deadline.check("publish_receipt:write")
        try:
            written += os.write(file_fd, payload[written : written + _WRITE_CHUNK_BYTES])
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
    active = ProductionAdmissionServices() if services is None else services
    try:
        receipt = run_admission(request, services=active, clock=clock)
    except AdmissionError as exc:
        print(f"status={exc.failure.status}")
        print(f"issue={_issue(request, exc.failure.code, exc.failure.detail)}")
        print(f"next_action={NEXT_ACTION_HOLD}")
        return 2
    repairs = tuple(active.runtime_permission_repairs) if isinstance(active, ProductionAdmissionServices) else ()
    for line in _summary(request, receipt, repairs):
        print(line)
    return 0 if receipt.status == "pass" else 2


def _summary(
    request: AdmissionRequest, receipt: AdmissionReceipt, runtime_permission_repairs: tuple[str, ...] = ()
) -> tuple[str, ...]:
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
        f"runtime_permission_repairs={','.join(sorted(runtime_permission_repairs)) or 'none'}",
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
