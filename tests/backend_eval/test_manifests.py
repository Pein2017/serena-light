"""Focused, disposable-fixture tests for evaluation corpus manifests."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
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
from scripts.backend_eval.models import PathRecord
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


def _mutate_after_metadata_records(
    monkeypatch: pytest.MonkeyPatch, mutation: Callable[[], None]
) -> Callable[[], bool]:
    """Inject one controlled write after all records exist but before a final Git snapshot."""

    import scripts.backend_eval.manifests as manifests

    original = manifests._scan_remainder
    mutated = False

    def capture_then_mutate(*args: object, **kwargs: object) -> tuple[tuple[PathRecord, ...], tuple[str, ...]]:
        nonlocal mutated
        records = original(*args, **kwargs)  # type: ignore[arg-type]
        mutation()
        mutated = True
        return records

    monkeypatch.setattr(manifests, "_scan_remainder", capture_then_mutate)
    return lambda: mutated


def test_git_manifest_hashes_the_closure_and_metadata_scans_the_whole_remainder(tmp_path: Path) -> None:
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

    request = _git_request(root, required_config_paths=("pyrightconfig.json",))
    manifest = capture_root_manifest(request)

    assert {record.path for record in manifest.hashed_paths} == {"src/a.py", "pyrightconfig.json"}
    # Every remaining in-scope path is captured, including both ignored cache trees.
    assert {record.path for record in manifest.metadata_paths} == {
        ".gitignore",
        "src",
        "model_cache",
        "model_cache/blob.bin",
        "other_cache",
        "other_cache/must-not-be-scanned.bin",
    }
    assert manifest.excluded_paths == (".git",)
    assert manifest.inventory_digest == git_trust_inventory(root).digest
    assert manifest.inventory_count == 1
    assert manifest.inventory_paths == ("src/a.py",)
    assert {record.path: record.disposition for record in manifest.hashed_paths} == {
        "pyrightconfig.json": "untracked",
        "src/a.py": "tracked",
    }
    remainder = {record.path: record.disposition for record in manifest.metadata_paths}
    assert remainder["model_cache/blob.bin"] == "ignored"
    assert remainder[".gitignore"] == "tracked"
    assert remainder["src"] == "tracked"


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
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.bin").write_bytes(b"outside")
    (root / "model_cache").symlink_to(outside, target_is_directory=True)

    manifest = capture_root_manifest(_git_request(root))

    record = next(item for item in manifest.metadata_paths if item.path == "model_cache")
    assert (record.kind, record.symlink_target) == ("symlink", str(outside))
    assert not any(item.path.startswith("model_cache/") for item in manifest.metadata_paths)


def test_metadata_leaf_symlink_is_recorded_without_reading_its_target(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    (root / "README.md").write_text("fixture\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "fixture")
    (root / "model_cache").mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    (root / "model_cache" / "link.bin").symlink_to(outside)

    manifest = capture_root_manifest(_git_request(root))

    record = next(item for item in manifest.metadata_paths if item.path == "model_cache/link.bin")
    assert (record.kind, record.symlink_target, record.content_sha256) == ("symlink", str(outside), None)
    assert any(item.path == "model_cache" and item.kind == "directory" for item in manifest.metadata_paths)


def test_remainder_scan_refuses_a_directory_swapped_to_a_symlink_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory replaced by a symlink between its ``lstat`` and its ``open`` fails closed.

    The scan never follows the substituted link out of the root; it stops instead.
    """

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

    original = manifests._metadata_record
    substituted = False

    def racing_record(relative: str, kind: str, *args: object, **kwargs: object) -> PathRecord:
        nonlocal substituted
        record = original(relative, kind, *args, **kwargs)  # type: ignore[arg-type]
        if relative == "model_cache/nested" and kind == "directory" and not substituted:
            substituted = True
            nested.rename(root / "model_cache" / "nested-old")
            nested.symlink_to(outside, target_is_directory=True)
        return record

    monkeypatch.setattr(manifests, "_metadata_record", racing_record)

    with pytest.raises(ManifestError, match="cannot open corpus directory"):
        capture_root_manifest(_git_request(root))
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


def test_git_manifest_rejects_untracked_source_created_after_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-m", "fixture")
    was_mutated = _mutate_after_metadata_records(
        monkeypatch,
        lambda: (root / "created.py").write_text("created = True\n", encoding="utf-8"),
    )

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root))
    assert was_mutated()


def test_git_manifest_rejects_untracked_source_deleted_after_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    _git(root, "add", "source.py")
    _git(root, "commit", "-m", "fixture")
    untracked = root / "untracked.py"
    untracked.write_text("untracked = True\n", encoding="utf-8")
    was_mutated = _mutate_after_metadata_records(monkeypatch, untracked.unlink)

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root))
    assert was_mutated()


def test_git_manifest_rejects_head_changed_after_records(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _repository(tmp_path)
    (root / "source.py").write_text("source = True\n", encoding="utf-8")
    readme = root / "README.md"
    readme.write_text("before\n", encoding="utf-8")
    _git(root, "add", "source.py", "README.md")
    _git(root, "commit", "-m", "fixture")

    def replace_head() -> None:
        readme.write_text("after\n", encoding="utf-8")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "changed-head")

    was_mutated = _mutate_after_metadata_records(monkeypatch, replace_head)

    with pytest.raises(ManifestError, match="changed while freezing"):
        capture_root_manifest(_git_request(root))
    assert was_mutated()


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
    ms_swift_request = by_root[Path("/data/ms-swift")]
    assert ms_swift_request.required_config_paths == ("setup.cfg",)
    assert (ms_swift_request.root / ms_swift_request.required_config_paths[0]).is_file()
    assert by_root[Path("/data/CoordExp/.worktrees/research-probes")].metadata_roots == ()
    assert by_root[LLM_FRAMEWORK_STUDY_SITE_PACKAGES].fully_hashed_paths == (
        "torchtune/__init__.py",
        "torchtune/config/__init__.py",
        "torchtune/config/_parse.py",
    )
