from __future__ import annotations

import io
import threading
from pathlib import Path

import pytest

from serena_light.debug_logging import DEBUG_LOG_NAME, MAX_MESSAGE_CHARS, DebugLogger
from serena_light.runtime_files import prepare_runtime_directory


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "runtime" / "serena-light"
    root.parent.mkdir(parents=True, exist_ok=True)
    return prepare_runtime_directory(root)


def test_reports_to_stderr_and_redacts_and_bounds_messages(tmp_path: Path) -> None:
    stream = io.StringIO()
    logger = DebugLogger(_root(tmp_path), stderr=stream)
    secret = "Bearer abc.def-123 authorization=other cookie:crumb password=hunter2 secret=hide token=gone"

    assert logger.report(" daemon token=event-secret started ", secret + " x" * 1_000)
    rendered = stream.getvalue()
    stored = (_root(tmp_path) / DEBUG_LOG_NAME).read_text()

    assert rendered == stored
    assert "<redacted>" in rendered
    for value in ("abc.def-123", "other", "crumb", "hunter2", "hide", "gone"):
        assert value not in rendered
    assert "event-secret" not in rendered
    assert len(rendered.rstrip("\n")) <= len("serena-light daemon token=<redacted> started: ") + MAX_MESSAGE_CHARS


def test_rotates_deterministically_with_private_modes(tmp_path: Path) -> None:
    root = _root(tmp_path)
    logger = DebugLogger(root, max_bytes=128, backup_count=2, stderr=io.StringIO())
    for message in ("a" * 90, "b" * 90, "c" * 90, "d" * 90):
        assert logger.report("event", message)

    assert (root / DEBUG_LOG_NAME).read_text().endswith("d" * 90 + "\n")
    assert (root / f"{DEBUG_LOG_NAME}.1").read_text().endswith("c" * 90 + "\n")
    assert (root / f"{DEBUG_LOG_NAME}.2").read_text().endswith("b" * 90 + "\n")
    assert not (root / f"{DEBUG_LOG_NAME}.3").exists()
    for path in root.glob("debug.log*"):
        assert path.stat().st_mode & 0o777 == 0o600


def test_fails_closed_for_symlinked_log_or_backup(tmp_path: Path) -> None:
    root = _root(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    (root / DEBUG_LOG_NAME).symlink_to(outside)
    assert not DebugLogger(root, stderr=io.StringIO()).report("event", "safe")
    assert outside.read_text() == "untouched"

    (root / DEBUG_LOG_NAME).unlink()
    logger = DebugLogger(root, max_bytes=128, stderr=io.StringIO())
    assert logger.report("event", "a" * 90)
    (root / f"{DEBUG_LOG_NAME}.1").symlink_to(outside)
    assert not logger.report("event", "b" * 90)
    assert outside.read_text() == "untouched"


def test_rejects_non_string_payloads_and_unsafe_limits(tmp_path: Path) -> None:
    logger = DebugLogger(_root(tmp_path), stderr=io.StringIO())
    with pytest.raises(TypeError):
        logger.report("event", {"tool_arguments": "must not serialize"})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DebugLogger(_root(tmp_path), max_bytes=127)


def test_concurrent_reports_are_complete_lines(tmp_path: Path) -> None:
    root = _root(tmp_path)
    logger = DebugLogger(root, max_bytes=32_768, stderr=io.StringIO())
    workers = 12
    barrier = threading.Barrier(workers)

    def report(index: int) -> None:
        barrier.wait()
        assert logger.report("worker", f"message-{index}")

    threads = [threading.Thread(target=report, args=(index,)) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = (root / DEBUG_LOG_NAME).read_text().splitlines()
    assert len(lines) == workers
    assert {line.rsplit("-", 1)[-1] for line in lines} == {str(index) for index in range(workers)}
