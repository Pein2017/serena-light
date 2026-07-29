from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from serena_light.runtime_files import (
    BEARER_NAME,
    DISCOVERY_NAME,
    SERVICE_GIT_CONFIG_NAME,
    STARTUP_NONCE_NAME,
    DiscoveryMetadata,
    RuntimeFileError,
    consume_startup_nonce,
    create_bearer_secret,
    create_startup_nonce,
    prepare_runtime_directory,
    prepare_runtime_layout,
    read_bearer_secret,
    read_discovery_metadata,
    write_discovery_metadata,
    write_service_git_config,
)


def _root(tmp_path: Path) -> Path:
    return tmp_path / "runtime" / "serena-light"


def _prepare_parent(root: Path) -> None:
    root.parent.mkdir(parents=True)


def _metadata(*, build_identity: str = "0" * 64) -> DiscoveryMetadata:
    return DiscoveryMetadata.create(
        daemon_id=str(uuid4()),
        pid=1234,
        process_start_time=42.5,
        endpoint="http://127.0.0.1:43123/mcp",
        protocol_version="1",
        server_version="0.1.0",
        created_at=50.0,
        build_identity=build_identity,
    )


def test_service_git_config_is_private_and_delegates_scope_to_workspace_policy(tmp_path: Path) -> None:
    home_root = _root(tmp_path) / "home"
    _prepare_parent(_root(tmp_path))
    prepare_runtime_directory(_root(tmp_path))

    config_path = write_service_git_config(home_root)

    assert config_path.name == SERVICE_GIT_CONFIG_NAME
    assert config_path.read_text() == "[safe]\n\tdirectory = *\n"
    assert config_path.stat().st_mode & 0o777 == 0o600


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


def test_runtime_layout_is_build_scoped_and_rejects_symlinked_children(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    identity = "a" * 64

    layout = prepare_runtime_layout(root, identity)

    assert layout.python_root == root / "python"
    assert layout.deps_root == root / "deps"
    assert layout.build_root == root / "builds" / identity
    assert layout.logs_root == layout.build_root / "logs"
    assert layout.home_root == root / "home"
    directories = (
        root,
        layout.python_root,
        layout.deps_root,
        layout.builds_root,
        layout.home_root,
        layout.build_root,
        layout.logs_root,
    )
    assert all(path.stat().st_mode & 0o777 == 0o700 for path in directories)

    broken_root = tmp_path / "broken" / "serena-light"
    broken_root.parent.mkdir(parents=True)
    prepare_runtime_directory(broken_root)
    outside = tmp_path / "outside-builds"
    outside.mkdir()
    (broken_root / "builds").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeFileError, match="symlinked"):
        prepare_runtime_layout(broken_root, identity)


def test_two_build_slots_keep_discovery_and_bearer_ownership_independent(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    first = prepare_runtime_layout(root, "a" * 64)
    second = prepare_runtime_layout(root, "b" * 64)

    first_secret = create_bearer_secret(first.build_root)
    second_secret = create_bearer_secret(second.build_root)
    write_discovery_metadata(first.build_root, _metadata(build_identity="a" * 64))
    write_discovery_metadata(second.build_root, _metadata(build_identity="b" * 64))

    assert first.build_root != second.build_root
    assert first_secret.value != second_secret.value
    assert read_discovery_metadata(
        first.build_root,
        is_process_identity_live=lambda _pid, _created: True,
    ).build_identity == "a" * 64
    assert read_discovery_metadata(
        second.build_root,
        is_process_identity_live=lambda _pid, _created: True,
    ).build_identity == "b" * 64


def test_startup_nonce_is_private_redacted_and_consumed_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)

    nonce = create_startup_nonce(root)

    assert (root / STARTUP_NONCE_NAME).stat().st_mode & 0o777 == 0o600
    assert nonce.value not in repr(nonce)
    consume_startup_nonce(root, nonce)
    assert not (root / STARTUP_NONCE_NAME).exists()
    with pytest.raises(RuntimeFileError, match="unavailable"):
        consume_startup_nonce(root, nonce)


def test_startup_nonce_mismatch_is_not_consumed(tmp_path: Path) -> None:
    root = _root(tmp_path)
    _prepare_parent(root)
    prepare_runtime_directory(root)
    nonce = create_startup_nonce(root)
    other = create_startup_nonce(root)

    with pytest.raises(RuntimeFileError, match="does not match"):
        consume_startup_nonce(root, nonce)
    assert (root / STARTUP_NONCE_NAME).exists()
    consume_startup_nonce(root, other)
