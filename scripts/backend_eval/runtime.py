"""Prepare one service-owned candidate runtime below an evaluation-owned runtime base.

The runtime is content addressed by the candidate-lock digest: the same frozen lock
always prepares ``<runtime-base>/<candidate-lock-digest>/`` and nothing else.  Only
``venv``, ``home``, ``cache``, ``config``, ``tmp``, the installed requirements snapshot,
and the published manifest exist there.

**Physical confinement.**  A lexically outside runtime base can still resolve into the
production repository or a corpus root through a symlinked ancestor, so the base is
validated physically: the realpath of its deepest existing ancestor must not overlap the
production repository or any declared corpus root in either direction, and the base must
contain no symlinked path component.  Preparation then re-establishes that confinement
atomically by creating and reopening every component of the runtime path with
``O_NOFOLLOW``, so an ancestor swapped after validation cannot redirect a single write.

**Requirements snapshot.**  The caller's lock file is read once through one ``O_NOFOLLOW``
descriptor, bound to ``CandidateLock.digest``, and copied to an evaluation-owned snapshot
inside the runtime root.  ``uv pip sync`` installs that snapshot, never the caller's
mutable path, and the snapshot digest is re-checked immediately before and after the sync
and again on every reuse, so a concurrent replacement of the source lock can never yield a
successful runtime built from different bytes.

**Serialization.**  Read, verify, purge, build, and publication run under an exclusive
``flock`` on ``<runtime-base>/.<digest>.lock``, so a caller that observes "no manifest"
cannot purge a runtime another caller published in the meantime, and every observation is
made under the lock.  A published runtime is never purged by a later verification failure.

**Identity.**  ``uv``, the base interpreter, and the manifest-declared ``ms`` and
``llm-framework-study`` interpreters live outside the runtime root and can change
independently, so their path, realpath, SHA-256, and version are bound into the manifest
and re-measured on every reuse.  Candidate executables live inside the runtime root, so
their recorded SHA-256 is recomputed from disk and their recorded version output is bound
to those exact bytes -- fail-closed, without launching a candidate on reuse.

Two environments are separated on purpose.  ``uv venv`` and ``uv pip sync`` are bootstrap
downloads: they inherit the ambient environment -- including the user's external-network
proxy -- and only redirect HOME, cache, config, temporary files, and the uv cache into the
runtime.  Every other command, and everything a candidate backend ever sees, comes from
:func:`minimal_backend_environment`: an exact allowlist with no proxy variable, no ambient
PATH, and no inherited ``PYTHONPATH``.

The manifest is published with an atomic ``os.replace`` plus file and directory fsync only
after every verification succeeds, so a published manifest always describes a complete
runtime.  Production identity is captured before the work and re-checked before publication
and on every exit path.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from scripts.backend_eval.candidate_lock import CommandResult, CommandRunner, subprocess_runner
from scripts.backend_eval.manifests import (
    LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
    MS_TRANSFORMERS_ROOT,
    default_corpus_requests,
)
from scripts.backend_eval.models import (
    CandidateLock,
    EnvironmentIdentity,
    ProductionIdentity,
    ServiceConfigIdentity,
    canonical_json,
    sha256_bytes,
)
from scripts.backend_eval.production_identity import (
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)

__all__ = [
    "BACKEND_ENVIRONMENT_KEYS",
    "DEFAULT_ENVIRONMENT_INTERPRETERS",
    "DEFAULT_RUNTIME_BASE",
    "MANIFEST_FILE_NAME",
    "REQUIREMENTS_SNAPSHOT_NAME",
    "RUNTIME_DIRECTORY_NAMES",
    "RUNTIME_FILE_NAMES",
    "RUNTIME_MANIFEST_SCHEMA_VERSION",
    "SERVICE_CONFIG_EXCLUDES",
    "SERVICE_CONFIG_PYTHON_VERSION",
    "SERVICE_CONFIG_RELPATHS",
    "CandidateRuntime",
    "ProductionIdentityChanged",
    "ProductionIdentityError",
    "RuntimePreparationError",
    "RuntimeRequest",
    "minimal_backend_environment",
    "prepare_candidate_runtime",
    "runtime_lock_path",
]

DEFAULT_RUNTIME_BASE = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval")
RUNTIME_DIRECTORY_NAMES = ("cache", "config", "home", "tmp", "venv")
MANIFEST_FILE_NAME = "runtime-manifest.json"
REQUIREMENTS_SNAPSHOT_NAME = "candidate-requirements.lock"
RUNTIME_FILE_NAMES = (REQUIREMENTS_SNAPSHOT_NAME, MANIFEST_FILE_NAME)
RUNTIME_MANIFEST_SCHEMA_VERSION = 1
BACKEND_ENVIRONMENT_KEYS = (
    "HOME",
    "PATH",
    "PYTHONPATH",
    "SERENA_LIGHT_SELECTED_PYTHON",
    "TMPDIR",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)

# One declaration renders all three service-owned configurations, so the backends
# express the same include/exclude and interpreter intent rather than three drifting ones.
SERVICE_CONFIG_PYTHON_VERSION = "3.12"
SERVICE_CONFIG_EXCLUDES = ("**/__pycache__", "**/node_modules")
SERVICE_CONFIG_RELPATHS: Mapping[str, str] = MappingProxyType(
    {
        "pyrefly": "pyrefly/pyrefly.toml",
        "pyright": "pyright/pyrightconfig.json",
        "ty": "ty/ty.toml",
    }
)

_MANIFEST_TEMPORARY_NAME = f".{MANIFEST_FILE_NAME}.tmp"
_INTERPRETER_VERSION_ARGS = ("-I", "-c", "import sys; print(sys.version.split()[0])")
_VERSION_FLAG_ARGS = ("--version",)
_TOOL_NAMES = ("python", "uv")
_TOOL_RECORD_KEYS = ["path", "realpath", "sha256", "version_output"]
_EXECUTABLE_RECORD_KEYS = ["path", "sha256", "version_output"]
_DIRECTORY_MODE = 0o700
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class RuntimePreparationError(RuntimeError):
    """Raised when the isolated candidate runtime cannot be prepared or reused exactly."""


def _conda_environment_interpreter(path: Path) -> tuple[str, Path]:
    """Return the (name, interpreter) of the Conda environment that contains ``path``."""

    for parent in path.parents:
        if parent.parent.name == "envs":
            return parent.name, parent / "bin" / "python"
    raise ValueError(f"{path} is not inside a Conda environment")


# The evaluation interpreters are the ones the corpus manifests already declare.
DEFAULT_ENVIRONMENT_INTERPRETERS: tuple[tuple[str, Path], ...] = tuple(
    sorted(
        (
            _conda_environment_interpreter(LLM_FRAMEWORK_STUDY_SITE_PACKAGES),
            _conda_environment_interpreter(MS_TRANSFORMERS_ROOT),
        )
    )
)


def runtime_lock_path(runtime_base: Path, digest: str) -> Path:
    """The per-digest serialization lock, kept beside -- never inside -- the runtime root."""

    return runtime_base / f".{digest}.lock"


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """The explicit inputs of one candidate runtime preparation.

    Every executable and interpreter is an absolute path: nothing is ever resolved
    through an ambient PATH lookup, and the runtime base is confined physically.
    """

    repo_root: Path
    runtime_base: Path
    uv: Path
    python: Path
    requirements_lock: Path
    environment_interpreters: tuple[tuple[str, Path], ...] = DEFAULT_ENVIRONMENT_INTERPRETERS

    def __post_init__(self) -> None:
        _require_directory(self.repo_root, "RuntimeRequest.repo_root")
        _require_runtime_base(self.runtime_base, self.repo_root)
        _require_executable(self.uv, "RuntimeRequest.uv")
        _require_executable(self.python, "RuntimeRequest.python")
        _require_regular_file(self.requirements_lock, "RuntimeRequest.requirements_lock")
        interpreters = tuple(self.environment_interpreters)
        if not interpreters:
            raise ValueError("RuntimeRequest.environment_interpreters must declare at least one environment")
        names = [name for name, _interpreter in interpreters]
        if names != sorted(set(names)):
            raise ValueError("RuntimeRequest.environment_interpreters must be sorted and unique by name")
        for name, interpreter in interpreters:
            if not name:
                raise ValueError("RuntimeRequest.environment_interpreters name must be a non-empty string")
            _require_executable(interpreter, f"RuntimeRequest.environment_interpreters[{name}]")


@dataclass(frozen=True, slots=True)
class CandidateRuntime:
    """One prepared, service-owned candidate runtime and its recorded identity."""

    root: Path
    python: Path
    ty: Path
    pyrefly: Path
    lock_digest: str
    executable_hashes: tuple[tuple[str, str], ...]
    home: Path
    cache: Path
    config: Path
    environments: tuple[EnvironmentIdentity, ...]
    service_configs: tuple[ServiceConfigIdentity, ...]

    def __post_init__(self) -> None:
        _require_absolute(self.root, "CandidateRuntime.root")
        layout = _Layout.from_root(self.root)
        for label, path, expected in (
            ("python", self.python, layout.bin_dir / "python"),
            ("ty", self.ty, layout.bin_dir / "ty"),
            ("pyrefly", self.pyrefly, layout.bin_dir / "pyrefly"),
            ("home", self.home, layout.home),
            ("cache", self.cache, layout.cache),
            ("config", self.config, layout.config),
        ):
            if path != expected:
                raise ValueError(f"CandidateRuntime.{label} must be {expected}, got {path}")
        if len(self.lock_digest) != 64 or self.lock_digest != self.lock_digest.lower().strip():
            raise ValueError("CandidateRuntime.lock_digest must be a canonical lowercase SHA-256 digest")
        if self.root.name != self.lock_digest:
            raise ValueError("CandidateRuntime.root must be addressed by the candidate lock digest")
        if tuple(name for name, _digest in self.executable_hashes) != ("pyrefly", "ty"):
            raise ValueError("CandidateRuntime.executable_hashes must record pyrefly and ty in sorted order")
        environment_names = [identity.name for identity in self.environments]
        if not environment_names or environment_names != sorted(set(environment_names)):
            raise ValueError("CandidateRuntime.environments must be sorted, unique, and non-empty")
        if tuple(identity.backend for identity in self.service_configs) != tuple(sorted(SERVICE_CONFIG_RELPATHS)):
            raise ValueError(
                f"CandidateRuntime.service_configs must record {sorted(SERVICE_CONFIG_RELPATHS)} in sorted order"
            )
        for identity in self.service_configs:
            expected_config = self.config / SERVICE_CONFIG_RELPATHS[identity.backend]
            if Path(identity.config_path) != expected_config:
                raise ValueError(f"CandidateRuntime.service_configs[{identity.backend}] must be {expected_config}")
            if (identity.home_path, identity.cache_path) != (str(self.home), str(self.cache)):
                raise ValueError(
                    f"CandidateRuntime.service_configs[{identity.backend}] must own the runtime HOME and cache"
                )


@dataclass(frozen=True, slots=True)
class _Layout:
    """The fixed directory layout of one runtime root."""

    root: Path
    venv: Path
    home: Path
    cache: Path
    config: Path
    tmp: Path
    bin_dir: Path
    requirements: Path

    @staticmethod
    def from_root(root: Path) -> _Layout:
        venv = root / "venv"
        return _Layout(
            root=root,
            venv=venv,
            home=root / "home",
            cache=root / "cache",
            config=root / "config",
            tmp=root / "tmp",
            bin_dir=venv / "bin",
            requirements=root / REQUIREMENTS_SNAPSHOT_NAME,
        )


def prepare_candidate_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    *,
    runner: CommandRunner = subprocess_runner,
) -> CandidateRuntime:
    """Return the service-owned runtime for ``lock``, preparing it at most once.

    Read, verify, purge, build, and publication are serialized per lock digest, so
    concurrent callers never race.  A published runtime is reused only after its manifest
    verifies in full against the on-disk state and the freshly re-measured identity of
    ``uv``, the base interpreter, and every declared environment interpreter; anything else
    fails closed.  Production identity must be unchanged before publication and on every
    exit path.
    """

    before = capture_production_identity(request.repo_root)
    try:
        runtime = _prepare(lock, request, runner, before)
    except BaseException as exc:
        # Drift raised inside the preparation is already the authoritative error.
        if not isinstance(exc, ProductionIdentityError):
            _assert_production_identity_unchanged(before, request.repo_root, cause=exc)
        raise
    _assert_production_identity_unchanged(before, request.repo_root, cause=None)
    return runtime


def minimal_backend_environment(runtime: CandidateRuntime, selected_interpreter: Path) -> dict[str, str]:
    """Return the exact allowlisted environment for one candidate backend process.

    The result carries no proxy variable, no ambient PATH, and no inherited
    ``PYTHONPATH``; the interpreter must be one this runtime already declared.
    """

    _require_absolute(selected_interpreter, "selected interpreter")
    declared = {str(runtime.python)}
    for identity in runtime.environments:
        declared.update((identity.interpreter_path, identity.interpreter_realpath))
    if str(selected_interpreter) not in declared:
        raise RuntimePreparationError(
            f"{selected_interpreter} is not a declared evaluation interpreter for {runtime.root}"
        )
    return _minimal_environment(_Layout.from_root(runtime.root), selected_interpreter)


# --- preparation --------------------------------------------------------------


def _prepare(
    lock: CandidateLock,
    request: RuntimeRequest,
    runner: CommandRunner,
    before: ProductionIdentity,
) -> CandidateRuntime:
    # The caller's lock is read and bound to the digest exactly once, through one descriptor.
    source = _read_requirements_source(lock, request)
    layout = _Layout.from_root(request.runtime_base / lock.digest)
    base_fd = _open_confined_directory(request.runtime_base)
    try:
        _require_physical_identity(base_fd, request.runtime_base)
        _require_outside_protected(request.runtime_base, request.repo_root, RuntimePreparationError)
        with _runtime_lock(base_fd, request.runtime_base, lock.digest):
            _require_unswapped_root(base_fd, layout.root)
            manifest = _read_manifest(layout.root)
            if manifest is not None:
                return _verify_published_runtime(lock, request, layout, manifest, runner)
            # No published manifest under the lock means no runtime was ever completed here.
            _purge_runtime_root(layout.root)
            try:
                return _build_runtime(lock, request, runner, layout, before, source, base_fd)
            except BaseException:
                _purge_runtime_root(layout.root)
                raise
    finally:
        os.close(base_fd)


def _build_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    runner: CommandRunner,
    layout: _Layout,
    before: ProductionIdentity,
    source: bytes,
    base_fd: int,
) -> CandidateRuntime:
    _create_runtime_directories(layout, base_fd)
    _write_file(layout.requirements, source)
    install_env = _install_environment(layout)
    tools = _capture_tools(request, layout, runner)
    venv_command = _venv_command(request, layout)
    _run(runner, venv_command, layout, install_env)
    _require_venv_interpreter(layout, request)
    # The installed bytes are re-bound to the digest immediately before and after the sync.
    _require_snapshot(layout, lock.digest)
    sync_command = _sync_command(request, layout)
    _run(runner, sync_command, layout, install_env)
    _require_snapshot(layout, lock.digest)
    executables = _capture_executables(lock, layout, runner)
    environments = tuple(
        _capture_environment(name, interpreter, layout, runner)
        for name, interpreter in request.environment_interpreters
    )
    service_configs = _write_service_configs(layout)
    runtime = _runtime(lock, layout, executables, environments, service_configs)
    _require_only_declared_entries(layout.root, published=False)
    # Publication happens only for a preparation that provably left production untouched.
    _assert_production_identity_unchanged(before, request.repo_root, cause=None)
    _publish_manifest(
        layout, _manifest_mapping(lock, runtime, (venv_command, sync_command), executables, tools)
    )
    return runtime


def _runtime(
    lock: CandidateLock,
    layout: _Layout,
    executables: Mapping[str, Mapping[str, str]],
    environments: tuple[EnvironmentIdentity, ...],
    service_configs: tuple[ServiceConfigIdentity, ...],
) -> CandidateRuntime:
    return CandidateRuntime(
        root=layout.root,
        python=layout.bin_dir / "python",
        ty=layout.bin_dir / "ty",
        pyrefly=layout.bin_dir / "pyrefly",
        lock_digest=lock.digest,
        executable_hashes=tuple((name, executables[name]["sha256"]) for name in sorted(executables)),
        home=layout.home,
        cache=layout.cache,
        config=layout.config,
        environments=environments,
        service_configs=service_configs,
    )


def _capture_tools(
    request: RuntimeRequest, layout: _Layout, runner: CommandRunner
) -> dict[str, dict[str, str]]:
    """Bind the identity of the two tools that live outside the runtime root."""

    return {
        "uv": _capture_tool("uv", request.uv, _VERSION_FLAG_ARGS, request, layout, runner),
        "python": _capture_tool("python", request.python, _INTERPRETER_VERSION_ARGS, request, layout, runner),
    }


def _capture_tool(
    name: str,
    path: Path,
    version_args: Sequence[str],
    request: RuntimeRequest,
    layout: _Layout,
    runner: CommandRunner,
) -> dict[str, str]:
    realpath = Path(os.path.realpath(path))
    _require_existing_regular_file(realpath, f"{name} executable")
    result = _run(runner, (str(path), *version_args), layout, _minimal_environment(layout, request.python))
    version_output = result.stdout.strip()
    if not version_output:
        raise RuntimePreparationError(f"{name} did not report a version: {path}")
    return {
        "path": str(path),
        "realpath": str(realpath),
        "sha256": _file_digest(realpath, f"{name} executable"),
        "version_output": version_output,
    }


def _capture_executables(
    lock: CandidateLock, layout: _Layout, runner: CommandRunner
) -> dict[str, dict[str, str]]:
    """Verify and record every locked candidate executable inside the runtime root."""

    executables: dict[str, dict[str, str]] = {}
    for candidate in sorted(lock.candidates, key=lambda package: package.name):
        path = layout.venv / candidate.executable_relpath
        _require_regular_executable_inside(path, layout.root, f"candidate executable {candidate.name}")
        version_output = _capture_version(path, candidate.name, candidate.version, layout, runner)
        executables[candidate.name] = {
            "path": str(path),
            "sha256": _file_digest(path, f"candidate executable {candidate.name}"),
            "version_output": version_output,
        }
    return executables


def _capture_version(
    path: Path, name: str, locked_version: str, layout: _Layout, runner: CommandRunner
) -> str:
    result = _run(
        runner, (str(path), *_VERSION_FLAG_ARGS), layout, _minimal_environment(layout, layout.bin_dir / "python")
    )
    version_output = result.stdout.strip()
    if not version_output:
        raise RuntimePreparationError(f"candidate executable {name} did not report a version: {path}")
    _require_reported_version(version_output, locked_version, f"candidate executable {name}")
    return version_output


def _require_reported_version(version_output: str, locked_version: str, label: str) -> None:
    if locked_version not in version_output.splitlines()[0].split():
        raise RuntimePreparationError(
            f"{label} does not report the locked version {locked_version}: {version_output!r}"
        )


def _capture_environment(
    name: str, interpreter: Path, layout: _Layout, runner: CommandRunner
) -> EnvironmentIdentity:
    """Record one manifest-declared interpreter without any ambient PATH lookup."""

    realpath = Path(os.path.realpath(interpreter))
    _require_existing_regular_file(realpath, f"environment interpreter {name}")
    command = (str(interpreter), *_INTERPRETER_VERSION_ARGS)
    result = _run(runner, command, layout, _minimal_environment(layout, interpreter))
    version = result.stdout.strip()
    if not version or len(version.splitlines()) != 1:
        raise RuntimePreparationError(f"environment interpreter {name} did not report a version: {interpreter}")
    return EnvironmentIdentity(
        name=name,
        interpreter_path=str(interpreter),
        interpreter_realpath=str(realpath),
        version=version,
    )


def _write_service_configs(layout: _Layout) -> tuple[ServiceConfigIdentity, ...]:
    """Materialize the deterministic service-owned configuration below the runtime config."""

    identities: list[ServiceConfigIdentity] = []
    for backend in sorted(SERVICE_CONFIG_RELPATHS):
        path = layout.config / SERVICE_CONFIG_RELPATHS[backend]
        _require_owned_directory(path.parent)
        payload = _service_config_bytes(backend)
        _write_file(path, payload)
        identities.append(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(path),
                config_sha256=sha256_bytes(payload),
                home_path=str(layout.home),
                cache_path=str(layout.cache),
            )
        )
    return tuple(identities)


def _service_config_bytes(backend: str) -> bytes:
    if backend == "pyright":
        return canonical_json(
            {
                "exclude": list(SERVICE_CONFIG_EXCLUDES),
                "pythonVersion": SERVICE_CONFIG_PYTHON_VERSION,
                "reportMissingImports": "error",
                "typeCheckingMode": "basic",
                "useLibraryCodeForTypes": True,
            }
        )
    if backend == "ty":
        return (
            "[environment]\n"
            f'python-version = "{SERVICE_CONFIG_PYTHON_VERSION}"\n'
            "\n"
            "[src]\n"
            f"exclude = {_toml_list(SERVICE_CONFIG_EXCLUDES)}\n"
        ).encode()
    if backend == "pyrefly":
        return (
            f'python-version = "{SERVICE_CONFIG_PYTHON_VERSION}"\n'
            f"project-excludes = {_toml_list(SERVICE_CONFIG_EXCLUDES)}\n"
        ).encode()
    raise RuntimePreparationError(f"no service-owned configuration is declared for {backend}")


def _toml_list(values: Sequence[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


# --- commands and environments -------------------------------------------------


def _venv_command(request: RuntimeRequest, layout: _Layout) -> tuple[str, ...]:
    return (
        str(request.uv),
        "venv",
        str(layout.venv),
        "--python",
        str(request.python),
        "--no-python-downloads",
        "--python-preference",
        "only-system",
    )


def _sync_command(request: RuntimeRequest, layout: _Layout) -> tuple[str, ...]:
    return (
        str(request.uv),
        "pip",
        "sync",
        str(layout.requirements),
        "--require-hashes",
        "--only-binary",
        ":all:",
        "--no-sources",
        "--no-python-downloads",
        "--python",
        str(layout.bin_dir / "python"),
    )


def _install_environment(layout: _Layout) -> dict[str, str]:
    """Bootstrap downloads keep the ambient external-network proxy but own their state."""

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(layout.home),
            "TMPDIR": str(layout.tmp),
            "XDG_CACHE_HOME": str(layout.cache),
            "XDG_CONFIG_HOME": str(layout.config),
            "UV_CACHE_DIR": str(layout.cache / "uv"),
            "UV_PYTHON_DOWNLOADS": "never",
        }
    )
    return env


def _minimal_environment(layout: _Layout, selected_interpreter: Path) -> dict[str, str]:
    """Build the exact allowlist every candidate, tool, and interpreter process receives."""

    env = {
        "HOME": str(layout.home),
        "PATH": str(layout.bin_dir),
        # Deliberately empty: no ambient module search path may reach a candidate.
        "PYTHONPATH": "",
        "SERENA_LIGHT_SELECTED_PYTHON": str(selected_interpreter),
        "TMPDIR": str(layout.tmp),
        "XDG_CACHE_HOME": str(layout.cache),
        "XDG_CONFIG_HOME": str(layout.config),
    }
    if tuple(sorted(env)) != BACKEND_ENVIRONMENT_KEYS:
        raise RuntimePreparationError("the minimal backend environment does not match its declared keys")
    return env


def _run(
    runner: CommandRunner, command: Sequence[str], layout: _Layout, env: Mapping[str, str]
) -> CommandResult:
    try:
        result = runner(command, cwd=layout.root, env=env)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot start the runtime preparation command {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimePreparationError(
            f"runtime preparation command failed ({result.returncode}): {' '.join(command)}: {detail}"
        )
    return result


# --- serialization -------------------------------------------------------------


@contextmanager
def _runtime_lock(base_fd: int, runtime_base: Path, digest: str) -> Iterator[None]:
    """Hold the exclusive per-digest lock across read, verify, purge, build, and publish.

    ``flock`` is held on an open file description, so two threads and two processes
    contend identically; closing the descriptor releases it on every exit path.
    """

    name = runtime_lock_path(runtime_base, digest).name
    try:
        fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=base_fd)
    except OSError as exc:
        raise RuntimePreparationError(
            f"cannot open the runtime lock {runtime_base / name}: {exc}"
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise RuntimePreparationError(f"cannot lock {runtime_base / name}: {exc}") from exc
        yield
    finally:
        os.close(fd)


# --- published manifest --------------------------------------------------------


def _manifest_mapping(
    lock: CandidateLock,
    runtime: CandidateRuntime,
    commands: Sequence[Sequence[str]],
    executables: Mapping[str, Mapping[str, str]],
    tools: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "candidate_lock_digest": lock.digest,
        "commands": [list(command) for command in commands],
        "directories": {name: str(runtime.root / name) for name in RUNTIME_DIRECTORY_NAMES},
        "environments": [_record(identity) for identity in runtime.environments],
        "executables": {name: dict(record) for name, record in executables.items()},
        "python": str(runtime.python),
        "requirements_snapshot": {
            "path": str(runtime.root / REQUIREMENTS_SNAPSHOT_NAME),
            "sha256": lock.digest,
        },
        "root": str(runtime.root),
        "service_configs": [_record(identity) for identity in runtime.service_configs],
        "tools": {name: dict(record) for name, record in tools.items()},
    }


def _expect_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RuntimePreparationError(f"published runtime manifest has a malformed {label} entry")
    return cast("Mapping[str, Any]", value)


def _record(identity: EnvironmentIdentity | ServiceConfigIdentity) -> dict[str, object]:
    return {field.name: getattr(identity, field.name) for field in fields(identity)}


def _restore_environment(value: object) -> EnvironmentIdentity:
    return EnvironmentIdentity(**_restore_fields(value, EnvironmentIdentity, "environments"))


def _restore_service_config(value: object) -> ServiceConfigIdentity:
    return ServiceConfigIdentity(**_restore_fields(value, ServiceConfigIdentity, "service_configs"))


def _restore_fields(
    value: object, model: type[EnvironmentIdentity] | type[ServiceConfigIdentity], label: str
) -> dict[str, str]:
    mapping = _expect_mapping(value, label)
    names = {field.name for field in fields(model)}
    if set(mapping) != names:
        raise RuntimePreparationError(f"published runtime manifest has a malformed {label} entry")
    restored = {name: mapping[name] for name in names}
    if any(not isinstance(item, str) for item in restored.values()):
        raise RuntimePreparationError(f"published runtime manifest has a malformed {label} entry")
    return cast("dict[str, str]", restored)


def _publish_manifest(layout: _Layout, manifest: Mapping[str, object]) -> None:
    """Publish the manifest atomically and durably, last of all."""

    temporary = layout.root / _MANIFEST_TEMPORARY_NAME
    _write_file(temporary, canonical_json(manifest))
    try:
        os.replace(temporary, layout.root / MANIFEST_FILE_NAME)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot publish the runtime manifest below {layout.root}: {exc}") from exc
    _fsync_directory(layout.root)


def _read_manifest(root: Path) -> Mapping[str, Any] | None:
    """Return the published manifest, or ``None`` when this runtime was never published."""

    path = root / MANIFEST_FILE_NAME
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise RuntimePreparationError(f"published runtime manifest must be a regular file: {path}")
    payload = path.read_bytes()
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimePreparationError(f"cannot decode the published runtime manifest {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RuntimePreparationError(f"published runtime manifest must be a JSON object: {path}")
    manifest = cast("dict[str, Any]", decoded)
    if payload != canonical_json(manifest):
        raise RuntimePreparationError(f"published runtime manifest is not canonical: {path}")
    return manifest


def _verify_published_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    layout: _Layout,
    manifest: Mapping[str, Any],
    runner: CommandRunner,
) -> CandidateRuntime:
    """Reuse a published runtime only after the manifest verifies against the disk.

    Everything that lives outside the runtime root -- the installed snapshot's digest,
    ``uv``, the base interpreter, and every declared environment interpreter -- is
    re-measured now rather than trusted from the manifest.  Nothing here purges anything:
    a verification failure leaves the published runtime exactly as it was.
    """

    root = layout.root
    if manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimePreparationError(f"published runtime manifest has an unsupported schema version: {root}")
    if manifest.get("candidate_lock_digest") != lock.digest or manifest.get("root") != str(root):
        raise RuntimePreparationError(f"published runtime manifest does not describe {root}")
    expected_commands = [list(_venv_command(request, layout)), list(_sync_command(request, layout))]
    if manifest.get("commands") != expected_commands:
        raise RuntimePreparationError(f"published runtime {root} was prepared by a different command")
    if manifest.get("directories") != {name: str(root / name) for name in RUNTIME_DIRECTORY_NAMES}:
        raise RuntimePreparationError(f"published runtime manifest does not describe the layout of {root}")
    if manifest.get("requirements_snapshot") != {"path": str(layout.requirements), "sha256": lock.digest}:
        raise RuntimePreparationError(f"published runtime manifest does not describe the installed lock of {root}")
    _require_only_declared_entries(root, published=True)
    for name in RUNTIME_DIRECTORY_NAMES:
        _require_owned_directory(root / name, create=False)
    _require_snapshot(layout, lock.digest)
    _require_venv_interpreter(layout, request)
    executables = _verify_published_executables(lock, layout, manifest)
    _verify_published_tools(request, layout, manifest, runner)
    environments = _verify_published_environments(request, layout, manifest, runner)
    service_configs = _verify_published_service_configs(layout, manifest)
    return _runtime(lock, layout, executables, environments, service_configs)


def _verify_published_executables(
    lock: CandidateLock, layout: _Layout, manifest: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    """Fail closed on the candidate executables: recorded hash and version bind to bytes."""

    recorded = _expect_mapping(manifest.get("executables"), "executables")
    names = sorted(candidate.name for candidate in lock.candidates)
    if sorted(recorded) != names:
        raise RuntimePreparationError(f"published runtime manifest does not record {names}: {layout.root}")
    executables: dict[str, dict[str, str]] = {}
    for candidate in sorted(lock.candidates, key=lambda package: package.name):
        entry = _expect_mapping(recorded[candidate.name], f"{candidate.name} executable")
        if sorted(entry) != _EXECUTABLE_RECORD_KEYS or any(
            not isinstance(item, str) for item in entry.values()
        ):
            raise RuntimePreparationError(
                f"published runtime manifest has a malformed {candidate.name} executable entry"
            )
        path = layout.venv / candidate.executable_relpath
        if entry["path"] != str(path):
            raise RuntimePreparationError(f"published runtime executable {candidate.name} moved: {path}")
        _require_regular_executable_inside(path, layout.root, f"candidate executable {candidate.name}")
        if _file_digest(path, f"candidate executable {candidate.name}") != entry["sha256"]:
            raise RuntimePreparationError(f"published runtime executable {candidate.name} changed: {path}")
        version_output: str = entry["version_output"]
        _require_reported_version(version_output, candidate.version, f"candidate executable {candidate.name}")
        executables[candidate.name] = {
            "path": str(path),
            "sha256": entry["sha256"],
            "version_output": version_output,
        }
    return executables


def _verify_published_tools(
    request: RuntimeRequest, layout: _Layout, manifest: Mapping[str, Any], runner: CommandRunner
) -> None:
    """Re-measure uv and the base interpreter: both live outside the runtime root."""

    recorded = _expect_mapping(manifest.get("tools"), "tools")
    if sorted(recorded) != sorted(_TOOL_NAMES):
        raise RuntimePreparationError(f"published runtime manifest does not record {sorted(_TOOL_NAMES)}")
    observed = _capture_tools(request, layout, runner)
    for name in sorted(_TOOL_NAMES):
        entry = _expect_mapping(recorded[name], f"{name} tool")
        if sorted(entry) != _TOOL_RECORD_KEYS:
            raise RuntimePreparationError(f"published runtime manifest has a malformed {name} tool entry")
        if dict(entry) != observed[name]:
            raise RuntimePreparationError(
                f"published runtime {name} identity changed: recorded {dict(entry)}, now {observed[name]}"
            )


def _verify_published_environments(
    request: RuntimeRequest, layout: _Layout, manifest: Mapping[str, Any], runner: CommandRunner
) -> tuple[EnvironmentIdentity, ...]:
    """Re-run every declared interpreter and compare path, realpath, and version exactly."""

    recorded = manifest.get("environments")
    if not isinstance(recorded, list):
        raise RuntimePreparationError("published runtime manifest does not record its environments")
    environments = tuple(_restore_environment(item) for item in recorded)
    declared = tuple((name, str(interpreter)) for name, interpreter in request.environment_interpreters)
    if tuple((identity.name, identity.interpreter_path) for identity in environments) != declared:
        raise RuntimePreparationError("published runtime manifest declares different evaluation interpreters")
    for identity, (name, interpreter) in zip(environments, request.environment_interpreters, strict=True):
        observed = _capture_environment(name, interpreter, layout, runner)
        if observed.interpreter_realpath != identity.interpreter_realpath:
            raise RuntimePreparationError(
                f"published runtime environment {name} now resolves to {observed.interpreter_realpath}, "
                f"recorded {identity.interpreter_realpath}"
            )
        if observed.version != identity.version:
            raise RuntimePreparationError(
                f"published runtime environment {name} now reports version {observed.version}, "
                f"recorded {identity.version}"
            )
    return environments


def _verify_published_service_configs(
    layout: _Layout, manifest: Mapping[str, Any]
) -> tuple[ServiceConfigIdentity, ...]:
    recorded = manifest.get("service_configs")
    if not isinstance(recorded, list):
        raise RuntimePreparationError("published runtime manifest does not record its service configuration")
    identities = tuple(_restore_service_config(item) for item in recorded)
    if tuple(identity.backend for identity in identities) != tuple(sorted(SERVICE_CONFIG_RELPATHS)):
        raise RuntimePreparationError("published runtime manifest records different service configuration backends")
    for identity in identities:
        path = layout.config / SERVICE_CONFIG_RELPATHS[identity.backend]
        expected = _service_config_bytes(identity.backend)
        if identity.config_path != str(path) or identity.config_sha256 != sha256_bytes(expected):
            raise RuntimePreparationError(
                f"published runtime service configuration for {identity.backend} is not the declared one: {path}"
            )
        _require_existing_regular_file(path, f"service configuration {identity.backend}")
        if path.read_bytes() != expected:
            raise RuntimePreparationError(
                f"published runtime service configuration for {identity.backend} changed: {path}"
            )
    return identities


# --- confined filesystem -------------------------------------------------------


def _open_confined_directory(path: Path) -> int:
    """Create and open ``path`` one component at a time, refusing any symlinked component.

    Every component below ``/`` is opened with ``O_NOFOLLOW`` from its parent descriptor, so
    an ancestor swapped after validation can never redirect a write below this descriptor.
    """

    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot open the filesystem root: {exc}") from exc
    try:
        for part in path.parts[1:]:
            child = _open_confined_child(fd, part, path)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_confined_child(parent_fd: int, name: str, path: Path) -> int:
    try:
        os.mkdir(name, _DIRECTORY_MODE, dir_fd=parent_fd)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimePreparationError(
            f"cannot create the service-owned runtime path component {name!r} of {path}: {exc}"
        ) from exc
    try:
        return os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimePreparationError(
            f"service-owned runtime path component {name!r} of {path} must be a directory, "
            f"not a symlink or special file: {exc}"
        ) from exc


def _require_unswapped_root(base_fd: int, root: Path) -> None:
    """Refuse an existing runtime root that is a symlink or otherwise not our directory."""

    try:
        mode = os.lstat(root.name, dir_fd=base_fd).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimePreparationError(f"cannot inspect the runtime root {root}: {exc}") from exc
    if not stat.S_ISDIR(mode):
        raise RuntimePreparationError(
            f"the runtime root {root} must be an evaluation-owned directory, not a symlink or special file"
        )
    fd = _open_confined_child(base_fd, root.name, root)
    try:
        _require_physical_identity(fd, root)
    finally:
        os.close(fd)


def _physical_path(fd: int) -> Path:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError as exc:
        raise RuntimePreparationError(f"cannot resolve the physical path of an open directory: {exc}") from exc


def _require_physical_identity(fd: int, path: Path) -> None:
    observed = _physical_path(fd)
    if observed != path:
        raise RuntimePreparationError(f"{path} physically resolves to {observed}")


def _create_runtime_directories(layout: _Layout, base_fd: int) -> None:
    root_fd = _open_confined_child(base_fd, layout.root.name, layout.root)
    try:
        _require_physical_identity(root_fd, layout.root)
        for name in RUNTIME_DIRECTORY_NAMES:
            os.close(_open_confined_child(root_fd, name, layout.root / name))
    finally:
        os.close(root_fd)


def _require_only_declared_entries(root: Path, *, published: bool) -> None:
    files = RUNTIME_FILE_NAMES if published else (REQUIREMENTS_SNAPSHOT_NAME,)
    declared = sorted((*files, *RUNTIME_DIRECTORY_NAMES))
    observed = sorted(entry.name for entry in root.iterdir())
    if observed != declared:
        unexpected = sorted(set(observed) - set(declared))
        raise RuntimePreparationError(
            f"runtime root {root} contains unexpected entries: {unexpected or observed}"
        )


def _require_owned_directory(path: Path, *, create: bool = True) -> None:
    if create:
        try:
            path.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimePreparationError(f"cannot create the evaluation-owned directory {path}: {exc}") from exc
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise RuntimePreparationError(f"cannot inspect the evaluation-owned directory {path}: {exc}") from exc
    if not stat.S_ISDIR(mode):
        raise RuntimePreparationError(
            f"{path} must be an evaluation-owned directory, not a symlink or special file"
        )


def _require_venv_interpreter(layout: _Layout, request: RuntimeRequest) -> None:
    """The venv interpreter may be a link, but it must resolve to the requested one."""

    path = layout.bin_dir / "python"
    if not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimePreparationError(f"the evaluation venv interpreter is missing or not executable: {path}")
    if os.path.realpath(path) != os.path.realpath(request.python):
        raise RuntimePreparationError(
            f"the evaluation venv interpreter {path} does not resolve to {request.python}"
        )


def _require_regular_executable_inside(path: Path, root: Path, label: str) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError as exc:
        raise RuntimePreparationError(f"{label} is missing: {path}") from exc
    except OSError as exc:
        raise RuntimePreparationError(f"cannot inspect {label} {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise RuntimePreparationError(f"{label} must be a regular file inside {root}: {path}")
    if not Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root))):
        raise RuntimePreparationError(f"{label} must be a regular file inside {root}: {path}")
    if not os.access(path, os.X_OK):
        raise RuntimePreparationError(f"{label} must be executable: {path}")


def _read_requirements_source(lock: CandidateLock, request: RuntimeRequest) -> bytes:
    """Read the caller's lock once through one verified descriptor and bind it to the digest.

    The bytes that are hashed are the bytes that are installed: the caller's mutable path is
    never handed to ``uv``.
    """

    payload = _read_regular_file(request.requirements_lock, "candidate requirements lock")
    _require_digest(payload, lock.digest, request.requirements_lock)
    return payload


def _require_snapshot(layout: _Layout, digest: str) -> None:
    """Re-bind the installed snapshot to the candidate lock digest."""

    _require_digest(
        _read_regular_file(layout.requirements, "installed candidate requirements lock"),
        digest,
        layout.requirements,
    )


def _require_digest(payload: bytes, digest: str, path: Path) -> None:
    observed = sha256_bytes(payload)
    if observed != digest:
        raise RuntimePreparationError(
            f"{path} does not match the candidate lock digest {digest}: {observed}"
        )


def _read_regular_file(path: Path, label: str) -> bytes:
    """Read one verified regular file through a single ``O_NOFOLLOW`` descriptor."""

    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot open {label} {path}: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimePreparationError(f"{label} must be a regular file: {path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise RuntimePreparationError(f"cannot read {label} {path}: {exc}") from exc
    finally:
        os.close(fd)


def _file_digest(path: Path, label: str) -> str:
    return sha256_bytes(_read_regular_file(path, label))


def _write_file(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise RuntimePreparationError(f"cannot write through a symlink: {path}")
    try:
        with open(path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimePreparationError(f"cannot write {path}: {exc}") from exc


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot open the runtime directory {path}: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot fsync the runtime directory {path}: {exc}") from exc
    finally:
        os.close(fd)


def _purge_runtime_root(root: Path) -> None:
    """Remove an unpublished or failed runtime.

    Only reachable under the per-digest lock and after physical confinement has been
    established, so no other caller's publication and nothing outside the runtime base can
    be reached from here.
    """

    if root.is_symlink():
        raise RuntimePreparationError(f"the runtime root must not be a symlink: {root}")
    if not root.exists():
        return
    if not shutil.rmtree.avoids_symlink_attacks:
        raise RuntimePreparationError(f"this platform cannot remove {root} without a symlink race")
    try:
        shutil.rmtree(root)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot remove the partially created runtime {root}: {exc}") from exc
    _fsync_directory(root.parent)


# --- request validation --------------------------------------------------------


def _require_absolute(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise RuntimePreparationError(f"{label} must be an absolute path, not an ambient name: {path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path")
    if not path.is_dir():
        raise ValueError(f"{label} must be an existing directory: {path}")


def _require_executable(path: Path, label: str) -> None:
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute path, not an ambient executable name")
    if not path.is_file():
        raise ValueError(f"{label} must be an existing file: {path}")
    if not os.access(path, os.X_OK):
        raise ValueError(f"{label} must be an executable file: {path}")


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be an existing regular file: {path}")


def _require_existing_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise RuntimePreparationError(f"{label} must be an existing regular file: {path}")


def _physical_prefix(path: Path) -> Path:
    """Resolve the deepest existing ancestor of ``path`` and re-append the missing tail."""

    anchor = path
    remainder: list[str] = []
    while not os.path.lexists(anchor) and anchor != anchor.parent:
        remainder.append(anchor.name)
        anchor = anchor.parent
    return Path(os.path.realpath(anchor)).joinpath(*reversed(remainder))


def _protected_roots(repo_root: Path) -> tuple[tuple[str, Path], ...]:
    return (
        ("production repository", repo_root),
        *(("corpus root", corpus.root) for corpus in default_corpus_requests()),
    )


def _require_outside_protected(
    runtime_base: Path, repo_root: Path, error: type[Exception] = ValueError
) -> None:
    """Refuse a runtime base that physically overlaps production or corpus state."""

    physical_base = _physical_prefix(runtime_base)
    for label, root in _protected_roots(repo_root):
        physical_root = _physical_prefix(root)
        if (
            physical_base == physical_root
            or physical_base.is_relative_to(physical_root)
            or physical_root.is_relative_to(physical_base)
        ):
            raise error(
                f"RuntimeRequest.runtime_base must stay physically outside the {label} {root}: "
                f"{runtime_base} resolves to {physical_base}"
            )


def _require_runtime_base(runtime_base: Path, repo_root: Path) -> None:
    if not runtime_base.is_absolute():
        raise ValueError("RuntimeRequest.runtime_base must be an absolute path")
    if ".." in runtime_base.parts:
        raise ValueError("RuntimeRequest.runtime_base must not contain parent references")
    _require_outside_protected(runtime_base, repo_root)
    physical_base = _physical_prefix(runtime_base)
    if physical_base != runtime_base:
        raise ValueError(
            f"RuntimeRequest.runtime_base must not contain a symlinked path component: "
            f"{runtime_base} resolves to {physical_base}"
        )


def _assert_production_identity_unchanged(
    before: ProductionIdentity, repo_root: Path, *, cause: BaseException | None
) -> None:
    """Re-check production identity; drift outranks and chains the failure that caused it."""

    try:
        assert_production_identity_unchanged(before, capture_production_identity(repo_root))
    except ProductionIdentityError as identity_error:
        if cause is None:
            raise
        raise identity_error from cause
