"""Service-owned candidate runtime preparation: confinement, commands, isolation, reuse, cleanup."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import pytest

from scripts.backend_eval.candidate_lock import CommandResult
from scripts.backend_eval.manifests import default_corpus_requests
from scripts.backend_eval.models import (
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    LockEvidence,
    ResolvedPackage,
    ServiceConfigIdentity,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.process import CommandTimeout, Deadline
from scripts.backend_eval.production_identity import PRODUCTION_IDENTITY_FILES, ProductionIdentityChanged
from scripts.backend_eval.runtime import (
    BACKEND_ENVIRONMENT_KEYS,
    DEFAULT_ENVIRONMENT_INTERPRETERS,
    DEFAULT_RUNTIME_BASE,
    MANIFEST_FILE_NAME,
    REQUIREMENTS_SNAPSHOT_NAME,
    RUNTIME_DIRECTORY_NAMES,
    RUNTIME_FILE_NAMES,
    SEALED_REQUIREMENTS_ARGUMENT,
    SERVICE_CONFIG_RELPATHS,
    CandidateRuntime,
    RuntimePreparationError,
    RuntimeRequest,
    minimal_backend_environment,
    owned_runtime_directory_relpaths,
    owned_runtime_file_relpaths,
    prepare_candidate_runtime,
    runtime_lock_path,
    runtime_manifest_digest,
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
_OTHER_BODY = _LOCK_BODY + b"attrs==25.1.0\n"
_LOCK_DIGEST = sha256_bytes(_LOCK_BODY)
_ENVIRONMENT_NAMES = ("llm-framework-study", "ms")
_INTERPRETER_ARGS = ("-I", "-c", "import sys; print(sys.version.split()[0])")
_DESCRIPTOR_RE = re.compile(r"/proc/\d+/fd/\d+")
_RUNTIME_ENV_KEYS = ("HOME", "PATH", "TMPDIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME")


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
        lock_evidence=LockEvidence.build(raw_sha256=_LOCK_DIGEST, raw_size=64, resolved_packages=resolved),
    )


@dataclass
class _FakeRunner:
    """A runner that behaves like a child process: relative arguments resolve against cwd."""

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
    transient_snapshot: bytes | None = None
    swap: tuple[str, Path, Path] | None = None
    probe_sync_input: bool = False
    gate: tuple[threading.Event, threading.Event] | None = None
    calls: list[tuple[tuple[str, ...], Path, Mapping[str, str]]] = field(default_factory=list)
    resolved_commands: list[tuple[str, ...]] = field(default_factory=list)
    resolved_cwds: list[Path] = field(default_factory=list)
    resolved_envs: list[dict[str, Path]] = field(default_factory=list)
    observed_sync_input: bytes | None = None
    sync_input_write_error: OSError | None = None

    @property
    def commands(self) -> list[tuple[str, ...]]:
        return [command for command, _cwd, _env in self.calls]

    @property
    def install_commands(self) -> list[tuple[str, ...]]:
        return [command for command in self.commands if command[1:2] == ("venv",) or command[1:3] == ("pip", "sync")]

    def environment_for(self, marker: str) -> Mapping[str, str]:
        return self.calls[self._index_of(marker)][2]

    def resolved_environment_for(self, marker: str) -> dict[str, Path]:
        """The runtime directories the command's environment named, resolved while it ran."""

        return self.resolved_envs[self._index_of(marker)]

    def _index_of(self, marker: str) -> int:
        for index, (command, _cwd, _env) in enumerate(self.calls):
            if marker in command:
                return index
        raise AssertionError(f"no recorded command containing {marker!r}")

    def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
    ) -> CommandResult:
        tokens = tuple(command)
        self.calls.append((tokens, cwd, dict(env)))
        # Both are captured while the descriptor is still open, as a child process would see them.
        self.resolved_cwds.append(Path(cwd).resolve())
        self.resolved_commands.append(
            (str(Path(tokens[0]).resolve()), *tokens[1:]) if tokens[0].startswith("/proc/") else tokens
        )
        self.resolved_envs.append(
            {key: Path(env[key]).resolve() for key in _RUNTIME_ENV_KEYS if key in env}
        )
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
            self._snapshot().write_bytes(payload)
        if self.swap is not None and self.swap[0] in tokens:
            _marker, logical, attacker = self.swap
            self.swap = None
            logical.rename(logical.parent / f"{logical.name}-moved")
            logical.symlink_to(attacker)

    def _snapshot(self) -> Path:
        return self.resolved_cwds[-1] / REQUIREMENTS_SNAPSHOT_NAME

    def _cwd_path(self, token: str) -> Path:
        """Resolve a command argument the way a child process with this cwd would."""

        return Path(token) if token.startswith("/") else self.resolved_cwds[-1] / token

    def _venv(self, tokens: tuple[str, ...]) -> CommandResult:
        if self.fail_command == "venv":
            return CommandResult(returncode=1, stdout="", stderr="uv venv refused")
        bin_dir = self._cwd_path(tokens[2]) / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        if not self.skip_venv_interpreter:
            (bin_dir / "python").symlink_to(tokens[tokens.index("--python") + 1])
        return CommandResult(returncode=0, stdout="", stderr="")

    def _sync(self, tokens: tuple[str, ...]) -> CommandResult:
        if self.probe_sync_input:
            self._probe_input(tokens[3])
        restore = None
        if self.transient_snapshot is not None:
            # Change the durable snapshot and put it back before this command returns.
            restore = self._snapshot().read_bytes()
            self._snapshot().write_bytes(self.transient_snapshot)
            self.transient_snapshot = None
            if self.probe_sync_input:
                self._probe_input(tokens[3])
        try:
            if self.fail_command == "sync":
                return CommandResult(returncode=1, stdout="", stderr="hash mismatch")
            bin_dir = self._cwd_path(tokens[tokens.index("--python") + 1]).parent
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
        finally:
            if restore is not None:
                self._snapshot().write_bytes(restore)

    def _probe_input(self, argument: str) -> None:
        """Read the bytes uv is given, then try to change them underneath it."""

        self.observed_sync_input = Path(argument).read_bytes()
        fd = os.open(argument, os.O_WRONLY)
        try:
            os.pwrite(fd, b"attrs==25.1.0\n", 0)
        except OSError as exc:
            self.sync_input_write_error = exc
        finally:
            os.close(fd)

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


