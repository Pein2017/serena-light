"""Bind the production semantic helpers the evaluator actually executes.

``scripts/backend_eval`` is not the whole executed evaluator.  Manifests, the corpus write
guard, and the production-identity capture run *production* code -- the trust-inventory
normalization, the guarded directory opener, the dependency-lock digest, the build identity,
and the runtime paths -- imported as ``serena_light``.  A CLI host virtual environment
resolves that name through whatever editable ``.pth`` installed it, so the executed helper
bytes can come from a different checkout than the one whose ``scripts/backend_eval`` digest
the receipt publishes.  A receipt bound only to the evaluator package would then name code
that did not produce it.

This module closes that gap in two steps:

* :func:`bind_production_source` walks ``sys.modules`` for every loaded ``serena_light``
  module and requires each one to resolve *inside this checkout's* ``src/serena_light``.  A
  module imported from another worktree, from site-packages, or through a symlink out of the
  owner tree is a refused binding, not a warning -- the run fails closed rather than
  publishing evidence about code it cannot name.
* it then digests the bytes of every bound module, so the receipt carries the executed
  production source closure the same way it already carries the evaluator source closure.

Some production helpers are no longer executed *in this process* at all: the dependency-lock
digest, the build identity, the runtime paths, and the trust-inventory file digest run inside
the bounded child in :mod:`scripts.backend_eval.production_helper`, so ``sys.modules`` cannot
see them.  Dropping them from the bound closure would quietly narrow exactly the evidence this
module exists to publish -- the receipt would stop naming the bytes of the helpers whose
answers it carries.  :data:`CHILD_EXECUTED_HELPERS` therefore declares the modules that child
loads, they are digested from this checkout alongside the in-process ones, and a test requires
the child's own reported closure, for every operation it supports, to be a subset of that
declaration -- so a helper that starts importing something new fails a test instead of
silently leaving the receipt.

Changing a helper's bytes, or repointing the ``.pth`` at another checkout, therefore changes
the published identity or refuses the run, without any change to ``scripts/backend_eval``.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType

from scripts.backend_eval.models import canonical_json, sha256_bytes

__all__ = [
    "CHILD_EXECUTED_HELPERS",
    "EVALUATION_OWNER_ROOT",
    "PRODUCTION_PACKAGE",
    "PRODUCTION_PACKAGE_NAME",
    "PRODUCTION_SOURCE_ROOT",
    "SourceBindingError",
    "bind_production_source",
    "production_source_digest",
]

# <owner-root>/scripts/backend_eval/source_binding.py -> <owner-root>
EVALUATION_OWNER_ROOT = Path(__file__).resolve().parent.parent.parent
PRODUCTION_SOURCE_ROOT = EVALUATION_OWNER_ROOT / "src"
PRODUCTION_PACKAGE_NAME = "serena_light"
PRODUCTION_PACKAGE = PRODUCTION_SOURCE_ROOT / PRODUCTION_PACKAGE_NAME

# Every production module the bounded child loads, relative to the owner root.  The child
# executes these instead of this process, so ``sys.modules`` cannot report them; pinned by a
# test against the child's own reported closure for each operation it supports.
CHILD_EXECUTED_HELPERS: tuple[str, ...] = (
    "src/serena_light/__init__.py",
    "src/serena_light/bootstrap.py",
    "src/serena_light/build_identity.py",
    "src/serena_light/workspace/__init__.py",
    "src/serena_light/workspace/identity.py",
    "src/serena_light/workspace/inventory.py",
)

# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check below then refuses it promptly rather than reading empty bytes.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class SourceBindingError(RuntimeError):
    """An executed production helper cannot be bound to this evaluator's own checkout."""


def bind_production_source(
    *,
    modules: Mapping[str, ModuleType] | None = None,
    owner_root: Path = EVALUATION_OWNER_ROOT,
    child_helpers: Sequence[str] = CHILD_EXECUTED_HELPERS,
) -> tuple[tuple[str, str], ...]:
    """Digest every executed production helper, refusing any not owned by ``owner_root``.

    "Executed" covers both what this process loaded and what the bounded child loads: the
    latter never reaches ``sys.modules`` here, and leaving it out would narrow the receipt to
    stop naming the bytes of the helpers whose answers it carries.  ``child_helpers`` exists so
    a synthetic single-module checkout can be bound in a test; every real caller takes the
    declared :data:`CHILD_EXECUTED_HELPERS`, which a test pins against the child's own report.

    The returned pairs are ``(path relative to the owner root, SHA-256)`` in canonical sorted
    order -- for example ``("src/serena_light/workspace/inventory.py", "...")``.
    """

    loaded = sys.modules if modules is None else modules
    package_root = (owner_root / "src" / PRODUCTION_PACKAGE_NAME).resolve()
    digests: dict[str, str] = {}
    for relative in child_helpers:
        path = Path(os.path.realpath(owner_root / relative))
        _require_owned(relative, path, package_root)
        digests[relative] = sha256_bytes(_read_regular_file(path))
    for name in sorted(loaded):
        if name != PRODUCTION_PACKAGE_NAME and not name.startswith(f"{PRODUCTION_PACKAGE_NAME}."):
            continue
        module = loaded[name]
        for path in _module_paths(name, module, package_root):
            relative = path.relative_to(owner_root.resolve()).as_posix()
            digests.setdefault(relative, sha256_bytes(_read_regular_file(path)))
    if not digests:
        raise SourceBindingError(
            f"no production helper is loaded from {package_root}; the evaluator cannot bind its executed source"
        )
    return tuple((relative, digests[relative]) for relative in sorted(digests))


def production_source_digest(production_files: tuple[tuple[str, str], ...]) -> str:
    """The canonical digest of one bound production source closure."""

    return sha256_bytes(canonical_json({"production_files": [list(entry) for entry in production_files]}))


def _module_paths(name: str, module: ModuleType, package_root: Path) -> tuple[Path, ...]:
    """Every owned file one loaded ``serena_light`` module contributes, or a refusal."""

    origin = getattr(module, "__file__", None)
    if origin is None:
        # A namespace package has no file; every search location must still be owned.
        locations = tuple(getattr(module, "__path__", ()) or ())
        if not locations:
            raise SourceBindingError(f"production helper {name} has neither a file nor a search path")
        for location in locations:
            _require_owned(name, Path(os.path.realpath(location)), package_root)
        return ()
    path = Path(os.path.realpath(origin))
    _require_owned(name, path, package_root)
    return (path,)


def _require_owned(name: str, path: Path, package_root: Path) -> None:
    if path != package_root and package_root not in path.parents:
        raise SourceBindingError(
            f"production helper {name} is executed from {path}, outside this evaluator's "
            f"own production source {package_root}"
        )


def _read_regular_file(path: Path) -> bytes:
    try:
        fd = os.open(path, _READ_FLAGS)
    except OSError as error:
        raise SourceBindingError(f"cannot open the production helper {path}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceBindingError(f"the production helper {path} must be a regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as error:
        raise SourceBindingError(f"cannot read the production helper {path}: {error}") from error
    finally:
        os.close(fd)
