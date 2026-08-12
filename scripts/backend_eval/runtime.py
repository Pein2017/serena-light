"""Prepare one service-owned candidate runtime below an evaluation-owned runtime base.

The runtime is content addressed by the candidate-lock digest: the same frozen lock always
prepares ``<runtime-base>/<candidate-lock-digest>/`` and nothing else.  Only ``venv``,
``home``, ``cache``, ``config``, ``tmp``, the installed requirements snapshot, and the
published manifest exist there.

**Physical confinement, then descriptor binding.**  A lexically outside runtime base can
resolve into the production repository or a corpus root through a symlinked ancestor, so
the base is validated physically -- realpath overlap in either direction is refused, and no
path component may be a symlink.  Preparation then opens the base and the runtime root with
``O_NOFOLLOW`` and *keeps both descriptors open for the whole critical section*: every
snapshot, configuration, manifest, probe, subprocess ``cwd``, command target, read, and
cleanup operation is expressed against ``/proc/<pid>/fd/<root>``, and the manifest is
replaced with ``dir_fd``.  An attacker who renames the root away and drops a symlink in its
place therefore cannot capture a single later write -- everything still lands in the inode
we opened -- and ``_require_open_root`` fails the run closed before publication.  The
logical ``CandidateRuntime.root`` and every recorded path stay content addressed.

``uv`` receives cwd-relative arguments rather than ``/proc`` paths, so the venv it builds
records the real absolute path and stays usable after this process exits.

**Sealed sync input.**  The caller's lock is read once through one ``O_NOFOLLOW``
descriptor and bound to ``CandidateLock.digest``.  Those bytes are then copied into a
sealed ``memfd`` (``F_SEAL_WRITE|F_SEAL_SHRINK|F_SEAL_GROW|F_SEAL_SEAL``) and *that* image
is what ``uv pip sync`` installs, so the installed bytes are immutable for the entire sync
and no transient mutation window exists.  A durable snapshot of the same bytes is written
into the runtime root as reusable evidence and re-bound to the digest before the sync,
after the sync, and on every reuse.

**Serialization.**  Read, verify, purge, build, and publication run under an exclusive
``flock`` on ``<runtime-base>/.<digest>.lock``, so a caller that observes "no manifest"
cannot purge a runtime another caller published in the meantime.  A published runtime is
never purged by a later verification failure.

**Identity.**  ``uv``, the base interpreter, and the manifest-declared ``ms`` and
``llm-framework-study`` interpreters live outside the runtime root and can change
independently, so the manifest binds each one's configured path, realpath, SHA-256, and
version output, and every field is re-measured and compared on reuse.  ``EnvironmentIdentity``
is unchanged: the executable bytes are recorded manifest-side.  Candidate executables live
inside the runtime root, so their SHA-256 is recomputed through the open descriptor and
their recorded version output is bound to those exact bytes -- fail-closed, without
launching a candidate on reuse.

Two environments are separated on purpose.  ``uv venv`` and ``uv pip sync`` are bootstrap
downloads: they inherit the ambient environment -- including the user's external-network
proxy -- and only redirect HOME, cache, config, temporary files, and the uv cache into the
runtime.  Every other command, and everything a candidate backend ever sees, comes from
:func:`minimal_backend_environment`: an exact allowlist with no proxy variable, no ambient
PATH, and no inherited ``PYTHONPATH``.

The manifest is published with an atomic descriptor-relative ``os.replace`` plus file and
directory fsync only after every verification succeeds, so a published manifest always
describes a complete runtime.  Production identity is captured before the work and
re-checked before publication and on every exit path.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from scripts.backend_eval.identity import bootstrap_environment, bootstrap_service_values
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
from scripts.backend_eval.process import (
    CommandResult,
    CommandRunner,
    CommandTimeout,
    Deadline,
    SealedImageError,
    acquire_exclusive_lock,
    descriptor_path,
    sealed_image,
    subprocess_runner,
)
from scripts.backend_eval.production_identity import (
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)
from scripts.backend_eval.source_binding import HelperExpectation

__all__ = [
    "BACKEND_ENVIRONMENT_KEYS",
    "DEFAULT_ENVIRONMENT_INTERPRETERS",
    "DEFAULT_RUNTIME_BASE",
    "MANIFEST_FILE_NAME",
    "REQUIREMENTS_SNAPSHOT_NAME",
    "RUNTIME_DIRECTORY_NAMES",
    "RUNTIME_FILE_NAMES",
    "RUNTIME_MANIFEST_SCHEMA_VERSION",
    "SEALED_REQUIREMENTS_ARGUMENT",
    "SERVICE_CONFIG_EXCLUDES",
    "SERVICE_CONFIG_PYTHON_VERSION",
    "SERVICE_CONFIG_RELPATHS",
    "CandidateRuntime",
    "ProductionIdentityChanged",
    "ProductionIdentityError",
    "RuntimePreparationError",
    "RuntimeRequest",
    "load_prepared_candidate_runtime",
    "minimal_backend_environment",
    "owned_runtime_directory_relpaths",
    "owned_runtime_file_relpaths",
    "prepare_candidate_runtime",
    "runtime_lock_path",
    "runtime_manifest_digest",
]

DEFAULT_RUNTIME_BASE = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval")
RUNTIME_DIRECTORY_NAMES = ("cache", "config", "home", "tmp", "venv")
MANIFEST_FILE_NAME = "runtime-manifest.json"
REQUIREMENTS_SNAPSHOT_NAME = "candidate-requirements.lock"
RUNTIME_FILE_NAMES = (REQUIREMENTS_SNAPSHOT_NAME, MANIFEST_FILE_NAME)
RUNTIME_MANIFEST_SCHEMA_VERSION = 1
# The executed sync reads a per-run sealed descriptor; the manifest records this stable name.
SEALED_REQUIREMENTS_ARGUMENT = "<sealed-candidate-requirements>"
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
_IDENTITY_RECORD_KEYS = ["path", "realpath", "sha256", "version_output"]
_EXECUTABLE_RECORD_KEYS = ["path", "sha256", "version_output"]
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check below then refuses it promptly rather than reading empty bytes or, for
# the owned-descendant walk, hanging the mode repair on a special node opened for fchmod.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
# A harness-owned write never carries O_TRUNC: an existing node keeps every byte until the
# descriptor it was opened through proves it is a regular file we own.  O_NONBLOCK makes a
# FIFO with no reader fail with ENXIO instead of blocking the calling thread forever.
_WRITE_FLAGS = os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_CREATE_FLAGS = _WRITE_FLAGS | os.O_CREAT | os.O_EXCL


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
    manifest_path: Path
    manifest_sha256: str
    permission_repairs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_absolute(self.root, "CandidateRuntime.root")
        owned = set(owned_runtime_file_relpaths())
        if tuple(self.permission_repairs) != tuple(sorted(self.permission_repairs)) or not owned.issuperset(
            self.permission_repairs
        ):
            raise ValueError(
                "CandidateRuntime.permission_repairs must be a sorted subset of the harness-owned runtime files"
            )
        if self.manifest_path != self.root / MANIFEST_FILE_NAME:
            raise ValueError(f"CandidateRuntime.manifest_path must be {self.root / MANIFEST_FILE_NAME}")
        if len(self.manifest_sha256) != 64 or self.manifest_sha256 != self.manifest_sha256.lower().strip():
            raise ValueError("CandidateRuntime.manifest_sha256 must be a canonical lowercase SHA-256 digest")
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
    """The fixed layout of one runtime root.

    ``logical_root`` is the content-addressed identity that is recorded everywhere;
    ``root`` and its children are the paths actually operated on, which are bound to the
    open runtime descriptor while preparation holds it.
    """

    logical_root: Path
    root: Path
    venv: Path
    home: Path
    cache: Path
    config: Path
    tmp: Path
    bin_dir: Path
    requirements: Path
    # The already-proven open runtime root.  Every harness-owned read and write below the
    # root walks out from *this* descriptor, one component at a time, rather than reopening
    # a pathname a later rename or symlink could have redirected.
    root_fd: int | None = None

    @staticmethod
    def from_root(root: Path) -> _Layout:
        """A purely logical layout, used for identity and for the public backend environment."""

        return _Layout._at(root, root, None)

    @staticmethod
    def opened(logical_root: Path, root_fd: int) -> _Layout:
        """A layout whose every operational path resolves through one open descriptor."""

        return _Layout._at(logical_root, descriptor_path(root_fd), root_fd)

    @staticmethod
    def _at(logical_root: Path, base: Path, root_fd: int | None) -> _Layout:
        venv = base / "venv"
        return _Layout(
            logical_root=logical_root,
            root=base,
            venv=venv,
            home=base / "home",
            cache=base / "cache",
            config=base / "config",
            tmp=base / "tmp",
            bin_dir=venv / "bin",
            requirements=base / REQUIREMENTS_SNAPSHOT_NAME,
            root_fd=root_fd,
        )

    def logical(self) -> _Layout:
        return _Layout.from_root(self.logical_root)

    def bound_root_fd(self) -> int:
        """The open runtime root, or a refusal: a logical layout owns no descriptor."""

        if self.root_fd is None:
            raise RuntimePreparationError(
                f"the runtime layout for {self.logical_root} is not bound to an open runtime root"
            )
        return self.root_fd


def prepare_candidate_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    *,
    expectation: HelperExpectation,
    runner: CommandRunner = subprocess_runner,
    deadline: Deadline | None = None,
) -> CandidateRuntime:
    """Return the service-owned runtime for ``lock``, preparing it at most once.

    Read, verify, purge, build, and publication are serialized per lock digest, so
    concurrent callers never race.  A published runtime is reused only after its manifest
    verifies in full against the on-disk state and the freshly re-measured identity of
    ``uv``, the base interpreter, and every declared environment interpreter; anything else
    fails closed.  Production identity must be unchanged before publication and on every
    exit path.
    """

    before = capture_production_identity(request.repo_root, expectation=expectation, deadline=deadline)
    try:
        runtime = _prepare(lock, request, runner, before, expectation, deadline)
    except BaseException as exc:
        # Drift raised inside the preparation is already the authoritative error.
        if not isinstance(exc, ProductionIdentityError):
            _assert_production_identity_unchanged(
                before, request.repo_root, expectation, cause=exc, deadline=deadline
            )
        raise
    _assert_production_identity_unchanged(
        before, request.repo_root, expectation, cause=None, deadline=deadline
    )
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
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> CandidateRuntime:
    # The caller's lock is read and bound to the digest exactly once, through one descriptor.
    source = _read_requirements_source(lock, request)
    logical_root = request.runtime_base / lock.digest
    base_fd = _open_confined_directory(request.runtime_base)
    try:
        _require_physical_identity(base_fd, request.runtime_base)
        _require_outside_protected(request.runtime_base, request.repo_root, RuntimePreparationError)
        with _runtime_lock(base_fd, request.runtime_base, lock.digest, deadline):
            # Opened once with O_NOFOLLOW and held open: every later path resolves through it.
            root_fd = _open_confined_child(base_fd, logical_root.name, logical_root)
            try:
                _require_open_root(base_fd, root_fd, logical_root, "at open")
                layout = _Layout.opened(logical_root, root_fd)
                published = _read_manifest(layout)
                if published is not None:
                    return _verify_published_runtime(
                        lock, request, layout, published, runner, base_fd, root_fd, deadline
                    )
                # No published manifest under the lock means no runtime was ever completed here.
                _purge_directory_contents(root_fd)
                try:
                    return _build_runtime(
                        lock, request, runner, layout, before, source, base_fd, root_fd, expectation, deadline
                    )
                except BaseException:
                    _purge_runtime_root(base_fd, root_fd, logical_root)
                    raise
            finally:
                os.close(root_fd)
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
    root_fd: int,
    expectation: HelperExpectation,
    deadline: Deadline | None,
) -> CandidateRuntime:
    _create_runtime_directories(root_fd, layout)
    install_env = _install_environment(layout)
    tools = _capture_tools(request, layout, runner, deadline)
    # Durable evidence of the verified bytes; the sync itself reads a sealed image of them.
    _write_confined_file(root_fd, REQUIREMENTS_SNAPSHOT_NAME, layout.logical_root, source)
    _require_snapshot(layout, lock.digest)
    venv_command = _venv_command(request)
    _run(runner, venv_command, layout, install_env, deadline)
    _require_venv_interpreter(layout, request)
    with _sealed_requirements(source) as sealed:
        _require_snapshot(layout, lock.digest)
        _run(runner, _sync_command(request, str(sealed)), layout, install_env, deadline)
        _require_sealed_image(sealed, source)
        _require_snapshot(layout, lock.digest)
    sync_command = _sync_command(request, SEALED_REQUIREMENTS_ARGUMENT)
    executables = _capture_executables(lock, layout, runner, deadline)
    environments, environment_executables = _capture_environments(request, layout, runner, deadline)
    service_configs = _write_service_configs(layout)
    payload = canonical_json(
        _manifest_mapping(
            lock, layout, (venv_command, sync_command), executables, tools, environment_executables,
            environments, service_configs,
        )
    )
    runtime = _runtime(lock, layout, executables, environments, service_configs, sha256_bytes(payload))
    _require_only_declared_entries(root_fd, layout.logical_root, published=False)
    # Publication happens only for a run whose root was never swapped and that provably left
    # production untouched.
    _require_open_root(base_fd, root_fd, layout.logical_root, "before publication")
    _assert_production_identity_unchanged(
        before, request.repo_root, expectation, cause=None, deadline=deadline
    )
    _publish_manifest(layout, root_fd, payload)
    _require_open_root(base_fd, root_fd, layout.logical_root, "after publication")
    # A freshly built runtime must already satisfy the contract reuse repairs: the ambient
    # umask never gets to widen a harness-written file.
    _require_owned_modes(root_fd, layout.logical_root)
    return runtime


def _runtime(
    lock: CandidateLock,
    layout: _Layout,
    executables: Mapping[str, Mapping[str, str]],
    environments: tuple[EnvironmentIdentity, ...],
    service_configs: tuple[ServiceConfigIdentity, ...],
    manifest_sha256: str,
    permission_repairs: tuple[str, ...] = (),
) -> CandidateRuntime:
    logical = layout.logical()
    return CandidateRuntime(
        root=logical.root,
        python=logical.bin_dir / "python",
        ty=logical.bin_dir / "ty",
        pyrefly=logical.bin_dir / "pyrefly",
        lock_digest=lock.digest,
        executable_hashes=tuple((name, executables[name]["sha256"]) for name in sorted(executables)),
        home=logical.home,
        cache=logical.cache,
        config=logical.config,
        environments=environments,
        service_configs=service_configs,
        manifest_path=logical.root / MANIFEST_FILE_NAME,
        manifest_sha256=manifest_sha256,
        permission_repairs=permission_repairs,
    )


def _capture_tools(
    request: RuntimeRequest, layout: _Layout, runner: CommandRunner, deadline: Deadline | None
) -> dict[str, dict[str, str]]:
    """Bind the identity of the two tools that live outside the runtime root."""

    return {
        "uv": _capture_executable_identity(
            "uv", request.uv, _VERSION_FLAG_ARGS, request.python, layout, runner, deadline
        ),
        "python": _capture_executable_identity(
            "python", request.python, _INTERPRETER_VERSION_ARGS, request.python, layout, runner, deadline
        ),
    }


def _capture_executable_identity(
    name: str,
    path: Path,
    version_args: Sequence[str],
    selected: Path,
    layout: _Layout,
    runner: CommandRunner,
    deadline: Deadline | None,
) -> dict[str, str]:
    """Measure one executable that lives outside the runtime root, bytes included."""

    realpath = Path(os.path.realpath(path))
    _require_existing_regular_file(realpath, f"{name} executable")
    result = _run(runner, (str(path), *version_args), layout, _minimal_environment(layout, selected), deadline)
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
    lock: CandidateLock, layout: _Layout, runner: CommandRunner, deadline: Deadline | None
) -> dict[str, dict[str, str]]:
    """Verify and record every locked candidate executable inside the runtime root."""

    executables: dict[str, dict[str, str]] = {}
    logical = layout.logical()
    for candidate in sorted(lock.candidates, key=lambda package: package.name):
        path = layout.venv / candidate.executable_relpath
        _require_regular_executable_inside(path, layout.root, f"candidate executable {candidate.name}")
        version_output = _capture_version(path, candidate.name, candidate.version, layout, runner, deadline)
        executables[candidate.name] = {
            "path": str(logical.venv / candidate.executable_relpath),
            "sha256": _confined_file_digest(
                layout.bound_root_fd(),
                f"venv/{candidate.executable_relpath}",
                layout.logical_root,
                f"candidate executable {candidate.name}",
            ),
            "version_output": version_output,
        }
    return executables


def _capture_version(
    path: Path, name: str, locked_version: str, layout: _Layout, runner: CommandRunner, deadline: Deadline | None
) -> str:
    result = _run(
        runner,
        (str(path), *_VERSION_FLAG_ARGS),
        layout,
        _minimal_environment(layout, layout.bin_dir / "python"),
        deadline,
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


def _capture_environments(
    request: RuntimeRequest, layout: _Layout, runner: CommandRunner, deadline: Deadline | None
) -> tuple[tuple[EnvironmentIdentity, ...], dict[str, dict[str, str]]]:
    """Measure every manifest-declared interpreter once, without any ambient PATH lookup.

    ``EnvironmentIdentity`` keeps its frozen public shape; the executable bytes are recorded
    alongside it, manifest-side, so reuse can refuse a changed interpreter that still reports
    the same path, realpath, and version.
    """

    identities: list[EnvironmentIdentity] = []
    records: dict[str, dict[str, str]] = {}
    for name, interpreter in request.environment_interpreters:
        record = _capture_executable_identity(
            name, interpreter, _INTERPRETER_VERSION_ARGS, interpreter, layout, runner, deadline
        )
        version = record["version_output"]
        if len(version.splitlines()) != 1:
            raise RuntimePreparationError(
                f"environment interpreter {name} did not report a single version line: {interpreter}"
            )
        identities.append(
            EnvironmentIdentity(
                name=name,
                interpreter_path=record["path"],
                interpreter_realpath=record["realpath"],
                version=version,
            )
        )
        records[name] = record
    return tuple(identities), records


def _write_service_configs(layout: _Layout) -> tuple[ServiceConfigIdentity, ...]:
    """Materialize the deterministic service-owned configuration below the runtime config."""

    identities: list[ServiceConfigIdentity] = []
    logical = layout.logical()
    root_fd = layout.bound_root_fd()
    for backend in sorted(SERVICE_CONFIG_RELPATHS):
        relpath = SERVICE_CONFIG_RELPATHS[backend]
        payload = _service_config_bytes(backend)
        # ``config/<backend>/<file>``: every component, including the per-backend directory
        # this creates, is opened from its parent's descriptor below the open runtime root.
        _write_confined_file(root_fd, f"config/{relpath}", layout.logical_root, payload)
        identities.append(
            ServiceConfigIdentity(
                backend=backend,
                config_path=str(logical.config / relpath),
                config_sha256=sha256_bytes(payload),
                home_path=str(logical.home),
                cache_path=str(logical.cache),
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


def _venv_command(request: RuntimeRequest) -> tuple[str, ...]:
    """Targets are cwd-relative: the child inherits the open runtime root as its cwd.

    That binds the target to the inode we opened rather than to a pathname an attacker can
    swap, and it keeps real absolute paths -- not ``/proc`` paths -- inside the created venv.
    """

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


def _sync_command(request: RuntimeRequest, requirements: str) -> tuple[str, ...]:
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


def _install_environment(layout: _Layout) -> dict[str, str]:
    """Bootstrap downloads keep the external-network proxy and nothing else ambient.

    The proxy, CA bundle, and locale come from the allowlist; ``HOME``, ``TMPDIR``, the XDG
    directories, and the uv cache are service owned.  No ambient ``UV_*``, ``PIP_*``,
    ``PYTHONPATH``, or ``PATH`` control is inherited.
    """

    return bootstrap_environment(
        bootstrap_service_values(
            home=layout.home,
            tmp=layout.tmp,
            cache=layout.cache,
            config=layout.config,
            uv_cache=layout.cache / "uv",
        )
    )


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
    runner: CommandRunner,
    command: Sequence[str],
    layout: _Layout,
    env: Mapping[str, str],
    deadline: Deadline | None = None,
) -> CommandResult:
    timeout = None if deadline is None else deadline.remaining()
    try:
        result = runner(command, cwd=layout.root, env=env, timeout=timeout)
    except CommandTimeout as exc:
        raise RuntimePreparationError(f"the runtime preparation command {command[0]} timed out: {exc}") from exc
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
def _runtime_lock(base_fd: int, runtime_base: Path, digest: str, deadline: Deadline | None) -> Iterator[None]:
    """Hold the exclusive per-digest lock across read, verify, purge, build, and publish.

    ``flock`` is held on an open file description, so two threads and two processes
    contend identically; closing the descriptor releases it on every exit path.  Waiting
    for it is bounded by the phase deadline, so a runtime another process is still
    preparing cannot carry this run past its ceiling.
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
            acquire_exclusive_lock(fd, deadline=deadline, step="candidate_runtime_lock")
        except OSError as exc:
            raise RuntimePreparationError(f"cannot lock {runtime_base / name}: {exc}") from exc
        yield
    finally:
        os.close(fd)


