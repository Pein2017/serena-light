"""Candidate-lock command construction, parsing, artifact safety, and freeze idempotency."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from scripts.backend_eval import candidate_lock as candidate_lock_module
from scripts.backend_eval.candidate_lock import (
    ARTIFACT_ROOT_BASE_PARTS,
    CANDIDATE_NAMES,
    CANONICAL_REQUIREMENTS_BYTES,
    LOCK_FILE_NAME,
    LOCK_ROLLBACK_NAME,
    QUARANTINE_PREFIX,
    RECEIPT_FILE_NAME,
    RECEIPT_ROLLBACK_NAME,
    REQUIREMENTS_IN_NAME,
    TRANSACTION_MARKER_NAME,
    CandidateLockError,
    CandidateLockRequest,
    CommandResult,
    compile_candidate_lock,
    subprocess_runner,
)
from scripts.backend_eval.models import CandidateLock, ProductionIdentity, canonical_json, sha256_bytes
from scripts.backend_eval.production_identity import PRODUCTION_IDENTITY_FILES, ProductionIdentityChanged

_EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
_HASH_A = "1" * 64
_HASH_B = "2" * 64
_HASH_C = "3" * 64

# The pyrefly hashes are emitted out of order so the parser must canonicalize them.
_LOCK_BODY = (
    f"pyrefly==0.30.0 \\\n    --hash=sha256:{_HASH_B} \\\n    --hash=sha256:{_HASH_A}\n"
    f"ty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n"
)


@dataclass
class _FakeRunner:
    bodies: tuple[str, ...] = (_LOCK_BODY,)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    write_output: bool = True
    calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = field(default_factory=list)

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        tokens = tuple(command)
        self.calls.append((tokens, cwd, dict(env)))
        if self.write_output:
            body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
            Path(tokens[tokens.index("--output-file") + 1]).write_text(body, encoding="utf-8")
        return CommandResult(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


@dataclass
class _DirectoryRunner:
    """A runner whose declared output is a directory rather than a lock file."""

    populate: bool = False
    calls: int = 0

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        del cwd, env
        self.calls += 1
        tokens = tuple(command)
        output = Path(tokens[tokens.index("--output-file") + 1])
        output.mkdir()
        if self.populate:
            (output / "top.txt").write_text("junk\n", encoding="utf-8")
            (output / "nested").mkdir()
            (output / "nested" / "deep.txt").write_text("junk\n", encoding="utf-8")
        return CommandResult(returncode=0, stdout="", stderr="")


@dataclass
class _InspectingRunner:
    """A runner that records the artifact state it observes while running."""

    observed: dict[str, object] = field(default_factory=dict)

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        del cwd, env
        tokens = tuple(command)
        output = Path(tokens[tokens.index("--output-file") + 1])
        root = output.parent
        self.observed["canonical_lock_exists"] = output.exists()
        self.observed["marker_exists"] = (root / TRANSACTION_MARKER_NAME).is_file()
        for name, key in ((LOCK_ROLLBACK_NAME, "lock_rollback"), (RECEIPT_ROLLBACK_NAME, "receipt_rollback")):
            path = root / name
            self.observed[key] = path.read_bytes() if path.is_file() else None
        output.write_text(_LOCK_BODY, encoding="utf-8")
        return CommandResult(returncode=0, stdout="", stderr="")


@dataclass
class _InterruptingRunner:
    calls: int = 0

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        del command, cwd, env
        self.calls += 1
        raise KeyboardInterrupt


@dataclass
class _SymlinkRunner:
    """A runner that tries to redirect its declared output through a symlink."""

    target: Path
    calls: int = 0

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        del cwd, env
        self.calls += 1
        tokens = tuple(command)
        Path(tokens[tokens.index("--output-file") + 1]).symlink_to(self.target)
        return CommandResult(returncode=0, stdout="", stderr="")


@dataclass
class _RaisingRunner:
    calls: int = 0

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        del command, cwd, env
        self.calls += 1
        raise PermissionError(13, "Permission denied")


@pytest.fixture(scope="session")
def production_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A byte-identical copy of the production identity inputs, owned by the tests."""

    source = Path(__file__).resolve().parents[2]
    root = tmp_path_factory.mktemp("production-root")
    for name in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(source / name, root / name)
    shutil.copytree(source / "src", root / "src")
    return root


