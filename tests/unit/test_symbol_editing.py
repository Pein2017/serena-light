from __future__ import annotations

import hashlib
import os
import stat
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from serena_light.lsp.executor import EditCommit, EditCommitState
from serena_light.lsp.positions import FileSnapshot, PositionEncoding, PositionMapper
from serena_light.tools.editing import (
    AuthorizedEdit,
    NotificationResult,
    ReplacementNotification,
    replace_symbol_body,
)
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, ToolEnvelope, WorkspaceMetadata
from serena_light.tools.navigation import DocumentSymbolInput
from serena_light.workspace.identity import WorkspaceError, WorkspaceErrorCode, WorkspaceErrorData

type RawSymbol = Mapping[str, Any]


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _lsp_range(snapshot: FileSnapshot, start: int, end: int) -> dict[str, dict[str, int]]:
    mapper = PositionMapper(snapshot, PositionEncoding.UTF16)
    first = mapper.text_offset_to_lsp(start)
    last = mapper.text_offset_to_lsp(end)
    return {
        "start": {"line": first.line, "character": first.character},
        "end": {"line": last.line, "character": last.character},
    }


def _nested_target_symbols(snapshot: FileSnapshot) -> list[dict[str, Any]]:
    text = snapshot.text
    class_start = text.index("class A:")
    target_start = text.index("def target")
    target_end = len(text)
    return [
        {
            "name": "A",
            "kind": 5,
            "range": _lsp_range(snapshot, class_start, target_end),
            "children": [
                {
                    "name": "target",
                    "kind": 6,
                    "range": _lsp_range(snapshot, target_start, target_end),
                }
            ],
        }
    ]


@dataclass
class _Authorizer:
    target: AuthorizedEdit | None = None
    failure: WorkspaceError | None = None
    action: Callable[[], None] | None = None
    calls: list[str] = field(default_factory=list)

    def authorize_edit(self, relative_path: str) -> AuthorizedEdit:
        self.calls.append(relative_path)
        if self.action is not None:
            self.action()
        if self.failure is not None:
            raise self.failure
        assert self.target is not None
        return self.target


@dataclass
class _Symbols:
    raw_factory: Callable[[FileSnapshot], Sequence[RawSymbol] | None]
    action: Callable[[], None] | None = None
    calls: int = 0
    snapshots: list[bytes] = field(default_factory=list)
    locked: Callable[[], bool] | None = None

    def resolve_document_symbols(
        self,
        target: AuthorizedEdit,
        snapshot: FileSnapshot,
    ) -> DocumentSymbolInput:
        self.calls += 1
        self.snapshots.append(snapshot.raw_bytes)
        if self.locked is not None:
            assert self.locked()
        if self.action is not None:
            self.action()
        return DocumentSymbolInput(
            target.relative_path,
            target.path.as_uri(),
            snapshot,
            self.raw_factory(snapshot),
            PositionEncoding.UTF16,
            target.workspace,
            AdapterMetadata("pyright", "python"),
            GenerationMetadata(trust=1, program=2, document=3, index=4),
        )


@dataclass
class _Notifier:
    result: NotificationResult = field(
        default_factory=lambda: NotificationResult(
            "notified",
            4,
            GenerationMetadata(trust=1, program=2, document=4, index=4),
        )
    )
    failure: Exception | None = None
    calls: list[ReplacementNotification] = field(default_factory=list)
    locked: Callable[[], bool] | None = None

    def notify_replaced(self, notification: ReplacementNotification) -> NotificationResult:
        self.calls.append(notification)
        if self.locked is not None:
            assert self.locked()
        if self.failure is not None:
            raise self.failure
        return self.result


class _TrackingLock:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self) -> _TrackingLock:
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.depth -= 1
        self._lock.release()

    def owned(self) -> bool:
        return self.depth > 0


def _target(path: Path) -> AuthorizedEdit:
    return AuthorizedEdit(
        path,
        path.name,
        WorkspaceMetadata(str(path.parent), "git", str(path.parent)),
    )


def _unexpected_symbols(_snapshot: FileSnapshot) -> Sequence[RawSymbol] | None:
    raise AssertionError("document-symbol resolution was not expected")


