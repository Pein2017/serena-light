"""Build and run the backend evaluator's sealed semantic source image.

This directly invoked, standard-library-only script is the minimal transport trust root.  It
validates closed CPython startup, reads the evaluator closure exactly once, seals those bytes,
and relays one bounded isolated child.  Admission semantics exist only inside that image.
"""

from __future__ import annotations

import ctypes
import fcntl
import io
import os
import signal
import stat
import subprocess
import sys
import time
import zipfile
from collections.abc import Mapping, Sequence
from contextlib import suppress
from pathlib import Path

EVALUATOR_BOOTSTRAP_SECONDS = 1800.0
EVALUATOR_BOOTSTRAP_REAP_SECONDS = 20.0
EVALUATOR_BOOTSTRAP_GRACE_SECONDS = 5.0
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY
_NOFOLLOW_DIRECTORY_FLAGS = _DIRECTORY_FLAGS | os.O_NOFOLLOW
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_MFD_FLAGS = 0x0001 | 0x0002
_ADD_SEALS = 1033
_GET_SEALS = 1034
_ALL_SEALS = 0x1 | 0x2 | 0x4 | 0x8
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_ZIP_MODE = 0o100600 << 16
_IMAGE_MAIN = b"""\
import fcntl
import importlib
import os
import stat
import sys
import types

owner_root, image_fd_raw, started_raw = sys.argv[1:4]
del sys.argv[1:4]
image_fd = int(image_fd_raw)
image_path = "/proc/self/fd/" + image_fd_raw
metadata = os.fstat(image_fd)
if (
    not os.path.isabs(owner_root)
    or sys.path[0] != image_path
    or not stat.S_ISREG(metadata.st_mode)
    or fcntl.fcntl(image_fd, 1034) != 15
):
    raise SystemExit("invalid sealed evaluator bootstrap")
started = float(started_raw)
if started < 0:
    raise SystemExit("invalid sealed evaluator deadline origin")
context = types.ModuleType("_serena_light_backend_eval_bootstrap")
context.image_fd = image_fd
context.image_path = image_path
context.owner_root = owner_root
context.started = started
sys.modules[context.__name__] = context
module = importlib.import_module("scripts.backend_eval.admission")
raise SystemExit(module.main())
"""
_INHERITED_KEYS = (
    "ALL_PROXY", "CURL_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "LANG", "LC_ALL",
    "LC_CTYPE", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "all_proxy", "http_proxy", "https_proxy", "no_proxy",
)


class EvaluatorBootstrapError(RuntimeError):
    """The immutable evaluator image cannot be built or started."""


class EvaluatorBootstrapTimeout(EvaluatorBootstrapError):
    """The sealed evaluator exceeded its outer safety bound and was killed."""


def _closed_startup() -> bool:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    prefix = os.path.realpath(sys.base_prefix)
    expected = [
        os.path.join(prefix, "lib", f"python{sys.version_info.major}{sys.version_info.minor}.zip"),
        os.path.join(prefix, "lib", version),
        os.path.join(prefix, "lib", version, "lib-dynload"),
    ]
    return (
        sys.flags.isolated == 1
        and sys.flags.no_site == 1
        and sys.flags.dont_write_bytecode == 1
        and sys.dont_write_bytecode
        and [os.path.realpath(entry) for entry in sys.path] == expected
    )


def _command_owner_root() -> Path:
    return Path(os.path.abspath(__file__)).parent.parent


def _open_filesystem_root(label: Path) -> int:
    try:
        return os.open("/", _DIRECTORY_FLAGS)
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot open filesystem root for {label}: {error}") from error


def _open_absolute_directory(path: Path) -> int:
    if not path.is_absolute():
        raise EvaluatorBootstrapError(f"the evaluator owner root must be absolute: {path}")
    fd = _open_filesystem_root(path)
    try:
        for part in path.parts[1:]:
            child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as error:
        with suppress(OSError):
            os.close(fd)
        raise EvaluatorBootstrapError(f"cannot open evaluator owner root {path}: {error}") from error


