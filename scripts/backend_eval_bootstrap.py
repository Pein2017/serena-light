"""Build and run the backend evaluator's sealed semantic source image.

This directly invoked, standard-library-only script is the minimal transport trust root.  It
validates closed CPython startup, reads the evaluator closure exactly once, seals those bytes,
and relays one bounded isolated child.  Admission semantics exist only inside that image.
"""

from __future__ import annotations

import ast
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
from dataclasses import dataclass
from pathlib import Path

EVALUATOR_BOOTSTRAP_SECONDS = 1800.0
PROTOCOL_BOOTSTRAP_SECONDS = 5400.0
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
_PSUTIL_VERSION = "7.0.0"
_PSUTIL_SOURCES = ("__init__.py", "_common.py", "_pslinux.py", "_psposix.py")
_PSUTIL_EXTENSIONS = ("_psutil_linux", "_psutil_posix")
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
_PROTOCOL_IMAGE_MAIN = b"""\
import fcntl
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import os
import stat
import sys
import types
import zipfile

owner_root, image_fd_raw, started_raw, deadline_raw, linux_fd_raw, posix_fd_raw = sys.argv[1:7]
del sys.argv[1:7]
image_fd = int(image_fd_raw)
image_path = "/proc/self/fd/" + image_fd_raw
started = float(started_raw)
deadline_seconds = float(deadline_raw)
dependency_fds = {
    "psutil._psutil_linux": int(linux_fd_raw),
    "psutil._psutil_posix": int(posix_fd_raw),
}
metadata = os.fstat(image_fd)
if (
    not os.path.isabs(owner_root)
    or sys.path[0] != image_path
    or not stat.S_ISREG(metadata.st_mode)
    or fcntl.fcntl(image_fd, 1034) != 15
    or started < 0
    or deadline_seconds != 5400.0
):
    raise SystemExit("invalid sealed protocol bootstrap")
for dependency_fd in dependency_fds.values():
    metadata = os.fstat(dependency_fd)
    if not stat.S_ISREG(metadata.st_mode) or fcntl.fcntl(dependency_fd, 1034) != 15:
        raise SystemExit("invalid sealed protocol dependency")

class _BoundProductionLoader(importlib.abc.Loader):
    def __init__(self, finder, fullname, entry, filename, is_package):
        self.finder = finder
        self.fullname = fullname
        self.entry = entry
        self.filename = filename
        self.is_package = is_package

    def create_module(self, spec):
        del spec
        return None

    def exec_module(self, module):
        if self.finder.loaders.get(self.fullname) is not self:
            raise ImportError("unbound sealed production loader")
        payload = self.finder.archive.read(self.entry)
        module.__file__ = self.filename
        module.__loader__ = self
        if self.is_package:
            module.__path__ = []
        code = compile(payload, self.filename, "exec", dont_inherit=True)
        exec(code, module.__dict__)

class _BoundProductionFinder(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.archive = zipfile.ZipFile(image_path, "r")
        names = self.archive.namelist()
        if len(names) != len(set(names)):
            raise SystemExit("duplicate sealed protocol image entry")
        self.entries = {}
        self.loaders = {}
        for entry in names:
            if not entry.startswith("serena_light/") or not entry.endswith(".py"):
                continue
            if entry.endswith("/__init__.py"):
                fullname = entry.removesuffix("/__init__.py").replace("/", ".")
                is_package = True
            else:
                fullname = entry.removesuffix(".py").replace("/", ".")
                is_package = False
            self.entries[fullname] = (entry, is_package)

    def find_spec(self, fullname, path=None, target=None):
        del path, target
        bound = self.entries.get(fullname)
        if bound is None:
            return None
        entry, is_package = bound
        filename = os.path.join(owner_root, "src", *entry.split("/"))
        loader = _BoundProductionLoader(self, fullname, entry, filename, is_package)
        self.loaders[fullname] = loader
        spec = importlib.machinery.ModuleSpec(
            fullname, loader, origin=filename, is_package=is_package
        )
        spec.has_location = True
        if is_package:
            spec.submodule_search_locations = []
        return spec

class _BoundExtensionFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        del path, target
        dependency_fd = dependency_fds.get(fullname)
        if dependency_fd is None:
            return None
        origin = "/proc/self/fd/" + str(dependency_fd)
        loader = importlib.machinery.ExtensionFileLoader(fullname, origin)
        return importlib.util.spec_from_loader(fullname, loader, origin=origin)

extension_finder = _BoundExtensionFinder()
production_finder = _BoundProductionFinder()
sys.meta_path.insert(0, extension_finder)
sys.meta_path.insert(0, production_finder)
context = types.ModuleType("_serena_light_backend_eval_bootstrap")
context.image_fd = image_fd
context.image_path = image_path
context.owner_root = owner_root
context.started = started
context.deadline_seconds = deadline_seconds
context.entrypoint = "protocol_phase"
context.dependency_fds = dependency_fds
context.extension_finder = extension_finder
context.production_finder = production_finder
sys.modules[context.__name__] = context
module = importlib.import_module("scripts.backend_eval.protocol_phase")
raise SystemExit(module.main())
"""
_INHERITED_KEYS = (
    "ALL_PROXY", "CURL_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "LANG", "LC_ALL",
    "LC_CTYPE", "NO_PROXY", "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR", "SSL_CERT_FILE",
    "all_proxy", "http_proxy", "https_proxy", "no_proxy",
)


