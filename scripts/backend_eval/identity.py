"""Bind one admission run to the evaluator code, the CLI host, and the bootstrap environment.

Four facts decide whether a receipt is reproducible evidence of *this* evaluator:

* the digest of the complete ``scripts/backend_eval`` source image from which the command's
  semantic child imports, measured from the inherited sealed descriptor rather than from a
  later disk read or merely from a commit;
* the digest of the executed *production* helper closure -- the ``serena_light`` modules
  this evaluator runs for manifests, the write guard, and the production identity -- taken
  from :mod:`scripts.backend_eval.source_binding`, which refuses any helper resolved outside
  this checkout, so a CLI host virtual environment cannot silently supply another worktree's
  semantics behind an unchanged ``scripts/backend_eval`` digest;
* the source Git commit, recorded as corroboration when the checkout is available, plus
  whether the evaluator source and the production source were clean at that commit;
* the CLI host interpreter's configured path, realpath, SHA-256, and version.

The bootstrap environment is the fifth.  Resolver and installer calls keep the user's
external-network proxy, CA bundle, and locale -- nothing else.  Ambient package-index,
source, ``PATH``, and ``PYTHONPATH`` controls are refused by name, and the receipt records
only key names plus SHA-256 digests of values, so a proxy URL carrying a credential is
never published in plaintext.
"""

from __future__ import annotations

import os
import platform
import stat
import sys
from collections.abc import Mapping
from pathlib import Path

from scripts.backend_eval.models import (
    BootstrapEnvironmentIdentity,
    EvaluatorIdentity,
    sha256_bytes,
)
from scripts.backend_eval.process import (
    GIT_EXECUTABLE,
    CommandTimeout,
    Deadline,
    ExecutableBindingError,
    SealedImageError,
    bound_executable,
    descriptor_path,
    run_bounded_bytes,
    sealed_image,
)
from scripts.backend_eval.source_binding import (
    EVALUATION_OWNER_ROOT,
    PRODUCTION_SOURCE_ROOT,
    SourceBindingError,
    bind_production_source,
)
from scripts.backend_eval.source_image import (
    SourceImageError,
    dependency_source_files,
    evaluator_source_files,
    production_source_files,
    require_bound_runtime_modules,
    require_image_module,
    source_image_active,
    source_image_deadline_seconds,
)

__all__ = [
    "BOOTSTRAP_INHERITED_KEYS",
    "BOOTSTRAP_REFUSED_KEYS",
    "BOOTSTRAP_REFUSED_PREFIXES",
    "BOOTSTRAP_SERVICE_KEYS",
    "BOOTSTRAP_SERVICE_PATH",
    "EVALUATOR_PACKAGE",
    "IdentityError",
    "bootstrap_environment",
    "bootstrap_environment_identity",
    "bootstrap_service_values",
    "capture_evaluator_identity",
]

EVALUATOR_PACKAGE = EVALUATION_OWNER_ROOT / "scripts" / "backend_eval"

# The only ambient values a bootstrap download may inherit.
BOOTSTRAP_INHERITED_KEYS: tuple[str, ...] = (
    "ALL_PROXY",
    "CURL_CA_BUNDLE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
)

# Ambient package-index, source, PATH, and module-search controls are never inherited.
BOOTSTRAP_REFUSED_PREFIXES: tuple[str, ...] = (
    "CONDA",
    "GIT_",
    "HATCH_",
    "NODE_",
    "NPM_",
    "PDM_",
    "PIP_",
    "POETRY_",
    "PYTHON",
    "UV_",
)
BOOTSTRAP_REFUSED_KEYS: tuple[str, ...] = (
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "PATH",
    "PKG_CONFIG_PATH",
    "VIRTUAL_ENV",
)

# Every bootstrap call receives exactly these service-owned keys.
BOOTSTRAP_SERVICE_KEYS: tuple[str, ...] = (
    "HOME",
    "PATH",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_NO_CONFIG",
    "UV_PYTHON_DOWNLOADS",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)
BOOTSTRAP_SERVICE_PATH = "/usr/bin:/bin"

# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check below then refuses it promptly rather than reading empty bytes.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class IdentityError(RuntimeError):
    """The evaluator, host, or bootstrap environment cannot be bound exactly."""


# --- evaluator and host -----------------------------------------------------------


def capture_evaluator_identity(*, deadline: Deadline | None = None) -> EvaluatorIdentity:
    """Measure the executed evaluator and production closures and the CLI host interpreter."""

    source_files = _source_closure()
    _require_no_shadowed_module(dict(source_files))
    production_files = _production_closure()
    commit, clean = _source_commit(deadline, source_files)
    production_clean = (
        commit is not None
        and _is_clean(PRODUCTION_SOURCE_ROOT, deadline)
        and _production_matches_current_checkout(production_files)
    )
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise IdentityError(f"the CLI host interpreter is not an absolute path: {executable}")
    realpath = Path(os.path.realpath(executable))
    return EvaluatorIdentity.build(
        source_files=source_files,
        source_commit=commit,
        source_clean=clean,
        production_root=str(PRODUCTION_SOURCE_ROOT),
        production_files=production_files,
        production_clean=production_clean,
        host_python_path=str(executable),
        host_python_realpath=str(realpath),
        host_python_sha256=sha256_bytes(_read_regular_file(realpath)),
        host_python_version=platform.python_version(),
    )


