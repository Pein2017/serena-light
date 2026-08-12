"""Runtime facts for an evaluator imported from the sealed command source image.

The command bootstrap passes one already-sealed zip descriptor to an isolated interpreter.
This module never trusts a pathname digest supplied by that bootstrap: it reads the inherited
descriptor itself, verifies all Linux write/grow/shrink seals, and hashes the evaluator entries
inside those exact archive bytes.  Ordinary test imports have no image descriptor and retain
the checkout-backed behavior.
"""

from __future__ import annotations

import fcntl
import io
import os
import stat
import sys
import zipfile
from pathlib import Path
from types import ModuleType

_BOOTSTRAP_CONTEXT_MODULE = "_serena_light_backend_eval_bootstrap"

_GET_SEALS = 1034
_ALL_SEALS = 0x1 | 0x2 | 0x4 | 0x8
_EVALUATOR_PREFIX = "scripts/backend_eval/"


class SourceImageError(RuntimeError):
    """The evaluator image or one of its imported modules is not the sealed source universe."""


def source_image_active() -> bool:
    try:
        _verified_context()
    except SourceImageError:
        return False
    return True


def evaluation_owner_root() -> Path:
    """The disk owner whose production sources and Git identity this image evaluates."""

    if _context() is not None:
        owner = Path(_verified_context().owner_root)
        if not owner.is_absolute():
            raise SourceImageError(f"the sealed evaluator owner root is not absolute: {owner}")
        return owner
    return Path(__file__).resolve().parent.parent.parent


def source_image_started() -> float | None:
    """The outer bootstrap's monotonic origin, shared across processes on this host."""

    if not source_image_active():
        return None
    return float(_verified_context().started)


def require_sealed_execution() -> None:
    """Refuse publication unless this module and admission came from the sealed image."""

    context = _verified_context()
    admission = sys.modules.get("scripts.backend_eval.admission")
    if admission is None:
        raise SourceImageError("the admission module is not loaded")
    require_image_module("scripts.backend_eval.admission", admission, {"admission.py"})
    if getattr(context, "image_path", None) != getattr(getattr(admission, "__loader__", None), "archive", None):
        raise SourceImageError("the publishing admission module is not owned by the sealed image")


def evaluator_source_files() -> tuple[tuple[str, bytes], ...] | None:
    """Return every evaluator module byte from the inherited sealed image, or ``None`` on disk."""

    if not source_image_active():
        return None
    payload = _source_image_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                raise SourceImageError("the evaluator source image contains duplicate archive entries")
            evaluator = sorted(
                name for name in names if name.startswith(_EVALUATOR_PREFIX) and name.endswith(".py")
            )
            if not evaluator:
                raise SourceImageError("the evaluator source image contains no evaluator modules")
            result = [(name.removeprefix(_EVALUATOR_PREFIX), archive.read(name)) for name in evaluator]
            if "scripts/__init__.py" not in names:
                raise SourceImageError("the evaluator source image omits scripts/__init__.py")
            result.append(("scripts/__init__.py", archive.read("scripts/__init__.py")))
            return tuple(sorted(result))
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise SourceImageError(f"cannot read the evaluator source image: {error}") from error


def require_image_module(name: str, module: ModuleType, recorded: set[str]) -> None:
    """Require one loaded evaluator module to come from this image through its zip loader."""

    image_path = _verified_context().image_path
    origin = getattr(module, "__file__", None)
    if name == "scripts":
        expected_name = "scripts/__init__.py"
        expected_origin = f"{image_path}/{expected_name}"
    else:
        relative = "/".join(name.removeprefix("scripts.backend_eval").lstrip(".").split("."))
        expected_name = "__init__.py" if not relative else f"{relative}.py"
        expected_origin = f"{image_path}/{_EVALUATOR_PREFIX}{expected_name}"
    loader = getattr(module, "__loader__", None)
    archive = getattr(loader, "archive", None)
    if expected_name not in recorded or origin != expected_origin or archive != image_path:
        raise SourceImageError(
            f"imported evaluator module {name} did not come from the sealed source image: "
            f"origin={origin!r} loader_archive={archive!r}"
        )


def _source_image_bytes() -> bytes:
    fd = _verified_context().image_fd
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceImageError("the evaluator source image descriptor is not a regular file")
        if fcntl.fcntl(fd, _GET_SEALS) != _ALL_SEALS:
            raise SourceImageError("the evaluator source image descriptor is not immutable")
        return os.pread(fd, metadata.st_size + 1, 0)
    except OSError as error:
        raise SourceImageError(f"cannot read the sealed evaluator image descriptor: {error}") from error


def _context() -> ModuleType | None:
    value = sys.modules.get(_BOOTSTRAP_CONTEXT_MODULE)
    return value if isinstance(value, ModuleType) else None


def _verified_context() -> ModuleType:
    context = _context()
    if context is None:
        raise SourceImageError("the sealed evaluator bootstrap context is absent")
    image_fd = getattr(context, "image_fd", None)
    image_path = getattr(context, "image_path", None)
    if not isinstance(image_fd, int) or image_path != f"/proc/self/fd/{image_fd}":
        raise SourceImageError("the sealed evaluator bootstrap descriptor is invalid")
    try:
        metadata = os.fstat(image_fd)
        if not stat.S_ISREG(metadata.st_mode) or fcntl.fcntl(image_fd, _GET_SEALS) != _ALL_SEALS:
            raise SourceImageError("the evaluator source image descriptor is not immutable")
    except OSError as error:
        raise SourceImageError(f"cannot verify the evaluator source image descriptor: {error}") from error
    return context


__all__ = [
    "SourceImageError",
    "evaluation_owner_root",
    "evaluator_source_files",
    "require_image_module",
    "require_sealed_execution",
    "source_image_active",
    "source_image_started",
]
