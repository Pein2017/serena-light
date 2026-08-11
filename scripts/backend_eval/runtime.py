"""Prepare one service-owned candidate runtime below an evaluation-owned runtime base.

The runtime is content addressed by the candidate-lock digest: the same frozen lock
always prepares ``<runtime-base>/<candidate-lock-digest>/`` and nothing else.  Only
``venv``, ``home``, ``cache``, ``config``, and ``tmp`` are created there, the compiled
lock is installed hash-locked into the evaluation venv through explicit ``uv`` and
interpreter paths, and every candidate executable is verified as a regular file inside
the runtime root before its SHA-256 and ``--version`` output are recorded.

Two environments are separated on purpose.  ``uv venv`` and ``uv pip sync`` are bootstrap
downloads: they inherit the ambient environment -- including the user's external-network
proxy -- and only redirect HOME, cache, config, temporary files, and the uv cache into the
runtime.  Everything a candidate backend or interpreter ever sees comes from
:func:`minimal_backend_environment`, which is an exact allowlist with no proxy variable,
no ambient PATH, and no inherited ``PYTHONPATH``.

Manifest-declared ``ms`` and ``llm-framework-study`` interpreters are resolved from their
explicit absolute paths -- never an ambient PATH lookup -- retaining the configured path,
its realpath, and the exact version reported by that interpreter.  Service-owned
``pyright``, ``ty``, and ``pyrefly`` configuration is materialized below the runtime config
directory from one shared declaration, never inside a corpus root.

The runtime manifest is published with an atomic ``os.replace`` plus file and directory
fsync only after every verification succeeds, so a published manifest always describes a
complete runtime.  A published runtime is reused only after the manifest is verified in
full against the on-disk state; a runtime without a published manifest is discarded and
rebuilt, and any failure removes the partially created runtime.  Production identity is
captured before the work and re-checked before publication and after every path.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from collections.abc import Mapping, Sequence
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
    "RUNTIME_DIRECTORY_NAMES",
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
]

DEFAULT_RUNTIME_BASE = Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval")
RUNTIME_DIRECTORY_NAMES = ("cache", "config", "home", "tmp", "venv")
MANIFEST_FILE_NAME = "runtime-manifest.json"
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
_DIRECTORY_MODE = 0o700


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


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    """The explicit inputs of one candidate runtime preparation.

    Every executable and interpreter is an absolute path: nothing is ever resolved
    through an ambient PATH lookup.
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
        )