def _source_closure() -> tuple[tuple[str, str], ...]:
    """Digest every evaluator module from the sealed image, or guarded disk imports in tests."""

    try:
        imaged = evaluator_source_files()
    except SourceImageError as error:
        raise IdentityError(str(error)) from error
    if imaged is not None:
        closure = {name: sha256_bytes(payload) for name, payload in imaged}
        try:
            dependencies = dependency_source_files()
        except SourceImageError as error:
            raise IdentityError(str(error)) from error
        if dependencies is not None:
            closure.update((name, sha256_bytes(payload)) for name, payload in dependencies)
        return tuple(sorted(closure.items()))

    try:
        names = sorted(entry.name for entry in os.scandir(EVALUATOR_PACKAGE) if entry.name.endswith(".py"))
    except OSError as error:
        raise IdentityError(f"cannot list the evaluator source closure {EVALUATOR_PACKAGE}: {error}") from error
    if not names:
        raise IdentityError(f"the evaluator source closure is empty: {EVALUATOR_PACKAGE}")
    closure = [(name, sha256_bytes(_read_regular_file(EVALUATOR_PACKAGE / name))) for name in names]
    scripts_init = EVALUATION_OWNER_ROOT / "scripts" / "__init__.py"
    closure.append(("scripts/__init__.py", sha256_bytes(_read_regular_file(scripts_init))))
    return tuple(sorted(closure))


def _production_closure() -> tuple[tuple[str, str], ...]:
    """Digest every executed production helper, refusing one loaded from another checkout."""

    try:
        imaged = production_source_files()
        if imaged is None:
            return bind_production_source()
        closure = dict(bind_production_source(modules={}))
        for name, payload in imaged:
            digest = sha256_bytes(payload)
            previous = closure.setdefault(name, digest)
            if previous != digest:
                raise IdentityError(
                    f"production source {name} differs between the sealed protocol image "
                    "and the child-helper execution expectation"
                )
        return tuple(sorted(closure.items()))
    except SourceBindingError as error:
        raise IdentityError(str(error)) from error
    except SourceImageError as error:
        raise IdentityError(str(error)) from error


def _require_no_shadowed_module(recorded: Mapping[str, str]) -> None:
    """Every imported evaluator module must be one of the files this closure recorded."""

    for name, module in tuple(sys.modules.items()):
        if (
            name != "scripts"
            and name != "scripts.backend_eval"
            and not name.startswith("scripts.backend_eval.")
        ):
            continue
        if source_image_active():
            try:
                require_image_module(name, module, set(recorded))
            except SourceImageError as error:
                raise IdentityError(str(error)) from error
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            raise IdentityError(
                f"imported evaluator module {name} has no origin in the recorded closure"
            )
        path = Path(origin).resolve()
        if name == "scripts":
            owned = path == EVALUATION_OWNER_ROOT / "scripts" / "__init__.py"
            recorded_name = "scripts/__init__.py"
        else:
            owned = path.parent == EVALUATOR_PACKAGE
            recorded_name = path.name
        if not owned or recorded_name not in recorded:
            raise IdentityError(f"imported evaluator module {name} is not part of the recorded closure: {path}")
    if source_image_active():
        try:
            require_bound_runtime_modules()
        except SourceImageError as error:
            raise IdentityError(str(error)) from error


def _source_commit(
    deadline: Deadline | None, source_files: tuple[tuple[str, str], ...]
) -> tuple[str | None, bool]:
    """Return the Git commit of the evaluator checkout and whether its source is clean."""

    revision = _git(("rev-parse", "HEAD"), deadline)
    if revision is None:
        return None, False
    commit = revision.decode("utf-8", "replace").strip()
    if len(commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit):
        return None, False
    clean = _is_clean(EVALUATOR_PACKAGE, deadline) and _is_clean(
        EVALUATION_OWNER_ROOT / "scripts" / "__init__.py", deadline
    )
    if source_image_active():
        clean = clean and _image_matches_current_checkout(source_files)
    return commit, clean


def _image_matches_current_checkout(source_files: tuple[tuple[str, str], ...]) -> bool:
    """A restored pathname cannot make transient image bytes look clean at the Git commit."""

    recorded = {
        name: digest for name, digest in source_files if not name.startswith("dependencies/")
    }
    try:
        names = sorted(entry.name for entry in os.scandir(EVALUATOR_PACKAGE) if entry.name.endswith(".py"))
    except OSError as error:
        raise IdentityError(f"cannot compare the sealed evaluator with {EVALUATOR_PACKAGE}: {error}") from error
    expected = {*names, "scripts/__init__.py"}
    protocol_image = source_image_deadline_seconds() == 5400.0
    if (
        not set(recorded) <= expected
        or "scripts/__init__.py" not in recorded
        or (not protocol_image and set(recorded) != expected)
    ):
        return False
    evaluator_matches = all(
        name == "scripts/__init__.py"
        or sha256_bytes(_read_regular_file(EVALUATOR_PACKAGE / name)) == digest
        for name, digest in recorded.items()
    )
    scripts_init = EVALUATION_OWNER_ROOT / "scripts" / "__init__.py"
    return evaluator_matches and sha256_bytes(_read_regular_file(scripts_init)) == recorded[
        "scripts/__init__.py"
    ]