def _open_relative_directory(parent_fd: int, parts: Sequence[str], label: str) -> int:
    fd = os.dup(parent_fd)
    try:
        for part in parts:
            child = os.open(part, _NOFOLLOW_DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError as error:
        os.close(fd)
        raise EvaluatorBootstrapError(f"cannot open evaluator directory {label}: {error}") from error


def _read_relative_file(parent_fd: int, parts: Sequence[str], label: str) -> bytes:
    directory = _open_relative_directory(parent_fd, parts[:-1], label)
    try:
        fd = os.open(parts[-1], _READ_FLAGS, dir_fd=directory)
    except OSError as error:
        os.close(directory)
        raise EvaluatorBootstrapError(f"cannot open evaluator source {label}: {error}") from error
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise EvaluatorBootstrapError(f"evaluator source {label} must be a regular file")
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1024 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot read evaluator source {label}: {error}") from error
    finally:
        os.close(fd)
        os.close(directory)


def _zip_entry(name: str, payload: bytes) -> tuple[zipfile.ZipInfo, bytes]:
    info = zipfile.ZipInfo(name, _ZIP_TIME)
    info.compress_type = zipfile.ZIP_STORED
    info.external_attr = _ZIP_MODE
    info.create_system = 3
    return info, payload


def _build_evaluator_source_image(owner_root: Path) -> bytes:
    """Read the complete evaluator closure once and pack those exact bytes deterministically."""

    owner_fd = _open_absolute_directory(owner_root)
    package_fd = _open_relative_directory(owner_fd, ("scripts", "backend_eval"), "scripts/backend_eval")
    try:
        try:
            names = sorted(entry.name for entry in os.scandir(package_fd) if entry.name.endswith(".py"))
        except OSError as error:
            raise EvaluatorBootstrapError(f"cannot enumerate evaluator source closure: {error}") from error
        if not names:
            raise EvaluatorBootstrapError("the evaluator source closure is empty")
        entries = [
            _zip_entry("__main__.py", _IMAGE_MAIN),
            _zip_entry(
                "scripts/__init__.py",
                _read_relative_file(owner_fd, ("scripts", "__init__.py"), "scripts/__init__.py"),
            ),
        ]
        entries.extend(
            _zip_entry(
                f"scripts/backend_eval/{name}",
                _read_relative_file(
                    owner_fd, ("scripts", "backend_eval", name), f"scripts/backend_eval/{name}"
                ),
            )
            for name in names
        )
    finally:
        os.close(package_fd)
        os.close(owner_fd)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for info, payload in entries:
            archive.writestr(info, payload)
    return buffer.getvalue()


def _sealed_evaluator_image(payload: bytes) -> int:
    fd = -1
    try:
        handle = ctypes.CDLL(None, use_errno=True)
        if not hasattr(handle, "memfd_create"):
            raise EvaluatorBootstrapError("this platform cannot provide a sealed evaluator image")
        create = handle.memfd_create
        create.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        create.restype = ctypes.c_int
        fd = create(b"backend-eval-source-image", _MFD_FLAGS)
        if fd < 0:
            raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
        written = 0
        while written < len(payload):
            written += os.write(fd, payload[written:])
        if (
            fcntl.fcntl(fd, _ADD_SEALS, _ALL_SEALS) != 0
            or fcntl.fcntl(fd, _GET_SEALS) != _ALL_SEALS
            or os.pread(fd, len(payload) + 1, 0) != payload
        ):
            raise EvaluatorBootstrapError("cannot seal the evaluator source image")
        return fd
    except (AttributeError, OSError) as error:
        if fd >= 0:
            with suppress(OSError):
                os.close(fd)
        raise EvaluatorBootstrapError(f"cannot build the sealed evaluator source image: {error}") from error


def _kill_evaluator_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.communicate(timeout=EVALUATOR_BOOTSTRAP_REAP_SECONDS)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()
        process.wait(timeout=EVALUATOR_BOOTSTRAP_REAP_SECONDS)


def _run_sealed_evaluator(
    owner_root: Path,
    argv: Sequence[str],
    *,
    timeout: float,
    environ: Mapping[str, str],
) -> tuple[int, bytes, bytes]:
    started = time.monotonic()
    image = _build_evaluator_source_image(owner_root)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise EvaluatorBootstrapTimeout("the evaluator source image exhausted the command deadline")
    image_fd = _sealed_evaluator_image(image)
    child_environment = {key: environ[key] for key in _INHERITED_KEYS if key in environ}
    command = (
        sys.executable, "-I", "-S", "-B", f"/proc/self/fd/{image_fd}",
        str(owner_root), str(image_fd), repr(started), *argv,
    )
    try:
        process = subprocess.Popen(
            command,
            cwd=owner_root,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(image_fd,),
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _kill_evaluator_group(process)
            raise EvaluatorBootstrapTimeout(
                f"the sealed evaluator exceeded its {timeout:g}s outer bound and its process group was killed"
            ) from error
        except BaseException:
            _kill_evaluator_group(process)
            raise
        return process.returncode, stdout, stderr
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot start the sealed evaluator: {error}") from error
    finally:
        os.close(image_fd)


def _bootstrap_command() -> int:
    try:
        returncode, stdout, stderr = _run_sealed_evaluator(
            _command_owner_root(),
            tuple(sys.argv[1:]),
            timeout=EVALUATOR_BOOTSTRAP_SECONDS + EVALUATOR_BOOTSTRAP_GRACE_SECONDS,
            environ=os.environ,
        )
    except EvaluatorBootstrapError as error:
        sys.stdout.write("status=incomplete\n")
        sys.stdout.write(f"issue=evaluator_bootstrap_failed: {error}\n")
        sys.stdout.write("next_action=hold\n")
        return 2
    sys.stdout.buffer.write(stdout)
    sys.stderr.buffer.write(stderr)
    return returncode


def main() -> int:
    if not _closed_startup():
        sys.stderr.write(
            "backend evaluation requires closed CPython startup: "
            "python -I -S -B scripts/backend_eval_bootstrap.py\n"
        )
        return 2
    return _bootstrap_command()


if __name__ == "__main__":
    raise SystemExit(main())