def prepare_candidate_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    *,
    runner: CommandRunner = subprocess_runner,
) -> CandidateRuntime:
    """Return the service-owned runtime for ``lock``, preparing it at most once.

    A published runtime is reused only after its manifest verifies in full against the
    on-disk executables, configuration, and interpreters; anything else is rebuilt from
    the frozen lock.  Production identity must be unchanged before publication and on
    every exit path.
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
    _require_lock_matches_requirements(lock, request)
    layout = _Layout.from_root(request.runtime_base / lock.digest)
    manifest = _read_manifest(layout.root)
    if manifest is not None:
        return _verify_published_runtime(lock, request, layout, manifest)
    # No published manifest means no runtime was ever completed here.
    _purge_runtime_root(layout.root)
    try:
        return _build_runtime(lock, request, runner, layout, before)
    except BaseException:
        _purge_runtime_root(layout.root)
        raise


def _build_runtime(
    lock: CandidateLock,
    request: RuntimeRequest,
    runner: CommandRunner,
    layout: _Layout,
    before: ProductionIdentity,
) -> CandidateRuntime:
    _create_runtime_directories(layout)
    install_env = _install_environment(layout)
    venv_command = _venv_command(request, layout)
    _run(runner, venv_command, layout, install_env)
    _require_venv_interpreter(layout, request)
    sync_command = _sync_command(request, layout)
    _run(runner, sync_command, layout, install_env)
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
    _publish_manifest(layout, _manifest_mapping(lock, runtime, (venv_command, sync_command), executables))
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
            "sha256": sha256_bytes(path.read_bytes()),
            "version_output": version_output,
        }
    return executables


def _capture_version(
    path: Path, name: str, locked_version: str, layout: _Layout, runner: CommandRunner
) -> str:
    result = _run(runner, (str(path), "--version"), layout, _minimal_environment(layout, layout.bin_dir / "python"))
    version_output = result.stdout.strip()
    if not version_output:
        raise RuntimePreparationError(f"candidate executable {name} did not report a version: {path}")
    if locked_version not in version_output.splitlines()[0].split():
        raise RuntimePreparationError(
            f"candidate executable {name} does not report the locked version {locked_version}: {version_output!r}"
        )
    return version_output


def _capture_environment(
    name: str, interpreter: Path, layout: _Layout, runner: CommandRunner
) -> EnvironmentIdentity:
    """Record one manifest-declared interpreter without any ambient PATH lookup."""

    realpath = Path(os.path.realpath(interpreter))
    _require_regular_file(realpath, f"environment interpreter {name}")
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
        str(request.requirements_lock),
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
    """Build the exact allowlist every candidate and interpreter process receives."""

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


# --- published manifest --------------------------------------------------------


def _manifest_mapping(
    lock: CandidateLock,
    runtime: CandidateRuntime,
    commands: Sequence[Sequence[str]],
    executables: Mapping[str, Mapping[str, str]],
) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_MANIFEST_SCHEMA_VERSION,
        "candidate_lock_digest": lock.digest,
        "commands": [list(command) for command in commands],
        "directories": {name: str(runtime.root / name) for name in RUNTIME_DIRECTORY_NAMES},
        "environments": [_record(identity) for identity in runtime.environments],
        "executables": {name: dict(record) for name, record in executables.items()},
        "python": str(runtime.python),
        "root": str(runtime.root),
        "service_configs": [_record(identity) for identity in runtime.service_configs],
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

    _write_file(layout.root / _MANIFEST_TEMPORARY_NAME, canonical_json(manifest))
    try:
        os.replace(layout.root / _MANIFEST_TEMPORARY_NAME, layout.root / MANIFEST_FILE_NAME)
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
    lock: CandidateLock, request: RuntimeRequest, layout: _Layout, manifest: Mapping[str, Any]
) -> CandidateRuntime:
    """Reuse a published runtime only after the manifest verifies against the disk."""

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
    _require_only_declared_entries(root, published=True)
    for name in RUNTIME_DIRECTORY_NAMES:
        _require_owned_directory(root / name, create=False)
    _require_venv_interpreter(layout, request)
    executables = _verify_published_executables(lock, layout, manifest)
    environments = _verify_published_environments(request, manifest)
    service_configs = _verify_published_service_configs(layout, manifest)
    return _runtime(lock, layout, executables, environments, service_configs)


def _verify_published_executables(
    lock: CandidateLock, layout: _Layout, manifest: Mapping[str, Any]
) -> dict[str, dict[str, str]]:
    recorded = _expect_mapping(manifest.get("executables"), "executables")
    names = sorted(candidate.name for candidate in lock.candidates)
    if sorted(recorded) != names:
        raise RuntimePreparationError(f"published runtime manifest does not record {names}: {layout.root}")
    executables: dict[str, dict[str, str]] = {}
    for candidate in sorted(lock.candidates, key=lambda package: package.name):
        entry = _expect_mapping(recorded[candidate.name], f"{candidate.name} executable")
        if sorted(entry) != ["path", "sha256", "version_output"] or any(
            not isinstance(item, str) for item in entry.values()
        ):
            raise RuntimePreparationError(
                f"published runtime manifest has a malformed {candidate.name} executable entry"
            )
        path = layout.venv / candidate.executable_relpath
        if entry["path"] != str(path):
            raise RuntimePreparationError(f"published runtime executable {candidate.name} moved: {path}")
        _require_regular_executable_inside(path, layout.root, f"candidate executable {candidate.name}")
        if sha256_bytes(path.read_bytes()) != entry["sha256"]:
            raise RuntimePreparationError(f"published runtime executable {candidate.name} changed: {path}")
        version_output: str = entry["version_output"]
        if candidate.version not in version_output.splitlines()[0].split():
            raise RuntimePreparationError(
                f"published runtime executable {candidate.name} does not report the locked version "
                f"{candidate.version}"
            )
        executables[candidate.name] = {
            "path": str(path),
            "sha256": entry["sha256"],
            "version_output": version_output,
        }
    return executables


def _verify_published_environments(
    request: RuntimeRequest, manifest: Mapping[str, Any]
) -> tuple[EnvironmentIdentity, ...]:
    recorded = manifest.get("environments")
    if not isinstance(recorded, list):
        raise RuntimePreparationError("published runtime manifest does not record its environments")
    environments = tuple(_restore_environment(item) for item in recorded)
    declared = tuple((name, str(interpreter)) for name, interpreter in request.environment_interpreters)
    if tuple((identity.name, identity.interpreter_path) for identity in environments) != declared:
        raise RuntimePreparationError("published runtime manifest declares different evaluation interpreters")
    for identity in environments:
        interpreter = Path(identity.interpreter_path)
        realpath = Path(os.path.realpath(interpreter))
        _require_regular_file(realpath, f"environment interpreter {identity.name}")
        if str(realpath) != identity.interpreter_realpath:
            raise RuntimePreparationError(
                f"published runtime environment {identity.name} now resolves to {realpath}"
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
        _require_regular_file(path, f"service configuration {identity.backend}")
        if path.read_bytes() != expected:
            raise RuntimePreparationError(
                f"published runtime service configuration for {identity.backend} changed: {path}"
            )
    return identities


# --- filesystem ----------------------------------------------------------------


def _create_runtime_directories(layout: _Layout) -> None:
    _require_owned_directory(layout.root.parent)
    _require_owned_directory(layout.root)
    for name in RUNTIME_DIRECTORY_NAMES:
        _require_owned_directory(layout.root / name)


def _require_only_declared_entries(root: Path, *, published: bool) -> None:
    declared = sorted((MANIFEST_FILE_NAME, *RUNTIME_DIRECTORY_NAMES)) if published else sorted(
        RUNTIME_DIRECTORY_NAMES
    )
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
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot open the runtime directory {path}: {exc}") from exc
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RuntimePreparationError(f"cannot fsync the runtime directory {path}: {exc}") from exc
    finally:
        os.close(fd)


def _purge_runtime_root(root: Path) -> None:
    """Remove an unpublished or failed runtime without ever deleting through a symlink."""

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


def _require_lock_matches_requirements(lock: CandidateLock, request: RuntimeRequest) -> None:
    _require_regular_file(request.requirements_lock, "candidate requirements lock")
    digest = sha256_bytes(request.requirements_lock.read_bytes())
    if digest != lock.digest:
        raise RuntimePreparationError(
            f"{request.requirements_lock} does not match the candidate lock digest {lock.digest}: {digest}"
        )


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


def _require_runtime_base(runtime_base: Path, repo_root: Path) -> None:
    if not runtime_base.is_absolute():
        raise ValueError("RuntimeRequest.runtime_base must be an absolute path")
    if ".." in runtime_base.parts:
        raise ValueError("RuntimeRequest.runtime_base must not contain parent references")
    if runtime_base == repo_root or runtime_base.is_relative_to(repo_root):
        raise ValueError(
            f"RuntimeRequest.runtime_base must stay outside the production repository {repo_root}"
        )
    for corpus in default_corpus_requests():
        if runtime_base == corpus.root or runtime_base.is_relative_to(corpus.root):
            raise ValueError(f"RuntimeRequest.runtime_base must stay outside the corpus root {corpus.root}")


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
