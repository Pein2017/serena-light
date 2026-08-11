"""Service-owned candidate runtime preparation: confinement, commands, isolation, reuse, cleanup."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from scripts.backend_eval.candidate_lock import CommandResult
from scripts.backend_eval.manifests import default_corpus_requests
from scripts.backend_eval.models import (
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    ResolvedPackage,
    ServiceConfigIdentity,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.production_identity import PRODUCTION_IDENTITY_FILES, ProductionIdentityChanged
from scripts.backend_eval.runtime import (
    BACKEND_ENVIRONMENT_KEYS,
    DEFAULT_ENVIRONMENT_INTERPRETERS,
    DEFAULT_RUNTIME_BASE,
    MANIFEST_FILE_NAME,
    REQUIREMENTS_SNAPSHOT_NAME,
    RUNTIME_DIRECTORY_NAMES,
    RUNTIME_FILE_NAMES,
    SERVICE_CONFIG_RELPATHS,
    CandidateRuntime,
    RuntimePreparationError,
    RuntimeRequest,
    minimal_backend_environment,
    prepare_candidate_runtime,
    runtime_lock_path,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_EXCLUDE_NEWER = "2026-08-11T00:00:00Z"
_TY_VERSION = "0.0.24"
_PYREFLY_VERSION = "0.30.0"
_UV_VERSION = "uv 0.9.5"
_INTERPRETER_VERSION = "3.12.11"
_LOCK_BODY = (
    f"pyrefly=={_PYREFLY_VERSION} \\\n    --hash=sha256:{_HASH_A}\n"
    f"ty=={_TY_VERSION} \\\n    --hash=sha256:{_HASH_B}\n"
).encode()
_LOCK_DIGEST = sha256_bytes(_LOCK_BODY)
_ENVIRONMENT_NAMES = ("llm-framework-study", "ms")
_INTERPRETER_ARGS = ("-I", "-c", "import sys; print(sys.version.split()[0])")


def _candidate_lock() -> CandidateLock:
    resolved = (
        ResolvedPackage("pyrefly", _PYREFLY_VERSION, f"pyrefly=={_PYREFLY_VERSION}", (_HASH_A,)),
        ResolvedPackage("ty", _TY_VERSION, f"ty=={_TY_VERSION}", (_HASH_B,)),
    )
    candidates = (
        CandidatePackage("pyrefly", _PYREFLY_VERSION, f"pyrefly=={_PYREFLY_VERSION}", (_HASH_A,), "bin/pyrefly"),
        CandidatePackage("ty", _TY_VERSION, f"ty=={_TY_VERSION}", (_HASH_B,), "bin/ty"),
    )
    return CandidateLock(
        digest=_LOCK_DIGEST,
        exclude_newer=_EXCLUDE_NEWER,
        resolved_packages=resolved,
        candidates=candidates,
    )


@dataclass
class _FakeRunner:
    """A runner that materializes exactly what `uv` and the probed executables would."""

    versions: Mapping[str, str] = field(
        default_factory=lambda: {"pyrefly": f"pyrefly {_PYREFLY_VERSION}", "ty": f"ty {_TY_VERSION}"}
    )
    uv_version: str = _UV_VERSION
    interpreter_version: str = _INTERPRETER_VERSION
    interpreter_versions: Mapping[str, str] = field(default_factory=dict)
    bodies: Mapping[str, bytes] = field(
        default_factory=lambda: {"pyrefly": b"#!/bin/sh\nexit 0\n", "ty": b"#!/bin/sh\nexit 0\n"}
    )
    omit_executables: tuple[str, ...] = ()
    symlink_executables: tuple[str, ...] = ()
    fail_command: str | None = None
    skip_venv_interpreter: bool = False
    mutate: Path | None = None
    rewrite_source: tuple[str, Path, bytes] | None = None
    rewrite_snapshot: tuple[str, bytes] | None = None
    gate: tuple[threading.Event, threading.Event] | None = None
    calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = field(default_factory=list)

    @property
    def commands(self) -> list[tuple[str, ...]]:
        return [command for command, _cwd, _env in self.calls]

    @property
    def install_commands(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if command[1:2] == ("venv",) or command[1:3] == ("pip", "sync")]

    def environment_for(self, marker: str) -> Mapping[str, str]:
        for command, _cwd, env in self.calls:
            if marker in command:
                return env
        raise AssertionError(f"no recorded command containing {marker!r}")

    def __call__(self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> CommandResult:
        tokens = tuple(command)
        self.calls.append((tokens, cwd, dict(env)))
        self._release_gate()
        self._apply_mutations(tokens)
        if tokens[1:2] == ("venv",):
            return self._venv(tokens)
        if tokens[1:3] == ("pip", "sync"):
            return self._sync(tokens)
        if tokens[1:2] == ("--version",):
            return self._version(tokens)
        if "-c" in tokens:
            return CommandResult(
                returncode=0,
                stdout=f"{self.interpreter_versions.get(tokens[0], self.interpreter_version)}\n",
                stderr="",
            )
        raise AssertionError(f"unexpected runtime preparation command: {tokens}")

    def _release_gate(self) -> None:
        if self.gate is None:
            return
        started, release = self.gate
        self.gate = None
        started.set()
        assert release.wait(10)

    def _apply_mutations(self, tokens: tuple[str, ...]) -> None:
        if self.mutate is not None:
            self.mutate.write_bytes(self.mutate.read_bytes() + b"\n# evaluation drift\n")
            self.mutate = None
        if self.rewrite_source is not None and self.rewrite_source[0] in tokens:
            _marker, target, payload = self.rewrite_source
            self.rewrite_source = None
            target.write_bytes(payload)
        if self.rewrite_snapshot is not None and self.rewrite_snapshot[0] in tokens:
            _marker, payload = self.rewrite_snapshot
            self.rewrite_snapshot = None
            snapshot = next(token for token in tokens if token.endswith(REQUIREMENTS_SNAPSHOT_NAME))
            Path(snapshot).write_bytes(payload)

    def _venv(self, tokens: tuple[str, ...]) -> CommandResult:
        if self.fail_command == "venv":
            return CommandResult(returncode=1, stdout="", stderr="uv venv refused")
        bin_dir = Path(tokens[2]) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        if not self.skip_venv_interpreter:
            (bin_dir / "python").symlink_to(tokens[tokens.index("--python") + 1])
        return CommandResult(returncode=0, stdout="", stderr="")

    def _sync(self, tokens: tuple[str, ...]) -> CommandResult:
        if self.fail_command == "sync":
            return CommandResult(returncode=1, stdout="", stderr="hash mismatch")
        bin_dir = Path(tokens[tokens.index("--python") + 1]).parent
        for name, body in sorted(self.bodies.items()):
            if name in self.omit_executables:
                continue
            path = bin_dir / name
            if name in self.symlink_executables:
                outside = bin_dir.parent.parent.parent / f"{name}-outside"
                outside.write_bytes(body)
                outside.chmod(0o700)
                path.symlink_to(outside)
                continue
            path.write_bytes(body)
            path.chmod(0o700)
        return CommandResult(returncode=0, stdout="", stderr="")

    def _version(self, tokens: tuple[str, ...]) -> CommandResult:
        name = Path(tokens[0]).name
        if self.fail_command == f"{name}-version":
            return CommandResult(returncode=2, stdout="", stderr="not executable here")
        if name == "uv":
            return CommandResult(returncode=0, stdout=f"{self.uv_version}\n", stderr="")
        return CommandResult(returncode=0, stdout=f"{self.versions[name]}\n", stderr="")


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
def tools(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "tools"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    python = bin_dir / "python3.12"
    for path in (uv, python):
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o700)
    return uv, python


@pytest.fixture
def interpreters(tmp_path: Path) -> tuple[tuple[str, Path], ...]:
    """Conda-shaped interpreters where the configured path is a symlink to a real file."""

    envs = tmp_path / "envs"
    declared: list[tuple[str, Path]] = []
    for name in _ENVIRONMENT_NAMES:
        bin_dir = envs / name / "bin"
        bin_dir.mkdir(parents=True)
        real = bin_dir / "python3.12"
        real.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        real.chmod(0o700)
        configured = bin_dir / "python"
        configured.symlink_to(real)
        declared.append((name, configured))
    return tuple(declared)


@pytest.fixture
def requirements_lock(tmp_path: Path) -> Path:
    path = tmp_path / "source-candidate-requirements.lock"
    path.write_bytes(_LOCK_BODY)
    return path


@pytest.fixture
def request_(
    production_root: Path,
    tmp_path: Path,
    tools: tuple[Path, Path],
    interpreters: tuple[tuple[str, Path], ...],
    requirements_lock: Path,
) -> RuntimeRequest:
    uv, python = tools
    return RuntimeRequest(
        repo_root=production_root,
        runtime_base=tmp_path / "runtime-base",
        uv=uv,
        python=python,
        requirements_lock=requirements_lock,
        environment_interpreters=interpreters,
    )


@pytest.fixture
def lock() -> CandidateLock:
    return _candidate_lock()


def _expected_venv_command(request: RuntimeRequest, root: Path) -> tuple[str, ...]:
    return (
        str(request.uv),
        "venv",
        str(root / "venv"),
        "--python",
        str(request.python),
        "--no-python-downloads",
        "--python-preference",
        "only-system",
    )


def _expected_sync_command(request: RuntimeRequest, root: Path) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "sync",
        str(root / REQUIREMENTS_SNAPSHOT_NAME),
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--no-sources",
        "--no-python-downloads",
        "--python",
        str(root / "venv" / "bin" / "python"),
    )


def _expected_build_commands(request: RuntimeRequest, root: Path) -> list[tuple[str, ...]]:
    venv_bin = root / "venv" / "bin"
    return [
        (str(request.uv), "--version"),
        (str(request.python), *_INTERPRETER_ARGS),
        _expected_venv_command(request, root),
        _expected_sync_command(request, root),
        (str(venv_bin / "pyrefly"), "--version"),
        (str(venv_bin / "ty"), "--version"),
        *[
            (str(interpreter), *_INTERPRETER_ARGS)
            for _name, interpreter in request.environment_interpreters
        ],
    ]


def _expected_reuse_commands(request: RuntimeRequest) -> list[tuple[str, ...]]:
    return [
        (str(request.uv), "--version"),
        (str(request.python), *_INTERPRETER_ARGS),
        *[
            (str(interpreter), *_INTERPRETER_ARGS)
            for _name, interpreter in request.environment_interpreters
        ],
    ]


def _static_runtime() -> CandidateRuntime:
    root = DEFAULT_RUNTIME_BASE / _LOCK_DIGEST
    venv_bin = root / "venv" / "bin"
    return CandidateRuntime(
        root=root,
        python=venv_bin / "python",
        ty=venv_bin / "ty",
        pyrefly=venv_bin / "pyrefly",
        lock_digest=_LOCK_DIGEST,
        executable_hashes=(("pyrefly", _HASH_A), ("ty", _HASH_B)),
        home=root / "home",
        cache=root / "cache",
        config=root / "config",
        environments=(
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path="/root/miniconda3/envs/llm-framework-study/bin/python",
                interpreter_realpath="/root/miniconda3/envs/llm-framework-study/bin/python3.12",
                version=_INTERPRETER_VERSION,
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path="/root/miniconda3/envs/ms/bin/python",
                interpreter_realpath="/root/miniconda3/envs/ms/bin/python3.12",
                version=_INTERPRETER_VERSION,
            ),
        ),
        service_configs=tuple(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(root / "config" / relpath),
                config_sha256=_HASH_A,
                home_path=str(root / "home"),
                cache_path=str(root / "cache"),
            )
            for backend, relpath in sorted(SERVICE_CONFIG_RELPATHS.items())
        ),
    )


def _manifest(root: Path) -> Mapping[str, object]:
    return json.loads((root / MANIFEST_FILE_NAME).read_bytes())


# --- content-addressed layout -------------------------------------------------


def test_runtime_path_is_candidate_lock_content_addressed(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert runtime.root == request_.runtime_base / lock.digest
    assert runtime.lock_digest == lock.digest


def test_runtime_creates_only_the_declared_service_owned_directories(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert sorted(path.name for path in runtime.root.iterdir()) == sorted(
        (*RUNTIME_FILE_NAMES, *RUNTIME_DIRECTORY_NAMES)
    )
    assert (runtime.home, runtime.cache, runtime.config) == (
        runtime.root / "home",
        runtime.root / "cache",
        runtime.root / "config",
    )
    for name in RUNTIME_DIRECTORY_NAMES:
        assert stat.S_IMODE((runtime.root / name).stat().st_mode) == 0o700


# --- physical confinement of the service-owned runtime path -------------------


def test_runtime_request_refuses_a_runtime_base_that_physically_lands_in_the_repository(
    request_: RuntimeRequest, tmp_path: Path
) -> None:
    """A lexically outside base that resolves into the production repository is refused."""

    hop = tmp_path / "outside-hop"
    hop.symlink_to(request_.repo_root)
    assert not (hop / "runtime-base").is_relative_to(request_.repo_root)

    with pytest.raises(ValueError, match="production repository"):
        replace(request_, runtime_base=hop / "runtime-base")


def test_runtime_request_refuses_a_runtime_base_that_physically_lands_in_a_corpus_root(
    request_: RuntimeRequest, tmp_path: Path
) -> None:
    corpus = default_corpus_requests()[0].root
    hop = tmp_path / "corpus-hop"
    hop.symlink_to(corpus)

    with pytest.raises(ValueError, match="corpus root"):
        replace(request_, runtime_base=hop / "runtime-base")


def test_runtime_request_refuses_a_symlinked_runtime_base_component(
    request_: RuntimeRequest, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    hop = tmp_path / "neutral-hop"
    hop.symlink_to(elsewhere)

    with pytest.raises(ValueError, match="symlinked path component"):
        replace(request_, runtime_base=hop / "runtime-base")


def test_runtime_request_refuses_a_runtime_base_that_contains_a_protected_root(
    request_: RuntimeRequest
) -> None:
    with pytest.raises(ValueError, match="production repository"):
        replace(request_, runtime_base=request_.repo_root.parent)


def test_runtime_preparation_refuses_an_ancestor_swapped_after_validation(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    """An ancestor swapped between validation and preparation cannot redirect the writes."""

    hop = tmp_path / "swap-hop"
    hop.mkdir()
    swapped = replace(request_, runtime_base=hop / "runtime-base")
    elsewhere = tmp_path / "swap-target"
    elsewhere.mkdir()
    hop.rmdir()
    hop.symlink_to(elsewhere)

    with pytest.raises(RuntimePreparationError, match="symlink"):
        prepare_candidate_runtime(lock, swapped, runner=_FakeRunner())

    assert not (elsewhere / "runtime-base").exists()


def test_runtime_preparation_refuses_a_symlinked_runtime_root(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "root-target"
    elsewhere.mkdir()
    request_.runtime_base.mkdir(parents=True)
    (request_.runtime_base / lock.digest).symlink_to(elsewhere)

    with pytest.raises(RuntimePreparationError, match="symlink"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert sorted(path.name for path in elsewhere.iterdir()) == []


def test_runtime_request_refuses_a_lexically_nested_runtime_base(request_: RuntimeRequest) -> None:
    with pytest.raises(ValueError, match="corpus root"):
        replace(request_, runtime_base=Path("/data/CoordExp/serena-light/.backend-eval-runtime"))
    with pytest.raises(ValueError, match="production repository"):
        replace(request_, runtime_base=request_.repo_root / "runtime")


# --- requirements-lock snapshot ------------------------------------------------


def test_runtime_refuses_a_requirements_lock_that_is_not_the_candidate_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    request_.requirements_lock.write_bytes(_LOCK_BODY + b"attrs==25.1.0\n")

    with pytest.raises(RuntimePreparationError, match="does not match the candidate lock digest"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_installs_a_durable_evaluation_owned_snapshot_of_the_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    snapshot = runtime.root / REQUIREMENTS_SNAPSHOT_NAME
    assert snapshot.read_bytes() == _LOCK_BODY
    assert sha256_bytes(snapshot.read_bytes()) == lock.digest
    assert str(snapshot) in runner.install_commands[1]
    assert str(request_.requirements_lock) not in runner.install_commands[1]
    assert _manifest(runtime.root)["requirements_snapshot"] == {
        "path": str(snapshot),
        "sha256": lock.digest,
    }


def test_runtime_ignores_a_concurrent_replacement_of_the_source_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """A legitimate source-lock replacement mid-run cannot install different bytes."""

    other = _LOCK_BODY + b"attrs==25.1.0 \\\n    --hash=sha256:" + b"c" * 64 + b"\n"
    runner = _FakeRunner(rewrite_source=("venv", request_.requirements_lock, other))

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert request_.requirements_lock.read_bytes() == other
    snapshot = runtime.root / REQUIREMENTS_SNAPSHOT_NAME
    assert snapshot.read_bytes() == _LOCK_BODY
    assert sha256_bytes(snapshot.read_bytes()) == lock.digest


def test_runtime_fails_closed_when_the_installed_snapshot_changes_during_sync(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    other = _LOCK_BODY + b"attrs==25.1.0\n"
    runner = _FakeRunner(rewrite_snapshot=("sync", other))

    with pytest.raises(RuntimePreparationError, match="does not match the candidate lock digest"):
        prepare_candidate_runtime(lock, request_, runner=runner)

    assert not (request_.runtime_base / lock.digest).exists()


def test_runtime_refuses_a_symlinked_requirements_lock(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    real = tmp_path / "real-lock"
    real.write_bytes(_LOCK_BODY)
    linked = tmp_path / "linked-lock"
    linked.symlink_to(real)

    with pytest.raises(ValueError, match="regular file"):
        replace(request_, requirements_lock=linked)


def test_runtime_reuse_refuses_a_changed_requirements_snapshot(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    (runtime.root / REQUIREMENTS_SNAPSHOT_NAME).write_bytes(_LOCK_BODY + b"attrs==25.1.0\n")

    with pytest.raises(RuntimePreparationError, match="does not match the candidate lock digest"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


# --- exact commands -----------------------------------------------------------


def test_runtime_runs_the_exact_uv_venv_and_hash_locked_sync_commands(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runner.install_commands == [
        _expected_venv_command(request_, runtime.root),
        _expected_sync_command(request_, runtime.root),
    ]
    assert "--require-hashes" in runner.install_commands[1]


def test_runtime_probes_every_tool_candidate_and_interpreter_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runner.commands == _expected_build_commands(request_, runtime.root)


def test_runtime_reports_a_failed_install_command(lock: CandidateLock, request_: RuntimeRequest) -> None:
    with pytest.raises(RuntimePreparationError, match="hash mismatch"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(fail_command="sync"))


# --- executable identity ------------------------------------------------------


def test_runtime_records_candidate_executable_hashes(lock: CandidateLock, request_: RuntimeRequest) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runtime.executable_hashes == (
        ("pyrefly", sha256_bytes(runner.bodies["pyrefly"])),
        ("ty", sha256_bytes(runner.bodies["ty"])),
    )
    assert (runtime.ty, runtime.pyrefly) == (
        runtime.root / "venv" / "bin" / "ty",
        runtime.root / "venv" / "bin" / "pyrefly",
    )


def test_runtime_refuses_a_candidate_executable_outside_the_runtime_root(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    with pytest.raises(RuntimePreparationError, match="must be a regular file"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(symlink_executables=("ty",)))


def test_runtime_refuses_a_missing_candidate_executable(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    with pytest.raises(RuntimePreparationError, match="pyrefly"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(omit_executables=("pyrefly",)))


def test_runtime_refuses_an_executable_whose_version_is_not_the_locked_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner(versions={"pyrefly": f"pyrefly {_PYREFLY_VERSION}", "ty": "ty 0.0.25"})

    with pytest.raises(RuntimePreparationError, match="does not report the locked version"):
        prepare_candidate_runtime(lock, request_, runner=runner)


def test_runtime_refuses_a_venv_interpreter_that_is_not_the_requested_interpreter(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    with pytest.raises(RuntimePreparationError, match="evaluation venv interpreter"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(skip_venv_interpreter=True))


# --- tool and interpreter identity ---------------------------------------------


def test_runtime_binds_the_uv_and_base_python_identity(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    tools = _manifest(runtime.root)["tools"]
    assert tools == {
        "python": {
            "path": str(request_.python),
            "realpath": os.path.realpath(request_.python),
            "sha256": sha256_bytes(request_.python.read_bytes()),
            "version_output": _INTERPRETER_VERSION,
        },
        "uv": {
            "path": str(request_.uv),
            "realpath": os.path.realpath(request_.uv),
            "sha256": sha256_bytes(request_.uv.read_bytes()),
            "version_output": _UV_VERSION,
        },
    }


def test_runtime_records_configured_path_realpath_and_version_for_each_environment(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert tuple(identity.name for identity in runtime.environments) == _ENVIRONMENT_NAMES
    for identity, (name, interpreter) in zip(
        runtime.environments, request_.environment_interpreters, strict=True
    ):
        assert identity.name == name
        assert identity.interpreter_path == str(interpreter)
        assert identity.interpreter_realpath == os.path.realpath(interpreter)
        assert identity.interpreter_path != identity.interpreter_realpath
        assert identity.version == _INTERPRETER_VERSION


def test_runtime_default_environments_are_the_manifest_declared_conda_interpreters() -> None:
    declared = (
        ("llm-framework-study", Path("/root/miniconda3/envs/llm-framework-study/bin/python")),
        ("ms", Path("/root/miniconda3/envs/ms/bin/python")),
    )

    assert declared == DEFAULT_ENVIRONMENT_INTERPRETERS


def test_runtime_request_refuses_an_ambient_executable_or_interpreter_name(request_: RuntimeRequest) -> None:
    with pytest.raises(ValueError, match="absolute path"):
        replace(request_, uv=Path("uv"))
    with pytest.raises(ValueError, match="absolute path"):
        replace(request_, environment_interpreters=(("ms", Path("python")),))


def test_runtime_refuses_an_empty_interpreter_version(lock: CandidateLock, request_: RuntimeRequest) -> None:
    with pytest.raises(RuntimePreparationError, match="did not report a version"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(interpreter_version=""))


# --- service-owned configuration ----------------------------------------------


def test_runtime_materializes_deterministic_service_owned_configuration(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert tuple(identity.backend for identity in runtime.service_configs) == ("pyrefly", "pyright", "ty")
    for identity in runtime.service_configs:
        path = Path(identity.config_path)
        assert path == runtime.config / SERVICE_CONFIG_RELPATHS[identity.backend]
        assert path.is_relative_to(runtime.config)
        assert sha256_bytes(path.read_bytes()) == identity.config_sha256
        assert identity.home_path == str(runtime.home)
        assert identity.cache_path == str(runtime.cache)


def test_runtime_configuration_is_never_written_into_a_corpus_root(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    corpus_roots = [corpus.root for corpus in default_corpus_requests()]
    for identity in runtime.service_configs:
        assert not any(Path(identity.config_path).is_relative_to(root) for root in corpus_roots)
        assert Path(identity.config_path).is_relative_to(runtime.root)


def test_runtime_configuration_bytes_are_stable_across_preparations(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    first = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    second_request = replace(request_, runtime_base=tmp_path / "runtime-base-2")

    second = prepare_candidate_runtime(lock, second_request, runner=_FakeRunner())

    assert [identity.config_sha256 for identity in first.service_configs] == [
        identity.config_sha256 for identity in second.service_configs
    ]


# --- minimal, proxy-free backend environment ----------------------------------


def test_backend_environment_is_minimal_and_proxy_free() -> None:
    runtime = _static_runtime()

    env = minimal_backend_environment(runtime, Path("/root/miniconda3/envs/ms/bin/python"))

    assert set(env) == {
        "HOME",
        "PATH",
        "PYTHONPATH",
        "SERENA_LIGHT_SELECTED_PYTHON",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
    }
    assert set(env) == set(BACKEND_ENVIRONMENT_KEYS)
    assert not any(key.upper().endswith("_PROXY") for key in env)
    assert env["HOME"] == str(runtime.home)
    assert env["PATH"] == str(runtime.python.parent)
    assert env["PYTHONPATH"] == ""
    assert env["SERENA_LIGHT_SELECTED_PYTHON"] == "/root/miniconda3/envs/ms/bin/python"
    assert env["TMPDIR"] == str(runtime.root / "tmp")
    assert env["XDG_CACHE_HOME"] == str(runtime.cache)
    assert env["XDG_CONFIG_HOME"] == str(runtime.config)


def test_backend_environment_never_inherits_ambient_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:7890")
    monkeypatch.setenv("https_proxy", "http://proxy.internal:7890")
    monkeypatch.setenv("NO_PROXY", "localhost")
    monkeypatch.setenv("PYTHONPATH", "/ambient/python/path")
    runtime = _static_runtime()

    env = minimal_backend_environment(runtime, runtime.python)

    assert "https_proxy" not in env
    assert "NO_PROXY" not in env
    assert env["PYTHONPATH"] == ""
    assert str(Path(env["PATH"])) == str(runtime.python.parent)


def test_backend_environment_refuses_an_undeclared_interpreter() -> None:
    runtime = _static_runtime()

    with pytest.raises(RuntimePreparationError, match="not a declared evaluation interpreter"):
        minimal_backend_environment(runtime, Path("/usr/bin/python3"))
    with pytest.raises(RuntimePreparationError, match="absolute path"):
        minimal_backend_environment(runtime, Path("python"))


def test_backend_environment_accepts_the_realpath_of_a_declared_interpreter() -> None:
    runtime = _static_runtime()

    env = minimal_backend_environment(runtime, Path("/root/miniconda3/envs/ms/bin/python3.12"))

    assert env["SERENA_LIGHT_SELECTED_PYTHON"] == "/root/miniconda3/envs/ms/bin/python3.12"


def test_bootstrap_commands_keep_ambient_proxy_but_own_home_cache_and_config(
    lock: CandidateLock, request_: RuntimeRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:7890")
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    install_env = runner.environment_for("venv")
    assert install_env["HTTPS_PROXY"] == "http://proxy.internal:7890"
    assert install_env["HOME"] == str(runtime.home)
    assert install_env["TMPDIR"] == str(runtime.root / "tmp")
    assert install_env["XDG_CACHE_HOME"] == str(runtime.cache)
    assert install_env["XDG_CONFIG_HOME"] == str(runtime.config)
    assert install_env["UV_CACHE_DIR"] == str(runtime.cache / "uv")


def test_only_install_commands_receive_the_ambient_environment(
    lock: CandidateLock, request_: RuntimeRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:7890")
    monkeypatch.setenv("http_proxy", "http://proxy.internal:7890")
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    install = set(runner.install_commands)
    for command, _cwd, env in runner.calls:
        if command in install:
            assert env["HTTPS_PROXY"] == "http://proxy.internal:7890"
            continue
        assert set(env) == set(BACKEND_ENVIRONMENT_KEYS)
        assert not any(key.upper().endswith("_PROXY") for key in env)
        assert env["PATH"] == str(runtime.python.parent)


# --- idempotent reuse ---------------------------------------------------------


def test_runtime_reuse_runs_only_identity_revalidation_commands(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    first = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    reuse_runner = _FakeRunner()

    second = prepare_candidate_runtime(lock, request_, runner=reuse_runner)

    assert second == first
    assert reuse_runner.install_commands == []
    assert reuse_runner.commands == _expected_reuse_commands(request_)


def test_runtime_reuse_refuses_a_changed_interpreter_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    _name, interpreter = request_.environment_interpreters[1]
    drifted = _FakeRunner(interpreter_versions={str(interpreter): "3.12.12"})

    with pytest.raises(RuntimePreparationError, match="now reports"):
        prepare_candidate_runtime(lock, request_, runner=drifted)


def test_runtime_reuse_refuses_a_changed_interpreter_realpath(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    _name, interpreter = request_.environment_interpreters[1]
    moved = interpreter.parent / "python3.12-moved"
    shutil.copy2(os.path.realpath(interpreter), moved)
    interpreter.unlink()
    interpreter.symlink_to(moved)

    with pytest.raises(RuntimePreparationError, match="now resolves to"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_reuse_refuses_a_changed_uv_binary(lock: CandidateLock, request_: RuntimeRequest) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    request_.uv.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")

    with pytest.raises(RuntimePreparationError, match="uv identity changed"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_reuse_refuses_a_changed_base_python_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    drifted = _FakeRunner(interpreter_versions={str(request_.python): "3.12.12"})

    with pytest.raises(RuntimePreparationError, match="python identity changed"):
        prepare_candidate_runtime(lock, request_, runner=drifted)


def test_runtime_reuse_refuses_a_changed_uv_version(lock: CandidateLock, request_: RuntimeRequest) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    with pytest.raises(RuntimePreparationError, match="uv identity changed"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(uv_version="uv 0.9.6"))


def test_runtime_reuse_refuses_a_changed_candidate_executable(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    runtime.ty.write_bytes(b"#!/bin/sh\nexit 1\n")

    with pytest.raises(RuntimePreparationError, match="ty"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_reuse_refuses_changed_service_owned_configuration(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    Path(runtime.service_configs[0].config_path).write_bytes(b"{}\n")

    with pytest.raises(RuntimePreparationError, match="service configuration"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_reuse_refuses_a_manifest_recorded_for_other_inputs(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    other_uv = tmp_path / "tools" / "uv-2"
    other_uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    other_uv.chmod(0o700)

    with pytest.raises(RuntimePreparationError, match="was prepared by a different command"):
        prepare_candidate_runtime(lock, replace(request_, uv=other_uv), runner=_FakeRunner())


def test_runtime_reuse_refuses_a_malformed_manifest(lock: CandidateLock, request_: RuntimeRequest) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    (runtime.root / MANIFEST_FILE_NAME).write_bytes(b"{ not json\n")

    with pytest.raises(RuntimePreparationError, match="manifest"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_reuse_refuses_unexpected_content_in_the_runtime_root(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    (runtime.root / "stowaway.json").write_bytes(b"{}\n")

    with pytest.raises(RuntimePreparationError, match="unexpected"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_manifest_is_canonical_and_records_the_published_identity(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    payload = (runtime.root / MANIFEST_FILE_NAME).read_bytes()
    decoded = json.loads(payload)
    assert payload == canonical_json(decoded)
    assert decoded["candidate_lock_digest"] == lock.digest
    assert decoded["executables"]["ty"]["version_output"] == f"ty {_TY_VERSION}"
    assert decoded["executables"]["pyrefly"]["sha256"] == dict(runtime.executable_hashes)["pyrefly"]


# --- concurrency ---------------------------------------------------------------


def _prepare_in_thread(
    lock: CandidateLock, request: RuntimeRequest, runner: _FakeRunner, sink: dict[str, object], key: str
) -> threading.Thread:
    def run() -> None:
        try:
            sink[key] = prepare_candidate_runtime(lock, request, runner=runner)
        except BaseException as exc:  # recorded, then asserted by the caller
            sink[key] = exc

    thread = threading.Thread(target=run, name=f"prepare-{key}")
    thread.start()
    return thread


def test_concurrent_preparations_serialize_and_never_purge_a_publication(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """A second caller waits for the first publication instead of purging it."""

    started, release = threading.Event(), threading.Event()
    builder = _FakeRunner(gate=(started, release))
    follower = _FakeRunner()
    sink: dict[str, object] = {}

    first = _prepare_in_thread(lock, request_, builder, sink, "first")
    assert started.wait(10)
    second = _prepare_in_thread(lock, request_, follower, sink, "second")
    second.join(0.5)

    assert second.is_alive()
    assert follower.calls == []
    release.set()
    first.join(20)
    second.join(20)

    assert not first.is_alive() and not second.is_alive()
    assert isinstance(sink["first"], CandidateRuntime)
    assert sink["second"] == sink["first"]
    assert builder.install_commands == [
        _expected_venv_command(request_, request_.runtime_base / lock.digest),
        _expected_sync_command(request_, request_.runtime_base / lock.digest),
    ]
    assert follower.install_commands == []
    assert (request_.runtime_base / lock.digest / MANIFEST_FILE_NAME).is_file()


def test_preparation_waits_for_an_external_lock_holder(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """The per-digest lock is an flock, so another process blocks this one identically."""

    path = runtime_lock_path(request_.runtime_base, lock.digest)
    path.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    runner = _FakeRunner()
    sink: dict[str, object] = {}

    thread = _prepare_in_thread(lock, request_, runner, sink, "blocked")
    try:
        thread.join(0.5)
        assert thread.is_alive()
        assert runner.calls == []
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    thread.join(20)

    assert not thread.is_alive()
    assert isinstance(sink["blocked"], CandidateRuntime)


def test_a_failing_verification_never_purges_a_concurrent_publication(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    started, release = threading.Event(), threading.Event()
    builder = _FakeRunner(gate=(started, release))
    sink: dict[str, object] = {}
    first = _prepare_in_thread(lock, request_, builder, sink, "first")
    assert started.wait(10)
    follower = _FakeRunner(uv_version="uv 0.9.6")
    second = _prepare_in_thread(lock, request_, follower, sink, "second")
    second.join(0.5)

    release.set()
    first.join(20)
    second.join(20)

    runtime = sink["first"]
    assert isinstance(runtime, CandidateRuntime)
    assert isinstance(sink["second"], RuntimePreparationError)
    assert (runtime.root / MANIFEST_FILE_NAME).is_file()
    assert runtime.ty.is_file()


# --- partial runtime cleanup --------------------------------------------------


def test_runtime_removes_a_partially_created_runtime_on_failure(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    with pytest.raises(RuntimePreparationError):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner(fail_command="sync"))

    assert not (request_.runtime_base / lock.digest).exists()
    assert request_.runtime_base.is_dir()


def test_runtime_discards_an_unpublished_partial_runtime_before_rebuilding(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    root = request_.runtime_base / lock.digest
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "ty").write_bytes(b"stale\n")
    (root / "leftover").mkdir()

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert not (runtime.root / "leftover").exists()
    assert runtime.ty.read_bytes() == b"#!/bin/sh\nexit 0\n"


def test_runtime_keeps_a_published_runtime_when_a_later_preparation_fails(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    manifest_bytes = (runtime.root / MANIFEST_FILE_NAME).read_bytes()
    runtime.ty.write_bytes(b"#!/bin/sh\nexit 1\n")

    with pytest.raises(RuntimePreparationError):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert (runtime.root / MANIFEST_FILE_NAME).read_bytes() == manifest_bytes


# --- production identity ------------------------------------------------------


def test_runtime_preparation_leaves_production_identity_unchanged(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    before = {name: (request_.repo_root / name).read_bytes() for name in PRODUCTION_IDENTITY_FILES}

    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert {name: (request_.repo_root / name).read_bytes() for name in PRODUCTION_IDENTITY_FILES} == before


def test_runtime_preparation_fails_and_cleans_up_when_production_identity_drifts(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    pyproject = request_.repo_root / "pyproject.toml"
    original = pyproject.read_bytes()
    try:
        with pytest.raises(ProductionIdentityChanged):
            prepare_candidate_runtime(lock, request_, runner=_FakeRunner(mutate=pyproject))
        assert not (request_.runtime_base / lock.digest).exists()
    finally:
        pyproject.write_bytes(original)
