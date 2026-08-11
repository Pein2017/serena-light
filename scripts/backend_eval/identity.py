"""Bind one admission run to the evaluator code, the CLI host, and the bootstrap environment.

Three facts decide whether a receipt is reproducible evidence of *this* evaluator:

* the digest of the executed ``scripts/backend_eval`` source closure, measured from the
  bytes actually imported rather than from a commit;
* the source Git commit, recorded as corroboration when the checkout is available, plus
  whether the evaluator source was clean at that commit;
* the CLI host interpreter's configured path, realpath, SHA-256, and version.

The bootstrap environment is the fourth.  Resolver and installer calls keep the user's
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
from scripts.backend_eval.process import CommandTimeout, Deadline, run_bounded_bytes

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

EVALUATOR_PACKAGE = Path(__file__).resolve().parent

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

_GIT_EXECUTABLE = "/usr/bin/git"
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW


class IdentityError(RuntimeError):
    """The evaluator, host, or bootstrap environment cannot be bound exactly."""


# --- evaluator and host -----------------------------------------------------------


def capture_evaluator_identity(*, deadline: Deadline | None = None) -> EvaluatorIdentity:
    """Measure the executed evaluator source closure and the CLI host interpreter."""

    source_files = _source_closure()
    _require_no_shadowed_module(dict(source_files))
    commit, clean = _source_commit(deadline)
    executable = Path(sys.executable)
    if not executable.is_absolute():
        raise IdentityError(f"the CLI host interpreter is not an absolute path: {executable}")
    realpath = Path(os.path.realpath(executable))
    return EvaluatorIdentity.build(
        source_files=source_files,
        source_commit=commit,
        source_clean=clean,
        host_python_path=str(executable),
        host_python_realpath=str(realpath),
        host_python_sha256=sha256_bytes(_read_regular_file(realpath)),
        host_python_version=platform.python_version(),
    )


def _source_closure() -> tuple[tuple[str, str], ...]:
    """Digest every Python module of the evaluator package through one guarded read each."""

    try:
        names = sorted(entry.name for entry in os.scandir(EVALUATOR_PACKAGE) if entry.name.endswith(".py"))
    except OSError as error:
        raise IdentityError(f"cannot list the evaluator source closure {EVALUATOR_PACKAGE}: {error}") from error
    if not names:
        raise IdentityError(f"the evaluator source closure is empty: {EVALUATOR_PACKAGE}")
    return tuple((name, sha256_bytes(_read_regular_file(EVALUATOR_PACKAGE / name))) for name in names)


def _require_no_shadowed_module(recorded: Mapping[str, str]) -> None:
    """Every imported evaluator module must be one of the files this closure recorded."""

    for name, module in tuple(sys.modules.items()):
        if not name.startswith("scripts.backend_eval"):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        path = Path(origin).resolve()
        if path.parent != EVALUATOR_PACKAGE or path.name not in recorded:
            raise IdentityError(f"imported evaluator module {name} is not part of the recorded closure: {path}")


def _source_commit(deadline: Deadline | None) -> tuple[str | None, bool]:
    """Return the Git commit of the evaluator checkout and whether its source is clean."""

    revision = _git(("rev-parse", "HEAD"), deadline)
    if revision is None:
        return None, False
    commit = revision.decode("utf-8", "replace").strip()
    if len(commit) not in {40, 64} or any(character not in "0123456789abcdef" for character in commit):
        return None, False
    status = _git(("status", "--porcelain", "--", str(EVALUATOR_PACKAGE)), deadline)
    return commit, status is not None and not status.strip()


def _git(args: tuple[str, ...], deadline: Deadline | None) -> bytes | None:
    timeout = None if deadline is None else deadline.remaining()
    env = {"PATH": BOOTSTRAP_SERVICE_PATH, "LANG": "C.UTF-8"}
    home = os.environ.get("HOME")
    if home:
        env["HOME"] = home
    try:
        result = run_bounded_bytes(
            [_GIT_EXECUTABLE, *args], cwd=EVALUATOR_PACKAGE, env=env, timeout=timeout
        )
    except CommandTimeout as error:
        raise IdentityError(f"evaluator source Git probe timed out: {' '.join(args)}: {error}") from error
    except OSError:
        return None
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
