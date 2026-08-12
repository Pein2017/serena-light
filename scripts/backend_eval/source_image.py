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
import zipfile
from pathlib import Path
from types import ModuleType

SOURCE_IMAGE_ACTIVE_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE"
SOURCE_IMAGE_FD_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_FD"
SOURCE_IMAGE_PATH_KEY = "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_PATH"
SOURCE_IMAGE_OWNER_KEY = "SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT"
SOURCE_IMAGE_STARTED_KEY = "SERENA_LIGHT_BACKEND_EVAL_STARTED_MONOTONIC"
SOURCE_IMAGE_ACTIVE_VALUE = "1"

_GET_SEALS = 1034
_ALL_SEALS = 0x1 | 0x2 | 0x4 | 0x8
_EVALUATOR_PREFIX = "scripts/backend_eval/"


class SourceImageError(RuntimeError):
    """The evaluator image or one of its imported modules is not the sealed source universe."""


def source_image_active() -> bool:
    return os.environ.get(SOURCE_IMAGE_ACTIVE_KEY) == SOURCE_IMAGE_ACTIVE_VALUE


def evaluation_owner_root() -> Path:
    """The disk owner whose production sources and Git identity this image evaluates."""

    if source_image_active():
        configured = os.environ.get(SOURCE_IMAGE_OWNER_KEY)
        if configured is None:
            raise SourceImageError("the sealed evaluator did not receive its owner root")
        owner = Path(configured)
        if not owner.is_absolute():
            raise SourceImageError(f"the sealed evaluator owner root is not absolute: {owner}")
        return owner
    return Path(__file__).resolve().parent.parent.parent


def source_image_started() -> float | None:
    """The outer bootstrap's monotonic origin, shared across processes on this host."""

    if not source_image_active():
        return None
    raw = os.environ.get(SOURCE_IMAGE_STARTED_KEY)
    if raw is None:
        raise SourceImageError("the sealed evaluator did not receive its deadline origin")
    try:
        started = float(raw)
    except ValueError as error:
        raise SourceImageError(f"the sealed evaluator deadline origin is invalid: {raw!r}") from error
    if started < 0:
        raise SourceImageError(f"the sealed evaluator deadline origin is negative: {started}")
    return started


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
            return tuple((name.removeprefix(_EVALUATOR_PREFIX), archive.read(name)) for name in evaluator)
    except (OSError, zipfile.BadZipFile, KeyError) as error:
        raise SourceImageError(f"cannot read the evaluator source image: {error}") from error


def require_image_module(name: str, module: ModuleType, recorded: set[str]) -> None:
    """Require one loaded evaluator module to come from this image through its zip loader."""

    image_path = os.environ.get(SOURCE_IMAGE_PATH_KEY)
    if image_path is None:
        raise SourceImageError("the sealed evaluator did not receive its image path")
    origin = getattr(module, "__file__", None)
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
    raw = os.environ.get(SOURCE_IMAGE_FD_KEY)
    if raw is None or not raw.isdecimal():
        raise SourceImageError(f"the sealed evaluator image descriptor is invalid: {raw!r}")
    fd = int(raw)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise SourceImageError("the evaluator source image descriptor is not a regular file")
        if fcntl.fcntl(fd, _GET_SEALS) != _ALL_SEALS:
            raise SourceImageError("the evaluator source image descriptor is not immutable")
        return os.pread(fd, metadata.st_size + 1, 0)
    except OSError as error:
        raise SourceImageError(f"cannot read the sealed evaluator image descriptor: {error}") from error


__all__ = [
    "SOURCE_IMAGE_ACTIVE_KEY",
    "SOURCE_IMAGE_FD_KEY",
    "SOURCE_IMAGE_OWNER_KEY",
    "SOURCE_IMAGE_PATH_KEY",
    "SOURCE_IMAGE_STARTED_KEY",
    "SourceImageError",
    "evaluation_owner_root",
    "evaluator_source_files",
    "require_image_module",
    "source_image_active",
    "source_image_started",
]
