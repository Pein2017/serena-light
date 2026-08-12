"""Focused tests for bounded evaluation-root write detection."""

from __future__ import annotations

import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.backend_eval.manifests import RootManifestRequest, capture_root_manifest
from scripts.backend_eval.models import RootManifest, WriteDelta
from scripts.backend_eval.process import Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.write_guard import (
    WriteGuardError,
    assert_no_unexpected_writes,
    compare_root_manifests,
    enrich_after_manifest,
)
from serena_light.workspace.inventory import observe_file_digest


class _FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def _after(before: RootManifest, root: Path) -> RootManifest:
    """The second capture, enriched exactly as the admission gate enriches it."""

    return enrich_after_manifest(before, capture_root_manifest(_request(root)))


def _apply_mutation(before: RootManifest, root: Path, mutation: str) -> RootManifest:
    if mutation == "create":
        (root / "scratch" / "created.py").write_text("created = True\n", encoding="utf-8")
    elif mutation == "change":
        (root / "src" / "a.py").write_text("answer = 2\n", encoding="utf-8")
    elif mutation == "delete":
        (root / "scratch" / "cache.bin").unlink()
    elif mutation == "symlink_retarget":
        link = root / "scratch" / "link"
        link.unlink()
        second_target = root / "second-target"
        second_target.write_text("second\n", encoding="utf-8")
        link.symlink_to(second_target)
    else:
        raise AssertionError(f"unknown mutation: {mutation}")
    return _after(before, root)


@pytest.mark.parametrize("mutation", ["create", "change", "delete", "symlink_retarget"])
def test_write_guard_reports_unexpected_mutation(mutation: str, fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    after = _apply_mutation(before, fixture_root, mutation)

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
    """An unenriched second capture is an incomplete observation, never a clean one."""

    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer!")
    after = capture_root_manifest(_request(fixture_root))

    with pytest.raises(WriteGuardError, match="content hash"):
        compare_root_manifests(before, after)


def test_guard_accepts_changed_metadata_record_with_after_content_hash(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer and longer")
    after = _after(before, fixture_root)

    record = next(item for item in after.metadata_paths if item.path == "scratch/cache.bin")
    assert record.content_sha256 is not None
    assert compare_root_manifests(before, after).unexpected == ("scratch/cache.bin",)


def test_remainder_metadata_binds_only_what_metadata_can_see(fixture_root: Path) -> None:
    """The declared bound of the two-stage algorithm, stated as a test rather than assumed.

    A remainder rewrite that keeps size, inode, *and* ``mtime_ns`` -- possible when two
    writes land inside one filesystem timestamp tick -- is not observable from metadata, so
    the second stage has nothing to hash.  The fully hashed trust-inventory closure and the
    declared configuration paths are never subject to this bound; they are content hashed on
    every capture.
    """

    before = capture_root_manifest(_request(fixture_root))
    cache = fixture_root / "scratch" / "cache.bin"
    recorded = next(item for item in before.metadata_paths if item.path == "scratch/cache.bin")
    cache.write_bytes(b"newer")
    os.utime(cache, ns=(recorded.mtime_ns, recorded.mtime_ns))
    after = _after(before, fixture_root)
    assert compare_root_manifests(before, after).unexpected == ()

    # The same rewrite inside the fully hashed closure is always caught.
    source = fixture_root / "src" / "a.py"
    hashed = before.hashed_paths[0]
    source.write_text("answer = 9\n", encoding="utf-8")
    os.utime(source, ns=(hashed.mtime_ns, hashed.mtime_ns))
    assert compare_root_manifests(before, _after(before, fixture_root)).unexpected == ("src/a.py",)


def test_declared_parent_path_does_not_suppress_a_child(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer and longer")
    after = _after(before, fixture_root)

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

    elsewhere = fixture_root.parent / "elsewhere"
    (elsewhere / "src").mkdir(parents=True)
    (elsewhere / "src" / "a.py").write_text("answer = 1\n", encoding="utf-8")
    (elsewhere / "scratch").mkdir()
    other = capture_root_manifest(_request(elsewhere))

    with pytest.raises(WriteGuardError, match="root"):
        compare_root_manifests(before, other)
    git_shaped = RootManifest.build(
        root=before.root,
        kind="git",
        source_revision="a" * 40,
        inventory_digest=before.inventory_digest,
        inventory_paths=before.inventory_paths,
        excluded_paths=before.excluded_paths,
        hashed_paths=before.hashed_paths,
        metadata_paths=before.metadata_paths,
    )
    with pytest.raises(WriteGuardError, match="kind"):
        compare_root_manifests(git_shaped, after)


def test_manifest_capture_records_a_special_file_rather_than_hiding_it(fixture_root: Path) -> None:
    """A special node is recorded and compared; it is never silently skipped."""

    before = capture_root_manifest(_request(fixture_root))
    os.mkfifo(fixture_root / "scratch" / "named-pipe")
    after = _after(before, fixture_root)

    record = next(item for item in after.metadata_paths if item.path == "scratch/named-pipe")
    assert record.kind == "special"
    assert record.content_sha256 is None
    assert compare_root_manifests(before, after).unexpected == ("scratch/named-pipe",)


def test_guard_canonicalizes_delta_path_order(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "src" / "a.py").write_text("answer = 2\n", encoding="utf-8")
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"newer and longer")
    after = _after(before, fixture_root)

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
        control_changes=(),
    )

    with pytest.raises(WriteGuardError) as raised:
        assert_no_unexpected_writes((delta,))

    message = str(raised.value)
    assert "unexpected_paths=51" in message
    assert delta.before_manifest_digest in message
    assert delta.after_manifest_digest in message
    assert "path-049" in message
    assert "path-050" not in message


# --- the bounded production-helper enrichment read ---------------------------------------


def test_enrichment_hashes_changed_remainder_files_in_a_bounded_child(fixture_root: Path) -> None:
    """The default enrichment digest is production's, executed under the phase's ceiling."""

    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"cache!")
    after = capture_root_manifest(_request(fixture_root))

    enriched = enrich_after_manifest(before, after, deadline=Deadline.start(monotonic_clock, 60.0))

    changed = next(record for record in enriched.metadata_paths if record.path == "scratch/cache.bin")
    assert changed.content_sha256 == observe_file_digest(fixture_root / "scratch" / "cache.bin")


