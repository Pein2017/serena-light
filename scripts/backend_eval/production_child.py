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

* It is executed by absolute file path under ``-I``, so no ``PYTHONPATH``, user site
  directory, or ambient ``scripts`` namespace package can shadow anything, and it strips its
  own directory from ``sys.path`` before importing any non-stdlib module.  ``serena_light``
  therefore resolves only from the ``src`` root the parent named.
* It reads one canonical-JSON request from ``stdin`` and writes one canonical-JSON response
  to ``stdout``, echoing the SHA-256 of the exact request bytes it consumed, so the parent
  can bind a response to the request that produced it.
* It reports the byte digest of every ``serena_light`` module it actually loaded, relative to
  the owner root, in the same shape :mod:`scripts.backend_eval.source_binding` publishes, so
  the parent can refuse a child that executed helper bytes the receipt does not name.

Nothing else is computed here, and no evaluation module is importable from here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

PRODUCTION_PACKAGE_NAME = "serena_light"
SUCCESS = 0
REFUSED = 3


def canonical_json(value: dict[str, Any]) -> bytes:
    """The same canonical form :func:`scripts.backend_eval.models.canonical_json` produces."""

    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def _isolate_import_path(src_root: str) -> str:
    """Leave exactly one importable non-stdlib location: the ``src`` root the parent named."""

    here = os.path.dirname(os.path.abspath(__file__))
    resolved = os.path.realpath(src_root)
    kept = [entry for entry in sys.path if entry and os.path.abspath(entry) != here]
    sys.path[:] = [entry for entry in kept if os.path.realpath(entry) != resolved]
    sys.path.insert(0, resolved)
    return resolved


def _require_owned(name: str, path: str, src_root: str) -> None:
    package_root = os.path.join(src_root, PRODUCTION_PACKAGE_NAME)
    if path != package_root and not path.startswith(package_root + os.sep):
        raise RuntimeError(f"production helper {name} is executed from {path}, outside {package_root}")


def _production_files(owner_root: str, src_root: str) -> list[list[str]]:
    """Every loaded production module, as ``[owner-relative path, SHA-256]``, sorted."""

    digests: dict[str, str] = {}
    for name in sorted(sys.modules):
        if name != PRODUCTION_PACKAGE_NAME and not name.startswith(f"{PRODUCTION_PACKAGE_NAME}."):
            continue
        module = sys.modules[name]
        origin = getattr(module, "__file__", None)
        if origin is None:
            for location in tuple(getattr(module, "__path__", ()) or ()):
                _require_owned(name, os.path.realpath(location), src_root)
            continue
        path = os.path.realpath(origin)
        _require_owned(name, path, src_root)
        relative = os.path.relpath(path, owner_root)
        if relative not in digests:
            digests[relative] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if not digests:
        raise RuntimeError(f"no production helper loaded from {src_root}")
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
        sys.stderr.write("usage: production_child.py <owner-root> <src-root>\n")
        return REFUSED
    owner_root = os.path.realpath(argv[1])
    src_root = _isolate_import_path(argv[2])
    payload = sys.stdin.buffer.read()
    response: dict[str, Any] = {"request_sha256": hashlib.sha256(payload).hexdigest()}
    status = SUCCESS
    try:
        request = json.loads(payload.decode("utf-8"))
        response["op"] = request["op"]
        result = _dispatch(request)
        response["production_files"] = _production_files(owner_root, src_root)
        response["result"] = result
    except BaseException as error:
        response["error_type"] = type(error).__name__
        response["error_message"] = str(error)
        status = REFUSED
    sys.stdout.buffer.write(canonical_json(response))
    sys.stdout.buffer.flush()
    return status


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