def _production_matches_current_checkout(
    production_files: tuple[tuple[str, str], ...]
) -> bool:
    """A transient protocol production image remains visible after its path is restored."""

    for relative, digest in production_files:
        path = EVALUATION_OWNER_ROOT / relative
        try:
            if sha256_bytes(_read_regular_file(path)) != digest:
                return False
        except IdentityError:
            return False
    return True


def _is_clean(subtree: Path, deadline: Deadline | None) -> bool:
    """Whether the evaluator checkout has no tracked or untracked change below ``subtree``."""

    status = _git(("status", "--porcelain", "--", str(subtree)), deadline)
    return status is not None and not status.strip()


def _git(args: tuple[str, ...], deadline: Deadline | None) -> bytes | None:
    timeout = None if deadline is None else deadline.remaining()
    env = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "LANG": "C.UTF-8",
        "PATH": BOOTSTRAP_SERVICE_PATH,
    }
    try:
        executable = bound_executable(GIT_EXECUTABLE)
    except ExecutableBindingError as error:
        raise IdentityError(f"the declared Git executable cannot be bound: {error}") from error
    try:
        config = f'[safe]\n\tdirectory = "{EVALUATION_OWNER_ROOT}"\n'.encode()
        with sealed_image("backend-eval-identity-git-config", config) as config_fd:
            env["GIT_CONFIG_GLOBAL"] = str(descriptor_path(config_fd))
            result = run_bounded_bytes(
                [
                    str(executable),
                    "-c",
                    f"safe.directory={EVALUATION_OWNER_ROOT}",
                    *args,
                ],
                cwd=EVALUATOR_PACKAGE,
                env=env,
                timeout=timeout,
                pass_fds=(config_fd,),
            )
    except CommandTimeout as error:
        raise IdentityError(f"evaluator source Git probe timed out: {' '.join(args)}: {error}") from error
    except OSError:
        return None
    except SealedImageError as error:
        raise IdentityError(f"cannot build the explicit evaluator Git trust config: {error}") from error
    return result.stdout if result.returncode == 0 else None


def _read_regular_file(path: Path) -> bytes:
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise IdentityError(f"cannot open {path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise IdentityError(f"{path} must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as error:
        raise IdentityError(f"cannot read {path}: {error}") from error
    finally:
        os.close(fd)


# --- the bootstrap environment ------------------------------------------------------


def bootstrap_service_values(
    *, home: Path, tmp: Path, cache: Path, config: Path, uv_cache: Path
) -> dict[str, str]:
    """The exact service-owned half of every bootstrap environment."""

    values = {
        "HOME": str(home),
        "PATH": BOOTSTRAP_SERVICE_PATH,
        "TMPDIR": str(tmp),
        "UV_CACHE_DIR": str(uv_cache),
        # No ambient uv.toml or pyproject configuration may steer a bootstrap download.
        "UV_NO_CONFIG": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CACHE_HOME": str(cache),
        "XDG_CONFIG_HOME": str(config),
    }
    if tuple(sorted(values)) != BOOTSTRAP_SERVICE_KEYS:
        raise IdentityError("the service-owned bootstrap keys do not match their declaration")
    return values


def bootstrap_environment(
    service_values: Mapping[str, str], *, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return the exact bootstrap environment: allowlisted ambient values plus service state."""

    ambient = os.environ if environ is None else environ
    env = {**_inherited(ambient), **dict(service_values)}
    for key in env:
        if _is_refused(key) and key not in service_values:
            raise IdentityError(f"the bootstrap environment inherited a prohibited control: {key}")
    return env


def bootstrap_environment_identity(
    *, environ: Mapping[str, str] | None = None, service_keys: tuple[str, ...] = BOOTSTRAP_SERVICE_KEYS
) -> BootstrapEnvironmentIdentity:
    """Record the bootstrap environment by key name and value digest, never in plaintext."""

    ambient = os.environ if environ is None else environ
    inherited = _inherited(ambient)
    keys = tuple(sorted(inherited))
    refused = tuple(
        sorted(key for key in ambient if _is_refused(key) and key not in inherited and key not in service_keys)
    )
    return BootstrapEnvironmentIdentity(
        inherited_keys=keys,
        inherited_value_digests=tuple((key, sha256_bytes(inherited[key].encode("utf-8"))) for key in keys),
        service_keys=tuple(sorted(service_keys)),
        refused_keys=refused,
    )


def _inherited(ambient: Mapping[str, str]) -> dict[str, str]:
    return {key: ambient[key] for key in BOOTSTRAP_INHERITED_KEYS if ambient.get(key)}


def _is_refused(key: str) -> bool:
    return key in BOOTSTRAP_REFUSED_KEYS or key.startswith(BOOTSTRAP_REFUSED_PREFIXES)
