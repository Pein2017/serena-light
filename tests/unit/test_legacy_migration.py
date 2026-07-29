from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import cast
from uuid import uuid4

import psutil
import pytest

from serena_light.legacy_migration import (
    AuthenticatedLegacyStatus,
    LegacyMigrationDisposition,
    retire_legacy_v1_daemon,
)
from serena_light.runtime_files import DISCOVERY_NAME, LEGACY_BUILD_IDENTITY, DiscoveryMetadata


class FakeProcess:
    def __init__(self, *, pid: int, create_time: float, survives_term: bool = False) -> None:
        self.pid = pid
        self._create_time = create_time
        self._running = True
        self._survives_term = survives_term
        self.calls: list[str] = []

    def is_running(self) -> bool:
        self.calls.append("is_running")
        return self._running

    def status(self) -> str:
        self.calls.append("status")
        return psutil.STATUS_RUNNING

    def create_time(self) -> float:
        self.calls.append("create_time")
        return self._create_time

    def terminate(self) -> None:
        self.calls.append("terminate")
        if not self._survives_term:
            self._running = False

    def kill(self) -> None:
        self.calls.append("kill")
        self._running = False

    def wait(self, timeout: float | None = None) -> int:
        self.calls.append(f"wait:{timeout}")
        if self._running:
            raise psutil.TimeoutExpired(cast(float, timeout), pid=self.pid)
        return 0


def _legacy_root(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "runtime" / "serena-light"
    root.mkdir(parents=True, mode=0o700)
    metadata: dict[str, object] = {
        "schema_version": 1,
        "daemon_id": str(uuid4()),
        "pid": 43123,
        "process_start_time": 42.5,
        "endpoint": "http://127.0.0.1:43123/mcp",
        "protocol_version": "1",
        "server_version": "0.1.0",
        "created_at": 50.0,
    }
    discovery = root / DISCOVERY_NAME
    discovery.write_text(json.dumps(metadata))
    discovery.chmod(0o600)
    return root, metadata


def _status(metadata: dict[str, object], *, active_holders: int | None = 0) -> AuthenticatedLegacyStatus:
    return AuthenticatedLegacyStatus(
        daemon_id=cast(str, metadata["daemon_id"]),
        pid=cast(int, metadata["pid"]),
        process_start_time=cast(float, metadata["process_start_time"]),
        build_identity=LEGACY_BUILD_IDENTITY,
        active_holders=active_holders,
    )


def test_zero_holder_exact_identity_terminates_only_discovered_pid(tmp_path: Path) -> None:
    root, metadata = _legacy_root(tmp_path)
    process = FakeProcess(pid=43123, create_time=42.5)
    fetched: list[tuple[str, int, float, str]] = []
    requested_pids: list[int] = []

    def fetch(discovery: DiscoveryMetadata) -> AuthenticatedLegacyStatus:
        fetched.append(
            (
                discovery.daemon_id,
                discovery.pid,
                discovery.process_start_time,
                discovery.build_identity,
            )
        )
        return _status(metadata)

    def process_factory(pid: int) -> FakeProcess:
        requested_pids.append(pid)
        return process

    result = retire_legacy_v1_daemon(root, fetch_status=fetch, process_factory=process_factory)

    assert result.disposition is LegacyMigrationDisposition.TERMINATED
    assert result.daemon_id == metadata["daemon_id"]
    assert result.pid == 43123
    assert result.used_kill is False
    assert fetched == [(metadata["daemon_id"], 43123, 42.5, LEGACY_BUILD_IDENTITY)]
    assert requested_pids == [43123]
    assert process.calls == ["is_running", "status", "create_time", "terminate", "wait:2.0"]
    assert (root / DISCOVERY_NAME).exists()


def test_transitional_v2_legacy_identity_is_accepted(tmp_path: Path) -> None:
    root, metadata = _legacy_root(tmp_path)
    metadata["schema_version"] = 2
    metadata["build_identity"] = LEGACY_BUILD_IDENTITY
    discovery = root / DISCOVERY_NAME
    discovery.write_text(json.dumps(metadata))
    discovery.chmod(0o600)
    process = FakeProcess(pid=43123, create_time=42.5)

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=lambda _metadata: _status(metadata),
        process_factory=lambda _pid: process,
    )

    assert result.disposition is LegacyMigrationDisposition.TERMINATED
    assert process.calls[-2:] == ["terminate", "wait:2.0"]