def _select_entrypoint(argv: Sequence[str]) -> tuple[str, tuple[str, ...], float]:
    """Choose the sealed semantic entry only from one explicit command token.

    The absent-token branch is deliberately the historical admission command: every byte of
    its argument vector keeps its old meaning and its 1800-second semantic budget.
    """

    arguments = tuple(argv)
    if arguments[:1] == ("protocol-phase",):
        return "scripts.backend_eval.protocol_phase", arguments[1:], PROTOCOL_BOOTSTRAP_SECONDS
    return "scripts.backend_eval.admission", arguments, EVALUATOR_BOOTSTRAP_SECONDS


class EvaluatorBootstrapError(RuntimeError):
    """The immutable evaluator image cannot be built or started."""


class EvaluatorBootstrapTimeout(EvaluatorBootstrapError):
    """The sealed evaluator exceeded its outer safety bound and was killed."""


@dataclass(frozen=True, slots=True)
class ProtocolSourceImage:
    """The protocol's Python archive plus its exact native psutil dependencies."""

    archive: bytes
    extensions: tuple[tuple[str, bytes], ...]


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


def _build_protocol_source_image(owner_root: Path) -> ProtocolSourceImage:
    """Pack the reachable protocol evaluator and production Python closure.

    The graph starts at the one receipt-producing protocol module and follows only owned
    ``scripts.backend_eval`` and ``serena_light`` imports.  An unknown third-party import is
    refused.  psutil is the sole declared external dependency; its Linux Python modules and
    native extensions are read from the invoking interpreter's exact locked installation.
    """

    entries, needs_psutil = _reachable_protocol_sources(owner_root)
    extensions: tuple[tuple[str, bytes], ...] = ()
    if needs_psutil:
        psutil_entries, extensions = _bound_psutil_sources()
        entries.update(psutil_entries)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(*_zip_entry("__main__.py", _PROTOCOL_IMAGE_MAIN))
        for name in sorted(entries):
            archive.writestr(*_zip_entry(name, entries[name]))
    return ProtocolSourceImage(archive=buffer.getvalue(), extensions=extensions)


def _reachable_protocol_sources(owner_root: Path) -> tuple[dict[str, bytes], bool]:
    pending = ["scripts.backend_eval.protocol_phase"]
    visited: set[str] = set()
    entries: dict[str, bytes] = {}
    needs_psutil = False
    while pending:
        module = pending.pop()
        if module in visited:
            continue
        location = _owned_module_location(owner_root, module)
        if location is None:
            raise EvaluatorBootstrapError(f"protocol source closure cannot resolve owned module {module}")
        parts, archive_name, is_package = location
        payload = _read_owned_source(owner_root, parts, archive_name)
        entries[archive_name] = payload
        visited.add(module)
        for parent in _parent_packages(module):
            if parent not in visited:
                pending.append(parent)
        try:
            syntax = ast.parse(payload, filename=archive_name)
        except (SyntaxError, ValueError) as error:
            raise EvaluatorBootstrapError(f"cannot parse protocol source {archive_name}: {error}") from error
        for imported in _source_imports(module, is_package, syntax):
            root = imported.partition(".")[0]
            if root in {"scripts", "serena_light"}:
                if _owned_module_location(owner_root, imported) is not None:
                    pending.append(imported)
                continue
            if root == "psutil":
                needs_psutil = True
                continue
            if root and root not in sys.stdlib_module_names:
                raise EvaluatorBootstrapError(
                    f"protocol source {archive_name} imports undeclared external dependency {imported}"
                )
    return entries, needs_psutil


