"""Transport-neutral, hash-guarded replacement of one complete symbol body.

Workspace identity, language-server dispatch, and transport recovery remain in
their owning layers.  This module receives those capabilities through three
small seams and owns only the conflict check plus the atomic file operation.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import TracebackType
from typing import Any, Protocol

from serena_light.lsp.executor import EditCommit
from serena_light.lsp.positions import FileSnapshot, PositionError
from serena_light.tools.envelopes import (
    ErrorCode,
    ErrorEnvelope,
    GenerationMetadata,
    RetryMetadata,
    ToolEnvelope,
    WorkspaceMetadata,
    error,
    from_workspace_error,
    success,
)
from serena_light.tools.navigation import DocumentNavigation, DocumentSymbolInput, find_symbol
from serena_light.workspace.identity import WorkspaceError, open_guarded_directory

type WriteCall = Callable[[int, memoryview], int]
type FlushCall = Callable[[int], None]


def _fsync(file_descriptor: int) -> None:
    """Indirect the default flush so it is resolved per call, not at import."""

    os.fsync(file_descriptor)


@dataclass(frozen=True, slots=True)
class AuthorizedEdit:
    """The authorizer's canonical target, before any source read or LSP call."""

    path: Path
    relative_path: str
    workspace: WorkspaceMetadata | None = None
    root: Path | None = None

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("authorized edit path must be absolute")
        if not _valid_normalized_relative_path(self.relative_path):
            raise ValueError("authorized relative path must be normalized")
        if self.root is not None and self.path != self.root / self.relative_path:
            raise ValueError("authorized edit path must be the lexical root-relative path")


@dataclass(frozen=True, slots=True)
class ReplacementNotification:
    """Installed bytes passed exactly once to the owning adapter seam."""

    path: Path
    relative_path: str
    uri: str
    text: str
    old_hash: str
    new_hash: str
    symbol_name_path: str


@dataclass(frozen=True, slots=True)
class NotificationResult:
    """Adapter state observed after its change notification returns."""

    state: str
    file_generation: int
    generations: GenerationMetadata | None = None

    def __post_init__(self) -> None:
        if not self.state or self.file_generation < 0:
            raise ValueError("notification state and file generation must be valid")


class EditAuthorizer(Protocol):
    def authorize_edit(self, relative_path: str) -> AuthorizedEdit: ...


class CurrentDocumentSymbolProvider(Protocol):
    def resolve_document_symbols(
        self,
        target: AuthorizedEdit,
        snapshot: FileSnapshot,
    ) -> DocumentSymbolInput: ...


class EditNotifier(Protocol):
    def notify_replaced(self, notification: ReplacementNotification) -> NotificationResult: ...


class OperationLock(Protocol):
    def __enter__(self) -> object: ...

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


