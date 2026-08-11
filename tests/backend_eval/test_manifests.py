"""Focused, disposable-fixture tests for evaluation corpus manifests."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import serena_light.workspace.inventory as inventory_module
from scripts.backend_eval.manifests import (
    LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
    MS_TRANSFORMERS_ROOT,
    ManifestError,
    RootManifestRequest,
    capture_root_manifest,
    default_corpus_requests,
)
from serena_light.workspace.inventory import git_trust_inventory


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "user.name", "Test User")
    return root


def _git_request(root: Path, **overrides: object) -> RootManifestRequest:
    fields: dict[str, object] = {
        "root": root,
        "kind": "git",
        "fully_hashed_paths": (),
        "metadata_roots": (),
        "required_config_paths": (),
    }
    fields.update(overrides)
    return RootManifestRequest(**fields)


def _non_git_request(root: Path, **overrides: object) -> RootManifestRequest:
    fields: dict[str, object] = {
        "root": root,
        "kind": "non_git",
        "fully_hashed_paths": (),
        "metadata_roots": (),
        "required_config_paths": (),
    }
    fields.update(overrides)
    return RootManifestRequest(**fields)


def test_git_manifest_hashes_trust_inventory_but_only_stats_declared_ignored_root(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / ".gitignore").write_text("model_cache/\nother_cache/\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("answer = 42\n", encoding="utf-8")
    (root / "pyrightconfig.json").write_text('{"include": ["src"]}\n', encoding="utf-8")
    (root / "model_cache").mkdir()
    (root / "model_cache" / "blob.bin").write_bytes(b"cache")
    (root / "other_cache").mkdir()
    (root / "other_cache" / "must-not-be-scanned.bin").write_bytes(b"other")
    _git(root, "add", ".gitignore", "src/a.py")
    _git(root, "commit", "-m", "fixture")

    request = _git_request(
        root,
        metadata_roots=("model_cache",),
        required_config_paths=("pyrightconfig.json",),
    )
    manifest = capture_root_manifest(request)

    assert {record.path for record in manifest.hashed_paths} == {"src/a.py", "pyrightconfig.json"}
    assert {record.path for record in manifest.metadata_paths} == {"model_cache/blob.bin"}
    assert manifest.inventory_digest == git_trust_inventory(root).digest
    assert manifest.inventory_count == 1
    assert {record.path: record.disposition for record in manifest.hashed_paths} == {
        "pyrightconfig.json": "untracked",
        "src/a.py": "tracked",
    }
    assert manifest.metadata_paths[0].disposition == "ignored"


def test_git_manifest_records_head_and_untracked_inventory_disposition(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "tracked.py").write_text("tracked = True\n", encoding="utf-8")
    _git(root, "add", "src/tracked.py")
    _git(root, "commit", "-m", "fixture")
    (root / "src" / "untracked.py").write_text("untracked = True\n", encoding="utf-8")

    manifest = capture_root_manifest(_git_request(root))

    assert manifest.source_revision == _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    assert {record.path: record.disposition for record in manifest.hashed_paths} == {
        "src/tracked.py": "tracked",
        "src/untracked.py": "untracked",
    }


def test_manifest_is_byte_stable_and_lexically_ordered(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "z.py").write_text("z = 1\n", encoding="utf-8")
    (root / "a.py").write_text("a = 1\n", encoding="utf-8")
    (root / "pyrightconfig.json").write_text("{}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "fixture")
    request = _git_request(root, required_config_paths=("pyrightconfig.json",))

    first = capture_root_manifest(request)
    second = capture_root_manifest(request)

    assert first == second
    assert first.manifest_digest == second.manifest_digest
    assert tuple(record.path for record in first.hashed_paths) == ("a.py", "pyrightconfig.json", "z.py")


def test_manifest_does_not_follow_symlinked_directory(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"outside")
    (root / "model_cache").symlink_to(outside, target_is_directory=True)

    request_with_symlinked_metadata_root = _git_request(root, metadata_roots=("model_cache",))

    with pytest.raises(ManifestError, match="symlink"):
        capture_root_manifest(request_with_symlinked_metadata_root)


def test_metadata_leaf_symlink_is_recorded_without_reading_its_target(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    (root / "model_cache").mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "model_cache" / "link.bin").symlink_to(outside)

    manifest = capture_root_manifest(_git_request(root, metadata_roots=("model_cache",)))

    assert len(manifest.metadata_paths) == 1
    record = manifest.metadata_paths[0]
    assert (record.path, record.kind, record.symlink_target, record.content_sha256) == (
        "model_cache/link.bin",
        "symlink",
        str(outside),
        None,
    )


def test_metadata_scan_refuses_nested_directory_swapped_to_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    nested = root / "model_cache" / "nested"
    nested.mkdir(parents=True)
    (nested / "inside.bin").write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "must-not-be-enumerated.bin").write_bytes(b"outside")

    import scripts.backend_eval.manifests as manifests

    real_lstat = manifests._lstat
    real_os_lstat = manifests.os.lstat
    substituted = False

    def substitute() -> None:
        nonlocal substituted
        if not substituted:
            substituted = True
            nested.rename(root / "model_cache" / "nested-old")
            nested.symlink_to(outside, target_is_directory=True)

    def racing_lstat(path: Path, relative: str) -> os.stat_result:
        observed = real_lstat(path, relative)
        if relative == "model_cache/nested":
            substitute()
        return observed

    def racing_os_lstat(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes], *, dir_fd: int | None = None
    ) -> os.stat_result:
        observed = real_os_lstat(path, dir_fd=dir_fd)
        if path == "nested" and dir_fd is not None:
            substitute()
        return observed

    monkeypatch.setattr(manifests, "_lstat", racing_lstat)
    monkeypatch.setattr(manifests.os, "lstat", racing_os_lstat)

    with pytest.raises(ManifestError, match="symlink"):
        capture_root_manifest(_git_request(root, metadata_roots=("model_cache",)))
    assert substituted


def test_manifest_rejects_mid_freeze_same_size_rewrite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repository(tmp_path)
    source = root / "module.py"
    source.write_bytes(b"x" * (2 * 1024 * 1024))
    _git(root, "add", "module.py")
    _git(root, "commit", "-m", "fixture")
    real_read = inventory_module.os.read
    rewritten = False

    def racing_read(file_descriptor: int, size: int) -> bytes:
        nonlocal rewritten
        chunk = real_read(file_descriptor, size)
        if chunk and not rewritten and os.fstat(file_descriptor).st_ino == source.stat().st_ino:
            rewritten = True
            with source.open("r+b") as stream:
                stream.write(b"y" * (1024 * 1024))
                stream.flush()
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr(inventory_module.os, "read", racing_read)

    with pytest.raises(ManifestError, match="unstable"):
        capture_root_manifest(_git_request(root))
    assert rewritten


def test_non_git_manifest_hashes_only_declared_task_paths_without_inventory_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "site-packages"
    selected = root / "torchtune" / "__init__.py"
    selected.parent.mkdir(parents=True)
    selected.write_text("selected = True\n", encoding="utf-8")
    unselected = root / "other-package" / "large.py"
    unselected.parent.mkdir()
    unselected.write_text("must_not_be_read = True\n", encoding="utf-8")

    import scripts.backend_eval.manifests as manifests

    def forbidden_inventory(_root: Path) -> object:
        raise AssertionError("exact non-Git task paths must not walk the full environment")

    monkeypatch.setattr(manifests, "bounded_non_git_trust_inventory", forbidden_inventory)
    manifest = capture_root_manifest(_non_git_request(root, fully_hashed_paths=("torchtune/__init__.py",)))

    assert manifest.kind == "non_git"
    assert manifest.source_revision is None
    assert manifest.inventory_count == 1
    assert tuple(record.path for record in manifest.hashed_paths) == ("torchtune/__init__.py",)
    assert manifest.hashed_paths[0].disposition == "declared"


def test_manifest_rejects_missing_roots_special_files_duplicate_paths_and_traversal(tmp_path: Path) -> None:
    missing = _non_git_request(tmp_path / "missing", fully_hashed_paths=("needed.py",))
    with pytest.raises(ManifestError, match="missing"):
        capture_root_manifest(missing)

    root = _repository(tmp_path)
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
    fifo = root / "pyrightconfig.json"
    os.mkfifo(fifo)
    _git(root, "add", "src/a.py")
    _git(root, "commit", "-m", "fixture")

    with pytest.raises(ManifestError, match="regular"):
        capture_root_manifest(_git_request(root, required_config_paths=("pyrightconfig.json",)))
    with pytest.raises(ManifestError, match="duplicate"):
        capture_root_manifest(_git_request(root, fully_hashed_paths=("src/a.py", "src/a.py")))
    with pytest.raises(ManifestError, match="traversal"):
        capture_root_manifest(_git_request(root, fully_hashed_paths=("../outside.py",)))


def test_default_corpus_requests_are_fixed_and_do_not_capture_live_roots() -> None:
    requests = default_corpus_requests()
    by_root = {request.root: request for request in requests}

    assert tuple(request.root for request in requests) == tuple(sorted(by_root))
    assert set(by_root) == {
        Path("/data/CoordExp/serena-light"),
        Path("/data/CoordExp/.worktrees/research-probes"),
        Path("/data/ms-swift"),
        MS_TRANSFORMERS_ROOT,
        LLM_FRAMEWORK_STUDY_SITE_PACKAGES,
    }
    assert by_root[Path("/data/CoordExp/.worktrees/research-probes")].metadata_roots == ("model_cache",)
    assert by_root[LLM_FRAMEWORK_STUDY_SITE_PACKAGES].fully_hashed_paths == (
        "torchtune/__init__.py",
        "torchtune/config/__init__.py",
        "torchtune/config/_parse.py",
    )