@dataclass
class _Editor:
    authorizer: _Authorizer
    symbols: _Symbols
    notifier: _Notifier
    lock: _TrackingLock
    write_call: Callable[[int, memoryview], int]
    flush_call: Callable[[int], None]
    directory_flush_call: Callable[[int], None] = os.fsync
    commit: EditCommit | None = None

    def replace_symbol_body(
        self,
        name_path: str,
        relative_path: str,
        body: str,
        expected_hash: str,
    ) -> ToolEnvelope:
        return replace_symbol_body(
            name_path,
            relative_path,
            body,
            expected_hash,
            authorizer=self.authorizer,
            symbol_provider=self.symbols,
            notifier=self.notifier,
            operation_lock=self.lock,
            commit=self.commit,
            write_call=self.write_call,
            flush_call=self.flush_call,
            directory_flush_call=self.directory_flush_call,
        )


def _editor(
    path: Path,
    symbols: _Symbols,
    notifier: _Notifier | None = None,
    *,
    authorizer: _Authorizer | None = None,
    lock: _TrackingLock | None = None,
    write_call: Callable[[int, memoryview], int] = os.write,
    flush_call: Callable[[int], None] = os.fsync,
    directory_flush_call: Callable[[int], None] = os.fsync,
    commit: EditCommit | None = None,
) -> _Editor:
    return _Editor(
        authorizer or _Authorizer(_target(path)),
        symbols,
        notifier or _Notifier(),
        lock or _TrackingLock(),
        write_call,
        flush_call,
        directory_flush_call,
        commit,
    )