@pytest.fixture
def artifact_root(production_root: Path, request: pytest.FixtureRequest) -> Path:
    name = re.sub(r"[^A-Za-z0-9]+", "-", request.node.name).strip("-")
    return production_root.joinpath(*ARTIFACT_ROOT_BASE_PARTS, name)


@pytest.fixture
def tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    python = bin_dir / "python"
    for path in (uv, python):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    return uv, python


@pytest.fixture
def request_(production_root: Path, artifact_root: Path, tools: tuple[Path, Path]) -> CandidateLockRequest:
    uv, python = tools
    return CandidateLockRequest(
        repo_root=production_root,
        artifact_root=artifact_root,
        uv=uv,
        python=python,
        exclude_newer=_EXCLUDE_NEWER,
    )


def _expected_command(request: CandidateLockRequest) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "compile",
        str(request.artifact_root / REQUIREMENTS_IN_NAME),
        "--output-file",
        str(request.artifact_root / LOCK_FILE_NAME),
        "--generate-hashes",
        "--no-annotate",
        "--no-header",
        "--resolution",
        "highest",
        "--prerelease",
        "disallow",
        "--only-binary",
        ":all:",
        "--python",
        str(request.python),
        "--no-sources",
        "--no-python-downloads",
        "--exclude-newer",
        request.exclude_newer,
    )


def _freeze(request: CandidateLockRequest) -> tuple[CandidateLock, bytes, bytes]:
    lock = compile_candidate_lock(request, runner=_FakeRunner())
    return (
        lock,
        (request.artifact_root / LOCK_FILE_NAME).read_bytes(),
        (request.artifact_root / RECEIPT_FILE_NAME).read_bytes(),
    )


def _assert_freeze_intact(request: CandidateLockRequest, lock_bytes: bytes, receipt_bytes: bytes) -> None:
    assert (request.artifact_root / LOCK_FILE_NAME).read_bytes() == lock_bytes
    assert (request.artifact_root / RECEIPT_FILE_NAME).read_bytes() == receipt_bytes


def _assert_transaction_clean(request: CandidateLockRequest) -> None:
    root = request.artifact_root
    assert not (root / TRANSACTION_MARKER_NAME).exists()
    assert not (root / LOCK_ROLLBACK_NAME).exists()
    assert not (root / RECEIPT_ROLLBACK_NAME).exists()
    assert [path.name for path in root.iterdir() if path.name.startswith(QUARANTINE_PREFIX)] == []


def _assert_no_reusable_freeze(request: CandidateLockRequest) -> None:
    root = request.artifact_root
    assert not (root / LOCK_FILE_NAME).exists()
    assert not (root / LOCK_FILE_NAME).is_symlink()
    assert not (root / RECEIPT_FILE_NAME).exists()
    _assert_transaction_clean(request)


def _interrupt_rename(monkeypatch: pytest.MonkeyPatch, *, source: str | None, target: str | None) -> None:
    real = candidate_lock_module._rename_artifact

    def patched(dir_fd: int, src: str, dst: str) -> None:
        if (source is None or src == source) and (target is None or dst == target):
            raise KeyboardInterrupt
        real(dir_fd, src, dst)

    monkeypatch.setattr(candidate_lock_module, "_rename_artifact", patched)


# --- request validation -------------------------------------------------------


def test_request_rejects_ambient_executables(request_: CandidateLockRequest) -> None:
    with pytest.raises(ValueError, match="absolute"):
        replace(request_, uv=Path("uv"))
    with pytest.raises(ValueError, match="absolute"):
        replace(request_, python=Path("python3.12"))


def test_request_rejects_missing_executables(request_: CandidateLockRequest, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="uv"):
        replace(request_, uv=tmp_path / "absent-uv")


def test_request_rejects_non_executable_tools(request_: CandidateLockRequest, tools: tuple[Path, Path]) -> None:
    uv, python = tools
    uv.chmod(0o600)
    with pytest.raises(ValueError, match="executable"):
        replace(request_, uv=uv)
    uv.chmod(0o700)
    python.chmod(0o600)
    with pytest.raises(ValueError, match="executable"):
        replace(request_, python=python)


def test_request_rejects_a_non_utc_exclude_newer(request_: CandidateLockRequest) -> None:
    for value in ("2026-08-11", "2026-08-11T00:00:00", "2026-08-11T00:00:00+02:00", ""):
        with pytest.raises(ValueError, match="exclude_newer"):
            replace(request_, exclude_newer=value)