def _owned_module_location(
    owner_root: Path, module: str
) -> tuple[tuple[str, ...], str, bool] | None:
    if module == "scripts":
        return ("scripts", "__init__.py"), "scripts/__init__.py", True
    if module == "scripts.backend_eval":
        parts = ("scripts", "backend_eval", "__init__.py")
        return parts, "scripts/backend_eval/__init__.py", True
    if module.startswith("scripts.backend_eval."):
        suffix = tuple(module.removeprefix("scripts.backend_eval.").split("."))
        parts = ("scripts", "backend_eval", *suffix)
        return _module_file_location(owner_root, parts, "scripts/backend_eval")
    if module == "serena_light":
        parts = ("src", "serena_light", "__init__.py")
        return parts, "serena_light/__init__.py", True
    if module.startswith("serena_light."):
        suffix = tuple(module.removeprefix("serena_light.").split("."))
        parts = ("src", "serena_light", *suffix)
        return _module_file_location(owner_root, parts, "serena_light")
    return None


def _module_file_location(
    owner_root: Path, parts: tuple[str, ...], archive_root: str
) -> tuple[tuple[str, ...], str, bool] | None:
    package_init = (*parts, "__init__.py")
    module_file = (*parts[:-1], f"{parts[-1]}.py")
    package_path = owner_root.joinpath(*package_init)
    module_path = owner_root.joinpath(*module_file)
    if package_path.is_file():
        relative = "/".join(parts[2:])
        return package_init, f"{archive_root}/{relative}/__init__.py", True
    if module_path.is_file():
        relative_parts = parts[2:]
        return module_file, f"{archive_root}/{'/'.join(relative_parts)}.py", False
    return None


def _read_owned_source(owner_root: Path, parts: Sequence[str], label: str) -> bytes:
    owner_fd = _open_absolute_directory(owner_root)
    try:
        return _read_relative_file(owner_fd, parts, label)
    finally:
        os.close(owner_fd)


def _parent_packages(module: str) -> tuple[str, ...]:
    parts = module.split(".")
    return tuple(".".join(parts[:index]) for index in range(1, len(parts)))


def _source_imports(module: str, is_package: bool, syntax: ast.AST) -> set[str]:
    imported: set[str] = set()
    package = module.split(".") if is_package else module.split(".")[:-1]
    for node in ast.walk(syntax):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            keep = len(package) - node.level + 1
            if keep < 0:
                raise EvaluatorBootstrapError(f"protocol source {module} has an invalid relative import")
            base_parts = package[:keep]
        else:
            base_parts = []
        if node.module:
            base_parts.extend(node.module.split("."))
        base = ".".join(base_parts)
        if base:
            imported.add(base)
        for alias in node.names:
            if alias.name != "*" and base:
                imported.add(f"{base}.{alias.name}")
    return imported


