"""Bind the production semantic helpers the evaluator actually executes.

``scripts/backend_eval`` is not the whole executed evaluator.  Manifests, the corpus write
guard, and the production-identity capture depend on *production* code -- the trust-inventory
normalization and bounded indexing, the dependency-lock digest, the build identity, and the
runtime paths -- named ``serena_light``.  A CLI host virtual environment resolves that name
through whatever editable ``.pth`` installed it, so the executed helper bytes can come from a
different checkout than the one whose ``scripts/backend_eval`` digest the receipt publishes.
A receipt bound only to the evaluator package would then name code that did not produce it.

This module closes that gap in two steps:

* :func:`bind_production_source` walks ``sys.modules`` for every loaded ``serena_light``
  module and requires each one to resolve *inside this checkout's* ``src/serena_light``.  A
  module imported from another worktree, from site-packages, or through a symlink out of the
  owner tree is a refused binding, not a warning -- the run fails closed rather than
  publishing evidence about code it cannot name.  That walk is now a *backstop*: the evaluator
  process imports no production module at all, which a regression proves in a fresh
  interpreter, so an empty ``sys.modules`` contribution is the expected case rather than a
  narrowing.
* it then digests the bytes of every bound module, so the receipt carries the executed
  production source closure the same way it already carries the evaluator source closure.

No production helper is executed *in this process* at all any more.  The dependency-lock
digest, the build identity, the runtime paths, the trust-inventory file digest, the bounded
non-Git inventory, and the normalization of already-bounded Git candidate bytes all run inside
the bounded child in :mod:`scripts.backend_eval.production_helper`, so ``sys.modules`` cannot
see them.  Dropping them from the bound closure would quietly narrow exactly the evidence this
module exists to publish -- the receipt would stop naming the bytes of the helpers whose
answers it carries.  :data:`OPERATION_HELPER_CLOSURES` therefore declares, per child
operation, the exact modules that child may load; :data:`CHILD_EXECUTED_HELPERS` is their
union, they are digested from this checkout alongside the in-process ones, and both the child
at runtime and a test require the child's own reported closure to equal its operation's
declaration -- so a helper that starts importing something new refuses the run rather than
silently leaving the receipt.

Changing a helper's bytes, or repointing the ``.pth`` at another checkout, therefore changes
the published identity or refuses the run, without any change to ``scripts/backend_eval``.

**The identity is the execution expectation, not a record of it.**  Recording a closure after
the fact proves nothing about what ran: the first child use used to accept whatever bytes were
on disk at that moment and pin *those*, so a helper swapped between the identity capture and
the first use executed successfully and was only re-read afterwards.
:class:`HelperExpectation` closes that window structurally.  It is built *from* the captured
:class:`~scripts.backend_eval.models.EvaluatorIdentity`, carries the expected child-program
digest and the expected per-file helper closure, and is passed explicitly into every
production-helper call.  Before a child starts, the parent re-reads each expected file through
a confined component-wise walk, refuses any byte that is not the expected byte, and hands the
child *those verified bytes* in a sealed in-memory image -- so the bytes compared are the bytes
imported.  No process-global first-use pin exists, so two admissions in one process cannot
contaminate each other's truth.
"""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from scripts.backend_eval.models import EvaluatorIdentity, canonical_json, sha256_bytes
from scripts.backend_eval.source_image import evaluation_owner_root

__all__ = [
    "CHILD_EXECUTED_HELPERS",
    "EVALUATION_OWNER_ROOT",
    "OPERATION_HELPER_CLOSURES",
    "PRODUCTION_CHILD_NAME",
    "PRODUCTION_CHILD_RELPATH",
    "PRODUCTION_PACKAGE",
    "PRODUCTION_PACKAGE_NAME",
    "PRODUCTION_SOURCE_ROOT",
    "HelperExpectation",
    "SourceBindingError",
    "bind_production_source",
    "production_source_digest",
]

# In the sealed command child ``__file__`` names an entry inside the memfd zip image, not the
# checkout.  The image bootstrap passes the exact disk owner before this module is imported.
EVALUATION_OWNER_ROOT = evaluation_owner_root()
PRODUCTION_SOURCE_ROOT = EVALUATION_OWNER_ROOT / "src"
PRODUCTION_PACKAGE_NAME = "serena_light"
PRODUCTION_PACKAGE = PRODUCTION_SOURCE_ROOT / PRODUCTION_PACKAGE_NAME

# The evaluator program the bounded child executes, relative to the owner root.
PRODUCTION_CHILD_NAME = "production_child.py"
PRODUCTION_CHILD_RELPATH = f"scripts/backend_eval/{PRODUCTION_CHILD_NAME}"

