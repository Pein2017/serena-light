from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    DiscoveryMetadata,
    RuntimeFileError,
    create_bearer_secret,
    prepare_runtime_directory,
    read_bearer_secret,
    read_discovery_metadata,
    write_discovery_metadata,
)


def _root(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "serena-light"


def _prepare_parent(root: Path) -> None:
    root.parent.mkdir(parents=True)


def _metadata() -> DiscoveryMetadata:
    return DiscoveryMetadata.create(
        daemon_id=str(uuid4()),
        pid=1234,
        process_start_time=42.5,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="1",
        server_version="0.1.0",
        created_at=50.0,
    )


def test_prepare_runtime_directory_is_exact_mode_and_rejects_symlinks(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)

    assert prepare_runtime_directory(root) == root
    assert root.stat().st_mode & 0o777 == 0o700

    root.chmod(0o755)
    assert prepare_runtime_directory(root) == root
    assert root.stat().st_mode & 0o777 == 0o700

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeFileError, match="symlinked"):
        prepare_runtime_directory(linked / "state")


def test_private_writes_are_atomic_mode_0600_and_secret_is_redacted(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)

    secret = create_bearer_secret(root)
    write_discovery_metadata(root, _metadata())

    assert (root / BEARER_NAME).stat().st_mode & 0o777 == 0o600
    assert (root / DISCOVERY_NAME).stat().st_mode & 0o777 == 0o600
    assert repr(secret) == "BearerSecret(<redacted>)"
    assert secret.value not in repr(secret)
    assert read_bearer_secret(root).value == secret.value
    assert not tuple(root.glob("*.tmp"))


def test_discovery_reader_rejects_malformed_stale_and_non_loopback_metadata(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)
    path = root / DISCOVERY_NAME
    path.write_text("not json")
    path.chmod(0o600)
    with pytest.raises(RuntimeFileError, match="malformed JSON"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: True)

    write_discovery_metadata(root, _metadata())
    with pytest.raises(RuntimeFileError, match="stale daemon"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: False)

    payload = json.loads(path.read_text())
    payload["endpoint"] = "http://0.0.0.0:43123/mcp"
    path.write_text(json.dumps(payload))
    path.chmod(0o600)
    with pytest.raises(RuntimeFileError, match="loopback-only"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: True)


def test_reader_rejects_over_permissive_wrong_owner_and_symlinked_artifacts(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)
    write_discovery_metadata(root, _metadata())
    path = root / DISCOVERY_NAME

    path.chmod(0o644)
    with pytest.raises(RuntimeFileError, match="0600"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: True)
    path.chmod(0o600)

    os.chown(path, os.getuid() + 1, -1)
    with pytest.raises(RuntimeFileError, match="wrong owner"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: True)

    path.unlink()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(RuntimeFileError, match="symlinked"):
        read_discovery_metadata(root, is_process_identity_live=lambda _pid, _birth: True)


def test_discovery_round_trip_validates_exact_process_identity(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)
    metadata = _metadata()
    write_discovery_metadata(root, metadata)
    seen: list[tuple[int, float]] = []

    def live(pid: int, process_start_time: float) -> bool:
        seen.append((pid, process_start_time))
        return True

    assert read_discovery_metadata(root, is_process_identity_live=live) == metadata
    assert seen == [(metadata.pid, metadata.process_start_time)]