# --- published manifest --------------------------------------------------------


def _manifest_mapping(
    lock: CandidateLock,
    layout: _Layout,
    commands: Sequence[Sequence[str]],
    executables: Mapping[str, Mapping[str, str]],
    tools: Mapping[str, Mapping[str, str]],
    environment_executables: Mapping[str, Mapping[str, str]],
    environments: Sequence[EnvironmentIdentity],
    service_configs: Sequence[ServiceConfigIdentity],
) -> dict[str, object]:
    logical = layout.logical()
    return {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "candidate_lock_digest": lock.digest,
        "commands": [list(command) for command in commands],
        "directories": {name: str(logical.root / name) for name in RUNTIME_DIRECTORY_NAMES},
        "environment_executables": {name: dict(record) for name, record in environment_executables.items()},
        "environments": [_record(identity) for identity in environments],
        "executables": {name: dict(record) for name, record in executables.items()},
        "python": str(logical.bin_dir / "python"),
        "requirements_snapshot": {
            "path": str(logical.root / REQUIREMENTS_SNAPSHOT_NAME),
            "sha256": lock.digest,
        },
        "root": str(logical.root),
        "service_configs": [_record(identity) for identity in service_configs],
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


def _publish_manifest(layout: _Layout, root_fd: int, payload: bytes) -> None:
    """Publish the manifest atomically and durably, last of all, relative to the descriptor."""

    _write_confined_file(root_fd, _MANIFEST_TEMPORARY_NAME, layout.logical_root, payload)
    try:
        os.replace(
            _MANIFEST_TEMPORARY_NAME, MANIFEST_FILE_NAME, src_dir_fd=root_fd, dst_dir_fd=root_fd
        )
    except OSError as exc:
        raise RuntimePreparationError(
            f"cannot publish the runtime manifest below {layout.logical_root}: {exc}"
        ) from exc
    _fsync(root_fd, layout.logical_root)


def _read_manifest(layout: _Layout) -> tuple[Mapping[str, Any], bytes] | None:
    """Return the published manifest and its exact bytes, or ``None`` when never published."""

    path = layout.logical_root / MANIFEST_FILE_NAME
    payload = _read_confined_optional(
        layout.bound_root_fd(), MANIFEST_FILE_NAME, layout.logical_root, "published runtime manifest"
    )
    if payload is None:
        return None
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimePreparationError(f"cannot decode the published runtime manifest {path}: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RuntimePreparationError(f"published runtime manifest must be a JSON object: {path}")
    manifest = cast("dict[str, Any]", decoded)
    if payload != canonical_json(manifest):
        raise RuntimePreparationError(f"published runtime manifest is not canonical: {path}")
    return manifest, payload


def runtime_manifest_digest(root: Path) -> str:
    """Recompute the canonical runtime-manifest digest from the bytes on disk.

    The admission gate calls this independently of preparation, so the receipt's runtime
    binding is verified against the file rather than against a value carried in memory.
    """

    root_fd = _open_existing_confined_directory(root)
    try:
        return sha256_bytes(
            _read_confined_file(root_fd, MANIFEST_FILE_NAME, root, "published runtime manifest")
        )
    finally:
        os.close(root_fd)


def load_prepared_candidate_runtime(
    root: Path,
    *,
    expected_lock_digest: str,
    expected_manifest_sha256: str,
) -> CandidateRuntime:
    """Load one already-published runtime through a strictly read-only verification path.

    This seam never acquires a preparation lock, repairs permissions, runs a tool, resolves a
    new candidate lock, or consults ``PATH``.  It accepts only manifest evidence whose logical
    paths and bytes still describe the exact retained root supplied by the caller.
    """

    _require_absolute(root, "prepared runtime root")
    if root.name != expected_lock_digest:
        raise RuntimePreparationError("prepared runtime root is not addressed by the expected lock digest")
    for label, digest in (
        ("expected lock", expected_lock_digest),
        ("expected manifest", expected_manifest_sha256),
    ):
        if len(digest) != 64 or digest != digest.lower().strip():
            raise ValueError(f"{label} digest must be a canonical lowercase SHA-256")

    parent_fd = _open_existing_confined_directory(root.parent)
    try:
        try:
            root_fd = os.open(root.name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimePreparationError(f"cannot open prepared runtime root {root}: {exc}") from exc
        try:
            _require_open_root(parent_fd, root_fd, root, "read-only load open")
            layout = _Layout.opened(root, root_fd)
            published = _read_manifest(layout)
            if published is None:
                raise RuntimePreparationError(f"prepared runtime is missing {MANIFEST_FILE_NAME}: {root}")
            manifest, payload = published
            observed_manifest = sha256_bytes(payload)
            if observed_manifest != expected_manifest_sha256:
                raise RuntimePreparationError(
                    "prepared runtime manifest digest changed: "
                    f"{observed_manifest} != {expected_manifest_sha256}"
                )
            identity_before = _observe_prepared_runtime_identity(layout, manifest)
            runtime = _load_verified_manifest_runtime(
                layout,
                manifest,
                observed_manifest,
                expected_lock_digest,
            )
            identity_after = _observe_prepared_runtime_identity(layout, manifest)
            if identity_after != identity_before:
                raise RuntimePreparationError(
                    "prepared runtime identity-bearing paths changed during read-only verification"
                )
            _require_open_root(parent_fd, root_fd, root, "read-only load return")
            return runtime
        finally:
            os.close(root_fd)
    finally:
        os.close(parent_fd)


@dataclass(frozen=True)
class _RuntimeEntryObservation:
    label: str
    kind: str
    stat_fields: tuple[int, ...]
    content_or_target: str


@dataclass(frozen=True)
class _PreparedRuntimeObservation:
    root_entries: tuple[str, ...]
    entries: tuple[_RuntimeEntryObservation, ...]


def _observe_prepared_runtime_identity(
    layout: _Layout, manifest: Mapping[str, Any]
) -> _PreparedRuntimeObservation:
    """Observe the complete identity-bearing set bracketing read-only verification."""

    root_fd = layout.bound_root_fd()
    root = layout.logical_root
    entries: list[_RuntimeEntryObservation] = []
    directory_relpaths = set(owned_runtime_directory_relpaths())
    directory_relpaths.add("venv/bin")
    for relpath in sorted(directory_relpaths):
        entries.append(_observe_runtime_directory(root_fd, relpath, root))

    regular_relpaths = {
        MANIFEST_FILE_NAME,
        REQUIREMENTS_SNAPSHOT_NAME,
        "venv/bin/pyrefly",
        "venv/bin/ty",
        *(f"config/{relpath}" for relpath in SERVICE_CONFIG_RELPATHS.values()),
    }
    for relpath in sorted(regular_relpaths):
        entries.append(_observe_runtime_regular_file(root_fd, relpath, root))
    entries.append(_observe_runtime_symlink(root_fd, "venv/bin/python", root))

    external_groups = (
        ("tool", _expect_mapping(manifest.get("tools"), "tools")),
        (
            "environment interpreter",
            _expect_mapping(manifest.get("environment_executables"), "environment_executables"),
        ),
    )
    for group_label, records in external_groups:
        for name in sorted(records):
            record = _expect_mapping(records[name], f"{name} {group_label}")
            path = record.get("path")
            realpath = record.get("realpath")
            if not isinstance(path, str) or not isinstance(realpath, str):
                raise RuntimePreparationError(
                    f"published runtime manifest has a malformed {name} {group_label} identity"
                )
            entries.append(_observe_external_path_entry(Path(path), f"{name} {group_label} path"))
            entries.append(
                _observe_external_regular_file(Path(realpath), f"{name} {group_label} target")
            )
    return _PreparedRuntimeObservation(
        root_entries=tuple(sorted(_scandir_names(root_fd, root))),
        entries=tuple(entries),
    )


def _observation_stat(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_nlink,
        info.st_uid,
        info.st_gid,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _observe_runtime_directory(root_fd: int, relpath: str, root: Path) -> _RuntimeEntryObservation:
    expected = root if not relpath else root / relpath
    fd = _open_owned_descendant(root_fd, relpath, root, directory=True)
    try:
        _require_physical_identity(fd, expected)
        before = os.fstat(fd)
        if not stat.S_ISDIR(before.st_mode):
            raise RuntimePreparationError(f"prepared runtime directory changed identity: {expected}")
        _require_physical_identity(fd, expected)
        after = os.fstat(fd)
        if _observation_stat(after) != _observation_stat(before):
            raise RuntimePreparationError(f"prepared runtime directory changed while observed: {expected}")
        return _RuntimeEntryObservation(
            label=f"runtime:{relpath or '.'}",
            kind="directory",
            stat_fields=_observation_stat(after),
            content_or_target="",
        )
    finally:
        os.close(fd)


def _open_stable_observation_parent(path: Path) -> int:
    parent_fd = _open_existing_confined_directory(path.parent)
    try:
        _require_physical_identity(parent_fd, path.parent)
    except BaseException:
        os.close(parent_fd)
        raise
    return parent_fd


def _observe_regular_entry(path: Path, label: str) -> _RuntimeEntryObservation:
    parent_fd = _open_stable_observation_parent(path)
    try:
        return _observe_regular_from_parent(parent_fd, path, label)
    finally:
        os.close(parent_fd)


def _observe_regular_from_parent(
    parent_fd: int, path: Path, label: str
) -> _RuntimeEntryObservation:
    fd: int | None = None
    try:
        try:
            entry_before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            fd = os.open(path.name, _READ_FLAGS, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise RuntimePreparationError(f"published runtime is missing {label}: {path}") from exc
        except OSError as exc:
            raise RuntimePreparationError(f"cannot observe {label} {path}: {exc}") from exc
        opened_before = os.fstat(fd)
        if (
            not stat.S_ISREG(entry_before.st_mode)
            or not stat.S_ISREG(opened_before.st_mode)
            or (entry_before.st_dev, entry_before.st_ino)
            != (opened_before.st_dev, opened_before.st_ino)
        ):
            raise RuntimePreparationError(f"{label} must remain a regular file: {path}")
        payload = _read_owned_descriptor(fd, label, path)
        opened_after = os.fstat(fd)
        entry_after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        _require_physical_identity(parent_fd, path.parent)
        if (
            _observation_stat(opened_after) != _observation_stat(opened_before)
            or not stat.S_ISREG(entry_after.st_mode)
            or (entry_after.st_dev, entry_after.st_ino)
            != (opened_after.st_dev, opened_after.st_ino)
        ):
            raise RuntimePreparationError(f"{label} changed while observed: {path}")
        return _RuntimeEntryObservation(
            label=label,
            kind="regular",
            stat_fields=_observation_stat(opened_after),
            content_or_target=sha256_bytes(payload),
        )
    except OSError as exc:
        raise RuntimePreparationError(f"cannot observe {label} {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _observe_runtime_regular_file(
    root_fd: int, relpath: str, root: Path
) -> _RuntimeEntryObservation:
    path = root / relpath
    parent_relpath = relpath.rsplit("/", 1)[0] if "/" in relpath else ""
    parent_fd = _open_confined_relpath(
        root_fd,
        parent_relpath,
        root,
        leaf_flags=_NOFOLLOW_DIRECTORY_FLAGS,
    )
    try:
        _require_physical_identity(parent_fd, path.parent)
        return _observe_regular_from_parent(parent_fd, path, f"runtime:{relpath}")
    finally:
        os.close(parent_fd)


def _observe_external_regular_file(path: Path, label: str) -> _RuntimeEntryObservation:
    if not path.is_absolute():
        raise RuntimePreparationError(f"published runtime {label} path must be absolute")
    return _observe_regular_entry(path, label)


def _observe_external_path_entry(path: Path, label: str) -> _RuntimeEntryObservation:
    if not path.is_absolute():
        raise RuntimePreparationError(f"published runtime {label} path must be absolute")
    parent_fd = _open_stable_observation_parent(path)
    try:
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            target = os.readlink(path.name, dir_fd=parent_fd) if stat.S_ISLNK(before.st_mode) else ""
            after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise RuntimePreparationError(f"cannot observe {label} {path}: {exc}") from exc
        _require_physical_identity(parent_fd, path.parent)
        if _observation_stat(after) != _observation_stat(before) or not (
            stat.S_ISREG(after.st_mode) or stat.S_ISLNK(after.st_mode)
        ):
            raise RuntimePreparationError(f"published runtime {label} changed identity: {path}")
        return _RuntimeEntryObservation(
            label=label,
            kind="symlink" if stat.S_ISLNK(after.st_mode) else "regular",
            stat_fields=_observation_stat(after),
            content_or_target=target,
        )
    finally:
        os.close(parent_fd)


def _observe_runtime_symlink(root_fd: int, relpath: str, root: Path) -> _RuntimeEntryObservation:
    path = root / relpath
    parent_fd = _open_confined_relpath(
        root_fd,
        relpath.rsplit("/", 1)[0],
        root,
        leaf_flags=_NOFOLLOW_DIRECTORY_FLAGS,
    )
    try:
        _require_physical_identity(parent_fd, path.parent)
        try:
            before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            target = os.readlink(path.name, dir_fd=parent_fd)
            after = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise RuntimePreparationError(f"cannot observe selected python link {path}: {exc}") from exc
        _require_physical_identity(parent_fd, path.parent)
        if not stat.S_ISLNK(after.st_mode) or _observation_stat(after) != _observation_stat(before):
            raise RuntimePreparationError(f"prepared runtime selected python link changed identity: {path}")
        return _RuntimeEntryObservation(
            label=f"runtime:{relpath}",
            kind="symlink",
            stat_fields=_observation_stat(after),
            content_or_target=target,
        )
    finally:
        os.close(parent_fd)


def _load_verified_manifest_runtime(
    layout: _Layout,
    manifest: Mapping[str, Any],
    manifest_sha256: str,
    lock_digest: str,
) -> CandidateRuntime:
    root = layout.logical_root
    logical = layout.logical()
    if manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimePreparationError(f"published runtime manifest has an unsupported schema version: {root}")
    if manifest.get("candidate_lock_digest") != lock_digest or manifest.get("root") != str(root):
        raise RuntimePreparationError(f"published runtime manifest does not describe {root}")
    if manifest.get("directories") != {name: str(root / name) for name in RUNTIME_DIRECTORY_NAMES}:
        raise RuntimePreparationError(f"published runtime manifest does not describe the layout of {root}")
    if manifest.get("requirements_snapshot") != {
        "path": str(logical.requirements),
        "sha256": lock_digest,
    }:
        raise RuntimePreparationError(f"published runtime manifest does not describe the installed lock of {root}")
    if manifest.get("python") != str(logical.bin_dir / "python"):
        raise RuntimePreparationError(f"published runtime manifest does not describe the selected python of {root}")

    _require_only_declared_entries(layout.bound_root_fd(), root, published=True)
    _require_owned_modes(layout.bound_root_fd(), root)
    _require_digest(
        _read_confined_file(
            layout.bound_root_fd(),
            REQUIREMENTS_SNAPSHOT_NAME,
            root,
            "installed candidate requirements lock",
        ),
        lock_digest,
        logical.requirements,
    )

    tools = _expect_mapping(manifest.get("tools"), "tools")
    if sorted(tools) != ["python", "uv"]:
        raise RuntimePreparationError("published runtime manifest must record python and uv tools")
    verified_tools = {
        name: _verify_manifest_external_identity(tools[name], f"{name} tool")
        for name in sorted(tools)
    }
    _verify_manifest_commands(manifest.get("commands"), verified_tools)

    environment_records = _expect_mapping(
        manifest.get("environment_executables"), "environment_executables"
    )
    raw_environments = manifest.get("environments")
    if not isinstance(raw_environments, list):
        raise RuntimePreparationError("published runtime manifest does not record its environments")
    environments = tuple(_restore_environment(item) for item in raw_environments)
    if tuple(identity.name for identity in environments) != tuple(sorted(environment_records)):
        raise RuntimePreparationError("published runtime manifest records different evaluation environments")
    for identity in environments:
        record = _verify_manifest_external_identity(
            environment_records[identity.name], f"{identity.name} interpreter"
        )
        if (
            identity.interpreter_path,
            identity.interpreter_realpath,
            identity.version,
        ) != (record["path"], record["realpath"], record["version_output"]):
            raise RuntimePreparationError(
                f"published runtime environment identity does not match {identity.name} interpreter"
            )

    selected_python = verified_tools["python"]
    _verify_selected_python_link(layout, selected_python)
    executables = _verify_manifest_candidate_executables(layout, manifest)
    service_configs = _verify_published_service_configs(layout, manifest)
    return CandidateRuntime(
        root=root,
        python=logical.bin_dir / "python",
        ty=logical.bin_dir / "ty",
        pyrefly=logical.bin_dir / "pyrefly",
        lock_digest=lock_digest,
        executable_hashes=tuple((name, executables[name]["sha256"]) for name in sorted(executables)),
        home=logical.home,
        cache=logical.cache,
        config=logical.config,
        environments=environments,
        service_configs=service_configs,
        manifest_path=root / MANIFEST_FILE_NAME,
        manifest_sha256=manifest_sha256,
    )


def _verify_manifest_external_identity(value: object, label: str) -> dict[str, str]:
    entry = _expect_mapping(value, label)
    if sorted(entry) != _IDENTITY_RECORD_KEYS or any(not isinstance(item, str) for item in entry.values()):
        raise RuntimePreparationError(f"published runtime manifest has a malformed {label} identity")
    record = cast("dict[str, str]", dict(entry))
    path = Path(record["path"])
    realpath = Path(record["realpath"])
    if not path.is_absolute() or not realpath.is_absolute() or Path(os.path.realpath(path)) != realpath:
        raise RuntimePreparationError(f"published runtime {label} realpath identity changed")
    if not os.access(path, os.X_OK):
        raise RuntimePreparationError(f"published runtime {label} is not executable: {path}")
    if _file_digest(realpath, label) != record["sha256"]:
        raise RuntimePreparationError(f"published runtime {label} bytes changed: {realpath}")
    if not record["version_output"]:
        raise RuntimePreparationError(f"published runtime {label} has no version identity")
    return record


def _verify_manifest_commands(value: object, tools: Mapping[str, Mapping[str, str]]) -> None:
    expected = [
        [
            tools["uv"]["path"],
            "venv",
            "venv",
            "--python",
            tools["python"]["path"],
            "--no-python-downloads",
            "--python-preference",
            "only-system",
        ],
        [
            tools["uv"]["path"],
            "pip",
            "sync",
            SEALED_REQUIREMENTS_ARGUMENT,
            "--require-hashes",
            "--only-binary",
            ":all:",
            "--no-sources",
            "--no-python-downloads",
            "--python",
            "venv/bin/python",
        ],
    ]
    if value != expected:
        raise RuntimePreparationError("published runtime was prepared by a different command")


def _verify_selected_python_link(layout: _Layout, selected: Mapping[str, str]) -> None:
    parent_fd = _open_confined_relpath(
        layout.bound_root_fd(), "venv/bin", layout.logical_root, leaf_flags=_NOFOLLOW_DIRECTORY_FLAGS
    )
    try:
        try:
            target = os.readlink("python", dir_fd=parent_fd)
        except OSError as exc:
            raise RuntimePreparationError("prepared runtime selected python link is missing or invalid") from exc
    finally:
        os.close(parent_fd)
    if target != selected["path"]:
        raise RuntimePreparationError("prepared runtime selected python link changed identity")


def _verify_manifest_candidate_executables(
    layout: _Layout, manifest: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    recorded = _expect_mapping(manifest.get("executables"), "executables")
    if sorted(recorded) != ["pyrefly", "ty"]:
        raise RuntimePreparationError("published runtime manifest must record pyrefly and ty executables")
    verified: dict[str, dict[str, str]] = {}
    for name in sorted(recorded):
        entry = _expect_mapping(recorded[name], f"{name} executable")
        if sorted(entry) != _EXECUTABLE_RECORD_KEYS or any(
            not isinstance(item, str) for item in entry.values()
        ):
            raise RuntimePreparationError(f"published runtime manifest has a malformed {name} executable")
        expected_path = layout.logical().bin_dir / name
        if entry["path"] != str(expected_path) or not entry["version_output"]:
            raise RuntimePreparationError(f"published runtime executable {name} identity changed")
        fd = _open_owned_descendant(
            layout.bound_root_fd(), f"venv/bin/{name}", layout.logical_root, directory=False
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o111 == 0:
                raise RuntimePreparationError(f"candidate executable {name} must be executable")
            observed = sha256_bytes(_read_owned_descriptor(fd, f"candidate executable {name}", expected_path))
        finally:
            os.close(fd)
        if observed != entry["sha256"]:
            raise RuntimePreparationError(f"published runtime executable {name} changed: {expected_path}")
        verified[name] = cast("dict[str, str]", dict(entry))
    return verified


def _verify_published_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    layout: _Layout,
    published: tuple[Mapping[str, Any], bytes],
    runner: CommandRunner,
    base_fd: int,
    root_fd: int,
    deadline: Deadline | None,
) -> CandidateRuntime:
    """Reuse a published runtime only after the manifest verifies against the disk.

    Everything that lives outside the runtime root -- the installed snapshot's digest,
    ``uv``, the base interpreter, and every declared environment interpreter -- is
    re-measured now rather than trusted from the manifest.  Verification reads through the
    open descriptor, but the returned :class:`CandidateRuntime` names *logical* paths, so the
    logical root is re-checked once more immediately before it is returned: a root renamed
    and symlinked during one of these probes would otherwise hand the caller a runtime whose
    every path points at an attacker-controlled directory.

    Nothing here purges or writes anything: a verification failure -- including that final
    swap check -- leaves the already-published runtime exactly as it is under whatever
    directory it now lives in, and never touches the swapped-in target.
    """

    manifest, payload = published
    root = layout.logical_root
    logical = layout.logical()
    if manifest.get("schema_version") != RUNTIME_MANIFEST_SCHEMA_VERSION:
        raise RuntimePreparationError(f"published runtime manifest has an unsupported schema version: {root}")
    if manifest.get("candidate_lock_digest") != lock.digest or manifest.get("root") != str(root):
        raise RuntimePreparationError(f"published runtime manifest does not describe {root}")
    expected_commands = [
        list(_venv_command(request)),
        list(_sync_command(request, SEALED_REQUIREMENTS_ARGUMENT)),
    ]
    if manifest.get("commands") != expected_commands:
        raise RuntimePreparationError(f"published runtime {root} was prepared by a different command")
    if manifest.get("directories") != {name: str(root / name) for name in RUNTIME_DIRECTORY_NAMES}:
        raise RuntimePreparationError(f"published runtime manifest does not describe the layout of {root}")
    if manifest.get("requirements_snapshot") != {"path": str(logical.requirements), "sha256": lock.digest}:
        raise RuntimePreparationError(f"published runtime manifest does not describe the installed lock of {root}")
    _require_only_declared_entries(root_fd, root, published=True)
    for name in RUNTIME_DIRECTORY_NAMES:
        _require_owned_directory(layout.root / name)
    # Still under the per-digest lock, and before anything reads the published bytes: repair
    # the mode of any harness-written file left behind by a runtime built before the contract
    # was enforced, then re-assert the whole contract.  Modes only -- no byte moves.
    permission_repairs = _normalize_owned_modes(root_fd, root)
    _require_owned_modes(root_fd, root)
    _require_snapshot(layout, lock.digest)
    _require_venv_interpreter(layout, request)
    executables = _verify_published_executables(lock, layout, manifest)
    _verify_published_identities(
        _expect_mapping(manifest.get("tools"), "tools"), _capture_tools(request, layout, runner, deadline)
    )
    environments, environment_executables = _capture_environments(request, layout, runner, deadline)
    _verify_published_identities(
        _expect_mapping(manifest.get("environment_executables"), "environment_executables"),
        environment_executables,
    )
    _verify_published_environments(request, manifest, environments)
    service_configs = _verify_published_service_configs(layout, manifest)
    # The returned runtime is expressed in logical paths, so they must still be ours.
    _require_open_root(base_fd, root_fd, root, "before reuse return")
    return _runtime(
        lock, layout, executables, environments, service_configs, sha256_bytes(payload), permission_repairs
    )


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
        logical_path = layout.logical().venv / candidate.executable_relpath
        if entry["path"] != str(logical_path):
            raise RuntimePreparationError(f"published runtime executable {candidate.name} moved: {logical_path}")
        _require_regular_executable_inside(path, layout.root, f"candidate executable {candidate.name}")
        observed_digest = _confined_file_digest(
            layout.bound_root_fd(),
            f"venv/{candidate.executable_relpath}",
            layout.logical_root,
            f"candidate executable {candidate.name}",
        )
        if observed_digest != entry["sha256"]:
            raise RuntimePreparationError(f"published runtime executable {candidate.name} changed: {path}")
        version_output: str = entry["version_output"]
        _require_reported_version(version_output, candidate.version, f"candidate executable {candidate.name}")
        executables[candidate.name] = {
            "path": str(logical_path),
            "sha256": entry["sha256"],
            "version_output": version_output,
        }
    return executables


def _verify_published_identities(
    recorded: Mapping[str, Any], observed: Mapping[str, Mapping[str, str]]
) -> None:
    """Compare every recorded outside-the-root executable against a fresh measurement.

    Path, realpath, SHA-256, and version output must all match: an executable whose bytes
    changed at the same path, realpath, and reported version still fails closed.
    """

    if sorted(recorded) != sorted(observed):
        raise RuntimePreparationError(
            f"published runtime manifest does not record {sorted(observed)}"
        )
    for name in sorted(observed):
        entry = _expect_mapping(recorded[name], f"{name} identity")
        if sorted(entry) != _IDENTITY_RECORD_KEYS:
            raise RuntimePreparationError(f"published runtime manifest has a malformed {name} identity entry")
        if dict(entry) != dict(observed[name]):
            raise RuntimePreparationError(
                f"published runtime {name} identity changed: recorded {dict(entry)}, now {dict(observed[name])}"
            )


def _verify_published_environments(
    request: RuntimeRequest, manifest: Mapping[str, Any], observed: tuple[EnvironmentIdentity, ...]
) -> None:
    """The recorded EnvironmentIdentity tuple must equal what this run just measured."""

    recorded = manifest.get("environments")
    if not isinstance(recorded, list):
        raise RuntimePreparationError("published runtime manifest does not record its environments")
    environments = tuple(_restore_environment(item) for item in recorded)
    declared = tuple((name, str(interpreter)) for name, interpreter in request.environment_interpreters)
    if tuple((identity.name, identity.interpreter_path) for identity in environments) != declared:
        raise RuntimePreparationError("published runtime manifest declares different evaluation interpreters")
    if environments != observed:
        raise RuntimePreparationError(
            f"published runtime environments changed: recorded {environments}, now {observed}"
        )


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
        relpath = SERVICE_CONFIG_RELPATHS[identity.backend]
        path = layout.config / relpath
        expected = _service_config_bytes(identity.backend)
        if identity.config_path != str(layout.logical().config / relpath) or identity.config_sha256 != sha256_bytes(
            expected
        ):
            raise RuntimePreparationError(
                f"published runtime service configuration for {identity.backend} is not the declared one: {path}"
            )
        observed = _read_confined_file(
            layout.bound_root_fd(),
            f"config/{relpath}",
            layout.logical_root,
            f"service configuration {identity.backend}",
        )
        if observed != expected:
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


def _open_existing_confined_directory(path: Path) -> int:
    """Open an existing absolute directory one component at a time, creating nothing.

    The independent manifest-digest recomputation has no descriptor handed to it, so it
    anchors its own walk at the filesystem root and refuses a symlinked component rather
    than trusting one ``O_NOFOLLOW`` on the last one.
    """

    try:
        fd = os.open("/", _DIRECTORY_FLAGS)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot open the filesystem root: {exc}") from exc
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=fd)
            except OSError as exc:
                raise RuntimePreparationError(
                    f"cannot open {path} without following a link: {exc}"
                ) from exc
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
        fd = os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimePreparationError(
            f"service-owned runtime path component {name!r} of {path} must be a directory, "
            f"not a symlink or special file: {exc}"
        ) from exc
    # ``mkdir`` is masked by the ambient umask; a service-owned directory is always 0700.
    try:
        os.fchmod(fd, _DIRECTORY_MODE)
    except OSError as exc:
        os.close(fd)
        raise RuntimePreparationError(
            f"cannot own the service-owned runtime path component {name!r} of {path}: {exc}"
        ) from exc
    return fd


def _require_open_root(base_fd: int, root_fd: int, root: Path, stage: str) -> None:
    """Prove the logical runtime path still names exactly the inode we hold open.

    A rename, a symlink dropped in its place, or any other swap fails the run closed --
    after every write has already been steered into the open descriptor rather than into
    whatever the logical path now points at.  ``stage`` records where the swap was caught;
    the check before publication is the one that keeps a swapped run from ever publishing.
    """

    swapped = f"the runtime root {root} was swapped during preparation ({stage})"
    try:
        entry = os.lstat(root.name, dir_fd=base_fd)
    except OSError as exc:
        raise RuntimePreparationError(f"{swapped}: {exc}") from exc
    opened = os.fstat(root_fd)
    if not stat.S_ISDIR(entry.st_mode) or (entry.st_dev, entry.st_ino) != (opened.st_dev, opened.st_ino):
        raise RuntimePreparationError(f"{swapped}: {root} no longer names the open runtime directory")
    physical = _physical_path(root_fd)
    if physical != root:
        raise RuntimePreparationError(f"{swapped}: the open runtime directory now lives at {physical}")


def _physical_path(fd: int) -> Path:
    try:
        return Path(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError as exc:
        raise RuntimePreparationError(f"cannot resolve the physical path of an open directory: {exc}") from exc


def _require_physical_identity(fd: int, path: Path) -> None:
    observed = _physical_path(fd)
    if observed != path:
        raise RuntimePreparationError(f"{path} physically resolves to {observed}")


def _create_runtime_directories(root_fd: int, layout: _Layout) -> None:
    for name in RUNTIME_DIRECTORY_NAMES:
        os.close(_open_confined_child(root_fd, name, layout.logical_root / name))


def _require_only_declared_entries(root_fd: int, root: Path, *, published: bool) -> None:
    files = RUNTIME_FILE_NAMES if published else (REQUIREMENTS_SNAPSHOT_NAME,)
    declared = sorted((*files, *RUNTIME_DIRECTORY_NAMES))
    observed = sorted(_scandir_names(root_fd, root))
    if observed != declared:
        unexpected = sorted(set(observed) - set(declared))
        raise RuntimePreparationError(
            f"runtime root {root} contains unexpected entries: {unexpected or observed}"
        )


def _scandir_names(dir_fd: int, root: Path) -> list[str]:
    try:
        with os.scandir(dir_fd) as entries:
            return [entry.name for entry in entries]
    except OSError as exc:
        raise RuntimePreparationError(f"cannot list the runtime directory {root}: {exc}") from exc


def _require_owned_directory(path: Path) -> None:
    """Refuse a runtime directory that is a symlink or a special node.

    Creation never goes through a pathname: :func:`_open_confined_directory_chain` makes and
    owns every component from its parent's descriptor, so no ``mkdir(parents=True)`` can be
    satisfied by a symlinked intermediate component.  This is the cheap lexical pre-check;
    :func:`_require_owned_modes` re-proves the same directories through descriptors.
    """

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


@contextmanager
def _sealed_requirements(payload: bytes) -> Iterator[Path]:
    """Yield the path of a sealed, immutable in-memory image of ``payload``.

    ``uv pip sync`` reads this image rather than any on-disk file, so the exact bytes it
    installs cannot be changed by anyone -- including transiently -- for the whole sync.
    """

    try:
        with sealed_image(REQUIREMENTS_SNAPSHOT_NAME, payload) as fd:
            yield descriptor_path(fd)
    except SealedImageError as exc:
        raise RuntimePreparationError(str(exc)) from exc


def _require_sealed_image(path: Path, payload: bytes) -> None:
    """The sealed image must still read back exactly the bytes it was built from."""

    if path.read_bytes() != payload:
        raise RuntimePreparationError(f"the sealed candidate requirements image changed: {path}")


def _require_snapshot(layout: _Layout, digest: str) -> None:
    """Re-bind the installed snapshot to the candidate lock digest."""

    _require_digest(
        _read_confined_file(
            layout.bound_root_fd(),
            REQUIREMENTS_SNAPSHOT_NAME,
            layout.logical_root,
            "installed candidate requirements lock",
        ),
        digest,
        layout.logical_root / REQUIREMENTS_SNAPSHOT_NAME,
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


# --- the harness-owned permission contract --------------------------------------------
#
# The 0600/0700 contract is scoped, deliberately, to what *this harness* writes: its own
# regular files (the installed lock snapshot, the published manifest, the three service
# configurations), its own lock files, and every service-owned ancestor directory.  It does
# not extend to the interiors of third-party trees.  ``uv`` and ``virtualenv`` create their
# own cache and environment files -- including a world-writable ``.lock`` -- and rewriting
# those modes would mean recursively chmod-ing a tool's private cache, breaking its own
# assumptions for no confidentiality gain: they already sit behind service-owned ``0700``
# ancestors, and they are excluded from the receipt's artifact-tree digest.  The boundary is
# therefore ownership, not location, and it is pinned by test.


def owned_runtime_file_relpaths() -> tuple[str, ...]:
    """Every harness-written regular file inside one runtime root, in canonical order."""

    configs = tuple(SERVICE_CONFIG_RELPATHS[backend] for backend in sorted(SERVICE_CONFIG_RELPATHS))
    return tuple(sorted((REQUIREMENTS_SNAPSHOT_NAME, MANIFEST_FILE_NAME, *(f"config/{name}" for name in configs))))


def owned_runtime_directory_relpaths() -> tuple[str, ...]:
    """Every service-owned directory inside one runtime root, the root itself first."""

    return ("", *RUNTIME_DIRECTORY_NAMES, *(f"config/{backend}" for backend in sorted(SERVICE_CONFIG_RELPATHS)))


class _MissingOwnedPath(RuntimePreparationError):
    """One harness-owned relative path does not exist below the open runtime root."""


def _open_owned_descendant(root_fd: int, relpath: str, root: Path, *, directory: bool) -> int:
    """Open one harness-owned path under the runtime root without following any link.

    ``O_NOFOLLOW`` guards only the *last* component, so a single
    ``open("config/ty/ty.toml", O_NOFOLLOW)`` still traverses a symlinked ``config`` or
    ``config/ty`` -- and an ``fchmod`` on the descriptor it returns would land on a file
    outside the root.  Every component is therefore opened from its parent's descriptor with
    ``O_NOFOLLOW``, starting at the already-proven open root, so the returned descriptor can
    only name something the runtime root physically contains.
    """

    flags = _NOFOLLOW_DIRECTORY_FLAGS if directory else _READ_FLAGS
    return _open_confined_relpath(root_fd, relpath, root, leaf_flags=flags)


def _open_confined_relpath(root_fd: int, relpath: str, root: Path, *, leaf_flags: int) -> int:
    """Walk one existing relative path out from the open runtime root, component by component."""

    parts = tuple(part for part in relpath.split("/") if part)
    if not parts:
        return os.dup(root_fd)
    current = os.dup(root_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            child = _open_confined_existing_child(
                current, part, root, relpath, leaf_flags if last else _NOFOLLOW_DIRECTORY_FLAGS
            )
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


def _open_confined_existing_child(parent_fd: int, name: str, root: Path, relpath: str, flags: int) -> int:
    try:
        return os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise _MissingOwnedPath(f"published runtime {root} is missing {relpath}") from exc
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            raise RuntimePreparationError(
                f"harness-owned runtime path must be a regular file: {root / relpath}"
            ) from exc
        raise RuntimePreparationError(
            f"cannot open {root / relpath} without following a link: {exc}"
        ) from exc


def _open_confined_directory_chain(root_fd: int, relpath: str, root: Path) -> int:
    """Create, own, and open every component of one directory relpath below the open root.

    ``mkdir(parents=True)`` on the logical pathname would silently accept a symlinked
    intermediate component and then let the write land wherever it points; each component is
    created and reopened ``O_NOFOLLOW`` from its parent's descriptor instead.
    """

    current = os.dup(root_fd)
    try:
        for part in (item for item in relpath.split("/") if item):
            child = _open_confined_child(current, part, root / relpath)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    return current


def _write_confined_file(root_fd: int, relpath: str, root: Path, payload: bytes) -> None:
    """Write one harness-owned ``0600`` regular file through a fully confined descriptor walk.

    No component is ever resolved by pathname: every directory on the way is created and
    reopened ``O_NOFOLLOW`` from its parent, and the leaf is opened non-blocking and *without*
    ``O_TRUNC``.  A FIFO with no reader therefore fails with ``ENXIO`` rather than blocking,
    a symlink fails with ``ELOOP``, and an existing file keeps every byte until ``fstat`` on
    that same descriptor proves it regular and ``fchmod`` proves we own it.
    """

    parts = tuple(part for part in relpath.split("/") if part)
    if not parts:
        raise RuntimePreparationError(f"the runtime root itself is not a harness-owned file: {root}")
    parent_fd = _open_confined_directory_chain(root_fd, "/".join(parts[:-1]), root)
    try:
        fd = _open_owned_write_leaf(parent_fd, parts[-1], root, relpath)
        try:
            _write_owned_descriptor(fd, payload, root / relpath)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _open_owned_write_leaf(parent_fd: int, name: str, root: Path, relpath: str) -> int:
    """Open the write leaf from its parent's descriptor, creating it only if it is absent."""

    try:
        return os.open(name, _WRITE_FLAGS, _FILE_MODE, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError as exc:
        if exc.errno == errno.ENXIO:
            # O_WRONLY|O_NONBLOCK on a FIFO with no reader: the node exists and is not regular.
            raise RuntimePreparationError(
                f"harness-owned runtime path must be a regular file: {root / relpath}"
            ) from exc
        raise RuntimePreparationError(
            f"cannot open {root / relpath} without following a link: {exc}"
        ) from exc
    try:
        return os.open(name, _CREATE_FLAGS, _FILE_MODE, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimePreparationError(
            f"cannot create {root / relpath} without following a link: {exc}"
        ) from exc


def _write_owned_descriptor(fd: int, payload: bytes, path: Path) -> None:
    """Prove the open descriptor is a regular file we own, then truncate and write it."""

    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise RuntimePreparationError(f"harness-owned runtime path must be a regular file: {path}")
    try:
        os.fchmod(fd, _FILE_MODE)
        os.ftruncate(fd, 0)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise RuntimePreparationError(f"cannot write {path}: {exc}") from exc
    after = os.fstat(fd)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino) or stat.S_IMODE(
        after.st_mode
    ) != _FILE_MODE:
        raise RuntimePreparationError(f"harness-owned runtime file changed while it was written: {path}")


def _read_confined_file(root_fd: int, relpath: str, root: Path, label: str) -> bytes:
    """Read one harness-owned regular file through the same confined walk the writes use."""

    fd = _open_owned_descendant(root_fd, relpath, root, directory=False)
    try:
        return _read_owned_descriptor(fd, label, root / relpath)
    finally:
        os.close(fd)


def _read_confined_optional(root_fd: int, relpath: str, root: Path, label: str) -> bytes | None:
    """The same read, with a missing path reported as ``None`` rather than as a failure."""

    try:
        return _read_confined_file(root_fd, relpath, root, label)
    except _MissingOwnedPath:
        return None


def _read_owned_descriptor(fd: int, label: str, path: Path) -> bytes:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        raise RuntimePreparationError(f"{label} must be a regular file: {path}")
    try:
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise RuntimePreparationError(f"cannot read {label} {path}: {exc}") from exc


def _confined_file_digest(root_fd: int, relpath: str, root: Path, label: str) -> str:
    return sha256_bytes(_read_confined_file(root_fd, relpath, root, label))


def _normalize_owned_modes(root_fd: int, root: Path) -> tuple[str, ...]:
    """Repair the mode of every harness-written file in a published runtime to ``0600``.

    Runtimes published before the mode contract was enforced carry ``0660`` files created
    under the ambient umask.  Reuse happens under the per-digest runtime lock, which is the
    only safe place to correct them, so the repair is done here: ``fchmod`` on a descriptor
    whose every component was opened ``O_NOFOLLOW`` from the open runtime root and which is
    proven to be a regular file *through that same descriptor*.  No byte is written, so
    neither the installed snapshot digest nor the published manifest digest can change; a
    symlink anywhere on the path, a special file, or anything outside the open root is
    refused rather than repaired.  The returned relative paths are the repair record.
    """

    repaired: list[str] = []
    for relpath in owned_runtime_file_relpaths():
        fd = _open_owned_descendant(root_fd, relpath, root, directory=False)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimePreparationError(f"harness-owned runtime path must be a regular file: {root / relpath}")
            if stat.S_IMODE(info.st_mode) == _FILE_MODE:
                continue
            try:
                os.fchmod(fd, _FILE_MODE)
            except OSError as exc:
                raise RuntimePreparationError(f"cannot own {root / relpath} at {_FILE_MODE:04o}: {exc}") from exc
            observed = stat.S_IMODE(os.fstat(fd).st_mode)
            if observed != _FILE_MODE:
                raise RuntimePreparationError(
                    f"{root / relpath} is {observed:04o} after repair, not {_FILE_MODE:04o}"
                )
            repaired.append(relpath)
        finally:
            os.close(fd)
    return tuple(repaired)


def _require_owned_modes(root_fd: int, root: Path) -> None:
    """Every harness-written file and service-owned directory is ``0600``/``0700``.

    Read through the same link-free descriptor walk the repair uses, so the mode this
    accepts is the mode of an inode the runtime root actually contains.
    """

    for relpath in owned_runtime_file_relpaths():
        fd = _open_owned_descendant(root_fd, relpath, root, directory=False)
        try:
            info = os.fstat(fd)
        finally:
            os.close(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimePreparationError(f"harness-owned runtime path must be a regular file: {root / relpath}")
        observed = stat.S_IMODE(info.st_mode)
        if observed != _FILE_MODE:
            raise RuntimePreparationError(f"harness-owned {root / relpath} is {observed:04o}, not {_FILE_MODE:04o}")
    for relpath in owned_runtime_directory_relpaths():
        fd = _open_owned_descendant(root_fd, relpath, root, directory=True)
        try:
            observed = stat.S_IMODE(os.fstat(fd).st_mode)
        finally:
            os.close(fd)
        if observed != _DIRECTORY_MODE:
            raise RuntimePreparationError(
                f"service-owned directory {root / relpath} is {observed:04o}, not {_DIRECTORY_MODE:04o}"
            )


def _fsync(fd: int, label: Path) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot fsync {label}: {exc}") from exc


def _purge_directory_contents(dir_fd: int) -> None:
    """Empty one directory through its descriptor, never following a link out of it."""

    try:
        with os.scandir(dir_fd) as entries:
            children = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]
        for name, is_directory in children:
            if not is_directory:
                os.unlink(name, dir_fd=dir_fd)
                continue
            child_fd = os.open(name, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=dir_fd)
            try:
                _purge_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=dir_fd)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot clear the runtime directory: {exc}") from exc


def _purge_runtime_root(base_fd: int, root_fd: int, root: Path) -> None:
    """Remove an unpublished or failed runtime through the descriptor we opened.

    The contents always go, because the descriptor still names our directory whatever
    happened to the pathname.  The directory entry itself is unlinked only while it still
    names that inode: an attacker-controlled entry is left alone rather than removed for
    them, and the caller's original failure is the one that propagates.
    """

    _purge_directory_contents(root_fd)
    _fsync(root_fd, root)
    try:
        _require_open_root(base_fd, root_fd, root, "before cleanup")
    except RuntimePreparationError:
        return
    try:
        os.rmdir(root.name, dir_fd=base_fd)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot remove the partially created runtime {root}: {exc}") from exc
    _fsync(base_fd, root.parent)


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
    before: ProductionIdentity,
    repo_root: Path,
    expectation: HelperExpectation,
    *,
    cause: BaseException | None,
    deadline: Deadline | None,
) -> None:
    """Re-check production identity; drift outranks and chains the failure that caused it."""

    try:
        assert_production_identity_unchanged(
            before, capture_production_identity(repo_root, expectation=expectation, deadline=deadline)
        )
    except ProductionIdentityError as identity_error:
        if cause is None:
            raise
        raise identity_error from cause
