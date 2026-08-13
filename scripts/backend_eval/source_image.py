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
_PSUTIL_PYTHON_MODULES = {
    "psutil": ("psutil/__init__.py", "dependencies/psutil/__init__.py"),
    "psutil._common": ("psutil/_common.py", "dependencies/psutil/_common.py"),
    "psutil._pslinux": ("psutil/_pslinux.py", "dependencies/psutil/_pslinux.py"),
    "psutil._psposix": ("psutil/_psposix.py", "dependencies/psutil/_psposix.py"),
}
_PSUTIL_EXTENSION_MODULES = {
    "psutil._psutil_linux": "dependencies/psutil/_psutil_linux.so",
    "psutil._psutil_posix": "dependencies/psutil/_psutil_posix.so",
}


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


def source_image_deadline_seconds() -> float | None:
    """Return the semantic phase budget carried by the sealed transport context."""

    if not source_image_active():
        return None
    value = getattr(_verified_context(), "deadline_seconds", None)
    if value is None:
        return None
    if not isinstance(value, float) or value <= 0:
        raise SourceImageError("the sealed evaluator deadline budget is invalid")
    return value


def require_sealed_execution() -> None:
    """Refuse publication unless this module and admission came from the sealed image."""

    context = _verified_context()
    admission = sys.modules.get("scripts.backend_eval.admission")
    if admission is None:
        raise SourceImageError("the admission module is not loaded")
    require_image_module("scripts.backend_eval.admission", admission, {"admission.py"})
    if getattr(context, "image_path", None) != getattr(getattr(admission, "__loader__", None), "archive", None):
        raise SourceImageError("the publishing admission module is not owned by the sealed image")


def require_protocol_execution() -> None:
    """Refuse protocol publication outside its explicit sealed command entry."""

    context = _verified_context()
    if getattr(context, "entrypoint", None) != "protocol_phase":
        raise SourceImageError("the sealed evaluator entrypoint is not the protocol phase")
    if getattr(context, "deadline_seconds", None) != 5400.0:
        raise SourceImageError("the sealed protocol evaluator does not carry the frozen 5400s budget")
    protocol_phase = sys.modules.get("scripts.backend_eval.protocol_phase")
    if protocol_phase is None:
        raise SourceImageError("the protocol phase module is not loaded")
    require_image_module(
        "scripts.backend_eval.protocol_phase", protocol_phase, {"protocol_phase.py"}
    )
    if getattr(context, "image_path", None) != getattr(
        getattr(protocol_phase, "__loader__", None), "archive", None
    ):
        raise SourceImageError("the publishing protocol module is not owned by the sealed image")


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


def production_source_files() -> tuple[tuple[str, bytes], ...] | None:
    """Return the exact image bytes of the protocol's reachable production closure.

    The identity is captured before probes run, so it must pre-bind function-local imports
    that may execute later.  The bootstrap's static reachable closure is the closed universe;
    every already-loaded member is additionally proven to use that archive's private loader.
    """

    if not source_image_active() or getattr(_verified_context(), "entrypoint", None) != "protocol_phase":
        return None
    archive, names = _source_archive()
    context = _verified_context()
    finder = getattr(context, "production_finder", None)
    entries = getattr(finder, "entries", None)
    loaders = getattr(finder, "loaders", None)
    if not isinstance(entries, dict) or not isinstance(loaders, dict):
        raise SourceImageError("the sealed production loader registry is absent")
    production_entries = {
        name for name in names if name.startswith("serena_light/") and name.endswith(".py")
    }
    finder_entries = {
        entry
        for bound in entries.values()
        if isinstance(bound, tuple)
        and len(bound) == 2
        and isinstance((entry := bound[0]), str)
    }
    if not production_entries or finder_entries != production_entries:
        raise SourceImageError(
            "the sealed production loader registry does not match the image closure"
        )
    with archive:
        for name, module in tuple(sys.modules.items()):
            if name != "serena_light" and not name.startswith("serena_light."):
                continue
            bound = entries.get(name)
            loader = loaders.get(name)
            origin = getattr(module, "__file__", None)
            if (
                not isinstance(bound, tuple)
                or len(bound) != 2
                or getattr(module, "__loader__", None) is not loader
                or getattr(loader, "finder", None) is not finder
            ):
                raise SourceImageError(
                    f"loaded production module {name} has no sealed-image origin: {origin!r}"
                )
            entry, _is_package = bound
            if not isinstance(entry, str):
                raise SourceImageError(f"loaded production module {name} has an invalid image entry")
            if entry not in names:
                raise SourceImageError(
                    f"loaded production module {name} is absent from the sealed protocol image"
                )
            expected_origin = str(Path(context.owner_root) / "src" / entry)
            if origin != expected_origin or getattr(loader, "entry", None) != entry:
                raise SourceImageError(
                    f"loaded production module {name} has no sealed-image origin: {origin!r}"
                )
        return tuple(
            (f"src/{entry}", archive.read(entry)) for entry in sorted(production_entries)
        )


