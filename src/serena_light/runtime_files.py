"""Symlink-safe daemon discovery and bearer-secret files."""

from __future__ import annotations

import hmac
import json
import math
import os
import secrets
import stat
import string
import time
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import UUID

from serena_light.build_identity import validate_build_identity

RUNTIME_ROOT = Path("/data/CoordExp/.codex/runtime/serena-light")
DISCOVERY_NAME = "daemon.json"
BEARER_NAME = "bearer"
STARTUP_NONCE_NAME = "startup-nonce"
SERVICE_GIT_CONFIG_NAME = "gitconfig"
RUNTIME_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
DISCOVERY_SCHEMA_VERSION = 2
LEGACY_BUILD_IDENTITY = "0" * 64


class RuntimeFileError(RuntimeError):
    """Raised when local daemon state cannot be trusted."""


@dataclass(frozen=True, slots=True)
class BearerSecret:
    value: str

    def __repr__(self) -> str:
        return "BearerSecret(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class StartupNonce:
    value: str

    def __repr__(self) -> str:
        return "StartupNonce(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


@dataclass(frozen=True, slots=True)
class RuntimeLayout:
    root: Path
    python_root: Path
    deps_root: Path
    builds_root: Path
    home_root: Path
    build_identity: str
    build_root: Path
    logs_root: Path


@dataclass(frozen=True, slots=True)
class DiscoveryMetadata:
    schema_version: int
    daemon_id: str
    pid: int
    process_start_time: float
    endpoint: str
    protocol_version: str
    server_version: str
    created_at: float
    build_identity: str = LEGACY_BUILD_IDENTITY

    @classmethod
    def create(
        cls,
        *,
        daemon_id: str,
        pid: int,
        process_start_time: float,
        endpoint: str,
        protocol_version: str,
        server_version: str,
        created_at: float | None = None,
        build_identity: str = LEGACY_BUILD_IDENTITY,
    ) -> DiscoveryMetadata:
        metadata = cls(
            schema_version=DISCOVERY_SCHEMA_VERSION,
            daemon_id=daemon_id,
            pid=pid,
            process_start_time=process_start_time,
            endpoint=endpoint,
            protocol_version=protocol_version,
            server_version=server_version,
            created_at=time.time() if created_at is None else created_at,
            build_identity=build_identity,
        )
        _validate_metadata(metadata)
        return metadata


type ProcessIdentityValidator = Callable[[int, float], bool]


def prepare_runtime_layout(root: Path, build_identity: str) -> RuntimeLayout:
    """Create the service-owned base directories and one identity-specific slot."""

    identity = validate_build_identity(build_identity)
    base = prepare_runtime_directory(root)
    python_root = _prepare_private_child(base, "python")
    deps_root = _prepare_private_child(base, "deps")
    builds_root = _prepare_private_child(base, "builds")
    home_root = _prepare_private_child(base, "home")
    build_root = _prepare_private_child(builds_root, identity)
    logs_root = _prepare_private_child(build_root, "logs")
    return RuntimeLayout(
        root=base,
        python_root=python_root,
        deps_root=deps_root,
        builds_root=builds_root,
        home_root=home_root,
        build_identity=identity,
        build_root=build_root,
        logs_root=logs_root,
    )


