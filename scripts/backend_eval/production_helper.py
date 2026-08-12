"""Run one production helper in a bounded, source-bound, minimal-environment child.

The evaluation must not edit ``src/serena_light`` to close an evaluation-only exposure, and
it must not reimplement a production helper's semantics -- a copy drifts, and a receipt that
names production while running a copy is worse than one that names neither.  So the exact
production bytes are executed, in :mod:`scripts.backend_eval.production_child`, under the
phase's own monotonic ceiling.

What this module guarantees about that child:

* **Bounded.** The remaining time on the caller's :class:`~scripts.backend_eval.process.Deadline`
  is the child's timeout, and :func:`~scripts.backend_eval.process.run_bounded_bytes` starts it
  in its own session and ``SIGKILL``s the whole process group on expiry.  A production helper
  that blocks on a substituted FIFO therefore costs the phase its remaining budget and a typed
  failure, never an unbounded hang.  A caller with no deadline still gets a bounded wait.
* **Minimal environment.** The child receives no ambient ``PATH``, ``PYTHONPATH``,
  ``PYTHONHOME``, or user site directory; ``-I`` refuses them a second time inside the
  interpreter, and the child strips its own directory from ``sys.path``.
* **Canonically bound I/O.** The request is canonical JSON, the response is canonical JSON
  and must re-serialize to the exact bytes received, and the child echoes the SHA-256 of the
  request bytes it consumed.  A response that does not name this request is refused.

**Source binding is an expectation, checked before execution -- not a report checked after.**

Every call takes a :class:`~scripts.backend_eval.source_binding.HelperExpectation` built from
the :class:`~scripts.backend_eval.models.EvaluatorIdentity` the run captured *before* any
child started.  There is no process-global first-use pin: the expectation is the only truth,
it is passed in explicitly at every call site, and two admissions in one process therefore
cannot contaminate each other.

Before a child starts, this module reads the expected child program and the exact
operation-appropriate helper closure through a confined component-wise walk from an open
descriptor on the owner root, and refuses any file whose bytes are not the expected bytes.
``O_NOFOLLOW`` on ``src/serena_light/workspace/inventory.py`` would guard only the last
component, so a symlinked ``src`` or ``src/serena_light`` could hand back another tree's
bytes; every component is opened from its parent's descriptor instead.

Those verified bytes are then the only bytes the child can execute:

* **The child program runs from a sealed image, not from a path.**  Handing ``python`` the
  pathname ``scripts/backend_eval/production_child.py`` leaves a window between the read that
  digested those bytes and the ``execve`` that ran them.  Instead the verified program is
  copied into a ``memfd`` sealed ``F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL``
  and executed as ``/proc/self/fd/<image>``.
* **The production helpers are imported from a second sealed image.**  The verified helper
  bytes are packed into one canonical JSON source image, sealed the same way, and passed by
  descriptor; the child installs a meta-path finder over it and never puts a ``src`` root on
  ``sys.path``.  The bytes this module compared against the admission expectation are
  therefore the bytes Python compiles and executes -- a helper substituted on disk during the
  import window is unreachable rather than merely detected afterwards.

The child reports the closure it loaded, and this module requires it to equal the operation's
expected closure exactly: an unexpected extra module, a missing expected module, a digest
mismatch, or a module that arrived through any loader other than the image's is a refusal.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, cast

from scripts.backend_eval.models import canonical_json, sha256_bytes
from scripts.backend_eval.process import (
    CommandBytesResult,
    CommandTimeout,
    Deadline,
    SealedImageError,
    run_bounded_bytes,
    sealed_image,
)
from scripts.backend_eval.source_binding import (
    EVALUATION_OWNER_ROOT,
    PRODUCTION_CHILD_RELPATH,
    HelperExpectation,
    SourceBindingError,
)

__all__ = [
    "PRODUCTION_CHILD_PATH",
    "PRODUCTION_CHILD_RELPATH",
    "UNBOUNDED_HELPER_SECONDS",
    "ProductionHelperError",
    "ProductionHelperTimeout",
    "run_production_helper",
]

PRODUCTION_CHILD_PATH = EVALUATION_OWNER_ROOT / PRODUCTION_CHILD_RELPATH
PRODUCTION_CHILD_IMAGE_NAME = "backend-eval-production-child"
PRODUCTION_SOURCE_IMAGE_NAME = "backend-eval-production-source"
# A caller with no phase ceiling still never waits forever on a production helper.
UNBOUNDED_HELPER_SECONDS = 120.0
# ``-I`` is isolated mode: no PYTHONPATH, no user site, no ambient sys.path[0] injection.
_CHILD_FLAGS = ("-I", "-B")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW


class ProductionHelperError(RuntimeError):
    """A bounded production-helper child did not return a usable, source-bound answer."""


class ProductionHelperTimeout(ProductionHelperError):
    """A production helper exceeded the phase's remaining time and its group was killed."""


