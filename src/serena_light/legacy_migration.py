"""Fail-closed retirement of the explicit pre-build-slot Serena Light daemon."""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import psutil

from serena_light.runtime_files import (
    DISCOVERY_NAME,
    LEGACY_BUILD_IDENTITY,
    DiscoveryMetadata,
    RuntimeFileError,
    _read_private_file,
)

LEGACY_DISCOVERY_SCHEMA_VERSION = 1
TRANSITIONAL_DISCOVERY_SCHEMA_VERSION = 2


class LegacyMigrationDisposition(StrEnum):
    """Typed result; every value except ``TERMINATED`` means no further signal was sent."""

    TERMINATED = "terminated"
    DISCOVERY_UNTRUSTED = "discovery_untrusted"
    STATUS_UNAVAILABLE = "status_unavailable"
    STATUS_INVALID = "status_invalid"
    STATUS_MISMATCH = "status_mismatch"
    HOLDERS_UNKNOWN = "holders_unknown"
    ACTIVE_HOLDERS = "active_holders"
    PROCESS_IDENTITY_MISMATCH = "process_identity_mismatch"
    TERMINATION_FAILED = "termination_failed"


@dataclass(frozen=True, slots=True)
class AuthenticatedLegacyStatus:
    """Identity and holder projection returned by a caller-authenticated status request."""

    daemon_id: str
    pid: int
    process_start_time: float
    build_identity: str
    active_holders: int | None


@dataclass(frozen=True, slots=True)
class LegacyMigrationResult:
    disposition: LegacyMigrationDisposition
    daemon_id: str | None = None
    pid: int | None = None
    used_kill: bool = False

    @property
    def terminated(self) -> bool:
        return self.disposition is LegacyMigrationDisposition.TERMINATED


class LegacyProcessHandle(Protocol):
    def is_running(self) -> bool: ...

    def status(self) -> str: ...

    def create_time(self) -> float: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...


type AuthenticatedStatusFetcher = Callable[[DiscoveryMetadata], AuthenticatedLegacyStatus]
type ProcessFactory = Callable[[int], LegacyProcessHandle]


def retire_legacy_v1_daemon(
    legacy_runtime_root: Path,
    *,
    fetch_status: AuthenticatedStatusFetcher,
    process_factory: ProcessFactory = psutil.Process,
    terminate_timeout: float = 2.0,
    kill_timeout: float = 2.0,
    expected_uid: int | None = None,
) -> LegacyMigrationResult:
    """Retire only the daemon proven idle by one explicit Serena Light legacy root.

    ``fetch_status`` owns bearer-authenticated transport.  This primitive never
    searches process names or enumerates processes and never removes runtime
    artifacts; cleanup remains owned by the daemon.
    """

    if not _valid_timeout(terminate_timeout) or not _valid_timeout(kill_timeout):
        return LegacyMigrationResult(LegacyMigrationDisposition.TERMINATION_FAILED)
    try:
        metadata = _read_legacy_discovery(legacy_runtime_root, expected_uid=expected_uid)
    except (OSError, RuntimeFileError, TypeError, ValueError):
        return LegacyMigrationResult(LegacyMigrationDisposition.DISCOVERY_UNTRUSTED)
    def outcome(
        disposition: LegacyMigrationDisposition,
        *,
        used_kill: bool = False,
    ) -> LegacyMigrationResult:
        return LegacyMigrationResult(
            disposition,
            daemon_id=metadata.daemon_id,
            pid=metadata.pid,
            used_kill=used_kill,
        )

    try:
        status = fetch_status(metadata)
    except Exception:
        return outcome(LegacyMigrationDisposition.STATUS_UNAVAILABLE)
    if not _valid_status(status):
        return outcome(LegacyMigrationDisposition.STATUS_INVALID)
    if not _status_matches(metadata, status):
        return outcome(LegacyMigrationDisposition.STATUS_MISMATCH)
    if status.active_holders is None:
        return outcome(LegacyMigrationDisposition.HOLDERS_UNKNOWN)
    if status.active_holders != 0:
        return outcome(LegacyMigrationDisposition.ACTIVE_HOLDERS)
    try:
        process = process_factory(metadata.pid)
    except (psutil.Error, OSError, ValueError):
        return outcome(LegacyMigrationDisposition.PROCESS_IDENTITY_MISMATCH)
    if not _process_identity_matches(process, metadata.process_start_time):
        return outcome(LegacyMigrationDisposition.PROCESS_IDENTITY_MISMATCH)
    try:
        process.terminate()
        process.wait(timeout=terminate_timeout)
        return outcome(LegacyMigrationDisposition.TERMINATED)
    except psutil.NoSuchProcess:
        return outcome(LegacyMigrationDisposition.TERMINATED)
    except (psutil.TimeoutExpired, TimeoutError):
        pass
    except (psutil.Error, OSError, ValueError):
        return outcome(LegacyMigrationDisposition.TERMINATION_FAILED)

    # Revalidate the same process handle immediately before the fallback signal.
    if not _process_identity_matches(process, metadata.process_start_time):
        return outcome(LegacyMigrationDisposition.PROCESS_IDENTITY_MISMATCH)
    try:
        process.kill()
        process.wait(timeout=kill_timeout)
    except psutil.NoSuchProcess:
        pass
    except (psutil.Error, OSError, TimeoutError, ValueError):
        return outcome(LegacyMigrationDisposition.TERMINATION_FAILED, used_kill=True)
    return outcome(LegacyMigrationDisposition.TERMINATED, used_kill=True)