def test_request_rejects_an_unowned_artifact_root(
    request_: CandidateLockRequest, production_root: Path, tmp_path: Path
) -> None:
    base = production_root.joinpath(*ARTIFACT_ROOT_BASE_PARTS)
    unowned = (
        tmp_path / "outside",
        production_root,
        production_root / "scratch",
        production_root / ".admission-artifacts",
        production_root / ".admission-artifacts" / "other",
        base,
        base / ".." / "escape",
        Path(".admission-artifacts/backend-eval/relative"),
    )
    for candidate in unowned:
        with pytest.raises(ValueError, match="artifact_root"):
            replace(request_, artifact_root=candidate)


def test_request_accepts_a_nested_evaluation_owned_root(request_: CandidateLockRequest) -> None:
    nested = replace(request_, artifact_root=request_.artifact_root / "phase-1")
    assert nested.artifact_root.parent == request_.artifact_root


# --- command construction -----------------------------------------------------


def test_compile_builds_the_exact_locked_command(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner()
    compile_candidate_lock(request_, runner=runner)
    assert len(runner.calls) == 1
    command, cwd, _env = runner.calls[0]
    assert command == _expected_command(request_)
    assert cwd == request_.artifact_root


def test_compile_writes_the_canonical_candidate_requirements(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    data = (request_.artifact_root / REQUIREMENTS_IN_NAME).read_bytes()
    assert data == CANONICAL_REQUIREMENTS_BYTES
    assert data == b"pyrefly\nty\n"
    assert data.decode("utf-8").split() == list(CANDIDATE_NAMES)


def test_compile_uses_a_service_owned_cache_and_inherits_proxy_settings(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("NO_PROXY", "localhost")
    runner = _FakeRunner()
    compile_candidate_lock(request_, runner=runner)
    _command, _cwd, env = runner.calls[0]
    cache_dir = request_.artifact_root / "uv-cache"
    assert env["UV_CACHE_DIR"] == str(cache_dir)
    assert cache_dir.is_dir()
    assert env["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert env["NO_PROXY"] == "localhost"
    assert os.environ.get("UV_CACHE_DIR") != str(cache_dir)


def test_compile_writes_only_below_the_artifact_root(
    request_: CandidateLockRequest, production_root: Path
) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    written = {path.relative_to(request_.artifact_root).as_posix() for path in request_.artifact_root.rglob("*")}
    assert written == {REQUIREMENTS_IN_NAME, LOCK_FILE_NAME, RECEIPT_FILE_NAME, "uv-cache"}
    assert {path.name for path in production_root.iterdir()} == {
        *PRODUCTION_IDENTITY_FILES,
        "src",
        ".admission-artifacts",
    }


# --- resolution parsing -------------------------------------------------------


def test_compile_returns_every_resolved_distribution_and_two_candidates(request_: CandidateLockRequest) -> None:
    lock = compile_candidate_lock(request_, runner=_FakeRunner())
    lock_bytes = (request_.artifact_root / LOCK_FILE_NAME).read_bytes()
    assert lock.digest == sha256_bytes(lock_bytes)
    assert lock.exclude_newer == _EXCLUDE_NEWER
    assert [package.name for package in lock.resolved_packages] == ["pyrefly", "ty"]
    assert [package.requirement for package in lock.resolved_packages] == ["pyrefly==0.30.0", "ty==0.0.24"]
    assert lock.resolved_packages[0].artifact_hashes == (_HASH_A, _HASH_B)
    assert lock.resolved_packages[1].artifact_hashes == (_HASH_C,)
    assert [(package.name, package.executable_relpath) for package in lock.candidates] == [
        ("pyrefly", "bin/pyrefly"),
        ("ty", "bin/ty"),
    ]
    assert [package.version for package in lock.candidates] == ["0.30.0", "0.0.24"]
    assert lock.candidates[0].artifact_hashes == (_HASH_A, _HASH_B)


def test_compile_keeps_transitive_distributions_out_of_the_candidate_set(request_: CandidateLockRequest) -> None:
    body = _LOCK_BODY + f"typing-extensions==4.12.2 \\\n    --hash=sha256:{'4' * 64}\n"
    lock = compile_candidate_lock(request_, runner=_FakeRunner(bodies=(body,)))
    assert [package.name for package in lock.resolved_packages] == ["pyrefly", "ty", "typing-extensions"]
    assert [package.name for package in lock.candidates] == ["pyrefly", "ty"]


def test_compile_accepts_an_eligible_zero_zero_x_ty_release(request_: CandidateLockRequest) -> None:
    body = _LOCK_BODY.replace("ty==0.0.24", "ty==0.0.1")
    lock = compile_candidate_lock(request_, runner=_FakeRunner(bodies=(body,)))
    assert [package.version for package in lock.candidates] == ["0.30.0", "0.0.1"]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (f"pyrefly==0.30.0\nty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n", "hash"),
        (
            f"-e /data/CoordExp/pyrefly \\\n    --hash=sha256:{_HASH_A}\n"
            f"ty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n",
            "editable",
        ),
        (
            f"pyrefly @ https://example.invalid/pyrefly.whl \\\n    --hash=sha256:{_HASH_A}\n"
            f"ty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n",
            "direct",
        ),
        (f"--index-url https://example.invalid/simple\n{_LOCK_BODY}", "option"),
        (_LOCK_BODY + f"ty==0.0.25 \\\n    --hash=sha256:{_HASH_A}\n", "duplicate"),
        (_LOCK_BODY.replace("0.0.24", "0.0.25rc1"), "pre-release"),
        (_LOCK_BODY.replace("0.30.0", "0.30.0.dev1"), "pre-release"),
        (_LOCK_BODY.replace("0.30.0", "0.30.0+local"), "pre-release"),
        (_LOCK_BODY.replace("sha256", "md5", 1), "sha256"),
        (_LOCK_BODY + f"ruff==0.12.5; python_version < '3.13' \\\n    --hash=sha256:{_HASH_A}\n", "marker"),
        (f"ty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n", "pyrefly"),
        ("\n", "empty"),
    ],
)
def test_compile_rejects_an_unusable_resolution(request_: CandidateLockRequest, body: str, message: str) -> None:
    with pytest.raises(CandidateLockError, match=message):
        compile_candidate_lock(request_, runner=_FakeRunner(bodies=(body,)))


def test_compile_rejects_a_duplicate_hash_for_one_distribution(request_: CandidateLockRequest) -> None:
    body = (
        f"pyrefly==0.30.0 \\\n    --hash=sha256:{_HASH_A} \\\n    --hash=sha256:{_HASH_A}\n"
        f"ty==0.0.24 \\\n    --hash=sha256:{_HASH_C}\n"
    )
    with pytest.raises(CandidateLockError, match="duplicate"):
        compile_candidate_lock(request_, runner=_FakeRunner(bodies=(body,)))


# --- artifact path safety -----------------------------------------------------


def test_compile_rejects_a_symlinked_artifact_root(request_: CandidateLockRequest, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    request_.artifact_root.parent.mkdir(parents=True, exist_ok=True)
    request_.artifact_root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(request_, runner=_FakeRunner())
    assert list(outside.iterdir()) == []


def test_compile_rejects_a_symlinked_artifact_path_component(
    request_: CandidateLockRequest, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    request_.artifact_root.parent.mkdir(parents=True, exist_ok=True)
    request_.artifact_root.symlink_to(outside, target_is_directory=True)
    nested = replace(request_, artifact_root=request_.artifact_root / "phase-1")
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(nested, runner=_FakeRunner())
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("name", [REQUIREMENTS_IN_NAME, LOCK_FILE_NAME, RECEIPT_FILE_NAME])
def test_compile_rejects_a_symlinked_artifact_output(
    request_: CandidateLockRequest, tmp_path: Path, name: str
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("stale\n", encoding="utf-8")
    request_.artifact_root.mkdir(parents=True)
    (request_.artifact_root / name).symlink_to(outside)
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(request_, runner=_FakeRunner())
    assert outside.read_text(encoding="utf-8") == "stale\n"


def test_compile_rejects_a_special_file_artifact_output(request_: CandidateLockRequest) -> None:
    request_.artifact_root.mkdir(parents=True)
    os.mkfifo(request_.artifact_root / LOCK_FILE_NAME)
    with pytest.raises(CandidateLockError, match="regular file"):
        compile_candidate_lock(request_, runner=_FakeRunner())


def test_compile_rejects_a_symlinked_cache_directory(request_: CandidateLockRequest, tmp_path: Path) -> None:
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    request_.artifact_root.mkdir(parents=True)
    (request_.artifact_root / "uv-cache").symlink_to(outside, target_is_directory=True)
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(request_, runner=_FakeRunner())
    assert list(outside.iterdir()) == []


# --- command failure ----------------------------------------------------------


def test_compile_rejects_a_nonzero_command_exit(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner(returncode=2, stderr="no compatible version", write_output=False)
    with pytest.raises(CandidateLockError, match="no compatible version"):
        compile_candidate_lock(request_, runner=runner)
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()
    assert not (request_.artifact_root / RECEIPT_FILE_NAME).exists()


def test_compile_rejects_a_missing_output_file(request_: CandidateLockRequest) -> None:
    with pytest.raises(CandidateLockError, match="did not write"):
        compile_candidate_lock(request_, runner=_FakeRunner(write_output=False))
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()


def test_compile_removes_a_partial_output_when_the_first_resolution_fails(
    request_: CandidateLockRequest,
) -> None:
    runner = _FakeRunner(bodies=("pyrefly==0.30.0\n",), returncode=1, stderr="interrupted")
    with pytest.raises(CandidateLockError, match="interrupted"):
        compile_candidate_lock(request_, runner=runner)
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()
    assert not (request_.artifact_root / RECEIPT_FILE_NAME).exists()


def test_compile_rejects_a_symlinked_resolution_output(request_: CandidateLockRequest, tmp_path: Path) -> None:
    outside = tmp_path / "planted.lock"
    outside.write_text(_LOCK_BODY, encoding="utf-8")
    runner = _SymlinkRunner(target=outside)
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(request_, runner=runner)
    assert runner.calls == 1
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()
    assert not (request_.artifact_root / LOCK_FILE_NAME).is_symlink()
    assert outside.read_text(encoding="utf-8") == _LOCK_BODY


def test_recompilation_restores_the_freeze_after_a_symlinked_resolution_output(
    request_: CandidateLockRequest, tmp_path: Path
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    outside = tmp_path / "planted.lock"
    outside.write_text(_LOCK_BODY.replace("0.0.24", "0.0.25"), encoding="utf-8")
    with pytest.raises(CandidateLockError, match="symlink"):
        compile_candidate_lock(request_, runner=_SymlinkRunner(target=outside), recompile=True)
    assert not (request_.artifact_root / LOCK_FILE_NAME).is_symlink()
    assert outside.read_text(encoding="utf-8") == _LOCK_BODY.replace("0.0.24", "0.0.25")
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_compile_normalizes_a_runner_start_failure(request_: CandidateLockRequest) -> None:
    runner = _RaisingRunner()
    with pytest.raises(CandidateLockError, match="cannot start"):
        compile_candidate_lock(request_, runner=runner)
    assert runner.calls == 1
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()


# --- production identity ------------------------------------------------------


def _drifting_capture(
    monkeypatch: pytest.MonkeyPatch, *, after_call: int = 1
) -> None:
    real = candidate_lock_module.capture_production_identity
    seen: list[int] = []

    def drifting(root: Path) -> ProductionIdentity:
        identity = real(root)
        seen.append(1)
        if len(seen) > after_call:
            return replace(identity, uv_lock_sha256="f" * 64)
        return identity

    monkeypatch.setattr(candidate_lock_module, "capture_production_identity", drifting)


def test_compile_rejects_production_identity_drift(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drifting_capture(monkeypatch)
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        compile_candidate_lock(request_, runner=_FakeRunner())


def test_production_identity_drift_takes_precedence_over_a_runner_failure(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drifting_capture(monkeypatch)
    runner = _FakeRunner(returncode=2, stderr="no compatible version", write_output=False)
    with pytest.raises(ProductionIdentityChanged, match="uv.lock") as excinfo:
        compile_candidate_lock(request_, runner=runner)
    cause = excinfo.value.__cause__
    assert isinstance(cause, CandidateLockError)
    assert "no compatible version" in str(cause)


def test_compile_rechecks_production_identity_when_reusing_a_frozen_lock(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    _drifting_capture(monkeypatch)
    runner = _FakeRunner()
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        compile_candidate_lock(request_, runner=runner)
    assert runner.calls == []


def test_production_identity_is_checked_after_a_rejected_artifact_root(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    request_.artifact_root.parent.mkdir(parents=True, exist_ok=True)
    request_.artifact_root.symlink_to(outside, target_is_directory=True)
    _drifting_capture(monkeypatch)
    with pytest.raises(ProductionIdentityChanged) as excinfo:
        compile_candidate_lock(request_, runner=_FakeRunner())
    assert isinstance(excinfo.value.__cause__, CandidateLockError)


# --- freeze idempotency -------------------------------------------------------


def test_compile_reuses_an_existing_frozen_lock_without_resolving_again(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner()
    first = compile_candidate_lock(request_, runner=runner)
    second = compile_candidate_lock(request_, runner=runner)
    assert second == first
    assert len(runner.calls) == 1


def test_compile_rejects_reuse_when_the_receipt_does_not_match(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    with pytest.raises(CandidateLockError, match="receipt"):
        compile_candidate_lock(replace(request_, exclude_newer="2026-08-10T00:00:00Z"), runner=_FakeRunner())


def test_compile_rejects_reuse_when_the_receipt_is_missing(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    (request_.artifact_root / RECEIPT_FILE_NAME).unlink()
    with pytest.raises(CandidateLockError, match="receipt"):
        compile_candidate_lock(request_, runner=_FakeRunner())


@pytest.mark.parametrize(
    "content",
    [b"pyrefly\nrequests\nty\n", b"ty\npyrefly\n", b"pyrefly\nty", b"pyrefly ty\n", b" pyrefly\nty\n", b""],
)
def test_compile_rejects_reuse_with_a_non_canonical_requirements_input(
    request_: CandidateLockRequest, content: bytes
) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    (request_.artifact_root / REQUIREMENTS_IN_NAME).write_bytes(content)
    with pytest.raises(CandidateLockError, match="candidate requirements input"):
        compile_candidate_lock(request_, runner=_FakeRunner())


def test_compile_rejects_reuse_when_the_frozen_lock_was_edited(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    (request_.artifact_root / LOCK_FILE_NAME).write_text(_LOCK_BODY.replace("0.0.24", "0.0.23"), encoding="utf-8")
    with pytest.raises(CandidateLockError, match="receipt"):
        compile_candidate_lock(request_, runner=_FakeRunner())


def test_recompilation_accepts_an_identical_second_freeze(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner(bodies=(_LOCK_BODY, _LOCK_BODY))
    first = compile_candidate_lock(request_, runner=runner)
    second = compile_candidate_lock(request_, runner=runner, recompile=True)
    assert second == first
    assert len(runner.calls) == 2


def test_recompilation_rejects_a_changed_second_freeze_output(request_: CandidateLockRequest) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    runner = _FakeRunner(bodies=(_LOCK_BODY.replace("0.0.24", "0.0.25"),))
    with pytest.raises(CandidateLockError, match="changed"):
        compile_candidate_lock(request_, runner=runner, recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_recompilation_restores_the_freeze_when_the_runner_writes_nothing(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    with pytest.raises(CandidateLockError, match="did not write"):
        compile_candidate_lock(request_, runner=_FakeRunner(write_output=False), recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_recompilation_restores_the_freeze_after_a_nonzero_partial_write(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    runner = _FakeRunner(bodies=("pyrefly==0.30.0 \\\n",), returncode=1, stderr="interrupted")
    with pytest.raises(CandidateLockError, match="interrupted"):
        compile_candidate_lock(request_, runner=runner, recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_recompilation_restores_the_freeze_after_a_parse_failure(request_: CandidateLockRequest) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    runner = _FakeRunner(bodies=("this is not a locked requirement\n",))
    with pytest.raises(CandidateLockError):
        compile_candidate_lock(request_, runner=runner, recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_recompilation_restores_the_freeze_after_a_runner_start_failure(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    with pytest.raises(CandidateLockError, match="cannot start"):
        compile_candidate_lock(request_, runner=_RaisingRunner(), recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_recompilation_restores_the_freeze_when_the_caller_is_interrupted(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    with pytest.raises(KeyboardInterrupt):
        compile_candidate_lock(request_, runner=_InterruptingRunner(), recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


# --- default runner -----------------------------------------------------------


def test_subprocess_runner_reports_exit_status_and_streams(tmp_path: Path) -> None:
    result = subprocess_runner(
        ["/bin/sh", "-c", "printf resolved; printf failed >&2; exit 3"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result == CommandResult(returncode=3, stdout="resolved", stderr="failed")


def test_subprocess_runner_normalizes_a_start_failure(tmp_path: Path) -> None:
    with pytest.raises(CandidateLockError, match="cannot start"):
        subprocess_runner([str(tmp_path / "absent-uv"), "pip", "compile"], cwd=tmp_path, env={})


# --- transactional artifact safety --------------------------------------------


def test_the_prior_freeze_is_durably_backed_up_before_the_runner_runs(request_: CandidateLockRequest) -> None:
    _frozen, lock_bytes, receipt_bytes = _freeze(request_)
    runner = _InspectingRunner()
    compile_candidate_lock(request_, runner=runner, recompile=True)
    assert runner.observed["canonical_lock_exists"] is False
    assert runner.observed["marker_exists"] is True
    assert runner.observed["lock_rollback"] == lock_bytes
    assert runner.observed["receipt_rollback"] == receipt_bytes
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_a_fresh_resolution_that_drifts_production_leaves_no_reusable_freeze(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    _drifting_capture(monkeypatch)
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        compile_candidate_lock(request_, runner=_FakeRunner())
    _assert_no_reusable_freeze(request_)
    monkeypatch.undo()
    lock = compile_candidate_lock(request_, runner=_FakeRunner())
    assert lock.digest == sha256_bytes(_LOCK_BODY.encode("utf-8"))


def test_a_recompilation_that_drifts_production_restores_the_prior_freeze(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    rebound = replace(request_, exclude_newer="2026-08-10T00:00:00Z")
    _drifting_capture(monkeypatch)
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        compile_candidate_lock(rebound, runner=_FakeRunner(), recompile=True)
    # The receipt still binds the original request, so the drifting run never published one.
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)
    monkeypatch.undo()
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


@pytest.mark.parametrize("populate", [False, True])
def test_a_directory_resolution_output_is_purged_without_a_freeze(
    request_: CandidateLockRequest, populate: bool
) -> None:
    runner = _DirectoryRunner(populate=populate)
    with pytest.raises(CandidateLockError, match="regular file"):
        compile_candidate_lock(request_, runner=runner)
    assert runner.calls == 1
    _assert_no_reusable_freeze(request_)
    lock = compile_candidate_lock(request_, runner=_FakeRunner())
    assert lock.digest == sha256_bytes(_LOCK_BODY.encode("utf-8"))


@pytest.mark.parametrize("populate", [False, True])
def test_a_directory_resolution_output_restores_the_prior_freeze(
    request_: CandidateLockRequest, populate: bool
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    with pytest.raises(CandidateLockError, match="regular file"):
        compile_candidate_lock(request_, runner=_DirectoryRunner(populate=populate), recompile=True)
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen


def test_an_interrupted_restore_keeps_the_durable_copy_and_the_next_call_recovers(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    _interrupt_rename(monkeypatch, source=LOCK_ROLLBACK_NAME, target=LOCK_FILE_NAME)
    with pytest.raises(KeyboardInterrupt):
        compile_candidate_lock(request_, runner=_FakeRunner(write_output=False), recompile=True)
    root = request_.artifact_root
    assert (root / LOCK_ROLLBACK_NAME).read_bytes() == lock_bytes
    assert (root / TRANSACTION_MARKER_NAME).is_file()
    monkeypatch.undo()
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_an_interrupted_restore_leaves_the_quarantined_output_recoverable(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    changed = _LOCK_BODY.replace("0.0.24", "0.0.25")
    _interrupt_rename(monkeypatch, source=LOCK_ROLLBACK_NAME, target=LOCK_FILE_NAME)
    with pytest.raises(KeyboardInterrupt):
        compile_candidate_lock(request_, runner=_FakeRunner(bodies=(changed,)), recompile=True)
    root = request_.artifact_root
    quarantined = [path for path in root.iterdir() if path.name.startswith(QUARANTINE_PREFIX)]
    assert [path.read_bytes() for path in quarantined] == [changed.encode("utf-8")]
    assert (root / LOCK_ROLLBACK_NAME).read_bytes() == lock_bytes
    monkeypatch.undo()
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_an_interrupted_backup_is_recovered_on_the_next_call(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    _interrupt_rename(monkeypatch, source=RECEIPT_FILE_NAME, target=RECEIPT_ROLLBACK_NAME)
    runner = _FakeRunner()
    with pytest.raises(KeyboardInterrupt):
        compile_candidate_lock(request_, runner=runner, recompile=True)
    root = request_.artifact_root
    assert runner.calls == []
    assert not (root / LOCK_FILE_NAME).exists()
    assert (root / LOCK_ROLLBACK_NAME).read_bytes() == lock_bytes
    assert (root / RECEIPT_FILE_NAME).read_bytes() == receipt_bytes
    assert (root / TRANSACTION_MARKER_NAME).is_file()
    monkeypatch.undo()
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_recovery_rolls_back_an_interrupted_commit_consistently(request_: CandidateLockRequest) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    root = request_.artifact_root
    # An interrupt inside the commit window: canonical holds the new freeze while the
    # durable rollback entries and the marker still describe the previous one.
    (root / LOCK_ROLLBACK_NAME).write_bytes(lock_bytes)
    (root / RECEIPT_ROLLBACK_NAME).write_bytes(receipt_bytes)
    (root / LOCK_FILE_NAME).write_text(_LOCK_BODY.replace("0.0.24", "0.0.25"), encoding="utf-8")
    (root / RECEIPT_FILE_NAME).write_bytes(b'{"tampered":true}\n')
    (root / TRANSACTION_MARKER_NAME).write_bytes(canonical_json({"lock": True, "receipt": True}))
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_recovery_removes_a_partial_output_from_an_interrupted_fresh_resolution(
    request_: CandidateLockRequest,
) -> None:
    root = request_.artifact_root
    root.mkdir(parents=True)
    (root / LOCK_FILE_NAME).write_text("pyrefly==0.30.0 \\\n", encoding="utf-8")
    (root / TRANSACTION_MARKER_NAME).write_bytes(canonical_json({"lock": False, "receipt": False}))
    lock = compile_candidate_lock(request_, runner=_FakeRunner())
    assert lock.digest == sha256_bytes(_LOCK_BODY.encode("utf-8"))
    _assert_transaction_clean(request_)


def test_recovery_restores_a_rollback_entry_left_without_a_canonical_file(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    root = request_.artifact_root
    (root / LOCK_FILE_NAME).rename(root / LOCK_ROLLBACK_NAME)
    (root / RECEIPT_FILE_NAME).rename(root / RECEIPT_ROLLBACK_NAME)
    (root / TRANSACTION_MARKER_NAME).write_bytes(canonical_json({"lock": True, "receipt": True}))
    runner = _FakeRunner()
    assert compile_candidate_lock(request_, runner=runner) == frozen
    assert runner.calls == []
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_stray_rollback_entries_without_a_marker_are_purged(request_: CandidateLockRequest) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    root = request_.artifact_root
    (root / LOCK_ROLLBACK_NAME).write_bytes(b"stale\n")
    (root / RECEIPT_ROLLBACK_NAME).write_bytes(b"stale\n")
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_stray_quarantined_nodes_are_purged_on_the_next_call(request_: CandidateLockRequest) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    root = request_.artifact_root
    (root / f"{QUARANTINE_PREFIX}{LOCK_FILE_NAME}-abc123").write_text("stale\n", encoding="utf-8")
    stale_directory = root / f"{QUARANTINE_PREFIX}{RECEIPT_FILE_NAME}-def456"
    stale_directory.mkdir()
    (stale_directory / "nested").mkdir()
    (stale_directory / "nested" / "deep.txt").write_text("stale\n", encoding="utf-8")
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)


def test_recovery_quarantines_an_unexpected_canonical_node_before_restoring(
    request_: CandidateLockRequest,
) -> None:
    frozen, lock_bytes, receipt_bytes = _freeze(request_)
    root = request_.artifact_root
    (root / LOCK_FILE_NAME).rename(root / LOCK_ROLLBACK_NAME)
    (root / RECEIPT_FILE_NAME).rename(root / RECEIPT_ROLLBACK_NAME)
    (root / LOCK_FILE_NAME).mkdir()
    (root / LOCK_FILE_NAME / "junk.txt").write_text("junk\n", encoding="utf-8")
    os.mkfifo(root / RECEIPT_FILE_NAME)
    (root / TRANSACTION_MARKER_NAME).write_bytes(canonical_json({"lock": True, "receipt": True}))
    assert compile_candidate_lock(request_, runner=_FakeRunner()) == frozen
    _assert_freeze_intact(request_, lock_bytes, receipt_bytes)
    _assert_transaction_clean(request_)