def replace_symbol_body(
    name_path: str | Sequence[str],
    relative_path: str,
    body: str,
    expected_hash: str,
    *,
    authorizer: EditAuthorizer,
    symbol_provider: CurrentDocumentSymbolProvider,
    notifier: EditNotifier,
    operation_lock: OperationLock,
    commit: EditCommit | None = None,
    write_call: WriteCall = os.write,
    flush_call: FlushCall = _fsync,
    directory_flush_call: FlushCall = _fsync,
) -> ToolEnvelope:
    """Authorize, compare, resolve, atomically install, then notify exactly once."""

    if (
        not _valid_name_path(name_path)
        or not isinstance(relative_path, str)
        or not relative_path
        or "\x00" in relative_path
        or not isinstance(body, str)
        or not _valid_hash(expected_hash)
    ):
        return error(ErrorCode.INVALID_INPUT, details={"field": "name_path, relative_path, body, or expected_hash"})
    expected_hash = expected_hash.lower()

    with operation_lock:
        target_or_error = _authorize(authorizer, relative_path)
        if isinstance(target_or_error, ErrorEnvelope):
            return target_or_error
        target = target_or_error
        if target.relative_path != relative_path:
            return error(ErrorCode.INVALID_PATH, details={"path": relative_path}, workspace=target.workspace)

        current_or_error = _read_target(target)
        if isinstance(current_or_error, ErrorEnvelope):
            return current_or_error
        current = current_or_error
        old_hash = _sha256(current.raw_bytes)
        if old_hash != expected_hash:
            return _stale(target, expected_hash, old_hash)

        try:
            snapshot = FileSnapshot.from_bytes(current.raw_bytes)
            supplied = symbol_provider.resolve_document_symbols(target, snapshot)
            resolved_or_error = _resolve_unique_symbol(target, snapshot, supplied, name_path)
        except WorkspaceError as exc:
            return from_workspace_error(exc)
        except (PositionError, TypeError, ValueError):
            return error(
                ErrorCode.INVALID_INPUT,
                details={"path": target.relative_path, "stage": "symbol_resolution"},
                workspace=target.workspace,
            )
        if isinstance(resolved_or_error, ErrorEnvelope):
            return resolved_or_error
        symbol_identity, start_byte, end_byte, document = resolved_or_error

        # An external writer does not share this lock, so verify the exact LSP
        # snapshot again before any temporary file is created.
        confirmed_or_error = _read_target(target)
        if isinstance(confirmed_or_error, ErrorEnvelope):
            return confirmed_or_error
        confirmed = confirmed_or_error
        confirmed_hash = _sha256(confirmed.raw_bytes)
        if confirmed.raw_bytes != current.raw_bytes:
            return _stale(target, expected_hash, confirmed_hash)

        try:
            replacement = _replacement_bytes(snapshot, body, start_byte, end_byte)
        except (PositionError, UnicodeError, ValueError):
            return error(
                ErrorCode.INVALID_INPUT,
                details={"path": target.relative_path, "stage": "replacement_body"},
                workspace=target.workspace,
                adapter=supplied.adapter,
                generations=supplied.generations,
            )
        new_hash = _sha256(replacement)

        try:
            changed_hash = _atomic_replace(
                target,
                replacement,
                expected_raw=current.raw_bytes,
                mode=confirmed.mode,
                commit=commit,
                write_call=write_call,
                flush_call=flush_call,
                directory_flush_call=directory_flush_call,
            )
        except _ConcurrentChange as exc:
            return _stale(target, expected_hash, exc.current_hash)
        except _InvalidTarget:
            return _invalid_target(target, stage="pre_install_validation")
        except _InstalledUncertain as exc:
            # The bytes are already installed, so the caller must re-read rather
            # than replay; the durability of the rename is what is unknown.
            return _uncertain(
                target,
                supplied,
                symbol_identity,
                old_hash=old_hash,
                new_hash=new_hash,
                stage=exc.stage,
            )

        try:
            notification = notifier.notify_replaced(
                ReplacementNotification(
                    target.path,
                    target.relative_path,
                    document.uri,
                    FileSnapshot.from_bytes(replacement).text,
                    old_hash,
                    new_hash,
                    symbol_identity["name_path"],
                )
            )
            if not isinstance(notification, NotificationResult):
                raise TypeError("notifier returned an invalid result")
        except Exception:
            return _uncertain(
                target,
                supplied,
                symbol_identity,
                old_hash=old_hash,
                new_hash=new_hash,
                stage="notification",
                fallback_hash=changed_hash,
            )

        if commit is not None:
            commit.mark_done()
        return success(
            {
                "relative_path": target.relative_path,
                "old_hash": old_hash,
                "new_hash": new_hash,
                "symbol": symbol_identity,
                "file_generation": notification.file_generation,
                "notification_state": notification.state,
            },
            workspace=target.workspace,
            adapter=supplied.adapter,
            generations=notification.generations or supplied.generations,
        )


def _authorize(authorizer: EditAuthorizer, relative_path: str) -> AuthorizedEdit | ErrorEnvelope:
    try:
        target = authorizer.authorize_edit(relative_path)
    except WorkspaceError as exc:
        return from_workspace_error(exc)
    if not isinstance(target, AuthorizedEdit):
        return error(ErrorCode.INVALID_PATH, details={"path": relative_path})
    return target


@dataclass(frozen=True, slots=True)
class _TargetSnapshot:
    raw_bytes: bytes
    mode: int


class _ConcurrentChange(RuntimeError):
    def __init__(self, current_hash: str) -> None:
        super().__init__("target changed before atomic replacement")
        self.current_hash = current_hash


class _InstalledUncertain(RuntimeError):
    """The replacement reached the filesystem but its durability is unknown."""

    def __init__(self, stage: str = "directory_fsync") -> None:
        super().__init__(stage)
        self.stage = stage


class _InvalidTarget(RuntimeError):
    """The guarded physical target is no longer unambiguous."""