def run_production_helper(
    operation: str,
    payload: dict[str, Any],
    *,
    expectation: HelperExpectation,
    deadline: Deadline | None = None,
    python: Path | None = None,
) -> dict[str, Any]:
    """Execute one production helper operation in a bounded child and return its result.

    ``operation`` and ``payload`` are the child's canonical request; the returned mapping is
    the child's ``result`` object, proven to answer exactly this request and to have been
    computed by the exact bytes ``expectation`` -- and therefore the published evaluator
    identity -- names.
    """

    interpreter = Path(sys.executable) if python is None else python
    owner_root = expectation.owner_root
    expected = expectation.modules_for(operation)
    request = canonical_json({"op": operation, **payload})
    owner_fd = _open_owner_root(owner_root, operation)
    try:
        program = _expected_bytes(owner_fd, owner_root, PRODUCTION_CHILD_RELPATH, expectation.child_digest, operation)
        image = _expected_source_image(owner_fd, owner_root, expected, operation)
        try:
            with (
                sealed_image(PRODUCTION_CHILD_IMAGE_NAME, program) as program_fd,
                sealed_image(PRODUCTION_SOURCE_IMAGE_NAME, image) as source_fd,
            ):
                result = _run_child(operation, request, interpreter, owner_root, deadline, program_fd, source_fd)
        except SealedImageError as error:
            raise ProductionHelperError(
                f"cannot seal the production helper images for {operation}: {error}"
            ) from error
    finally:
        os.close(owner_fd)
    response = _decode_response(operation, result.stdout)
    _require_bound_response(operation, response, request)
    if result.returncode != 0 or "result" not in response:
        _raise_child_refusal(operation, response, result)
    _require_expected_closure(operation, response, expected)
    return cast("dict[str, Any]", response["result"])


def _expected_bytes(owner_fd: int, owner_root: Path, relative: str, digest: str, operation: str) -> bytes:
    """Read one owned file confined, and refuse it unless it is the byte the identity names."""

    payload = _read_owned_file(owner_fd, relative, owner_root, operation)
    observed = sha256_bytes(payload)
    if observed != digest:
        raise SourceBindingError(
            f"the production helper {operation} would execute {owner_root / relative} with bytes "
            f"{observed}, not the {digest} this evaluator's identity names"
        )
    return payload


def _expected_source_image(
    owner_fd: int, owner_root: Path, expected: tuple[tuple[str, str], ...], operation: str
) -> bytes:
    """Pack exactly the verified helper bytes into one canonical, sealable source image."""

    modules = [
        [relative, base64.b64encode(_expected_bytes(owner_fd, owner_root, relative, digest, operation)).decode("ascii")]
        for relative, digest in expected
    ]
    return canonical_json({"modules": modules})


def _run_child(
    operation: str,
    request: bytes,
    interpreter: Path,
    owner_root: Path,
    deadline: Deadline | None,
    program_fd: int,
    source_fd: int,
) -> CommandBytesResult:
    timeout = UNBOUNDED_HELPER_SECONDS if deadline is None else deadline.remaining()
    # The program is the sealed image, addressed by descriptor; no pathname is re-resolved,
    # and the production source the child imports is the second sealed image, not a src root.
    command = (
        str(interpreter),
        *_CHILD_FLAGS,
        f"/proc/self/fd/{program_fd}",
        str(owner_root),
        str(source_fd),
    )
    try:
        return run_bounded_bytes(
            command,
            cwd=owner_root,
            env=_child_environment(),
            timeout=timeout,
            stdin=request,
            pass_fds=(program_fd, source_fd),
        )
    except CommandTimeout as error:
        raise ProductionHelperTimeout(
            f"the production helper {operation} exceeded the remaining {timeout:.3f}s "
            f"and its process group was killed: {error}"
        ) from error
    except OSError as error:
        raise ProductionHelperError(f"cannot start the production helper {operation}: {error}") from error