def test_replace_preserves_utf8_bom_crlf_mode_and_uses_utf16_astral_offsets(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = (
        b"\xef\xbb\xbf# before \xf0\x9f\x98\x80\r\nclass A:\r\n"
        b"    def target(self):\r\n        return '\xf0\x9f\x9a\x80'\r\n"
    )
    path.write_bytes(original)
    path.chmod(0o640)
    symbols = _Symbols(_nested_target_symbols)
    notifier = _Notifier()
    lock = _TrackingLock()
    symbols.locked = lock.owned
    notifier.locked = lock.owned

    result = _editor(path, symbols, notifier, lock=lock).replace_symbol_body(
        "A/target",
        path.name,
        "def target(self):\n        return '✨'\n",
        _sha256(original),
    ).to_dict()

    replaced = path.read_bytes()
    assert result["ok"] is True
    assert result["data"] == {
        "relative_path": "sample.py",
        "old_hash": _sha256(original),
        "new_hash": _sha256(replaced),
        "symbol": {"name": "target", "name_path": "A/target", "kind": 6},
        "file_generation": 4,
        "notification_state": "notified",
    }
    assert result["generations"] == {"trust": 1, "program": 2, "document": 4, "index": 4}
    assert replaced.startswith(b"\xef\xbb\xbf# before \xf0\x9f\x98\x80\r\nclass A:\r\n    ")
    assert b"return '\xe2\x9c\xa8'\r\n" in replaced
    assert b"\n" not in replaced.replace(b"\r\n", b"")
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert len(notifier.calls) == 1
    assert notifier.calls[0].text.startswith("# before 😀\r\n")
    assert notifier.calls[0].new_hash == _sha256(replaced)
    assert not list(tmp_path.glob(".sample.py.serena-light-*.tmp"))


@pytest.mark.parametrize(
    "code",
    [
        WorkspaceErrorCode.INVALID_PATH,
        WorkspaceErrorCode.UNTRUSTED_ROOT,
        WorkspaceErrorCode.OUT_OF_WORKSPACE,
        WorkspaceErrorCode.READ_ONLY_ROOT,
    ],
)
def test_every_authorization_error_returns_before_symbol_or_temp_work(
    tmp_path: Path,
    code: WorkspaceErrorCode,
) -> None:
    path = tmp_path / "sample.py"
    original = b"def target():\n    return 1\n"
    path.write_bytes(original)
    failure = WorkspaceError(WorkspaceErrorData(code, "private policy detail", path=path))
    authorizer = _Authorizer(failure=failure)
    symbols = _Symbols(_unexpected_symbols)

    result = _editor(path, symbols, authorizer=authorizer).replace_symbol_body(
        "target", path.name, "def target():\n    return 2\n", _sha256(original)
    ).to_dict()

    assert result["error"]["code"] == code.value
    assert path.read_bytes() == original
    assert symbols.calls == 0
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


def test_symlink_escape_is_rejected_by_authorization_before_file_io(tmp_path: Path) -> None:
    outside = tmp_path / "outside.py"
    outside.write_text("def target():\n    return 1\n")
    link = tmp_path / "link.py"
    link.symlink_to(outside)
    failure = WorkspaceError(
        WorkspaceErrorData(WorkspaceErrorCode.OUT_OF_WORKSPACE, "symlink escape", path=outside)
    )
    authorizer = _Authorizer(failure=failure)
    symbols = _Symbols(_unexpected_symbols)

    result = _editor(link, symbols, authorizer=authorizer).replace_symbol_body(
        "target", "link.py", "def target():\n    return 2\n", _sha256(outside.read_bytes())
    ).to_dict()

    assert result["error"]["code"] == "OUT_OF_WORKSPACE"
    assert outside.read_text() == "def target():\n    return 1\n"
    assert link.is_symlink()
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


def test_stale_hash_short_circuits_current_symbol_resolution(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    current = b"def target():\n    return 2\n"
    path.write_bytes(current)
    symbols = _Symbols(_unexpected_symbols)

    result = _editor(path, symbols).replace_symbol_body(
        "target", path.name, "def target():\n    return 3\n", "0" * 64
    ).to_dict()

    assert result["error"]["code"] == "STALE_HASH"
    assert result["error"]["retry"] == {"retryable": False}
    assert result["error"]["details"]["current_hash"] == _sha256(current)
    assert path.read_bytes() == current
    assert symbols.calls == 0


def test_concurrent_change_during_symbol_resolution_is_conflict_visible(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    changed = original + b"# external\n"
    path.write_bytes(original)
    symbols = _Symbols(_nested_target_symbols, action=lambda: path.write_bytes(changed))
    notifier = _Notifier()

    result = _editor(path, symbols, notifier).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()

    assert result["error"]["code"] == "STALE_HASH"
    assert result["error"]["details"]["current_hash"] == _sha256(changed)
    assert path.read_bytes() == changed
    assert notifier.calls == []
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


def test_external_deletion_after_authorization_is_not_recreated(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"def target():\n    return 1\n"
    path.write_bytes(original)
    authorizer = _Authorizer(_target(path), action=path.unlink)
    symbols = _Symbols(_unexpected_symbols)

    result = _editor(path, symbols, authorizer=authorizer).replace_symbol_body(
        "target", path.name, "def target():\n    return 2\n", _sha256(original)
    ).to_dict()

    assert result["error"]["code"] == "INVALID_PATH"
    assert not path.exists()
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


def test_missing_and_ambiguous_current_symbols_never_write(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    snapshot = FileSnapshot.from_bytes(original)
    one_range = _lsp_range(snapshot, snapshot.text.index("def target"), len(snapshot.text))
    ambiguous = [
        {"name": "A", "kind": 5, "range": one_range, "children": [{"name": "target", "kind": 6, "range": one_range}]},
        {"name": "B", "kind": 5, "range": one_range, "children": [{"name": "target", "kind": 6, "range": one_range}]},
    ]

    missing = _editor(path, _Symbols(lambda _snapshot: [])).replace_symbol_body(
        "target", path.name, "def target():\n    return 2\n", _sha256(original)
    ).to_dict()
    multiple = _editor(path, _Symbols(lambda _snapshot: ambiguous)).replace_symbol_body(
        "target", path.name, "def target():\n    return 2\n", _sha256(original)
    ).to_dict()

    assert missing["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert multiple["error"]["code"] == "AMBIGUOUS_SYMBOL"
    assert [item["name_path"] for item in multiple["error"]["details"]["candidates"]] == [
        "A/target",
        "B/target",
    ]
    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


@pytest.mark.parametrize("failure_stage", ["write", "flush"])
def test_pre_replace_write_and_flush_failures_preserve_original_and_clean_temp(
    tmp_path: Path,
    failure_stage: str,
) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)

    def fail_write(file_fd: int, content: memoryview) -> int:
        os.write(file_fd, content[:5])
        raise OSError("injected write failure")

    def fail_flush(_file_fd: int) -> None:
        raise OSError("injected flush failure")

    with pytest.raises(OSError, match=failure_stage):
        _editor(
            path,
            _Symbols(_nested_target_symbols),
            write_call=fail_write if failure_stage == "write" else os.write,
            flush_call=fail_flush if failure_stage == "flush" else os.fsync,
        ).replace_symbol_body(
            "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
        )

    assert path.read_bytes() == original
    assert not list(tmp_path.glob(".*.serena-light-*.tmp"))


def test_notification_failure_is_uncertain_and_old_hash_retry_never_replays(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    notifier = _Notifier(failure=ConnectionError("response lost"))
    symbols = _Symbols(_nested_target_symbols)
    editor = _editor(path, symbols, notifier)

    first = editor.replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()
    installed = path.read_bytes()
    second = editor.replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()

    assert first["error"]["code"] == "UNCERTAIN"
    assert first["error"]["retry"] == {"retryable": False}
    assert first["error"]["details"]["current_hash"] == _sha256(installed)
    assert first["error"]["details"]["requires_current_reread"] is True
    assert second["error"]["code"] == "STALE_HASH"
    assert symbols.calls == 1
    assert len(notifier.calls) == 1


def test_post_replace_directory_fsync_failure_is_uncertain_with_the_installed_hash(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    commit = EditCommit()

    def failing_directory_flush(_file_fd: int) -> None:
        raise OSError("directory flush failed")

    symbols = _Symbols(_nested_target_symbols)
    notifier = _Notifier()
    result = _editor(
        path,
        symbols,
        notifier,
        directory_flush_call=failing_directory_flush,
        commit=commit,
    ).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()
    installed = path.read_bytes()

    assert result["error"]["code"] == "UNCERTAIN"
    assert result["error"]["retry"] == {"retryable": False}
    assert result["error"]["details"]["uncertain_stage"] == "directory_fsync"
    assert result["error"]["details"]["current_hash"] == _sha256(installed)
    assert result["error"]["details"]["requires_current_reread"] is True
    # The replacement is installed, so the notifier must never see the change and
    # the commit stays short of done: no caller may replay this edit.
    assert installed != original
    assert notifier.calls == []
    assert commit.state is EditCommitState.INSTALLED


def test_commit_state_reaches_done_only_after_a_notified_success(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    done = EditCommit()
    done.mark_running()
    result = _editor(path, _Symbols(_nested_target_symbols), commit=done).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()

    assert result["ok"] is True
    assert done.state is EditCommitState.DONE

    stale = EditCommit()
    stale_result = _editor(path, _Symbols(_unexpected_symbols), commit=stale).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 3\n", _sha256(original)
    ).to_dict()

    assert stale_result["error"]["code"] == "STALE_HASH"
    assert stale.state is EditCommitState.QUEUED


def test_rooted_target_refuses_a_symlinked_directory_component(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    path = real / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    linked = tmp_path / "linked"
    os.symlink(real, linked)
    target = AuthorizedEdit(
        linked / "sample.py",
        "linked/sample.py",
        WorkspaceMetadata(str(tmp_path), "git", str(tmp_path)),
        tmp_path,
    )

    result = _editor(
        path, _Symbols(_unexpected_symbols), authorizer=_Authorizer(target)
    ).replace_symbol_body(
        "A/target", "linked/sample.py", "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()

    assert result["error"]["code"] == "INVALID_PATH"
    assert path.read_bytes() == original


def test_lost_success_response_and_daemon_restart_require_current_reread(tmp_path: Path) -> None:
    path = tmp_path / "sample.py"
    original = b"class A:\n    def target(self):\n        return 1\n"
    path.write_bytes(original)
    first_symbols = _Symbols(_nested_target_symbols)
    first_notifier = _Notifier()

    # The successful response is intentionally discarded to model transport loss.
    _editor(path, first_symbols, first_notifier).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    )
    installed = path.read_bytes()

    restarted_symbols = _Symbols(_unexpected_symbols)
    restarted_notifier = _Notifier()
    retry = _editor(path, restarted_symbols, restarted_notifier).replace_symbol_body(
        "A/target", path.name, "def target(self):\n        return 2\n", _sha256(original)
    ).to_dict()

    assert retry["error"]["code"] == "STALE_HASH"
    assert retry["error"]["details"]["current_hash"] == _sha256(installed)
    assert restarted_symbols.calls == 0
    assert restarted_notifier.calls == []
    assert len(first_notifier.calls) == 1