def _open_target_directory(target: AuthorizedEdit) -> int:
    """Open the target's parent, walking in-root components without following links."""

    if target.root is None:
        return os.open(target.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    return open_guarded_directory(target.root, PurePosixPath(target.relative_path).parts[:-1])


def safe_current_hash(target: AuthorizedEdit) -> str | None:
    """Read the installed bytes once, returning ``None`` when that is not safe."""

    try:
        observed = _read_target(target)
    except OSError:
        return None
    return None if isinstance(observed, ErrorEnvelope) else _sha256(observed.raw_bytes)


def _read_target(target: AuthorizedEdit) -> _TargetSnapshot | ErrorEnvelope:
    try:
        directory_fd = _open_target_directory(target)
    except OSError:
        return _invalid_target(target)
    try:
        try:
            return _read_target_in_directory(target, directory_fd)
        except _InvalidTarget:
            return _invalid_target(target)
    finally:
        os.close(directory_fd)


def _read_target_in_directory(target: AuthorizedEdit, directory_fd: int) -> _TargetSnapshot:
    """Read one stable regular-file entry through an already guarded parent."""

    try:
        file_fd = os.open(target.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except OSError as exc:
        raise _InvalidTarget from exc
    try:
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise _InvalidTarget
        chunks: list[bytes] = []
        while chunk := os.read(file_fd, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(file_fd)
        if _stat_identity(before) != _stat_identity(after):
            raise _InvalidTarget
        entry = os.stat(target.path.name, dir_fd=directory_fd, follow_symlinks=False)
        if _stat_identity(after) != _stat_identity(entry):
            raise _InvalidTarget
        return _TargetSnapshot(b"".join(chunks), stat.S_IMODE(after.st_mode))
    except OSError as exc:
        raise _InvalidTarget from exc
    finally:
        os.close(file_fd)


def _require_current_lexical_parent(target: AuthorizedEdit, pinned_directory_fd: int) -> None:
    """Prove the guarded lexical parent still names the pinned directory."""

    try:
        current_directory_fd = _open_target_directory(target)
    except OSError as exc:
        raise _InvalidTarget from exc
    try:
        try:
            pinned = os.fstat(pinned_directory_fd)
            current = os.fstat(current_directory_fd)
        except OSError as exc:
            raise _InvalidTarget from exc
        if _inode_identity(pinned) != _inode_identity(current):
            raise _InvalidTarget
    finally:
        os.close(current_directory_fd)


def _resolve_unique_symbol(
    target: AuthorizedEdit,
    snapshot: FileSnapshot,
    supplied: DocumentSymbolInput,
    name_path: str | Sequence[str],
) -> tuple[Mapping[str, Any], int, int, DocumentNavigation] | ErrorEnvelope:
    if not isinstance(supplied, DocumentSymbolInput):
        raise TypeError("symbol provider returned an invalid result")
    if (
        supplied.relative_path != target.relative_path
        or supplied.uri != target.path.as_uri()
        or supplied.snapshot.raw_bytes != snapshot.raw_bytes
    ):
        raise ValueError("document-symbol response does not describe the authorized snapshot")
    document = DocumentNavigation.from_input(supplied)
    result = find_symbol(document, name_path, max_answer_chars=64_000)
    if isinstance(result, ErrorEnvelope):
        return result
    payload = result.to_dict()["data"]
    if not isinstance(payload, Mapping) or not isinstance(payload.get("symbol"), Mapping):
        raise TypeError("find_symbol returned invalid symbol data")
    symbol = payload["symbol"]
    assert isinstance(symbol, Mapping)
    source_range = symbol.get("range")
    if not isinstance(source_range, Mapping):
        raise TypeError("find_symbol omitted the symbol range")
    start = _byte_offset(source_range, "start")
    end = _byte_offset(source_range, "end")
    identity = {key: symbol[key] for key in ("name", "name_path", "kind")}
    return identity, start, end, document


def _byte_offset(source_range: Mapping[str, Any], endpoint: str) -> int:
    position = source_range.get(endpoint)
    if not isinstance(position, Mapping) or not isinstance(position.get("byte_offset"), int):
        raise TypeError("find_symbol returned an invalid byte offset")
    return position["byte_offset"]


def _replacement_bytes(snapshot: FileSnapshot, body: str, start: int, end: int) -> bytes:
    newline = _newline_contract(snapshot)
    normalized_body = body.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline)
    encoded = normalized_body.encode(snapshot.encoding)
    return snapshot.raw_bytes[:start] + encoded + snapshot.raw_bytes[end:]


def _newline_contract(snapshot: FileSnapshot) -> str:
    endings = set(snapshot.line_endings)
    if not endings:
        return "\n"
    if endings == {"\n"}:
        return "\n"
    if endings == {"\r\n"}:
        return "\r\n"
    raise ValueError("mixed or bare-CR newline contracts are not editable in v1")


def _atomic_replace(
    target: AuthorizedEdit,
    content: bytes,
    *,
    expected_raw: bytes,
    mode: int,
    commit: EditCommit | None,
    write_call: WriteCall,
    flush_call: FlushCall,
    directory_flush_call: FlushCall,
) -> str:
    directory_fd = _open_target_directory(target)
    temporary = f".{target.path.name}.serena-light-{secrets.token_hex(12)}.tmp"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(file_fd, content, write_call)
        os.fchmod(file_fd, mode)
        flush_call(file_fd)
        os.close(file_fd)
        file_fd = None

        # The temporary and final rename stay anchored to this pinned parent.
        # Validate the target through that same descriptor, and bracket the read
        # with lexical identity checks so a renamed/recreated hierarchy cannot
        # redirect only the conflict check.
        _require_current_lexical_parent(target, directory_fd)
        current = _read_target_in_directory(target, directory_fd)
        if current.raw_bytes != expected_raw:
            raise _ConcurrentChange(_sha256(current.raw_bytes))
        _require_current_lexical_parent(target, directory_fd)
        os.replace(temporary, target.path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        if commit is not None:
            commit.mark_installed()
        # ``os.replace`` remains anchored to the pinned descriptor.  Recheck the
        # lexical path before declaring success: a parent replacement in the
        # final gap otherwise writes only the moved-away directory. The rename
        # has already happened, so a failed proof is UNCERTAIN, never INVALID_PATH.
        try:
            _require_current_lexical_parent(target, directory_fd)
        except _InvalidTarget as exc:
            raise _InstalledUncertain("post_install_path_validation") from exc
        try:
            directory_flush_call(directory_fd)
        except OSError as exc:
            raise _InstalledUncertain from exc
        return _sha256(content)
    finally:
        if file_fd is not None:
            with suppress(OSError):
                os.close(file_fd)
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _write_all(file_fd: int, content: bytes, write_call: WriteCall) -> None:
    remaining = memoryview(content)
    while remaining:
        written = write_call(file_fd, remaining)
        if written <= 0 or written > len(remaining):
            raise OSError("temporary write made no valid progress")
        remaining = remaining[written:]


def _valid_name_path(value: str | Sequence[str]) -> bool:
    if isinstance(value, str):
        components = tuple(value.lstrip("/").rstrip("/").split("/"))
    elif isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        components = tuple(value)
    else:
        return False
    return bool(components) and all(isinstance(item, str) and bool(item) for item in components)


def _valid_hash(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _valid_normalized_relative_path(value: str) -> bool:
    return (
        bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and "\x00" not in value
        and all(part not in {"", ".", ".."} for part in value.split("/"))
    )


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, value.st_mode)


def _inode_identity(value: os.stat_result) -> tuple[int, int]:
    return (value.st_dev, value.st_ino)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stale(target: AuthorizedEdit, expected_hash: str, current_hash: str) -> ErrorEnvelope:
    return error(
        ErrorCode.STALE_HASH,
        retry=RetryMetadata(retryable=False),
        details={
            "relative_path": target.relative_path,
            "expected_hash": expected_hash,
            "current_hash": current_hash,
            "requires_current_reread": True,
        },
        workspace=target.workspace,
    )


def _uncertain(
    target: AuthorizedEdit,
    supplied: DocumentSymbolInput,
    symbol_identity: Mapping[str, Any],
    *,
    old_hash: str,
    new_hash: str,
    stage: str,
    fallback_hash: str | None = None,
) -> ErrorEnvelope:
    """Report an installed-but-unconfirmed edit that must never be replayed."""

    observed = safe_current_hash(target)
    return error(
        ErrorCode.UNCERTAIN,
        retry=RetryMetadata(retryable=False),
        details={
            "relative_path": target.relative_path,
            "old_hash": old_hash,
            "new_hash": new_hash,
            "current_hash": observed if observed is not None else fallback_hash,
            "symbol": symbol_identity,
            "notification_state": "failed",
            "uncertain_stage": stage,
            "requires_current_reread": True,
        },
        workspace=target.workspace,
        adapter=supplied.adapter,
        generations=supplied.generations,
    )


def _invalid_target(target: AuthorizedEdit, *, stage: str = "current_file_reread") -> ErrorEnvelope:
    return error(
        ErrorCode.INVALID_PATH,
        details={"path": str(target.path), "stage": stage},
        workspace=target.workspace,
    )
