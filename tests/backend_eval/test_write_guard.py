"""Focused tests for bounded evaluation-root write detection."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.backend_eval.manifests import ManifestError, RootManifestRequest, capture_root_manifest
from scripts.backend_eval.models import RootManifest, WriteDelta
from scripts.backend_eval.write_guard import (
    WriteGuardError,
    assert_no_unexpected_writes,
    compare_root_manifests,
)


@pytest.fixture
def fixture_root(tmp_path: Path) -> Path:
    root = tmp_path / "fixture"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("answer = 1\n", encoding="utf-8")
    (root / "scratch").mkdir()
    (root / "scratch" / "cache.bin").write_bytes(b"cache")
    first_target = root / "first-target"
    first_target.write_text("first\n", encoding="utf-8")
    (root / "scratch" / "link").symlink_to(first_target)
    return root


def _request(root: Path) -> RootManifestRequest:
    return RootManifestRequest(
        root=root,
        kind="non_git",
        fully_hashed_paths=("src/a.py",),
        metadata_roots=("scratch",),
        required_config_paths=(),
    )


def _with_after_content_hash(manifest: RootManifest, relative: str) -> RootManifest:
    records = tuple(
        replace(
            record,
            content_sha256=hashlib.sha256((Path(manifest.root) / relative).read_bytes()).hexdigest(),
        )
        if record.path == relative
        else record
        for record in manifest.metadata_paths
    )
    return replace(manifest, metadata_paths=records)


def _apply_mutation(root: Path, mutation: str) -> RootManifest:
    if mutation == "create":
        (root / "scratch" / "created.py").write_text("created = True\n", encoding="utf-8")
        return _with_after_content_hash(capture_root_manifest(_request(root)), "scratch/created.py")
    if mutation == "change":
        (root / "src" / "a.py").write_text("answer = 2\n", encoding="utf-8")
        return capture_root_manifest(_request(root))
    if mutation == "delete":
        (root / "scratch" / "cache.bin").unlink()
        return capture_root_manifest(_request(root))
    if mutation == "symlink_retarget":
        link = root / "scratch" / "link"
        link.unlink()
        second_target = root / "second-target"
        second_target.write_text("second\n", encoding="utf-8")
        link.symlink_to(second_target)
        return capture_root_manifest(_request(root))
    raise AssertionError(f"unknown mutation: {mutation}")


@pytest.mark.parametrize("mutation", ["create", "change", "delete", "symlink_retarget"])
def test_write_guard_reports_unexpected_mutation(mutation: str, fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    after = _apply_mutation(fixture_root, mutation)

    delta = compare_root_manifests(before, after)

    assert delta.unexpected
    assert delta.before_manifest_digest == before.manifest_digest
    assert delta.after_manifest_digest == after.manifest_digest


def test_declared_disposable_edit_is_not_backend_write(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "src" / "a.py").write_text("answer = 2\n", encoding="utf-8")
    after = capture_root_manifest(_request(fixture_root))

    delta = compare_root_manifests(before, after, declared_mutations=frozenset({"src/a.py"}))

    assert not delta.unexpected
    assert delta.declared == ("src/a.py",)
    assert delta.before_manifest_digest == before.manifest_digest
    assert delta.after_manifest_digest == after.manifest_digest


def test_guard_detects_same_size_mtime_rewrite_from_content_hash(fixture_root: Path) -> None:
    source = fixture_root / "src" / "a.py"
    before = capture_root_manifest(_request(fixture_root))
    before_record = before.hashed_paths[0]
    source.write_text("answer = 9\n", encoding="utf-8")
    os.utime(source, ns=(before_record.mtime_ns, before_record.mtime_ns))
    after = capture_root_manifest(_request(fixture_root))

    delta = compare_root_manifests(before, after)

    assert delta.unexpected == ("src/a.py",)


def test_guard_requires_content_hash_for_changed_metadata_record(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer!")
    after = capture_root_manifest(_request(fixture_root))

    with pytest.raises(WriteGuardError, match="content hash"):
        compare_root_manifests(before, after)


def test_guard_accepts_changed_metadata_record_with_after_content_hash(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer")
    after = _with_after_content_hash(capture_root_manifest(_request(fixture_root)), "scratch/cache.bin")

    assert compare_root_manifests(before, after).unexpected == ("scratch/cache.bin",)


def test_declared_parent_path_does_not_suppress_a_child(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer")
    after = _with_after_content_hash(capture_root_manifest(_request(fixture_root)), "scratch/cache.bin")

    delta = compare_root_manifests(before, after, declared_mutations=frozenset({"scratch"}))

    assert delta.declared == ()
    assert delta.unexpected == ("scratch/cache.bin",)


def test_declared_symlink_target_does_not_suppress_symlink_retarget(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    link = fixture_root / "scratch" / "link"
    link.unlink()
    target = fixture_root / "second-target"
    target.write_text("second\n", encoding="utf-8")
    link.symlink_to(target)
    after = capture_root_manifest(_request(fixture_root))

    delta = compare_root_manifests(before, after, declared_mutations=frozenset({str(target)}))

    assert delta.declared == ()
    assert delta.unexpected == ("scratch/link",)


def test_guard_rejects_root_or_kind_mismatch(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    after = capture_root_manifest(_request(fixture_root))

    with pytest.raises(WriteGuardError, match="root"):
        compare_root_manifests(before, replace(after, root="/different"))
    with pytest.raises(WriteGuardError, match="kind"):
        compare_root_manifests(before, replace(after, kind="git", source_revision="a" * 40))


def test_manifest_capture_rejects_special_file_in_metadata_root(fixture_root: Path) -> None:
    os.mkfifo(fixture_root / "scratch" / "named-pipe")

    with pytest.raises(ManifestError, match="supported regular file or symlink"):
        capture_root_manifest(_request(fixture_root))


def test_guard_canonicalizes_delta_path_order(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "src" / "a.py").write_text("answer = 2\n", encoding="utf-8")
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer")
    after = _with_after_content_hash(capture_root_manifest(_request(fixture_root)), "scratch/cache.bin")

    assert compare_root_manifests(before, after).unexpected == ("scratch/cache.bin", "src/a.py")


def test_assertion_error_is_bounded_and_includes_counts_and_digests() -> None:
    digest = "a" * 64
    delta = WriteDelta(
        root="/fixture",
        kind="non_git",
        before_manifest_digest=digest,
        after_manifest_digest="b" * 64,
        declared=(),
        unexpected=tuple(f"path-{index:03d}" for index in range(51)),
    )

    with pytest.raises(WriteGuardError) as raised:
        assert_no_unexpected_writes((delta,))

    message = str(raised.value)
    assert "unexpected_paths=51" in message
    assert delta.before_manifest_digest in message
    assert delta.after_manifest_digest in message
    assert "path-049" in message
    assert "path-050" not in message
