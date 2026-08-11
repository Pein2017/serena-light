"""Candidate-lock command construction, parsing, and freeze idempotency."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from scripts.backend_eval import candidate_lock as candidate_lock_module
from scripts.backend_eval.candidate_lock import (
    CANDIDATE_NAMES,
    LOCK_FILE_NAME,
    RECEIPT_FILE_NAME,
    REQUIREMENTS_IN_NAME,
    CandidateLockError,
    CandidateLockRequest,
    CommandResult,
    compile_candidate_lock,
    subprocess_runner,
)
from scripts.backend_eval.models import ProductionIdentity, sha256_bytes

_EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
_HASH_A = "1" * 64
_HASH_B = "2" * 64
_HASH_C = "3" * 64

_LOCK_BODY = (
    f"pyrefly==0.30.0 \\\n    --hash=sha256:{_HASH_A} \\\n    --hash=sha256:{_HASH_B}\n"
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
        if self.returncode == 0 and self.write_output:
            body = self.bodies[min(len(self.calls) - 1, len(self.bodies) - 1)]
            Path(tokens[tokens.index("--output-file") + 1]).write_text(body, encoding="utf-8")
        return CommandResult(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


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
def request_(tmp_path: Path, repo_root: Path, tools: tuple[Path, Path]) -> CandidateLockRequest:
    uv, python = tools
    artifact_root = tmp_path / "artifacts" / "backend-eval" / "identity"
    return CandidateLockRequest(
        repo_root=repo_root,
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


# --- request validation -------------------------------------------------------


def test_request_rejects_ambient_executables(request_: CandidateLockRequest) -> None:
    with pytest.raises(ValueError, match="absolute"):
        replace(request_, uv=Path("uv"))
    with pytest.raises(ValueError, match="absolute"):
        replace(request_, python=Path("python3.12"))


def test_request_rejects_missing_executables(request_: CandidateLockRequest, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="uv"):
        replace(request_, uv=tmp_path / "absent-uv")


def test_request_rejects_a_non_utc_exclude_newer(request_: CandidateLockRequest) -> None:
    for value in ("2026-08-11", "2026-08-11T00:00:00", "2026-08-11T00:00:00+02:00", ""):
        with pytest.raises(ValueError, match="exclude_newer"):
            replace(request_, exclude_newer=value)


def test_request_rejects_an_artifact_root_containing_the_repository(
    request_: CandidateLockRequest, repo_root: Path
) -> None:
    with pytest.raises(ValueError, match="artifact_root"):
        replace(request_, artifact_root=repo_root)
    with pytest.raises(ValueError, match="artifact_root"):
        replace(request_, artifact_root=repo_root.parent)


# --- command construction -----------------------------------------------------


def test_compile_builds_the_exact_locked_command(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner()
    compile_candidate_lock(request_, runner=runner)
    assert len(runner.calls) == 1
    command, cwd, _env = runner.calls[0]
    assert command == _expected_command(request_)
    assert cwd == request_.artifact_root


def test_compile_writes_exactly_the_two_candidate_requirements(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    text = (request_.artifact_root / REQUIREMENTS_IN_NAME).read_text(encoding="utf-8")
    assert text == "pyrefly\nty\n"
    assert text.split() == list(CANDIDATE_NAMES)
    assert set(text.split()) == {"ty", "pyrefly"}


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


def test_compile_writes_only_below_the_artifact_root(request_: CandidateLockRequest, tmp_path: Path) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    written = {path.relative_to(request_.artifact_root).as_posix() for path in request_.artifact_root.rglob("*")}
    assert written == {REQUIREMENTS_IN_NAME, LOCK_FILE_NAME, RECEIPT_FILE_NAME, "uv-cache"}
    assert {path.name for path in tmp_path.iterdir()} == {"artifacts", "tools"}


# --- resolution parsing -------------------------------------------------------


def test_compile_returns_every_resolved_distribution_and_two_candidates(request_: CandidateLockRequest) -> None:
    lock = compile_candidate_lock(request_, runner=_FakeRunner())
    lock_bytes = (request_.artifact_root / LOCK_FILE_NAME).read_bytes()
    assert lock.digest == sha256_bytes(lock_bytes)
    assert lock.exclude_newer == _EXCLUDE_NEWER
    assert [package.name for package in lock.resolved_packages] == ["pyrefly", "ty"]
    assert [package.requirement for package in lock.resolved_packages] == ["pyrefly==0.30.0", "ty==0.0.24"]
    assert dict(lock.resolved_packages[0].artifact_hashes) == {
        f"sha256:{_HASH_A}": _HASH_A,
        f"sha256:{_HASH_B}": _HASH_B,
    }
    assert [(package.name, package.executable_relpath) for package in lock.candidates] == [
        ("pyrefly", "bin/pyrefly"),
        ("ty", "bin/ty"),
    ]
    assert [package.version for package in lock.candidates] == ["0.30.0", "0.0.24"]


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
        (
            f"--index-url https://example.invalid/simple\n{_LOCK_BODY}",
            "option",
        ),
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


# --- command failure ----------------------------------------------------------


def test_compile_rejects_a_nonzero_command_exit(request_: CandidateLockRequest) -> None:
    runner = _FakeRunner(returncode=2, stderr="no compatible version")
    with pytest.raises(CandidateLockError, match="no compatible version"):
        compile_candidate_lock(request_, runner=runner)
    assert not (request_.artifact_root / LOCK_FILE_NAME).exists()


def test_compile_rejects_a_missing_output_file(request_: CandidateLockRequest) -> None:
    with pytest.raises(CandidateLockError, match="did not write"):
        compile_candidate_lock(request_, runner=_FakeRunner(write_output=False))


# --- production identity ------------------------------------------------------


def test_compile_rejects_production_identity_drift(
    request_: CandidateLockRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = candidate_lock_module.capture_production_identity
    seen: list[ProductionIdentity] = []

    def drifting(root: Path) -> ProductionIdentity:
        identity = real(root)
        seen.append(identity)
        if len(seen) > 1:
            return replace(identity, uv_lock_sha256="f" * 64)
        return identity

    monkeypatch.setattr(candidate_lock_module, "capture_production_identity", drifting)
    with pytest.raises(candidate_lock_module.ProductionIdentityChanged, match="uv.lock"):
        compile_candidate_lock(request_, runner=_FakeRunner())


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


def test_compile_rejects_reuse_with_unexpected_direct_requirements(request_: CandidateLockRequest) -> None:
    compile_candidate_lock(request_, runner=_FakeRunner())
    (request_.artifact_root / REQUIREMENTS_IN_NAME).write_text("pyrefly\nrequests\nty\n", encoding="utf-8")
    with pytest.raises(CandidateLockError, match="requests"):
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
    runner = _FakeRunner(bodies=(_LOCK_BODY, _LOCK_BODY.replace("0.0.24", "0.0.25")))
    frozen = compile_candidate_lock(request_, runner=runner)
    with pytest.raises(CandidateLockError, match="changed"):
        compile_candidate_lock(request_, runner=runner, recompile=True)
    assert (request_.artifact_root / LOCK_FILE_NAME).read_bytes() == _LOCK_BODY.encode("utf-8")
    assert frozen.digest == sha256_bytes(_LOCK_BODY.encode("utf-8"))


# --- default runner -----------------------------------------------------------


def test_subprocess_runner_reports_exit_status_and_streams(tmp_path: Path) -> None:
    result = subprocess_runner(
        ["/bin/sh", "-c", "printf resolved; printf failed >&2; exit 3"],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    assert result == CommandResult(returncode=3, stdout="resolved", stderr="failed")