def dependency_source_files() -> tuple[tuple[str, bytes], ...] | None:
    """Return the exact sealed psutil closure available to the protocol phase.

    As with production modules, dependency identity is captured before probes may execute a
    delayed import.  Every dependency byte is therefore pre-bound, while each dependency that
    is already loaded must prove that it came from the corresponding archive or descriptor.
    """

    if not source_image_active() or getattr(_verified_context(), "entrypoint", None) != "protocol_phase":
        return None
    archive, names = _source_archive()
    result: dict[str, bytes] = {}
    loaded = {
        name: module
        for name, module in tuple(sys.modules.items())
        if name == "psutil" or name.startswith("psutil.")
    }
    allowed = set(_PSUTIL_PYTHON_MODULES) | set(_PSUTIL_EXTENSION_MODULES)
    unexpected = sorted(set(loaded) - allowed)
    if unexpected:
        raise SourceImageError(
            f"the sealed psutil closure is not exact: unexpected={unexpected}"
        )
    with archive:
        for name, (entry, logical) in _PSUTIL_PYTHON_MODULES.items():
            if entry not in names:
                raise SourceImageError(f"the sealed protocol image omits {entry}")
            if module := loaded.get(name):
                _require_archive_module(name, module, entry)
            result[logical] = archive.read(entry)
    context = _verified_context()
    dependency_fds = getattr(context, "dependency_fds", None)
    if not isinstance(dependency_fds, dict) or set(dependency_fds) != set(_PSUTIL_EXTENSION_MODULES):
        raise SourceImageError("the sealed protocol dependency descriptor map is invalid")
    for name, logical in _PSUTIL_EXTENSION_MODULES.items():
        fd = dependency_fds[name]
        if module := loaded.get(name):
            origin = getattr(module, "__file__", None)
            loader_origin = getattr(getattr(module, "__loader__", None), "path", None)
            expected_origin = f"/proc/self/fd/{fd}"
            if origin != expected_origin or loader_origin != expected_origin:
                raise SourceImageError(
                    f"native dependency {name} did not load from its sealed descriptor: "
                    f"origin={origin!r} loader={loader_origin!r}"
                )
        result[logical] = _sealed_descriptor_bytes(fd, name)
    return tuple(sorted(result.items()))


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


def require_bound_runtime_modules() -> None:
    """Validate all protocol production and external modules against their sealed loaders."""

    if getattr(_verified_context(), "entrypoint", None) != "protocol_phase":
        return
    production_source_files()
    dependency_source_files()


def _require_archive_module(name: str, module: ModuleType, entry: str) -> None:
    image_path = _verified_context().image_path
    origin = getattr(module, "__file__", None)
    archive = getattr(getattr(module, "__loader__", None), "archive", None)
    if origin != f"{image_path}/{entry}" or archive != image_path:
        raise SourceImageError(
            f"module {name} did not load from the sealed protocol image: "
            f"origin={origin!r} loader_archive={archive!r}"
        )


def _source_archive() -> tuple[zipfile.ZipFile, set[str]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(_source_image_bytes()))
        names = archive.namelist()
        if len(names) != len(set(names)):
            archive.close()
            raise SourceImageError("the evaluator source image contains duplicate archive entries")
        return archive, set(names)
    except (OSError, zipfile.BadZipFile) as error:
        raise SourceImageError(f"cannot read the evaluator source image: {error}") from error


def _sealed_descriptor_bytes(fd: int, label: str) -> bytes:
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or fcntl.fcntl(fd, _GET_SEALS) != _ALL_SEALS:
            raise SourceImageError(f"native dependency {label} is not an immutable regular image")
        return os.pread(fd, metadata.st_size + 1, 0)
    except OSError as error:
        raise SourceImageError(f"cannot read native dependency {label}: {error}") from error


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
    "dependency_source_files",
    "evaluation_owner_root",
    "evaluator_source_files",
    "production_source_files",
    "require_bound_runtime_modules",
    "require_image_module",
    "require_protocol_execution",
    "require_sealed_execution",
    "source_image_active",
    "source_image_deadline_seconds",
    "source_image_started",
]