def test_enrichment_refuses_a_remainder_file_it_cannot_attribute(fixture_root: Path) -> None:
    """A FIFO where a changed remainder file was is an incomplete observation, not clean."""

    before = capture_root_manifest(_request(fixture_root))
    target = fixture_root / "scratch" / "cache.bin"
    target.unlink()
    os.mkfifo(target)
    after = capture_root_manifest(_request(fixture_root))
    # The metadata scan sees a special node, so force the enrichment to attempt the digest.
    forced = RootManifest.build(
        root=after.root,
        kind=after.kind,
        source_revision=after.source_revision,
        inventory_digest=after.inventory_digest,
        inventory_paths=after.inventory_paths,
        excluded_paths=after.excluded_paths,
        hashed_paths=after.hashed_paths,
        metadata_paths=tuple(
            record if record.path != "scratch/cache.bin" else replace(record, kind="file")
            for record in after.metadata_paths
        ),
    )

    started = time.monotonic()
    with pytest.raises(WriteGuardError, match="could not be hashed"):
        enrich_after_manifest(before, forced, deadline=Deadline.start(monotonic_clock, 30.0))
    assert time.monotonic() - started < 10.0


def test_enrichment_with_no_remaining_time_fails_closed(fixture_root: Path) -> None:
    before = capture_root_manifest(_request(fixture_root))
    (fixture_root / "scratch" / "cache.bin").write_bytes(b"cache!")
    after = capture_root_manifest(_request(fixture_root))
    clock = _FakeClock()
    deadline = Deadline.start(clock, 10.0)
    clock.advance(10.0)

    with pytest.raises((WriteGuardError, DeadlineExceeded)):
        enrich_after_manifest(before, after, deadline=deadline)