def test_transitional_v2_nonlegacy_build_identity_is_rejected(tmp_path: Path) -> None:
    root, metadata = _legacy_root(tmp_path)
    metadata["schema_version"] = 2
    metadata["build_identity"] = "a" * 64
    discovery = root / DISCOVERY_NAME
    discovery.write_text(json.dumps(metadata))
    discovery.chmod(0o600)
    calls: list[str] = []

    def forbidden_status(_metadata: DiscoveryMetadata) -> AuthenticatedLegacyStatus:
        calls.append("status")
        raise AssertionError("must not fetch status")

    result = retire_legacy_v1_daemon(root, fetch_status=forbidden_status)

    assert result.disposition is LegacyMigrationDisposition.DISCOVERY_UNTRUSTED
    assert calls == []


def test_bounded_term_uses_exact_identity_checked_kill_fallback(tmp_path: Path) -> None:
    root, metadata = _legacy_root(tmp_path)
    process = FakeProcess(pid=43123, create_time=42.5, survives_term=True)

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=lambda _metadata: _status(metadata),
        process_factory=lambda _pid: process,
        terminate_timeout=0.25,
        kill_timeout=0.5,
    )

    assert result.disposition is LegacyMigrationDisposition.TERMINATED
    assert result.used_kill is True
    assert process.calls == [
        "is_running",
        "status",
        "create_time",
        "terminate",
        "wait:0.25",
        "is_running",
        "status",
        "create_time",
        "kill",
        "wait:0.5",
    ]


@pytest.mark.parametrize(
    ("status_change", "expected"),
    [
        ({"active_holders": 2}, LegacyMigrationDisposition.ACTIVE_HOLDERS),
        ({"active_holders": None}, LegacyMigrationDisposition.HOLDERS_UNKNOWN),
        ({"daemon_id": str(uuid4())}, LegacyMigrationDisposition.STATUS_MISMATCH),
        ({"pid": 99999}, LegacyMigrationDisposition.STATUS_MISMATCH),
        ({"process_start_time": 99.0}, LegacyMigrationDisposition.STATUS_MISMATCH),
        ({"build_identity": "a" * 64}, LegacyMigrationDisposition.STATUS_MISMATCH),
    ],
)
def test_nonzero_unknown_and_mismatched_status_leave_process_untouched(
    tmp_path: Path,
    status_change: dict[str, object],
    expected: LegacyMigrationDisposition,
) -> None:
    root, metadata = _legacy_root(tmp_path)
    process = FakeProcess(pid=43123, create_time=42.5)
    status = replace(_status(metadata), **status_change)

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=lambda _metadata: status,
        process_factory=lambda _pid: process,
    )

    assert result.disposition is expected
    assert process.calls == []


def test_pid_reuse_create_time_mismatch_is_revalidated_before_signal(tmp_path: Path) -> None:
    root, metadata = _legacy_root(tmp_path)
    reused = FakeProcess(pid=43123, create_time=99.0)

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=lambda _metadata: _status(metadata),
        process_factory=lambda _pid: reused,
    )

    assert result.disposition is LegacyMigrationDisposition.PROCESS_IDENTITY_MISMATCH
    assert "terminate" not in reused.calls
    assert "kill" not in reused.calls


@pytest.mark.parametrize("discovery_content", [None, "not-json", "{}"])
def test_absent_or_malformed_discovery_is_typed_and_untouched(
    tmp_path: Path, discovery_content: str | None
) -> None:
    root, _metadata = _legacy_root(tmp_path)
    discovery = root / DISCOVERY_NAME
    if discovery_content is None:
        discovery.unlink()
    else:
        discovery.write_text(discovery_content)
        discovery.chmod(0o600)
    calls: list[str] = []

    def forbidden_status(_metadata: DiscoveryMetadata) -> AuthenticatedLegacyStatus:
        calls.append("status")
        raise AssertionError("must not fetch status")

    def forbidden_process(_pid: int) -> FakeProcess:
        calls.append("process")
        raise AssertionError("must not inspect a process")

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=forbidden_status,
        process_factory=forbidden_process,
    )

    assert result.disposition is LegacyMigrationDisposition.DISCOVERY_UNTRUSTED
    assert calls == []


def test_migration_never_enumerates_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, metadata = _legacy_root(tmp_path)
    process = FakeProcess(pid=43123, create_time=42.5)

    def forbid_enumeration(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("broad process enumeration is forbidden")

    monkeypatch.setattr(psutil, "process_iter", forbid_enumeration)

    def exact_process(pid: int) -> FakeProcess:
        if pid != 43123:
            raise AssertionError("unexpected pid")
        return process

    result = retire_legacy_v1_daemon(
        root,
        fetch_status=lambda _metadata: _status(metadata),
        process_factory=exact_process,
    )

    assert result.disposition is LegacyMigrationDisposition.TERMINATED
