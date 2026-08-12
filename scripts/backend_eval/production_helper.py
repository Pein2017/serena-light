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
* **Source bound.** The child is executed by absolute path under ``-I`` with an explicit
  ``src`` root, and it reports the byte digest of every ``serena_light`` module it loaded.
  Those digests are checked against this checkout's own bytes, so a child that ran another
  worktree's helpers is refused rather than believed.
* **Minimal environment.** The child receives no ambient ``PATH``, ``PYTHONPATH``,
  ``PYTHONHOME``, or user site directory; ``-I`` refuses them a second time inside the
  interpreter, and the child strips its own directory from ``sys.path``.
* **Canonically bound I/O.** The request is canonical JSON, the response is canonical JSON
  and must re-serialize to the exact bytes received, and the child echoes the SHA-256 of the
  request bytes it consumed.  A response that does not name this request is refused.

Two seams in that binding are closed explicitly, because "the receipt names these bytes"
means nothing if the thing executed is named by a mutable pathname.

* **The child program is executed from a sealed image, not from a path.**  Handing
  ``python`` the pathname ``scripts/backend_eval/production_child.py`` leaves a window
  between the read that digested those bytes and the ``execve`` that ran them.  Instead the
  program is read once through a confined descriptor walk, copied into a ``memfd`` sealed
  ``F_SEAL_WRITE | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_SEAL``, and executed as
  ``/proc/self/fd/<image>`` with that one descriptor -- and no other -- inherited.  The bytes
  that were digested are the bytes that run.  The digest is pinned on first use and re-checked
  on every later call, so a swap mid-run is refused rather than silently executed, and
  :func:`production_child_digest` lets the evaluator identity be proven to name the same
  bytes.  This does not rely on an after-the-fact ``source_clean`` observation.
* **The parent's re-read of the reported helper bytes is confined, not merely
  ``O_NOFOLLOW``.**  ``O_NOFOLLOW`` on ``src/serena_light/workspace/inventory.py`` guards only
  the last component, so a symlinked ``src`` or ``src/serena_light`` would let the parent
  "confirm" bytes from another tree.  Every component is opened from its parent's descriptor,
  starting at an open descriptor on the owner root.
"""

from __future__ import annotations

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
    PRODUCTION_SOURCE_ROOT,
    SourceBindingError,
)

__all__ = [
    "PRODUCTION_CHILD_PATH",
    "PRODUCTION_CHILD_RELPATH",
    "UNBOUNDED_HELPER_SECONDS",
    "ProductionHelperError",
    "ProductionHelperTimeout",
    "production_child_digest",
    "run_production_helper",
]

PRODUCTION_CHILD_RELPATH = "scripts/backend_eval/production_child.py"
PRODUCTION_CHILD_PATH = EVALUATION_OWNER_ROOT / PRODUCTION_CHILD_RELPATH
PRODUCTION_CHILD_IMAGE_NAME = "backend-eval-production-child"
# A caller with no phase ceiling still never waits forever on a production helper.
UNBOUNDED_HELPER_SECONDS = 120.0
# ``-I`` is isolated mode: no PYTHONPATH, no user site, no ambient sys.path[0] injection.
_CHILD_FLAGS = ("-I", "-B")
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW

# The child program's digest, pinned per owner root on first use.  A later read that differs
# is a mid-run substitution of the program this evaluator's identity names, and is refused.
_PINNED_CHILD_DIGESTS: dict[str, str] = {}


class ProductionHelperError(RuntimeError):
    """A bounded production-helper child did not return a usable, source-bound answer."""


class ProductionHelperTimeout(ProductionHelperError):
    """A production helper exceeded the phase's remaining time and its group was killed."""


