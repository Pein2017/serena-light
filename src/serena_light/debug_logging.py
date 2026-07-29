"""Small, secret-safe local debugging sink for daemon lifecycle summaries."""

from __future__ import annotations

import os
import re
import stat
import sys
import threading
from pathlib import Path

from serena_light.runtime_files import PRIVATE_FILE_MODE, RuntimeFileError, prepare_runtime_directory

DEBUG_LOG_NAME = "debug.log"
MAX_MESSAGE_CHARS = 512
DEFAULT_MAX_BYTES = 64 * 1024
DEFAULT_BACKUP_COUNT = 3

_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(bearer|authorization|cookie|password|secret|token)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")


class DebugLogger:
    """Write bounded, redacted lifecycle summaries without accepting payloads."""

    def __init__(
        self,
        runtime_directory: Path,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        backup_count: int = DEFAULT_BACKUP_COUNT,
        stderr: object | None = None,
    ) -> None:
        if max_bytes < 128 or backup_count < 0:
            raise ValueError("debug log limits are unsafe")
        self._root = runtime_directory
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        self._stderr = sys.stderr if stderr is None else stderr
        self._lock = threading.Lock()

    def report(self, event: str, message: str = "") -> bool:
        """Emit one concise redacted summary; return false if its file is unsafe."""
        if not isinstance(event, str) or not isinstance(message, str):
            raise TypeError("debug summaries must be strings")
        line = _render(event, message)
        with self._lock:
            self._write_stderr(line)
            try:
                self._write_file(line.encode("utf-8", errors="replace") + b"\n")
            except (OSError, RuntimeFileError):
                return False
        return True

    def _write_stderr(self, line: str) -> None:
        try:
            self._stderr.write(line + "\n")  # type: ignore[attr-defined]
            self._stderr.flush()  # type: ignore[attr-defined]
        except (AttributeError, OSError):
            pass

    def _write_file(self, line: bytes) -> None:
        prepare_runtime_directory(self._root)
        directory_fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            self._rotate_if_needed(directory_fd, len(line))
            fd = os.open(
                DEBUG_LOG_NAME,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
                PRIVATE_FILE_MODE,
                dir_fd=directory_fd,
            )
            try:
                _assert_private_regular(fd, DEBUG_LOG_NAME)
                _write_all(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)

    def _rotate_if_needed(self, directory_fd: int, incoming_size: int) -> None:
        try:
            fd = os.open(DEBUG_LOG_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            return
        try:
            _assert_private_regular(fd, DEBUG_LOG_NAME)
            current_size = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if current_size + incoming_size <= self._max_bytes:
            return
        for index in range(self._backup_count, 0, -1):
            name = f"{DEBUG_LOG_NAME}.{index}"
            if _exists_safe(directory_fd, name):
                if index == self._backup_count:
                    os.unlink(name, dir_fd=directory_fd)
                else:
                    os.rename(name, f"{DEBUG_LOG_NAME}.{index + 1}", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        if self._backup_count:
            os.rename(DEBUG_LOG_NAME, f"{DEBUG_LOG_NAME}.1", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            os.unlink(DEBUG_LOG_NAME, dir_fd=directory_fd)


def _render(event: str, message: str) -> str:
    compact_event = _redact(" ".join(event.split()))[:80] or "event"
    compact_message = _redact(" ".join(message.split()))
    return f"serena-light {compact_event}: {compact_message[:MAX_MESSAGE_CHARS]}".rstrip(": ")


def _redact(value: str) -> str:
    redacted = _BEARER_VALUE.sub("Bearer <redacted>", value)
    return _SENSITIVE_ASSIGNMENT.sub(r"\1\2<redacted>", redacted)


def _assert_private_regular(fd: int, name: str) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE:
        raise RuntimeFileError(f"unsafe debug log artifact: {name}")


def _exists_safe(directory_fd: int, name: str) -> bool:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    try:
        _assert_private_regular(fd, name)
        return True
    finally:
        os.close(fd)


def _write_all(fd: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:  # pragma: no cover - operating-system invariant
            raise RuntimeFileError("debug log write made no progress")
        view = view[written:]