def prepare_runtime_directory(root: Path = RUNTIME_ROOT, *, expected_uid: int | None = None) -> Path:
    """Open every existing component without following links; create only ``root``."""

    if not root.is_absolute() or root == Path("/"):
        raise RuntimeFileError("runtime directory must be a non-root absolute path")
    uid = os.getuid() if expected_uid is None else expected_uid
    parent = root.parent
    _assert_no_symlink_components(parent)
    try:
        parent_stat = parent.stat()
    except OSError as exc:
        raise RuntimeFileError(f"runtime parent is unavailable: {parent}") from exc
    if not stat.S_ISDIR(parent_stat.st_mode):
        raise RuntimeFileError(f"runtime parent is not a directory: {parent}")
    try:
        os.mkdir(root, RUNTIME_MODE)
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimeFileError(f"could not create runtime directory: {root}") from exc
    _assert_no_symlink_components(root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeFileError(f"runtime path is not a directory: {root}")
    if info.st_uid != uid:
        raise RuntimeFileError(f"runtime directory has wrong owner uid={info.st_uid}")
    if stat.S_IMODE(info.st_mode) != RUNTIME_MODE:
        os.chmod(root, RUNTIME_MODE, follow_symlinks=False)
        if stat.S_IMODE(root.lstat().st_mode) != RUNTIME_MODE:
            raise RuntimeFileError("runtime directory mode could not be restricted to 0700")
    return root


def create_bearer_secret(root: Path, *, expected_uid: int | None = None) -> BearerSecret:
    secret = BearerSecret(secrets.token_urlsafe(48))
    _atomic_private_write(root, BEARER_NAME, (secret.value + "\n").encode(), expected_uid=expected_uid)
    return secret


def create_startup_nonce(root: Path, *, expected_uid: int | None = None) -> StartupNonce:
    nonce = StartupNonce(secrets.token_urlsafe(48))
    _atomic_private_write(root, STARTUP_NONCE_NAME, (nonce.value + "\n").encode(), expected_uid=expected_uid)
    return nonce


def write_service_git_config(home_root: Path, *, expected_uid: int | None = None) -> Path:
    """Install the daemon's protected Git trust policy without touching user config.

    WorkspacePolicy still decides which activation roots are usable and keeps
    edits below ``/data``.  This file only disables Git's owner-UID heuristic
    inside the isolated daemon environment, where root-owned service processes
    intentionally serve worktrees owned by the interactive user.
    """

    content = b"[safe]\n\tdirectory = *\n"
    _atomic_private_write(
        home_root,
        SERVICE_GIT_CONFIG_NAME,
        content,
        expected_uid=expected_uid,
    )
    return home_root / SERVICE_GIT_CONFIG_NAME


def consume_startup_nonce(
    root: Path,
    expected: StartupNonce,
    *,
    expected_uid: int | None = None,
) -> None:
    """Validate and remove the connector-authorized nonce exactly once."""

    raw = _read_private_file(root, STARTUP_NONCE_NAME, expected_uid=expected_uid)
    try:
        observed = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeFileError("startup nonce is malformed") from exc
    if not observed.endswith("\n") or not hmac.compare_digest(observed[:-1], expected.value):
        raise RuntimeFileError("startup nonce does not match connector authorization")
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(STARTUP_NONCE_NAME, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except OSError as exc:
        raise RuntimeFileError("startup nonce could not be consumed") from exc
    finally:
        os.close(directory_fd)


def write_discovery_metadata(
    root: Path,
    metadata: DiscoveryMetadata,
    *,
    expected_uid: int | None = None,
) -> None:
    _validate_metadata(metadata)
    rendered = json.dumps(asdict(metadata), sort_keys=True, separators=(",", ":")).encode() + b"\n"
    _atomic_private_write(root, DISCOVERY_NAME, rendered, expected_uid=expected_uid)


def read_bearer_secret(root: Path, *, expected_uid: int | None = None) -> BearerSecret:
    raw = _read_private_file(root, BEARER_NAME, expected_uid=expected_uid)
    try:
        rendered = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeFileError("bearer secret is not ASCII") from exc
    if not rendered.endswith("\n") or "\n" in rendered[:-1]:
        raise RuntimeFileError("bearer secret is malformed")
    value = rendered[:-1]
    allowed = frozenset(string.ascii_letters + string.digits + "-_")
    if len(value) < 32 or any(character not in allowed for character in value):
        raise RuntimeFileError("bearer secret is malformed")
    return BearerSecret(value)


def read_discovery_metadata(
    root: Path,
    *,
    is_process_identity_live: ProcessIdentityValidator,
    expected_uid: int | None = None,
) -> DiscoveryMetadata:
    raw = _read_private_file(root, DISCOVERY_NAME, expected_uid=expected_uid)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFileError("discovery metadata is malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeFileError("discovery metadata must be an object")
    expected_fields = set(DiscoveryMetadata.__dataclass_fields__)
    if set(payload) != expected_fields:
        raise RuntimeFileError("discovery metadata has an invalid schema")
    try:
        metadata = DiscoveryMetadata(**payload)
    except TypeError as exc:
        raise RuntimeFileError("discovery metadata has invalid field types") from exc
    _validate_metadata(metadata)
    if not is_process_identity_live(metadata.pid, metadata.process_start_time):
        raise RuntimeFileError("discovery metadata refers to a stale daemon")
    return metadata


def _validate_metadata(metadata: DiscoveryMetadata) -> None:
    if metadata.schema_version != DISCOVERY_SCHEMA_VERSION:
        raise RuntimeFileError("unsupported discovery schema version")
    if not isinstance(metadata.daemon_id, str):
        raise RuntimeFileError("daemon identity must be a UUID")
    try:
        UUID(metadata.daemon_id)
    except (ValueError, AttributeError) as exc:
        raise RuntimeFileError("daemon identity must be a UUID") from exc
    if isinstance(metadata.pid, bool) or not isinstance(metadata.pid, int) or metadata.pid <= 0:
        raise RuntimeFileError("daemon pid must be positive")
    for name, value in {
        "process_start_time": metadata.process_start_time,
        "created_at": metadata.created_at,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
            raise RuntimeFileError(f"{name} must be positive")
    if not isinstance(metadata.endpoint, str):
        raise RuntimeFileError("discovery endpoint must be loopback-only HTTP")
    endpoint = urllib.parse.urlparse(metadata.endpoint)
    try:
        port = endpoint.port
    except ValueError as exc:
        raise RuntimeFileError("discovery endpoint has an invalid port") from exc
    if endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1" or port is None or endpoint.path != "/mcp":
        raise RuntimeFileError("discovery endpoint must be loopback-only HTTP")
    if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise RuntimeFileError("discovery endpoint contains forbidden components")
    if (
        not isinstance(metadata.protocol_version, str)
        or not isinstance(metadata.server_version, str)
        or not metadata.protocol_version
        or not metadata.server_version
    ):
        raise RuntimeFileError("discovery versions must be non-empty")
    try:
        validate_build_identity(metadata.build_identity)
    except ValueError as exc:
        raise RuntimeFileError("discovery build identity is invalid") from exc


def _prepare_private_child(parent: Path, name: str) -> Path:
    if not name or name in {".", ".."} or "/" in name:
        raise RuntimeFileError("runtime child name is invalid")
    directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        with suppress(FileExistsError):
            os.mkdir(name, RUNTIME_MODE, dir_fd=directory_fd)
        child_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
        try:
            info = os.fstat(child_fd)
            if info.st_uid != os.getuid():
                raise RuntimeFileError(f"runtime child has wrong owner: {name}")
            if stat.S_IMODE(info.st_mode) != RUNTIME_MODE:
                os.fchmod(child_fd, RUNTIME_MODE)
            if stat.S_IMODE(os.fstat(child_fd).st_mode) != RUNTIME_MODE:
                raise RuntimeFileError(f"runtime child mode could not be restricted: {name}")
        finally:
            os.close(child_fd)
    except OSError as exc:
        raise RuntimeFileError(f"runtime child is unavailable or symlinked: {name}") from exc
    finally:
        os.close(directory_fd)
    return parent / name


def _atomic_private_write(root: Path, name: str, content: bytes, *, expected_uid: int | None) -> None:
    prepare_runtime_directory(root, expected_uid=expected_uid)
    uid = os.getuid() if expected_uid is None else expected_uid
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            PRIVATE_FILE_MODE,
            dir_fd=directory_fd,
        )
        _write_all(file_fd, content)
        os.fsync(file_fd)
        info = os.fstat(file_fd)
        if info.st_uid != uid or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE:
            raise RuntimeFileError("temporary runtime file has unsafe ownership or mode")
        os.close(file_fd)
        file_fd = None
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _read_private_file(root: Path, name: str, *, expected_uid: int | None) -> bytes:
    prepare_runtime_directory(root, expected_uid=expected_uid)
    uid = os.getuid() if expected_uid is None else expected_uid
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except OSError as exc:
            raise RuntimeFileError(f"runtime file is unavailable or symlinked: {name}") from exc
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise RuntimeFileError(f"runtime artifact is not a regular file: {name}")
            if info.st_uid != uid:
                raise RuntimeFileError(f"runtime artifact has wrong owner: {name}")
            if stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE:
                raise RuntimeFileError(f"runtime artifact mode must be 0600: {name}")
            return _read_bounded(file_fd)
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


def _assert_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            info = current.lstat()
        except OSError as exc:
            raise RuntimeFileError(f"runtime path component is unavailable: {current}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeFileError(f"runtime path component is symlinked: {current}")


def _write_all(file_descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:  # pragma: no cover - operating-system invariant
            raise RuntimeFileError("runtime file write made no progress")
        view = view[written:]


def _read_bounded(file_descriptor: int, limit: int = 64 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(file_descriptor, min(8192, limit + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise RuntimeFileError("runtime artifact exceeds the bounded size")
