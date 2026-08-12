"""Capture the production identity the evaluation may never change.

The evaluation locks and installs candidate backends outside the production
dependency slot.  Every phase brackets its work with :func:`capture_production_identity`
and :func:`assert_production_identity_unchanged` so that a candidate resolution,
installation, probe, or cleanup that touched ``pyproject.toml``, ``uv.lock``,
``package-lock.json``, the dependency-lock digest, the build identity, or the
production runtime paths fails loudly instead of silently mutating the installed
service.

**Two read surfaces, each closed on its own terms.**

The three lock files are read *here*, through one guarded descriptor each: opened
``O_NOFOLLOW | O_NONBLOCK`` relative to the resolved root's own directory descriptor, proven
regular by ``fstat`` on that same descriptor, and read from it.  A check followed by
``Path.read_bytes()`` resolves the same mutable name twice, so a symlink dropped between the
two is followed and a FIFO dropped between the two blocks the phase inside one
uninterruptible ``open``; neither is possible through a single descriptor.

The dependency-lock digest, the build identity, and the runtime paths are *production*
semantics.  Reimplementing them here would drift, and ``src/serena_light`` is not edited to
close an evaluation-only exposure, so the exact production functions are executed --
unchanged, from this evaluator's own checkout -- inside the bounded child in
:mod:`scripts.backend_eval.production_helper`.  Those helpers do check a path's type and then
reopen it by name, so a substituted node can still block them; what the child guarantees is
that the block costs the phase its remaining deadline and a typed failure, with the whole
process group killed, rather than an unbounded hang the ceiling can never observe.  The
child's executed helper bytes are digest-bound to this checkout, so
:mod:`scripts.backend_eval.source_binding` still names every helper byte a receipt reports.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from scripts.backend_eval.models import ProductionIdentity, sha256_bytes
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.production_helper import ProductionHelperError, run_production_helper
from scripts.backend_eval.source_binding import SourceBindingError

PRODUCTION_IDENTITY_FILES = ("package-lock.json", "pyproject.toml", "uv.lock")

_IDENTITY_LABELS = (
    ("pyproject_toml_sha256", "pyproject.toml"),
    ("uv_lock_sha256", "uv.lock"),
    ("package_lock_json_sha256", "package-lock.json"),
    ("dependency_lock_digest", "dependency_lock_digest"),
    ("build_identity", "build_identity"),
    ("runtime_paths", "runtime_paths"),
)
# O_NONBLOCK keeps a FIFO or other blocking special node from hanging the open; the fstat
# regular-file check then refuses it promptly rather than reading empty bytes.
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY


class ProductionIdentityError(RuntimeError):
    """Raised when the production identity cannot be captured exactly."""


class ProductionIdentityChanged(ProductionIdentityError):
    """Raised when any production identity field changed across an evaluation step."""


def capture_production_identity(repo_root: Path, *, deadline: Deadline | None = None) -> ProductionIdentity:
    """Return the byte-exact production identity of ``repo_root``.

    The per-file digests are computed here through guarded descriptors; the dependency-lock
    digest, build identity, and runtime paths come from the production implementations,
    executed under ``deadline`` in a bounded, source-bound child.
    """

    root = repo_root.resolve()
    digests = {name: sha256_bytes(payload) for name, payload in _read_identity_inputs(root).items()}
    helper = _run_production_helpers(root, deadline)
    return ProductionIdentity(
        pyproject_toml_sha256=digests["pyproject.toml"],
        uv_lock_sha256=digests["uv.lock"],
        package_lock_json_sha256=digests["package-lock.json"],
        dependency_lock_digest=_expect_digest(helper, "dependency_lock_digest", root),
        build_identity=_expect_digest(helper, "build_identity", root),
        runtime_paths=_expect_runtime_paths(helper, root),
    )


def assert_production_identity_unchanged(before: ProductionIdentity, after: ProductionIdentity) -> None:
    """Raise when any production lock, digest, identity, or runtime path changed."""

    changed = [
        label
        for field_name, label in _IDENTITY_LABELS
        if getattr(before, field_name) != getattr(after, field_name)
    ]
    if changed:
        raise ProductionIdentityChanged(f"production identity changed: {', '.join(changed)}")


def _run_production_helpers(root: Path, deadline: Deadline | None) -> dict[str, object]:
    try:
        return run_production_helper("production_identity", {"root": str(root)}, deadline=deadline)
    except (ProductionHelperError, SourceBindingError) as exc:
        raise ProductionIdentityError(f"cannot capture production identity below {root}: {exc}") from exc


def _expect_digest(helper: dict[str, object], name: str, root: Path) -> str:
    value = helper.get(name)
    if not isinstance(value, str) or not value:
        raise ProductionIdentityError(f"the production helper did not report {name} below {root}")
    return value


def _expect_runtime_paths(helper: dict[str, object], root: Path) -> tuple[tuple[str, str], ...]:
    recorded = helper.get("runtime_paths")
    if not isinstance(recorded, list) or not recorded:
        raise ProductionIdentityError(f"the production helper did not report runtime_paths below {root}")
    entries: list[tuple[str, str]] = []
    for entry in recorded:
        if not isinstance(entry, list) or len(entry) != 2:
            raise ProductionIdentityError(f"the production helper reported a malformed runtime path below {root}")
        name, path = entry
        if not isinstance(name, str) or not isinstance(path, str):
            raise ProductionIdentityError(f"the production helper reported a malformed runtime path below {root}")
        entries.append((name, path))
    return tuple(sorted(entries))


def _read_identity_inputs(root: Path) -> dict[str, bytes]:
    """Read every declared lock input through one descriptor on the resolved root."""

    try:
        dir_fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise ProductionIdentityError(f"cannot open the production repository {root}: {exc}") from exc
    try:
        return {name: _read_guarded(dir_fd, name, root) for name in PRODUCTION_IDENTITY_FILES}
    finally:
        os.close(dir_fd)


def _read_guarded(dir_fd: int, name: str, root: Path) -> bytes:
    try:
        fd = os.open(name, _READ_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError as exc:
        raise ProductionIdentityError(f"missing production identity input: {root / name}") from exc
    except OSError as exc:
        raise ProductionIdentityError(
            f"cannot open the production identity input {root / name} without following a link: {exc}"
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ProductionIdentityError(
                f"production identity input must be a regular file: {root / name}"
            )
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as exc:
        raise ProductionIdentityError(
            f"cannot read the production identity input {root / name}: {exc}"
        ) from exc
    finally:
        os.close(fd)
