"""Fail-closed comparison of bounded evaluation-root manifests.

The instrument is two-stage, exactly as the spec's remainder algorithm requires.

1. Two captures record the full-content digest of the trust-inventory closure and declared
   configuration paths, and metadata only -- path, type, symlink target, size, ``mtime_ns``,
   inode -- for the complete in-scope remainder.
2. :func:`enrich_after_manifest` then hashes *only* the remainder files whose metadata
   changed or that did not exist before, through one guarded ``O_NOFOLLOW`` read each, and
   rebuilds the after manifest and its digest with those hashes.  Deletions, directories,
   symlinks, and special nodes need no content hash.  A file that cannot be hashed, or whose
   metadata moves again while it is being hashed, makes the observation *incomplete*: it is
   never reported as clean.

Only then is a :class:`WriteDelta` constructed, so every delta is bound to the two manifest
digests it was actually derived from.  A changed manifest *control* -- the Git revision, the
inventory digest, its count, its paths, or the recorded exclusions -- is reported as a
control change on the delta rather than as an unstable-root error, because a created or
deleted inventory member is a write, not a broken instrument.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from scripts.backend_eval.manifests import ManifestError, bounded_file_digests
from scripts.backend_eval.models import PathRecord, RootManifest, WriteDelta
from scripts.backend_eval.process import Deadline
from scripts.backend_eval.source_binding import HelperExpectation

__all__ = [
    "WriteGuardError",
    "assert_no_unexpected_writes",
    "compare_root_manifests",
    "enrich_after_manifest",
]

# Manifest facts that describe the root itself rather than one path below it.
_CONTROL_FIELDS = ("excluded_paths", "inventory_count", "inventory_digest", "inventory_paths", "source_revision")

DigestReader = Callable[[Path], str | None]


def _bounded_digest_reader(
    root: Path, expectation: HelperExpectation, deadline: Deadline | None
) -> DigestReader:
    """Production's ``observe_file_digest``, executed in a bounded, killable child.

    Enrichment reads only the remainder paths whose metadata actually moved, so this is
    normally called zero times in a clean run; when it is called, the same ceiling that
    bounds the corpus captures bounds it, one child per changed path.
    """

    def digest_for(absolute: Path) -> str | None:
        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError as error:  # pragma: no cover - structural guard
            raise WriteGuardError(f"{absolute} is not below the manifest root {root}") from error
        try:
            return bounded_file_digests(root, (relative,), expectation=expectation, deadline=deadline)[relative]
        except ManifestError as error:
            raise WriteGuardError(f"cannot hash the changed remainder file {relative}: {error}") from error

    return digest_for


class WriteGuardError(RuntimeError):
    """A manifest comparison cannot establish a bounded zero-write result."""


def enrich_after_manifest(
    before: RootManifest,
    after: RootManifest,
    *,
    expectation: HelperExpectation,
    deadline: Deadline | None = None,
    digest_for: DigestReader | None = None,
) -> RootManifest:
    """Return the after manifest with content hashes for changed or created remainder files.

    Nothing else is re-read: an unchanged remainder file keeps its metadata-only record, so
    the second stage costs one guarded read per actually-changed path.
    """

    _require_same_root_identity(before, after)
    before_records = {record.path: record for record in after_and_before_records(before)}
    root = Path(after.root)
    read_digest = (
        _bounded_digest_reader(root, expectation, deadline) if digest_for is None else digest_for
    )
    enriched: list[PathRecord] = []
    changed = False
    for record in after.metadata_paths:
        previous = before_records.get(record.path)
        if record.kind != "file" or record.content_sha256 is not None or previous == record:
            enriched.append(record)
            continue
        enriched.append(_hashed_remainder_record(root, record, read_digest))
        changed = True
    if not changed:
        return after
    return RootManifest.build(
        root=after.root,
        kind=after.kind,
        source_revision=after.source_revision,
        inventory_digest=after.inventory_digest,
        inventory_paths=after.inventory_paths,
        excluded_paths=after.excluded_paths,
        hashed_paths=after.hashed_paths,
        metadata_paths=tuple(enriched),
    )


def after_and_before_records(manifest: RootManifest) -> tuple[PathRecord, ...]:
    """Every path record one manifest carries, in one flat tuple."""

    return (*manifest.hashed_paths, *manifest.metadata_paths)


def _hashed_remainder_record(root: Path, record: PathRecord, digest_for: DigestReader) -> PathRecord:
    absolute = root / record.path
    digest = digest_for(absolute)
    if digest is None:
        raise WriteGuardError(
            f"changed remainder file could not be hashed and the observation is incomplete: {record.path}"
        )
    try:
        observed = absolute.lstat()
    except OSError as error:
        raise WriteGuardError(
            f"changed remainder file could not be hashed and the observation is incomplete: {record.path}: {error}"
        ) from error
    if (observed.st_size, observed.st_mtime_ns, observed.st_ino) != (record.size, record.mtime_ns, record.inode):
        raise WriteGuardError(f"remainder file changed while it was being hashed: {record.path}")
    return PathRecord(
        path=record.path,
        kind=record.kind,
        disposition=record.disposition,
        size=record.size,
        mtime_ns=record.mtime_ns,
        inode=record.inode,
        symlink_target=record.symlink_target,
        content_sha256=digest,
    )


def compare_root_manifests(
    before: RootManifest,
    after: RootManifest,
    *,
    declared_mutations: frozenset[str] = frozenset(),
) -> WriteDelta:
    """Return one digest-bound delta, rejecting incomplete observations.

    Declared mutations apply only to an exact changed relative path.  Metadata
    files are not content-hashed during ordinary capture, so a changed
    post-state metadata file must carry its content digest before it can be
    classified as a declared or unexpected mutation.
    """

    _require_same_root_identity(before, after)
    _require_declared_mutation_names(declared_mutations)

    before_records = _records_by_path(before)
    after_records = _records_by_path(after)
    metadata_paths = {record.path for record in before.metadata_paths}
    metadata_paths.update(record.path for record in after.metadata_paths)
    changed_paths = tuple(
        path
        for path in sorted(set(before_records) | set(after_records))
        if before_records.get(path) != after_records.get(path)
    )
    for path in changed_paths:
        _require_complete_metadata_observation(path, after_records.get(path), metadata_paths)

    declared = tuple(path for path in changed_paths if path in declared_mutations)
    unexpected = tuple(path for path in changed_paths if path not in declared_mutations)
    return WriteDelta(
        root=before.root,
        kind=before.kind,
        before_manifest_digest=before.manifest_digest,
        after_manifest_digest=after.manifest_digest,
        declared=declared,
        unexpected=unexpected,
        control_changes=_control_changes(before, after),
    )


def assert_no_unexpected_writes(deltas: Sequence[WriteDelta]) -> None:
    """Raise one bounded receipt-bearing error when any root changed unexpectedly."""

    unexpected = tuple(
        (delta.root, path, delta.before_manifest_digest, delta.after_manifest_digest)
        for delta in deltas
        for path in delta.unexpected
    )
    controls = tuple(
        (delta.root, name, delta.before_manifest_digest, delta.after_manifest_digest)
        for delta in deltas
        for name in delta.control_changes
    )
    if not unexpected and not controls:
        return
    observations = (*unexpected, *controls)
    samples = tuple(f"{root}:{name}" for root, name, _, _ in observations[:50])
    before_digests = tuple(sorted({before for _, _, before, _ in observations}))
    after_digests = tuple(sorted({after for _, _, _, after in observations}))
    roots = len({root for root, _, _, _ in observations})
    raise WriteGuardError(
        "unexpected backend-evaluation writes "
        f"unexpected_paths={len(unexpected)} control_changes={len(controls)} roots={roots} "
        f"before_manifest_digests={before_digests} after_manifest_digests={after_digests} "
        f"sample_paths={samples}"
    )


def _control_changes(before: RootManifest, after: RootManifest) -> tuple[str, ...]:
    return tuple(sorted(field for field in _CONTROL_FIELDS if getattr(before, field) != getattr(after, field)))


def _require_same_root_identity(before: RootManifest, after: RootManifest) -> None:
    if before.root != after.root:
        raise WriteGuardError(f"cannot compare manifests from different roots: {before.root!r} != {after.root!r}")
    if before.kind != after.kind:
        raise WriteGuardError(f"cannot compare manifests with different kinds: {before.kind!r} != {after.kind!r}")


def _require_declared_mutation_names(declared_mutations: frozenset[str]) -> None:
    valid = isinstance(declared_mutations, frozenset) and all(
        isinstance(path, str) and path for path in declared_mutations
    )
    if not valid:
        raise WriteGuardError("declared_mutations must be a frozenset of non-empty relative path strings")


def _records_by_path(manifest: RootManifest) -> dict[str, PathRecord]:
    return {record.path: record for record in (*manifest.hashed_paths, *manifest.metadata_paths)}


def _require_complete_metadata_observation(
    path: str,
    after: PathRecord | None,
    metadata_paths: set[str],
) -> None:
    if path in metadata_paths and after is not None and after.kind == "file" and after.content_sha256 is None:
        raise WriteGuardError(f"changed metadata record lacks an after content hash: {path}")