def _child_environment() -> dict[str, str]:
    """No ambient ``PATH``, ``PYTHONPATH``, ``PYTHONHOME``, or user site directory."""

    return {
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
    }


def _decode_response(operation: str, payload: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise ProductionHelperError(
            f"the production helper {operation} did not return canonical JSON: {error}"
        ) from error
    if not isinstance(decoded, dict):
        raise ProductionHelperError(f"the production helper {operation} did not return a JSON object")
    response = cast("dict[str, Any]", decoded)
    if canonical_json(response) != payload:
        raise ProductionHelperError(f"the production helper {operation} did not return canonical bytes")
    return response


def _require_bound_response(operation: str, response: dict[str, Any], request: bytes) -> None:
    if response.get("request_sha256") != sha256_bytes(request):
        raise ProductionHelperError(
            f"the production helper {operation} answered a different request than the one sent"
        )
    if response.get("op") not in (operation, None):
        raise ProductionHelperError(f"the production helper {operation} answered a different operation")


def _raise_child_refusal(operation: str, response: dict[str, Any], result: CommandBytesResult) -> None:
    detail = response.get("error_message") or result.stderr.decode("utf-8", "replace").strip()
    kind = response.get("error_type", "unknown")
    raise ProductionHelperError(
        f"the production helper {operation} refused ({result.returncode}) with {kind}: {detail}"
    )


def _require_expected_closure(
    operation: str, response: dict[str, Any], expected: tuple[tuple[str, str], ...]
) -> None:
    """The child must report exactly the operation's expected closure, byte for byte.

    The child already refuses an unexpected extra module, a missing expected module, and a
    module that entered through any loader but the sealed image's.  This is the parent's own
    independent statement of the same requirement, against the admission expectation rather
    than against anything the child chose.
    """

    recorded = response.get("production_files")
    if not isinstance(recorded, list) or not recorded:
        raise ProductionHelperError(
            f"the production helper {operation} did not report the production source it executed"
        )
    observed: list[tuple[str, str]] = []
    for entry in cast("list[Any]", recorded):
        if not isinstance(entry, list) or len(cast("list[Any]", entry)) != 2:
            raise ProductionHelperError(f"the production helper {operation} reported a malformed source entry")
        relative, digest = cast("list[Any]", entry)
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ProductionHelperError(f"the production helper {operation} reported a malformed source entry")
        observed.append((relative, digest))
    if tuple(sorted(observed)) != tuple(sorted(expected)):
        raise SourceBindingError(
            f"the production helper {operation} executed the closure {sorted(observed)}, "
            f"not the {sorted(expected)} this evaluator's identity names"
        )


def _open_owner_root(owner_root: Path, operation: str) -> int:
    try:
        return os.open(owner_root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise SourceBindingError(
            f"cannot open the evaluator checkout {owner_root} for {operation}: {error}"
        ) from error


def _read_owned_file(owner_fd: int, relative: str, owner_root: Path, operation: str) -> bytes:
    """Read one owned file component by component from the open owner root.

    ``O_NOFOLLOW`` on the whole relative path would guard only its last component, so a
    symlinked ``src`` or ``src/serena_light`` could hand back bytes from another tree and let
    them pass as this checkout's own.  Every component is opened from its parent's descriptor,
    the leaf is opened non-blocking, and the regular-file proof is taken on that descriptor.
    """

    path = owner_root / relative
    parts = tuple(part for part in relative.split("/") if part)
    if not parts or ".." in parts or "." in parts:
        raise SourceBindingError(f"{relative!r} is not an owned relative path below {owner_root}")
    current = os.dup(owner_fd)
    try:
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            try:
                child = os.open(part, _READ_FLAGS if last else _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=current)
            except OSError as error:
                raise SourceBindingError(
                    f"cannot re-read the production helper {path} bound by {operation} "
                    f"without following a link: {error}"
                ) from error
            os.close(current)
            current = child
        if not stat.S_ISREG(os.fstat(current).st_mode):
            raise SourceBindingError(f"the production helper {path} must be a regular file")
        with os.fdopen(current, "rb", closefd=False) as handle:
            return handle.read()
    except OSError as error:
        raise SourceBindingError(f"cannot re-read the production helper {path}: {error}") from error
    finally:
        os.close(current)