def _read_legacy_discovery(root: Path, *, expected_uid: int | None) -> DiscoveryMetadata:
    raw = _read_private_file(root, DISCOVERY_NAME, expected_uid=expected_uid)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeFileError("legacy discovery metadata is malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise RuntimeFileError("legacy discovery metadata must be an object")
    common_fields = {
        "schema_version",
        "daemon_id",
        "pid",
        "process_start_time",
        "endpoint",
        "protocol_version",
        "server_version",
        "created_at",
    }
    fields = set(payload)
    schema_version = payload.get("schema_version")
    original_v1 = schema_version == LEGACY_DISCOVERY_SCHEMA_VERSION and fields == common_fields
    transitional_v2 = (
        schema_version == TRANSITIONAL_DISCOVERY_SCHEMA_VERSION
        and fields == common_fields | {"build_identity"}
        and payload.get("build_identity") == LEGACY_BUILD_IDENTITY
    )
    if not (original_v1 or transitional_v2):
        raise RuntimeFileError("legacy discovery metadata has an invalid schema")
    return DiscoveryMetadata.create(
        daemon_id=payload["daemon_id"],
        pid=payload["pid"],
        process_start_time=payload["process_start_time"],
        endpoint=payload["endpoint"],
        protocol_version=payload["protocol_version"],
        server_version=payload["server_version"],
        created_at=payload["created_at"],
        build_identity=LEGACY_BUILD_IDENTITY,
    )


def _valid_status(status: object) -> bool:
    if not isinstance(status, AuthenticatedLegacyStatus):
        return False
    if not isinstance(status.daemon_id, str) or not isinstance(status.build_identity, str):
        return False
    if isinstance(status.pid, bool) or not isinstance(status.pid, int) or status.pid <= 0:
        return False
    if (
        isinstance(status.process_start_time, bool)
        or not isinstance(status.process_start_time, int | float)
        or not math.isfinite(status.process_start_time)
        or status.process_start_time <= 0
    ):
        return False
    return status.active_holders is None or (
        not isinstance(status.active_holders, bool)
        and isinstance(status.active_holders, int)
        and status.active_holders >= 0
    )


def _status_matches(metadata: DiscoveryMetadata, status: AuthenticatedLegacyStatus) -> bool:
    return (
        status.daemon_id == metadata.daemon_id
        and status.pid == metadata.pid
        and float(status.process_start_time) == metadata.process_start_time
        and status.build_identity == metadata.build_identity
    )


def _process_identity_matches(process: LegacyProcessHandle, expected_create_time: float) -> bool:
    try:
        return (
            process.is_running()
            and process.status() != psutil.STATUS_ZOMBIE
            and process.create_time() == float(expected_create_time)
        )
    except (psutil.Error, OSError, ValueError):
        return False


def _valid_timeout(value: float) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value) and value > 0
