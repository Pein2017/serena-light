"""Fail-closed comparison of bounded evaluation-root manifests."""

from __future__ import annotations

from collections.abc import Sequence

from scripts.backend_eval.models import PathRecord, RootManifest, WriteDelta


class WriteGuardError(RuntimeError):
    """A manifest comparison cannot establish a bounded zero-write result."""


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
    _require_stable_manifest_controls(before, after)
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
    )


def assert_no_unexpected_writes(deltas: Sequence[WriteDelta]) -> None:
    """Raise one bounded receipt-bearing error when any root changed unexpectedly."""

    unexpected = tuple(
        (delta.root, path, delta.before_manifest_digest, delta.after_manifest_digest)
        for delta in deltas
        for path in delta.unexpected
    )
    if not unexpected:
        return
    samples = tuple(f"{root}:{path}" for root, path, _, _ in unexpected[:50])
    before_digests = tuple(sorted({before for _, _, before, _ in unexpected}))
    after_digests = tuple(sorted({after for _, _, _, after in unexpected}))
    roots = len({root for root, _, _, _ in unexpected})
    raise WriteGuardError(
        "unexpected backend-evaluation writes "
        f"unexpected_paths={len(unexpected)} roots={roots} "
        f"before_manifest_digests={before_digests} after_manifest_digests={after_digests} "
        f"sample_paths={samples}"
    )


def _require_same_root_identity(before: RootManifest, after: RootManifest) -> None:
    if before.root != after.root:
        raise WriteGuardError(f"cannot compare manifests from different roots: {before.root!r} != {after.root!r}")
    if before.kind != after.kind:
        raise WriteGuardError(f"cannot compare manifests with different kinds: {before.kind!r} != {after.kind!r}")


def _require_stable_manifest_controls(before: RootManifest, after: RootManifest) -> None:
    for field in ("source_revision", "inventory_digest", "inventory_count"):
        if getattr(before, field) != getattr(after, field):
            raise WriteGuardError(f"manifest control changed without a path-level comparison: {field}")


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