def _expected_venv_command(request: RuntimeRequest) -> tuple[str, ...]:
    """The venv target is cwd-relative, so it binds to the open runtime descriptor."""

    return (
        str(request.uv),
        "venv",
        "venv",
        "--python",
        str(request.python),
        "--no-python-downloads",
        "--python-preference",
        "only-system",
    )


def _expected_sync_command(request: RuntimeRequest, requirements: str) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "sync",
        requirements,
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--no-sources",
        "--no-python-downloads",
        "--python",
        "venv/bin/python",
    )


def _expected_probe_commands(request: RuntimeRequest) -> list[tuple[str, ...]]:
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
        manifest_path=root / "runtime-manifest.json",
        manifest_sha256=_HASH_A,
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


def _manifest(root: Path) -> dict[str, Any]:
    decoded: dict[str, Any] = json.loads((root / MANIFEST_FILE_NAME).read_bytes())
    return decoded


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
    request_: RuntimeRequest,
) -> None:
    with pytest.raises(ValueError, match="production repository"):
        replace(request_, runtime_base=request_.repo_root.parent)


def test_runtime_request_refuses_a_lexically_nested_runtime_base(request_: RuntimeRequest) -> None:
    with pytest.raises(ValueError, match="corpus root"):
        replace(request_, runtime_base=Path("/data/CoordExp/serena-light/.backend-eval-runtime"))
    with pytest.raises(ValueError, match="production repository"):
        replace(request_, runtime_base=request_.repo_root / "runtime")


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