def run_production_helper(
    operation: str,
    payload: dict[str, Any],
    *,
    deadline: Deadline | None = None,
    python: Path | None = None,
    owner_root: Path = EVALUATION_OWNER_ROOT,
) -> dict[str, Any]:
    """Execute one production helper operation in a bounded child and return its result.

    ``operation`` and ``payload`` are the child's canonical request; the returned mapping is
    the child's ``result`` object, proven to answer exactly this request and to have been
    computed by this checkout's own production bytes.
    """

    interpreter = Path(sys.executable) if python is None else python
    src_root = owner_root / "src"
    request = canonical_json({"op": operation, **payload})
    owner_fd = _open_owner_root(owner_root, operation)
    try:
        program = _pinned_child_program(owner_root, owner_fd, operation)
        try:
            with sealed_image(PRODUCTION_CHILD_IMAGE_NAME, program) as image_fd:
                result = _run_child(operation, request, interpreter, owner_root, src_root, deadline, image_fd)
        except SealedImageError as error:
            raise ProductionHelperError(
                f"cannot seal the production helper program for {operation}: {error}"
            ) from error
        response = _decode_response(operation, result.stdout)
        _require_bound_response(operation, response, request)
        if result.returncode != 0 or "result" not in response:
            _raise_child_refusal(operation, response, result)
        _require_bound_production_source(operation, response, owner_root, owner_fd)
    finally:
        os.close(owner_fd)
    return cast("dict[str, Any]", response["result"])


def production_child_digest(owner_root: Path = EVALUATION_OWNER_ROOT) -> str:
    """The SHA-256 of the child program bytes this evaluator would actually execute.

    Read through the same confined walk the execution uses, so the value can be compared with
    the evaluator identity's own record of ``production_child.py`` and prove that the identity
    names the bytes that run.
    """

    owner_fd = _open_owner_root(owner_root, "production_child_digest")
    try:
        return sha256_bytes(_read_owned_file(owner_fd, PRODUCTION_CHILD_RELPATH, owner_root, "child program"))
    finally:
        os.close(owner_fd)


def _pinned_child_program(owner_root: Path, owner_fd: int, operation: str) -> bytes:
    """The child program, bound to the first bytes this process ever read for this owner."""

    program = _read_owned_file(owner_fd, PRODUCTION_CHILD_RELPATH, owner_root, operation)
    digest = sha256_bytes(program)
    key = str(owner_root)
    pinned = _PINNED_CHILD_DIGESTS.setdefault(key, digest)
    if digest != pinned:
        raise SourceBindingError(
            f"the production helper program {owner_root / PRODUCTION_CHILD_RELPATH} changed during this "
            f"run: it is now {digest}, not the {pinned} this evaluator's identity names"
        )
    return program


def _run_child(
    operation: str,
    request: bytes,
    interpreter: Path,
    owner_root: Path,
    src_root: Path,
    deadline: Deadline | None,
    image_fd: int,
) -> CommandBytesResult:
    timeout = UNBOUNDED_HELPER_SECONDS if deadline is None else deadline.remaining()
    # The program is the sealed image, addressed by descriptor; no pathname is re-resolved.
    command = (
        str(interpreter),
        *_CHILD_FLAGS,
        f"/proc/self/fd/{image_fd}",
        str(owner_root),
        str(src_root),
    )
    try:
        return run_bounded_bytes(
            command,
            cwd=owner_root,
            env=_child_environment(),
            timeout=timeout,
            stdin=request,
            pass_fds=(image_fd,),
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


def _require_bound_production_source(
    operation: str, response: dict[str, Any], owner_root: Path, owner_fd: int
) -> None:
    """Every helper byte the child executed must be this checkout's own byte."""

    recorded = response.get("production_files")
    if not isinstance(recorded, list) or not recorded:
        raise ProductionHelperError(
            f"the production helper {operation} did not report the production source it executed"
        )
    package_prefix = f"{PRODUCTION_SOURCE_ROOT.relative_to(EVALUATION_OWNER_ROOT).as_posix()}/"
    for entry in cast("list[Any]", recorded):
        if not isinstance(entry, list) or len(cast("list[Any]", entry)) != 2:
            raise ProductionHelperError(f"the production helper {operation} reported a malformed source entry")
        relative, digest = cast("list[Any]", entry)
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ProductionHelperError(f"the production helper {operation} reported a malformed source entry")
        if not relative.startswith(package_prefix) or ".." in Path(relative).parts:
            raise SourceBindingError(
                f"the production helper {operation} executed {relative}, outside {PRODUCTION_SOURCE_ROOT}"
            )
        observed = sha256_bytes(_read_owned_file(owner_fd, relative, owner_root, operation))
        if observed != digest:
            raise SourceBindingError(
                f"the production helper {operation} executed {relative} with bytes {digest}, "
                f"not this checkout's {observed}"
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