def _bound_psutil_sources() -> tuple[dict[str, bytes], tuple[tuple[str, bytes], ...]]:
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    platlib = Path(os.path.abspath(sys.executable)).parent.parent / "lib" / version / "site-packages"
    if not (platlib / "psutil" / "__init__.py").is_file():
        raise EvaluatorBootstrapError(
            f"protocol bootstrap cannot bind psutil in host interpreter environment {platlib}"
        )
    package_root = platlib / "psutil"
    root_fd = _open_absolute_directory(platlib)
    package_fd = _open_relative_directory(root_fd, ("psutil",), "psutil")
    try:
        sources: dict[str, bytes] = {
            f"psutil/{name}": _read_relative_file(root_fd, ("psutil", name), f"psutil/{name}")
            for name in _PSUTIL_SOURCES
        }
        if _psutil_version(sources["psutil/__init__.py"]) != _PSUTIL_VERSION:
            raise EvaluatorBootstrapError(
                f"protocol bootstrap requires psutil {_PSUTIL_VERSION} from {package_root}"
            )
        available = sorted(entry.name for entry in os.scandir(package_fd))
        extensions: list[tuple[str, bytes]] = []
        for stem in _PSUTIL_EXTENSIONS:
            matches = [name for name in available if name.startswith(f"{stem}.") and name.endswith(".so")]
            if len(matches) != 1:
                raise EvaluatorBootstrapError(
                    f"protocol bootstrap requires exactly one psutil/{stem} native extension, found {matches}"
                )
            extensions.append(
                (
                    f"psutil.{stem}",
                    _read_relative_file(root_fd, ("psutil", matches[0]), f"psutil/{matches[0]}"),
                )
            )
        return sources, tuple(extensions)
    finally:
        os.close(package_fd)
        os.close(root_fd)


def _psutil_version(payload: bytes) -> str | None:
    try:
        syntax = ast.parse(payload, filename="psutil/__init__.py")
    except (SyntaxError, ValueError) as error:
        raise EvaluatorBootstrapError(f"cannot parse bound psutil package: {error}") from error
    for node in syntax.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


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


def _run_sealed_protocol(
    owner_root: Path,
    argv: Sequence[str],
    *,
    timeout: float,
    environ: Mapping[str, str],
) -> tuple[int, bytes, bytes]:
    started = time.monotonic()
    protocol = _build_protocol_source_image(owner_root)
    if timeout - (time.monotonic() - started) <= 0:
        raise EvaluatorBootstrapTimeout("the protocol source image exhausted the command deadline")
    image_fd = _sealed_evaluator_image(protocol.archive)
    extension_fds: list[int] = []
    child_environment = {key: environ[key] for key in _INHERITED_KEYS if key in environ}
    try:
        for _name, payload in protocol.extensions:
            extension_fds.append(_sealed_evaluator_image(payload))
        if len(extension_fds) != len(_PSUTIL_EXTENSIONS):
            raise EvaluatorBootstrapError("the protocol source image omits a required native dependency")
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise EvaluatorBootstrapTimeout(
                "sealing the protocol dependencies exhausted the command deadline"
            )
        command = (
            sys.executable,
            "-I",
            "-S",
            "-B",
            f"/proc/self/fd/{image_fd}",
            str(owner_root),
            str(image_fd),
            repr(started),
            repr(PROTOCOL_BOOTSTRAP_SECONDS),
            *(str(fd) for fd in extension_fds),
            *argv,
        )
        process = subprocess.Popen(
            command,
            cwd=owner_root,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(image_fd, *extension_fds),
            start_new_session=True,
        )
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise EvaluatorBootstrapTimeout(
                    "starting the sealed protocol evaluator exhausted the command deadline"
                )
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired as error:
            _kill_evaluator_group(process)
            raise EvaluatorBootstrapTimeout(
                f"the sealed protocol evaluator exceeded its {timeout:g}s outer bound "
                "and its process group was killed"
            ) from error
        except BaseException:
            _kill_evaluator_group(process)
            raise
        return process.returncode, stdout, stderr
    except OSError as error:
        raise EvaluatorBootstrapError(f"cannot start the sealed protocol evaluator: {error}") from error
    finally:
        for fd in extension_fds:
            os.close(fd)
        os.close(image_fd)


def _bootstrap_command() -> int:
    module, arguments, semantic_seconds = _select_entrypoint(tuple(sys.argv[1:]))
    try:
        if module == "scripts.backend_eval.protocol_phase":
            returncode, stdout, stderr = _run_sealed_protocol(
                _command_owner_root(),
                arguments,
                timeout=semantic_seconds + EVALUATOR_BOOTSTRAP_GRACE_SECONDS,
                environ=os.environ,
            )
        else:
            returncode, stdout, stderr = _run_sealed_evaluator(
                _command_owner_root(),
                arguments,
                timeout=semantic_seconds + EVALUATOR_BOOTSTRAP_GRACE_SECONDS,
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