def test_runtime_preparation_fails_closed_when_the_root_is_swapped_mid_probe(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    """Rename plus symlink during the first tool probe: no later write may escape."""

    logical = request_.runtime_base / lock.digest
    attacker = tmp_path / "attacker-root"
    attacker.mkdir()
    runner = _FakeRunner(swap=("--version", logical, attacker))

    with pytest.raises(RuntimePreparationError, match=r"swapped during preparation \(before publication\)"):
        prepare_candidate_runtime(lock, request_, runner=runner)

    moved = request_.runtime_base / f"{lock.digest}-moved"
    # Every later command, write, and read followed the open descriptor to the real root.
    assert runner.resolved_cwds[1:] == [moved] * (len(runner.calls) - 1)
    assert runner.resolved_cwds[0] == logical
    # Nothing was written into the attacker's target and no manifest exists anywhere.
    assert sorted(path.name for path in attacker.iterdir()) == []
    assert not (attacker / MANIFEST_FILE_NAME).exists()
    assert not (moved / MANIFEST_FILE_NAME).exists()
    # The real root was emptied, and the attacker-controlled entry was not unlinked for us.
    assert sorted(path.name for path in moved.iterdir()) == []
    assert logical.is_symlink()


def test_runtime_preparation_fails_closed_when_the_root_is_swapped_before_publication(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    logical = request_.runtime_base / lock.digest
    attacker = tmp_path / "late-attacker-root"
    attacker.mkdir()
    runner = _FakeRunner(swap=(str(request_.environment_interpreters[-1][1]), logical, attacker))

    with pytest.raises(RuntimePreparationError, match=r"swapped during preparation \(before publication\)"):
        prepare_candidate_runtime(lock, request_, runner=runner)

    assert sorted(path.name for path in attacker.iterdir()) == []
    assert sorted(path.name for path in (request_.runtime_base / f"{lock.digest}-moved").iterdir()) == []


# --- requirements: verified source, sealed sync input, durable evidence --------


def test_runtime_refuses_a_requirements_lock_that_is_not_the_candidate_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    request_.requirements_lock.write_bytes(_OTHER_BODY)

    with pytest.raises(RuntimePreparationError, match="does not match the candidate lock digest"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_runtime_installs_a_durable_evaluation_owned_snapshot_of_the_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """The durable snapshot is evidence; the sync input is a sealed image of the same bytes."""

    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    snapshot = runtime.root / REQUIREMENTS_SNAPSHOT_NAME
    assert snapshot.read_bytes() == _LOCK_BODY
    assert sha256_bytes(snapshot.read_bytes()) == lock.digest
    sync_input = runner.install_commands[1][3]
    assert _DESCRIPTOR_RE.fullmatch(sync_input)
    assert sync_input not in {str(snapshot), str(request_.requirements_lock)}
    assert _manifest(runtime.root)["requirements_snapshot"] == {
        "path": str(snapshot),
        "sha256": lock.digest,
    }


def test_runtime_syncs_a_sealed_immutable_image_of_the_verified_bytes(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner(probe_sync_input=True)

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runner.observed_sync_input == _LOCK_BODY
    assert runner.sync_input_write_error is not None
    assert _manifest(runtime.root)["commands"][1] == list(
        _expected_sync_command(request_, SEALED_REQUIREMENTS_ARGUMENT)
    )


def test_runtime_sync_input_is_immutable_across_a_transient_snapshot_mutation(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """The reviewer's transient change-then-restore cannot alter what uv actually read."""

    runner = _FakeRunner(probe_sync_input=True, transient_snapshot=_OTHER_BODY)

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runner.observed_sync_input == _LOCK_BODY
    assert runner.sync_input_write_error is not None
    assert (runtime.root / REQUIREMENTS_SNAPSHOT_NAME).read_bytes() == _LOCK_BODY


def test_runtime_ignores_a_concurrent_replacement_of_the_source_lock(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """A legitimate source-lock replacement mid-run cannot install different bytes."""

    runner = _FakeRunner(rewrite_source=("venv", request_.requirements_lock, _OTHER_BODY))

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert request_.requirements_lock.read_bytes() == _OTHER_BODY
    snapshot = runtime.root / REQUIREMENTS_SNAPSHOT_NAME
    assert snapshot.read_bytes() == _LOCK_BODY
    assert sha256_bytes(snapshot.read_bytes()) == lock.digest


def test_runtime_fails_closed_when_the_installed_snapshot_changes_during_sync(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner(rewrite_snapshot=("sync", _OTHER_BODY))

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
    (runtime.root / REQUIREMENTS_SNAPSHOT_NAME).write_bytes(_OTHER_BODY)

    with pytest.raises(RuntimePreparationError, match="does not match the candidate lock digest"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


# --- exact commands, bound to the open descriptor -----------------------------


def test_runtime_runs_the_exact_uv_venv_and_hash_locked_sync_commands(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    venv_command, sync_command = runner.install_commands
    assert venv_command == _expected_venv_command(request_)
    assert sync_command == _expected_sync_command(request_, sync_command[3])
    assert "--require-hashes" in sync_command
    assert _manifest(runtime.root)["commands"] == [
        list(_expected_venv_command(request_)),
        list(_expected_sync_command(request_, SEALED_REQUIREMENTS_ARGUMENT)),
    ]


def test_runtime_targets_every_command_through_the_open_runtime_descriptor(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    assert runner.resolved_cwds == [runtime.root] * len(runner.calls)
    for _command, cwd, _env in runner.calls:
        assert _DESCRIPTOR_RE.fullmatch(str(cwd))
    candidate_probes = [command for command in runner.commands if command[1:] == ("--version",)][1:]
    assert [Path(command[0]).name for command in candidate_probes] == ["pyrefly", "ty"]
    for command in candidate_probes:
        assert _DESCRIPTOR_RE.match(command[0])


def test_runtime_probes_every_tool_candidate_and_interpreter_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    venv_bin = runtime.root / "venv" / "bin"
    assert runner.resolved_commands == [
        (str(request_.uv), "--version"),
        (str(request_.python), *_INTERPRETER_ARGS),
        _expected_venv_command(request_),
        _expected_sync_command(request_, runner.install_commands[1][3]),
        (str(venv_bin / "pyrefly"), "--version"),
        (str(venv_bin / "ty"), "--version"),
        *[
            (str(interpreter), *_INTERPRETER_ARGS)
            for _name, interpreter in request_.environment_interpreters
        ],
    ]


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


def _expected_executable_record(path: Path, version: str) -> dict[str, str]:
    realpath = Path(os.path.realpath(path))
    return {
        "path": str(path),
        "realpath": str(realpath),
        "sha256": sha256_bytes(realpath.read_bytes()),
        "version_output": version,
    }


def test_runtime_binds_the_uv_and_base_python_identity(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert _manifest(runtime.root)["tools"] == {
        "python": _expected_executable_record(request_.python, _INTERPRETER_VERSION),
        "uv": _expected_executable_record(request_.uv, _UV_VERSION),
    }


def test_runtime_binds_the_environment_interpreter_executable_identity(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """EnvironmentIdentity is unchanged; the executable bytes are bound manifest-side."""

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert _manifest(runtime.root)["environment_executables"] == {
        name: _expected_executable_record(interpreter, _INTERPRETER_VERSION)
        for name, interpreter in request_.environment_interpreters
    }
    assert [field.name for field in fields(EnvironmentIdentity)] == [
        "name",
        "interpreter_path",
        "interpreter_realpath",
        "version",
    ]


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
    monkeypatch.setenv("UV_INDEX_URL", "https://mirror.invalid/simple")
    monkeypatch.setenv("PIP_INDEX_URL", "https://mirror.invalid/simple")
    monkeypatch.setenv("PYTHONPATH", "/data/verl")
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    install_env = runner.environment_for("venv")
    assert install_env["HTTPS_PROXY"] == "http://proxy.internal:7890"
    # The external-network proxy survives; every ambient package-manager control does not.
    assert install_env["PATH"] == "/usr/bin:/bin"
    assert install_env["UV_NO_CONFIG"] == "1"
    assert "UV_INDEX_URL" not in install_env
    assert "PIP_INDEX_URL" not in install_env
    assert "PYTHONPATH" not in install_env
    resolved = runner.resolved_environment_for("venv")
    ignored = {"PATH", "UV_NO_CONFIG", "UV_PYTHON_DOWNLOADS"}
    assert {key: value for key, value in resolved.items() if key not in ignored} == {
        "HOME": runtime.home,
        "TMPDIR": runtime.root / "tmp",
        "UV_CACHE_DIR": runtime.cache / "uv",
        "XDG_CACHE_HOME": runtime.cache,
        "XDG_CONFIG_HOME": runtime.config,
    }
    assert install_env["UV_PYTHON_DOWNLOADS"] == "never"


def test_only_install_commands_receive_the_ambient_environment(
    lock: CandidateLock, request_: RuntimeRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:7890")
    monkeypatch.setenv("http_proxy", "http://proxy.internal:7890")
    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    install = set(runner.install_commands)
    for index, (command, _cwd, env) in enumerate(runner.calls):
        if command in install:
            assert env["HTTPS_PROXY"] == "http://proxy.internal:7890"
            continue
        assert set(env) == set(BACKEND_ENVIRONMENT_KEYS)
        assert not any(key.upper().endswith("_PROXY") for key in env)
        assert runner.resolved_envs[index]["PATH"] == runtime.python.parent


def test_every_environment_path_stays_inside_the_runtime(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """Even the install environment points only at descriptor-bound runtime directories."""

    runner = _FakeRunner()

    runtime = prepare_candidate_runtime(lock, request_, runner=runner)

    for index, (_command, _cwd, env) in enumerate(runner.calls):
        for key in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
            assert _DESCRIPTOR_RE.match(env[key])
            assert runner.resolved_envs[index][key].is_relative_to(runtime.root)


# --- idempotent reuse ---------------------------------------------------------


def test_runtime_reuse_runs_only_identity_revalidation_commands(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    first = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    reuse_runner = _FakeRunner()

    second = prepare_candidate_runtime(lock, request_, runner=reuse_runner)

    assert second == first
    assert reuse_runner.install_commands == []
    assert reuse_runner.commands == _expected_probe_commands(request_)


def test_runtime_reuse_fails_closed_when_the_root_is_swapped_mid_probe(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    """A root swapped during a reuse probe must not yield a runtime naming attacker paths."""

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    manifest_bytes = (runtime.root / MANIFEST_FILE_NAME).read_bytes()
    executable_bytes = runtime.ty.read_bytes()
    attacker = tmp_path / "reuse-attacker-root"
    attacker.mkdir()
    runner = _FakeRunner(swap=("--version", runtime.root, attacker))

    with pytest.raises(RuntimePreparationError, match=r"swapped during preparation \(before reuse return\)"):
        prepare_candidate_runtime(lock, request_, runner=runner)

    moved = request_.runtime_base / f"{lock.digest}-moved"
    # The already-published runtime survives intact under its moved directory.
    assert (moved / MANIFEST_FILE_NAME).read_bytes() == manifest_bytes
    assert (moved / "venv" / "bin" / "ty").read_bytes() == executable_bytes
    assert (moved / REQUIREMENTS_SNAPSHOT_NAME).read_bytes() == _LOCK_BODY
    assert sorted(path.name for path in moved.iterdir()) == sorted(
        (*RUNTIME_FILE_NAMES, *RUNTIME_DIRECTORY_NAMES)
    )
    # Nothing was written into the attacker's target and its entry was left alone.
    assert sorted(path.name for path in attacker.iterdir()) == []
    assert runtime.root.is_symlink()
    # Every probe after the swap still read through the descriptor, not the swapped pathname.
    assert runner.resolved_cwds[1:] == [moved] * (len(runner.calls) - 1)


def test_runtime_reuse_refuses_changed_interpreter_bytes_at_the_same_path_and_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """Same configured path, same realpath, same version, different bytes: fail closed."""

    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    _name, interpreter = request_.environment_interpreters[1]
    realpath = Path(os.path.realpath(interpreter))
    realpath.write_text("#!/bin/sh\nexit 0\n# tampered\n", encoding="utf-8")
    realpath.chmod(0o700)
    unchanged = _FakeRunner()

    with pytest.raises(RuntimePreparationError, match="ms identity changed"):
        prepare_candidate_runtime(lock, request_, runner=unchanged)

    assert str(interpreter) == str(request_.environment_interpreters[1][1])
    assert os.path.realpath(interpreter) == str(realpath)


def test_runtime_reuse_refuses_a_changed_interpreter_version(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    _name, interpreter = request_.environment_interpreters[1]
    drifted = _FakeRunner(interpreter_versions={str(interpreter): "3.12.12"})

    with pytest.raises(RuntimePreparationError, match="ms identity changed"):
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

    with pytest.raises(RuntimePreparationError, match="ms identity changed"):
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
    assert decoded["root"] == str(runtime.root)
    assert decoded["executables"]["ty"]["path"] == str(runtime.ty)


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
    assert builder.install_commands[0] == _expected_venv_command(request_)
    assert builder.install_commands[1][:3] == (str(request_.uv), "pip", "sync")
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


# --- bounded commands and service-owned modes ------------------------------------


def test_a_hung_preparation_command_is_reported_as_a_typed_timeout(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """A hung `uv` cannot outlive the phase: the runner kills it and this module types it."""

    def hanging_runner(
        command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
    ) -> CommandResult:
        del command, cwd, env, timeout
        raise CommandTimeout("uv timed out after 1s and its process group was killed")

    with pytest.raises(RuntimePreparationError, match="timed out"):
        prepare_candidate_runtime(lock, request_, runner=hanging_runner)


def test_the_preparation_deadline_is_propagated_to_every_child(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    clock = _StepClock()
    deadline = Deadline.start(clock, 600, reserve=100)
    runner = _RecordingTimeoutRunner()

    prepare_candidate_runtime(lock, request_, runner=runner, deadline=deadline)

    assert runner.timeouts, "no command received a bound"
    assert all(timeout is not None and 0 < timeout <= 500 for timeout in runner.timeouts)
    # Later commands receive strictly less remaining time than earlier ones.
    assert runner.timeouts == sorted(runner.timeouts, reverse=True)


def test_service_owned_files_and_directories_ignore_the_ambient_umask(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    previous = os.umask(0o000)
    try:
        runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    finally:
        os.umask(previous)

    assert stat.S_IMODE(runtime.root.stat().st_mode) == 0o700
    for name in RUNTIME_DIRECTORY_NAMES:
        assert stat.S_IMODE((runtime.root / name).stat().st_mode) == 0o700, name
    for relpath in SERVICE_CONFIG_RELPATHS.values():
        config = runtime.config / relpath
        assert stat.S_IMODE(config.stat().st_mode) == 0o600, relpath
        assert stat.S_IMODE(config.parent.stat().st_mode) == 0o700, relpath
    assert stat.S_IMODE((runtime.root / REQUIREMENTS_SNAPSHOT_NAME).stat().st_mode) == 0o600
    assert stat.S_IMODE((runtime.root / MANIFEST_FILE_NAME).stat().st_mode) == 0o600


@dataclass(slots=True)
class _StepClock:
    now: float = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


@dataclass(slots=True)
class _RecordingTimeoutRunner:
    delegate: _FakeRunner = field(default_factory=lambda: _FakeRunner())
    timeouts: list[float | None] = field(default_factory=list)

    def __call__(
        self, command: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout: float | None = None
    ) -> CommandResult:
        self.timeouts.append(timeout)
        return self.delegate(command, cwd=cwd, env=env, timeout=timeout)


# --- the harness-owned permission contract --------------------------------------------


def _modes(root: Path, relpaths: tuple[str, ...]) -> dict[str, int]:
    return {relpath: stat.S_IMODE((root / relpath).stat().st_mode) for relpath in relpaths}


def test_owned_runtime_directories_are_the_root_and_every_service_owned_child() -> None:
    assert owned_runtime_directory_relpaths() == (
        "",
        "cache",
        "config",
        "home",
        "tmp",
        "venv",
        "config/pyrefly",
        "config/pyright",
        "config/ty",
    )


def test_owned_runtime_files_are_the_five_harness_written_paths() -> None:
    assert owned_runtime_file_relpaths() == (
        "candidate-requirements.lock",
        "config/pyrefly/pyrefly.toml",
        "config/pyright/pyrightconfig.json",
        "config/ty/ty.toml",
        "runtime-manifest.json",
    )


def test_a_fresh_runtime_already_satisfies_the_owned_permission_contract(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert set(_modes(runtime.root, owned_runtime_file_relpaths()).values()) == {0o600}
    for name in owned_runtime_directory_relpaths():
        assert stat.S_IMODE((runtime.root / name).stat().st_mode) == 0o700
    assert runtime.permission_repairs == ()


def test_reuse_repairs_stale_harness_written_modes_without_touching_bytes(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """A runtime published before the contract carries 0660 files; reuse corrects them."""

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    owned = owned_runtime_file_relpaths()
    before_bytes = {relpath: (runtime.root / relpath).read_bytes() for relpath in owned}
    for relpath in owned:
        os.chmod(runtime.root / relpath, 0o660)

    reused = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert reused.permission_repairs == owned
    assert set(_modes(reused.root, owned).values()) == {0o600}
    assert {relpath: (reused.root / relpath).read_bytes() for relpath in owned} == before_bytes
    # The published manifest digest is a function of bytes alone, so it cannot have moved.
    assert reused.manifest_sha256 == runtime.manifest_sha256


def test_a_second_reuse_reports_no_repair_once_the_modes_are_correct(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    os.chmod(request_.runtime_base / lock.digest / MANIFEST_FILE_NAME, 0o660)

    first = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    second = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert first.permission_repairs == (MANIFEST_FILE_NAME,)
    assert second.permission_repairs == ()


def test_reuse_leaves_third_party_cache_internals_at_their_tool_defined_modes(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """The contract is ownership-scoped: uv's own cache files keep uv's modes.

    They are third-party, they live behind a service-owned ``0700`` ancestor, and the
    receipt's artifact-tree digest excludes the cache entirely, so rewriting them would buy
    nothing and would break the tool's own assumptions.
    """

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    cache_lock = runtime.cache / ".lock"
    cache_lock.write_bytes(b"")
    os.chmod(cache_lock, 0o777)
    venv_file = runtime.root / "venv" / ".lock"
    venv_file.write_bytes(b"")
    os.chmod(venv_file, 0o777)

    reused = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert reused.permission_repairs == ()
    assert stat.S_IMODE(cache_lock.stat().st_mode) == 0o777
    assert stat.S_IMODE(venv_file.stat().st_mode) == 0o777
    # ...and they are still confined behind service-owned 0700 ancestors.
    assert stat.S_IMODE(runtime.cache.stat().st_mode) == 0o700
    assert stat.S_IMODE((runtime.root / "venv").stat().st_mode) == 0o700


def test_reuse_refuses_a_harness_owned_path_replaced_by_a_symlink(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    """A repair never follows a link, so it can never chmod a target outside the root."""

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    outside = tmp_path / "outside.toml"
    outside.write_bytes(b"outside\n")
    os.chmod(outside, 0o644)
    target = runtime.root / "config" / SERVICE_CONFIG_RELPATHS["ty"]
    target.unlink()
    target.symlink_to(outside)

    with pytest.raises(RuntimePreparationError, match="without following a link"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert stat.S_IMODE(outside.stat().st_mode) == 0o644


def test_reuse_refuses_a_widened_service_owned_directory(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    os.chmod(runtime.config / "ty", 0o750)

    with pytest.raises(RuntimePreparationError, match="is 0750, not 0700"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())


def test_the_permission_repair_does_not_touch_production(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    before = {name: (request_.repo_root / name).stat().st_mode for name in PRODUCTION_IDENTITY_FILES}
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    for relpath in owned_runtime_file_relpaths():
        os.chmod(request_.runtime_base / lock.digest / relpath, 0o660)
    prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert {name: (request_.repo_root / name).stat().st_mode for name in PRODUCTION_IDENTITY_FILES} == before


def test_reuse_refuses_a_symlinked_owned_directory_and_never_chmods_outside(
    lock: CandidateLock, request_: RuntimeRequest, tmp_path: Path
) -> None:
    """An intermediate symlink is refused too: ``O_NOFOLLOW`` only guards the last component.

    Opening ``config/ty/ty.toml`` in one call follows a symlinked ``config/ty``, so a repair
    that named the whole relative path at once could ``fchmod`` a file outside the runtime
    root.  Every component is opened from its parent's descriptor instead.
    """

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    outside = tmp_path / "outside-config"
    outside.mkdir()
    decoy = outside / "ty.toml"
    decoy.write_bytes(b"outside\n")
    os.chmod(decoy, 0o644)
    os.chmod(outside, 0o755)
    shutil.rmtree(runtime.config / "ty")
    (runtime.config / "ty").symlink_to(outside)

    with pytest.raises(RuntimePreparationError, match="without following a link"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())

    assert stat.S_IMODE(decoy.stat().st_mode) == 0o644
    assert stat.S_IMODE(outside.stat().st_mode) == 0o755


def test_runtime_manifest_digest_refuses_a_fifo_promptly(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """``O_RDONLY`` on a FIFO with no writer blocks until one appears.

    The guarded read adds ``O_NONBLOCK`` so the open returns immediately and the ``fstat``
    regular-file check refuses it, rather than the admission gate's independent manifest-digest
    recomputation hanging indefinitely on a FIFO left where ``runtime-manifest.json`` belongs.
    """

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    (runtime.root / MANIFEST_FILE_NAME).unlink()
    os.mkfifo(runtime.root / MANIFEST_FILE_NAME)
    before_fds = len(os.listdir("/proc/self/fd"))

    started = time.monotonic()
    with pytest.raises(RuntimePreparationError, match="must be a regular file"):
        runtime_manifest_digest(runtime.root)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_reuse_refuses_a_harness_owned_path_replaced_by_a_fifo(
    lock: CandidateLock, request_: RuntimeRequest
) -> None:
    """The mode-repair walk opens each owned file to inspect and ``fchmod`` it.

    A FIFO swapped in for one of those files must fail fast rather than hang the reuse path
    that recomputes the permission contract on every prepare call.
    """

    runtime = prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    target = runtime.config / SERVICE_CONFIG_RELPATHS["ty"]
    target.unlink()
    os.mkfifo(target)
    before_fds = len(os.listdir("/proc/self/fd"))

    started = time.monotonic()
    with pytest.raises(RuntimePreparationError, match="must be a regular file"):
        prepare_candidate_runtime(lock, request_, runner=_FakeRunner())
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert len(os.listdir("/proc/self/fd")) == before_fds