# The exact production modules each child operation may load, relative to the owner root.
# The child executes these instead of this process, so ``sys.modules`` cannot report them.
# Membership is exact in both directions: an operation that loads one module more, or one
# module fewer, than its declaration refuses inside the child and again in the parent.
OPERATION_HELPER_CLOSURES: Mapping[str, tuple[str, ...]] = {
    "production_identity": (
        "src/serena_light/__init__.py",
        "src/serena_light/bootstrap.py",
        "src/serena_light/build_identity.py",
    ),
    "observe_file_digests": (
        "src/serena_light/__init__.py",
        "src/serena_light/workspace/__init__.py",
        "src/serena_light/workspace/identity.py",
        "src/serena_light/workspace/inventory.py",
    ),
    "bounded_non_git_inventory": (
        "src/serena_light/__init__.py",
        "src/serena_light/workspace/__init__.py",
        "src/serena_light/workspace/identity.py",
        "src/serena_light/workspace/inventory.py",
    ),
    "git_inventory_from_bytes": (
        "src/serena_light/__init__.py",
        "src/serena_light/workspace/__init__.py",
        "src/serena_light/workspace/identity.py",
        "src/serena_light/workspace/inventory.py",
    ),
}

# The union of those closures: every production module the bounded child can load, and
# therefore every one whose bytes a receipt must name even though this process never
# imports it.  Each operation's own closure is an exact allowed subset of this union.
CHILD_EXECUTED_HELPERS: tuple[str, ...] = tuple(
    sorted({relative for closure in OPERATION_HELPER_CLOSURES.values() for relative in closure})
)

# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check below then refuses it promptly rather than reading empty bytes.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


class SourceBindingError(RuntimeError):
    """An executed production helper cannot be bound to this evaluator's own checkout."""


@dataclass(frozen=True, slots=True)
class HelperExpectation:
    """The exact bytes one admission run requires every production-helper child to execute.

    Built once, from the :class:`~scripts.backend_eval.models.EvaluatorIdentity` the receipt
    publishes, and then passed explicitly into every production-helper call the run makes.
    It is an *expectation*, not an observation: the digests come from the identity that was
    captured before any child ran, so a helper or child program substituted after that
    capture cannot execute -- the parent's pre-execution comparison fails instead.

    ``closure`` names every module the child may load across all supported operations;
    :meth:`modules_for` narrows it to the exact subset one operation is allowed to load.
    """

    owner_root: Path
    child_digest: str
    closure: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.owner_root, Path) or not self.owner_root.is_absolute():
            raise SourceBindingError("HelperExpectation.owner_root must be an absolute path")
        _require_digest(self.child_digest, PRODUCTION_CHILD_RELPATH)
        recorded = tuple(relative for relative, _digest in self.closure)
        if recorded != CHILD_EXECUTED_HELPERS:
            raise SourceBindingError(
                "HelperExpectation.closure must name exactly the declared child-executed helpers "
                f"{list(CHILD_EXECUTED_HELPERS)}, not {list(recorded)}"
            )
        for relative, digest in self.closure:
            _require_digest(digest, relative)

    @staticmethod
    def from_identity(
        identity: EvaluatorIdentity, *, owner_root: Path = EVALUATION_OWNER_ROOT
    ) -> HelperExpectation:
        """Derive the execution expectation from the identity a receipt will publish.

        Every digest below is one the identity already carries, so the expectation cannot
        drift from the published evidence: enforcing it *is* enforcing the receipt.
        """

        source = dict(identity.source_files)
        child_digest = source.get(PRODUCTION_CHILD_NAME)
        if child_digest is None:
            raise SourceBindingError(
                f"the evaluator identity does not name {PRODUCTION_CHILD_NAME}; "
                "no production helper may execute without an expected child program"
            )
        production = dict(identity.production_files)
        missing = [relative for relative in CHILD_EXECUTED_HELPERS if relative not in production]
        if missing:
            raise SourceBindingError(
                f"the evaluator identity does not name every child-executed helper: {missing}"
            )
        return HelperExpectation(
            owner_root=owner_root,
            child_digest=child_digest,
            closure=tuple((relative, production[relative]) for relative in CHILD_EXECUTED_HELPERS),
        )

    def modules_for(self, operation: str) -> tuple[tuple[str, str], ...]:
        """The exact ``(owner-relative path, SHA-256)`` closure ``operation`` may load."""

        declared = OPERATION_HELPER_CLOSURES.get(operation)
        if declared is None:
            raise SourceBindingError(f"unknown production helper operation: {operation!r}")
        digests = dict(self.closure)
        return tuple((relative, digests[relative]) for relative in declared)


def _require_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise SourceBindingError(f"HelperExpectation needs a SHA-256 digest for {label}, not {value!r}")


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
