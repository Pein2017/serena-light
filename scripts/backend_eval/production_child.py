"""The bounded child that executes production helpers, and nothing else.

Three production helpers the evaluation depends on -- ``dependency_lock_digest``,
``compute_build_identity``, and ``runtime_paths`` -- and the trust-inventory digest
``observe_file_digest`` all read files the evaluation does not own.  Every one of them
checks a path's type and then reopens that path by name (``Path.is_file()`` then
``Path.read_bytes()``, or ``O_RDONLY | O_NOFOLLOW`` with no ``O_NONBLOCK``), so a node
substituted between the check and the open blocks the calling thread inside one
uninterruptible syscall.  ``src/serena_light`` is production and is not edited to close an
evaluation-only exposure, so the evaluation runs *the exact production bytes* here instead,
in a child whose whole process group the phase deadline can kill.

The child is deliberately small and stdlib-only:

* **It imports production from a sealed image, never from disk.**  The parent verifies every
  expected helper file against the admission expectation through a confined component-wise
  walk and hands *those verified bytes* over as one sealed ``memfd`` image, addressed by
  descriptor.  This child installs a single meta-path finder over that image and never puts
  a ``src`` root on ``sys.path`` at all, so the bytes the parent compared are precisely the
  bytes Python compiles and executes.  A helper swapped on disk during the import window
  cannot be reached, because no import ever consults the disk.
* Each module keeps the ``__file__`` its on-disk import would have had, so production
  semantics that derive a repository root from ``__file__`` are unchanged.  Origin is proven
  by *loader identity*, not by that pathname: a ``serena_light`` module loaded by anything
  other than this image's loader is an escape and refuses the request.
* It reads one canonical-JSON request from ``stdin`` and writes one canonical-JSON response
  to ``stdout``, echoing the SHA-256 of the exact request bytes it consumed, so the parent
  can bind a response to the request that produced it.
* It reports the byte digest of every ``serena_light`` module it actually loaded, relative to
  the owner root, and requires that set to equal the image's declared closure exactly -- one
  module more, or one module fewer, than the operation's declaration refuses here and again
  in the parent.

Nothing else is computed here, and no evaluation module is importable from here.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType
from typing import Any

PRODUCTION_PACKAGE_NAME = "serena_light"
PRODUCTION_SOURCE_PREFIX = "src/"
SUCCESS = 0
REFUSED = 3


def canonical_json(value: dict[str, Any]) -> bytes:
    """The same canonical form :func:`scripts.backend_eval.models.canonical_json` produces."""

    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _ImageLoader(importlib.abc.Loader):
    """Execute one production module from the sealed image and from nowhere else."""

    def __init__(self, finder: _ImageFinder, relative: str, source: bytes, filename: str) -> None:
        self.finder = finder
        self.relative = relative
        self.source = source
        self.filename = filename

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> ModuleType | None:
        del spec
        return None

    def exec_module(self, module: ModuleType) -> None:
        # ``__file__`` is the pathname an ordinary import would have produced, so production
        # helpers that derive a repository root from it keep their exact semantics.  It is
        # never used to *find* anything here: the bytes below came from the sealed image.
        module.__file__ = self.filename
        exec(compile(self.source, self.filename, "exec", dont_inherit=True), module.__dict__)

    def get_source(self, fullname: str) -> str:
        del fullname
        return self.source.decode("utf-8")


class _ImageFinder(importlib.abc.MetaPathFinder):
    """The only way a ``serena_light`` module can enter this interpreter."""

    def __init__(self, modules: dict[str, tuple[str, bytes, bool, str]]) -> None:
        self.modules = modules
        self.loaders: dict[str, _ImageLoader] = {}

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        del path, target
        entry = self.modules.get(fullname)
        if entry is None:
            return None
        relative, source, is_package, filename = entry
        loader = _ImageLoader(self, relative, source, filename)
        self.loaders[fullname] = loader
        return importlib.util.spec_from_loader(fullname, loader, origin=filename, is_package=is_package)


def _module_name(relative: str) -> tuple[str, bool]:
    """``src/serena_light/workspace/inventory.py`` -> ``serena_light.workspace.inventory``."""

    if not relative.startswith(PRODUCTION_SOURCE_PREFIX) or not relative.endswith(".py"):
        raise RuntimeError(f"the sealed production image declares a non-source entry: {relative}")
    parts = relative[len(PRODUCTION_SOURCE_PREFIX) :].split("/")
    if parts[0] != PRODUCTION_PACKAGE_NAME or any(part in ("", ".", "..") for part in parts):
        raise RuntimeError(f"the sealed production image declares a foreign module: {relative}")
    if parts[-1] == "__init__.py":
        return ".".join(parts[:-1]), True
    return ".".join(parts)[: -len(".py")], False


def _load_image(image_fd: int, owner_root: str) -> _ImageFinder:
    """Decode the sealed source image the parent verified and built."""

    size = os.fstat(image_fd).st_size
    payload = os.pread(image_fd, size, 0)
    declared = json.loads(payload.decode("utf-8"))
    modules: dict[str, tuple[str, bytes, bool, str]] = {}
    for entry in declared["modules"]:
        relative, encoded = entry
        name, is_package = _module_name(relative)
        source = base64.b64decode(encoded, validate=True)
        modules[name] = (relative, source, is_package, os.path.join(owner_root, relative))
    if not modules:
        raise RuntimeError("the sealed production image declares no module")
    return _ImageFinder(modules)


def _install_image(finder: _ImageFinder) -> None:
    """Leave exactly one way to import ``serena_light``: this image, in front of everything."""

    here = os.path.dirname(os.path.abspath(__file__))
    sys.path[:] = [entry for entry in sys.path if entry and os.path.abspath(entry) != here]
    sys.meta_path.insert(0, finder)


def _production_files(finder: _ImageFinder) -> list[list[str]]:
    """Every loaded production module, as ``[owner-relative path, SHA-256]``, sorted.

    The set must equal the image's declared closure exactly.  A module the operation did not
    load is as much a refusal as one it loaded and the closure does not name, and a module
    that arrived through any loader other than this image's is an origin escape.
    """

    digests: dict[str, str] = {}
    for name in sorted(sys.modules):
        if name != PRODUCTION_PACKAGE_NAME and not name.startswith(f"{PRODUCTION_PACKAGE_NAME}."):
            continue
        module = sys.modules[name]
        loader = getattr(module, "__loader__", None)
        if not isinstance(loader, _ImageLoader) or loader.finder is not finder:
            raise RuntimeError(
                f"production helper {name} was loaded from {getattr(module, '__file__', '<unknown>')} "
                "rather than from the sealed production image this run verified"
            )
        digests[loader.relative] = hashlib.sha256(loader.source).hexdigest()
    declared = {relative for relative, _source, _package, _file in finder.modules.values()}
    if set(digests) != declared:
        raise RuntimeError(
            f"the loaded production closure {sorted(digests)} is not the declared closure {sorted(declared)}"
        )
    return [[relative, digests[relative]] for relative in sorted(digests)]


def _production_identity(root: str) -> dict[str, Any]:
    from serena_light.bootstrap import runtime_paths
    from serena_light.build_identity import compute_build_identity, dependency_lock_digest

    repository = Path(root)
    paths = runtime_paths(repository)
    return {
        "build_identity": compute_build_identity(repository),
        "dependency_lock_digest": dependency_lock_digest(repository),
        "runtime_paths": sorted([name, str(path)] for name, path in paths.items()),
    }


def _observe_file_digests(paths: list[str]) -> dict[str, Any]:
    from serena_light.workspace.inventory import observe_file_digest

    return {"digests": [[path, observe_file_digest(Path(path))] for path in paths]}


def _dispatch(request: dict[str, Any]) -> dict[str, Any]:
    operation = request["op"]
    if operation == "production_identity":
        return _production_identity(request["root"])
    if operation == "observe_file_digests":
        return _observe_file_digests(list(request["paths"]))
    raise RuntimeError(f"unknown production helper operation: {operation!r}")


def main(argv: list[str]) -> int:
    """Answer exactly one request, structurally, and never raise past this boundary."""

    if len(argv) != 3:
        sys.stderr.write("usage: production_child.py <owner-root> <source-image-fd>\n")
        return REFUSED
    owner_root = os.path.realpath(argv[1])
    payload = sys.stdin.buffer.read()
    response: dict[str, Any] = {"request_sha256": hashlib.sha256(payload).hexdigest()}
    status = SUCCESS
    try:
        finder = _load_image(int(argv[2]), owner_root)
        _install_image(finder)
        request = json.loads(payload.decode("utf-8"))
        response["op"] = request["op"]
        result = _dispatch(request)
        response["production_files"] = _production_files(finder)
        response["result"] = result
    except BaseException as error:  # a refusal is a response, never a traceback
        response["error_type"] = type(error).__name__
        response["error_message"] = str(error)
        status = REFUSED
    sys.stdout.buffer.write(canonical_json(response))
    sys.stdout.buffer.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
