"""Canonical evaluation models and serialization for the backend evaluation harness.

Every later task consumes these exact frozen dataclasses and the two module
functions below; no later task may introduce a second receipt or manifest
representation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, cast

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_PATH_RECORD_KINDS = frozenset({"file", "symlink"})
_PATH_RECORD_DISPOSITIONS = frozenset({"tracked", "untracked", "ignored", "declared"})
_ROOT_MANIFEST_KINDS = frozenset({"git", "non_git"})
_ADMISSION_STATUSES = frozenset({"pass", "hold", "incomplete", "fail"})
_CANDIDATE_NAMES = frozenset({"ty", "pyrefly"})
_SERVICE_CONFIG_BACKENDS = frozenset({"pyright", "ty", "pyrefly"})

ADMISSION_RECEIPT_SCHEMA_VERSION = 1
EVALUATION_CONTRACT_VERSION = "python-backend-evaluation-v1"


def canonical_json(value: Mapping[str, object]) -> bytes:
    """Serialize a mapping to sorted, compact, UTF-8, newline-terminated JSON bytes."""

    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


# --- validation helpers -----------------------------------------------------


def _validate_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a canonical lowercase SHA-256 digest")
    return value


def _validate_non_empty_str(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _validate_absolute_path(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if not text.startswith("/"):
        raise ValueError(f"{label} must be an absolute path")
    return text


def _validate_relative_path(value: object, label: str) -> str:
    text = _validate_non_empty_str(value, label)
    if text.startswith("/"):
        raise ValueError(f"{label} must be a relative path")
    return text


def _validate_int(value: object, label: str, *, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} must be >= {minimum}")
    return value


def _validate_positive_int(value: object, label: str) -> int:
    return _validate_int(value, label, minimum=1)


def _validate_unique_names(names: Sequence[str], label: str) -> None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ValueError(f"{label} contains duplicate name: {name!r}")
        seen.add(name)


def _validate_sorted_unique(names: Sequence[str], label: str) -> None:
    _validate_unique_names(names, label)
    if list(names) != sorted(names):
        raise ValueError(f"{label} must be in canonical sorted order")


def _validate_tuple(value: object, label: str) -> tuple[Any, ...]:
    if not isinstance(value, tuple):
        raise ValueError(f"{label} must be a tuple, not {type(value).__name__}")
    return value


def _validate_artifact_hashes(value: object, label: str) -> tuple[str, ...]:
    hashes = _validate_tuple(value, label)
    if not hashes:
        raise ValueError(f"{label} must not be empty")
    for digest in hashes:
        _validate_sha256(digest, label)
    _validate_sorted_unique(cast("Sequence[str]", hashes), label)
    return cast("tuple[str, ...]", hashes)


def _validate_path_pairs(value: object, label: str) -> tuple[tuple[str, str], ...]:
    pairs = _validate_tuple(value, label)
    names: list[str] = []
    for entry in pairs:
        if not isinstance(entry, tuple) or len(entry) != 2:
            raise ValueError(f"{label} entries must be (name, path) tuples")
        name, path = entry
        _validate_non_empty_str(name, f"{label} name")
        _validate_non_empty_str(path, f"{label} path")
        names.append(name)
    _validate_sorted_unique(names, label)
    return pairs


# --- decode helpers ----------------------------------------------------------


def _expect_mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return cast("Mapping[str, Any]", value)


def _expect_list(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return value


def _expect_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    return value


def _expect_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an integer")
    return value


def _closed_fields(value: Mapping[str, object], required: frozenset[str], label: str) -> None:
    missing = required - value.keys()
    if missing:
        raise ValueError(f"{label} is missing required fields: {sorted(missing)}")
    unknown = value.keys() - required
    if unknown:
        raise ValueError(f"{label} has unknown fields: {sorted(unknown)}")


# --- PhaseBudget --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseBudget:
    name: str
    seconds: int

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "PhaseBudget.name")
        _validate_positive_int(self.seconds, "PhaseBudget.seconds")


_PHASE_BUDGET_FIELDS = frozenset({"name", "seconds"})


def _phase_budget_to_dict(budget: PhaseBudget) -> dict[str, object]:
    return {"name": budget.name, "seconds": budget.seconds}


def _phase_budget_from_dict(value: object) -> PhaseBudget:
    mapping = _expect_mapping(value, "PhaseBudget")
    _closed_fields(mapping, _PHASE_BUDGET_FIELDS, "PhaseBudget")
    return PhaseBudget(
        name=_expect_str(mapping["name"], "PhaseBudget.name"),
        seconds=_expect_int(mapping["seconds"], "PhaseBudget.seconds"),
    )


DEFAULT_PHASE_BUDGETS: Mapping[str, PhaseBudget] = MappingProxyType(
    {
        "admission": PhaseBudget("admission", 30 * 60),
        "protocol": PhaseBudget("protocol", 90 * 60),
        "product_seam": PhaseBudget("product_seam", 3 * 60 * 60),
        "feature": PhaseBudget("feature", 2 * 60 * 60),
        "agent": PhaseBudget("agent", 8 * 60 * 60),
        "total": PhaseBudget("total", 16 * 60 * 60),
    }
)


# --- ProductionIdentity --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProductionIdentity:
    pyproject_toml_sha256: str
    uv_lock_sha256: str
    package_lock_json_sha256: str
    dependency_lock_digest: str
    build_identity: str
    runtime_paths: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.pyproject_toml_sha256, "ProductionIdentity.pyproject_toml_sha256")
        _validate_sha256(self.uv_lock_sha256, "ProductionIdentity.uv_lock_sha256")
        _validate_sha256(self.package_lock_json_sha256, "ProductionIdentity.package_lock_json_sha256")
        _validate_sha256(self.dependency_lock_digest, "ProductionIdentity.dependency_lock_digest")
        _validate_sha256(self.build_identity, "ProductionIdentity.build_identity")
        _validate_path_pairs(self.runtime_paths, "ProductionIdentity.runtime_paths")


_PRODUCTION_IDENTITY_FIELDS = frozenset(
    {
        "pyproject_toml_sha256",
        "uv_lock_sha256",
        "package_lock_json_sha256",
        "dependency_lock_digest",
        "build_identity",
        "runtime_paths",
    }
)


def _production_identity_to_dict(identity: ProductionIdentity) -> dict[str, object]:
    return {
        "pyproject_toml_sha256": identity.pyproject_toml_sha256,
        "uv_lock_sha256": identity.uv_lock_sha256,
        "package_lock_json_sha256": identity.package_lock_json_sha256,
        "dependency_lock_digest": identity.dependency_lock_digest,
        "build_identity": identity.build_identity,
        "runtime_paths": dict(identity.runtime_paths),
    }


def _production_identity_from_dict(value: object) -> ProductionIdentity:
    mapping = _expect_mapping(value, "ProductionIdentity")
    _closed_fields(mapping, _PRODUCTION_IDENTITY_FIELDS, "ProductionIdentity")
    runtime_paths_mapping = _expect_mapping(mapping["runtime_paths"], "ProductionIdentity.runtime_paths")
    runtime_paths = tuple(
        sorted(
            (
                _expect_str(key, "ProductionIdentity.runtime_paths key"),
                _expect_str(path, "ProductionIdentity.runtime_paths value"),
            )
            for key, path in runtime_paths_mapping.items()
        )
    )
    return ProductionIdentity(
        pyproject_toml_sha256=_expect_str(mapping["pyproject_toml_sha256"], "ProductionIdentity.pyproject_toml_sha256"),
        uv_lock_sha256=_expect_str(mapping["uv_lock_sha256"], "ProductionIdentity.uv_lock_sha256"),
        package_lock_json_sha256=_expect_str(
            mapping["package_lock_json_sha256"], "ProductionIdentity.package_lock_json_sha256"
        ),
        dependency_lock_digest=_expect_str(
            mapping["dependency_lock_digest"], "ProductionIdentity.dependency_lock_digest"
        ),
        build_identity=_expect_str(mapping["build_identity"], "ProductionIdentity.build_identity"),
        runtime_paths=runtime_paths,
    )


# --- EnvironmentIdentity / ServiceConfigIdentity -------------------------------


@dataclass(frozen=True, slots=True)
class EnvironmentIdentity:
    name: str
    interpreter_path: str
    interpreter_realpath: str
    version: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "EnvironmentIdentity.name")
        _validate_absolute_path(self.interpreter_path, "EnvironmentIdentity.interpreter_path")
        _validate_absolute_path(self.interpreter_realpath, "EnvironmentIdentity.interpreter_realpath")
        _validate_non_empty_str(self.version, "EnvironmentIdentity.version")


_ENVIRONMENT_IDENTITY_FIELDS = frozenset({"name", "interpreter_path", "interpreter_realpath", "version"})


def _environment_identity_to_dict(identity: EnvironmentIdentity) -> dict[str, object]:
    return {
        "name": identity.name,
        "interpreter_path": identity.interpreter_path,
        "interpreter_realpath": identity.interpreter_realpath,
        "version": identity.version,
    }


def _environment_identity_from_dict(value: object) -> EnvironmentIdentity:
    mapping = _expect_mapping(value, "EnvironmentIdentity")
    _closed_fields(mapping, _ENVIRONMENT_IDENTITY_FIELDS, "EnvironmentIdentity")
    return EnvironmentIdentity(
        name=_expect_str(mapping["name"], "EnvironmentIdentity.name"),
        interpreter_path=_expect_str(mapping["interpreter_path"], "EnvironmentIdentity.interpreter_path"),
        interpreter_realpath=_expect_str(
            mapping["interpreter_realpath"], "EnvironmentIdentity.interpreter_realpath"
        ),
        version=_expect_str(mapping["version"], "EnvironmentIdentity.version"),
    )


@dataclass(frozen=True, slots=True)
class ServiceConfigIdentity:
    backend: str
    config_path: str
    config_sha256: str
    home_path: str
    cache_path: str

    def __post_init__(self) -> None:
        if self.backend not in _SERVICE_CONFIG_BACKENDS:
            raise ValueError(f"ServiceConfigIdentity.backend must be one of {sorted(_SERVICE_CONFIG_BACKENDS)}")
        _validate_absolute_path(self.config_path, "ServiceConfigIdentity.config_path")
        _validate_sha256(self.config_sha256, "ServiceConfigIdentity.config_sha256")
        _validate_absolute_path(self.home_path, "ServiceConfigIdentity.home_path")
        _validate_absolute_path(self.cache_path, "ServiceConfigIdentity.cache_path")


_SERVICE_CONFIG_IDENTITY_FIELDS = frozenset({"backend", "config_path", "config_sha256", "home_path", "cache_path"})


def _service_config_identity_to_dict(identity: ServiceConfigIdentity) -> dict[str, object]:
    return {
        "backend": identity.backend,
        "config_path": identity.config_path,
        "config_sha256": identity.config_sha256,
        "home_path": identity.home_path,
        "cache_path": identity.cache_path,
    }


def _service_config_identity_from_dict(value: object) -> ServiceConfigIdentity:
    mapping = _expect_mapping(value, "ServiceConfigIdentity")
    _closed_fields(mapping, _SERVICE_CONFIG_IDENTITY_FIELDS, "ServiceConfigIdentity")
    return ServiceConfigIdentity(
        backend=_expect_str(mapping["backend"], "ServiceConfigIdentity.backend"),
        config_path=_expect_str(mapping["config_path"], "ServiceConfigIdentity.config_path"),
        config_sha256=_expect_str(mapping["config_sha256"], "ServiceConfigIdentity.config_sha256"),
        home_path=_expect_str(mapping["home_path"], "ServiceConfigIdentity.home_path"),
        cache_path=_expect_str(mapping["cache_path"], "ServiceConfigIdentity.cache_path"),
    )


# --- ResolvedPackage / CandidatePackage / CandidateLock ------------------------


@dataclass(frozen=True, slots=True)
class ResolvedPackage:
    name: str
    version: str
    requirement: str
    artifact_hashes: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "ResolvedPackage.name")
        _validate_non_empty_str(self.version, "ResolvedPackage.version")
        _validate_non_empty_str(self.requirement, "ResolvedPackage.requirement")
        _validate_artifact_hashes(self.artifact_hashes, f"ResolvedPackage.artifact_hashes[{self.name}]")


_RESOLVED_PACKAGE_FIELDS = frozenset({"name", "version", "requirement", "artifact_hashes"})


def _resolved_package_to_dict(package: ResolvedPackage) -> dict[str, object]:
    return {
        "name": package.name,
        "version": package.version,
        "requirement": package.requirement,
        "artifact_hashes": list(package.artifact_hashes),
    }


def _resolved_package_from_dict(value: object) -> ResolvedPackage:
    mapping = _expect_mapping(value, "ResolvedPackage")
    _closed_fields(mapping, _RESOLVED_PACKAGE_FIELDS, "ResolvedPackage")
    artifact_hashes = tuple(
        _expect_str(digest, "ResolvedPackage.artifact_hashes digest")
        for digest in _expect_list(mapping["artifact_hashes"], "ResolvedPackage.artifact_hashes")
    )
    return ResolvedPackage(
        name=_expect_str(mapping["name"], "ResolvedPackage.name"),
        version=_expect_str(mapping["version"], "ResolvedPackage.version"),
        requirement=_expect_str(mapping["requirement"], "ResolvedPackage.requirement"),
        artifact_hashes=artifact_hashes,
    )


@dataclass(frozen=True, slots=True)
class CandidatePackage:
    name: str
    version: str
    requirement: str
    artifact_hashes: tuple[str, ...]
    executable_relpath: str

    def __post_init__(self) -> None:
        _validate_non_empty_str(self.name, "CandidatePackage.name")
        _validate_non_empty_str(self.version, "CandidatePackage.version")
        _validate_non_empty_str(self.requirement, "CandidatePackage.requirement")
        _validate_relative_path(self.executable_relpath, "CandidatePackage.executable_relpath")
        _validate_artifact_hashes(self.artifact_hashes, f"CandidatePackage.artifact_hashes[{self.name}]")


_CANDIDATE_PACKAGE_FIELDS = frozenset({"name", "version", "requirement", "artifact_hashes", "executable_relpath"})


def _candidate_package_to_dict(package: CandidatePackage) -> dict[str, object]:
    return {
        "name": package.name,
        "version": package.version,
        "requirement": package.requirement,
        "artifact_hashes": list(package.artifact_hashes),
        "executable_relpath": package.executable_relpath,
    }


def _candidate_package_from_dict(value: object) -> CandidatePackage:
    mapping = _expect_mapping(value, "CandidatePackage")
    _closed_fields(mapping, _CANDIDATE_PACKAGE_FIELDS, "CandidatePackage")
    artifact_hashes = tuple(
        _expect_str(digest, "CandidatePackage.artifact_hashes digest")
        for digest in _expect_list(mapping["artifact_hashes"], "CandidatePackage.artifact_hashes")
    )
    return CandidatePackage(
        name=_expect_str(mapping["name"], "CandidatePackage.name"),
        version=_expect_str(mapping["version"], "CandidatePackage.version"),
        requirement=_expect_str(mapping["requirement"], "CandidatePackage.requirement"),
        artifact_hashes=artifact_hashes,
        executable_relpath=_expect_str(mapping["executable_relpath"], "CandidatePackage.executable_relpath"),
    )


@dataclass(frozen=True, slots=True)
class CandidateLock:
    digest: str
    exclude_newer: str
    resolved_packages: tuple[ResolvedPackage, ...]
    candidates: tuple[CandidatePackage, ...]

    def __post_init__(self) -> None:
        _validate_sha256(self.digest, "CandidateLock.digest")
        _validate_non_empty_str(self.exclude_newer, "CandidateLock.exclude_newer")
        resolved_packages = _validate_tuple(self.resolved_packages, "CandidateLock.resolved_packages")
        if not resolved_packages:
            raise ValueError("CandidateLock.resolved_packages must not be empty")
        _validate_sorted_unique([package.name for package in resolved_packages], "CandidateLock.resolved_packages")
        candidates = _validate_tuple(self.candidates, "CandidateLock.candidates")
        if len(candidates) != 2:
            raise ValueError("CandidateLock.candidates must contain exactly two entries")
        candidate_names = [package.name for package in candidates]
        _validate_sorted_unique(candidate_names, "CandidateLock.candidates")
        if set(candidate_names) != _CANDIDATE_NAMES:
            raise ValueError("CandidateLock.candidates must be exactly the ty and pyrefly candidates")
        resolved_by_name = {package.name: package for package in resolved_packages}
        for candidate in candidates:
            resolved = resolved_by_name.get(candidate.name)
            if resolved is None:
                raise ValueError(f"CandidateLock.candidates[{candidate.name}] is missing from resolved_packages")
            if (resolved.version, resolved.requirement, resolved.artifact_hashes) != (
                candidate.version,
                candidate.requirement,
                candidate.artifact_hashes,
            ):
                raise ValueError(
                    f"CandidateLock.candidates[{candidate.name}] does not match its resolved_packages entry"
                )


_CANDIDATE_LOCK_FIELDS = frozenset({"digest", "exclude_newer", "resolved_packages", "candidates"})


def _candidate_lock_to_dict(lock: CandidateLock) -> dict[str, object]:
    return {
        "digest": lock.digest,
        "exclude_newer": lock.exclude_newer,
        "resolved_packages": [_resolved_package_to_dict(package) for package in lock.resolved_packages],
        "candidates": [_candidate_package_to_dict(package) for package in lock.candidates],
    }


def _candidate_lock_from_dict(value: object) -> CandidateLock:
    mapping = _expect_mapping(value, "CandidateLock")
    _closed_fields(mapping, _CANDIDATE_LOCK_FIELDS, "CandidateLock")
    resolved_packages = tuple(
        _resolved_package_from_dict(item)
        for item in _expect_list(mapping["resolved_packages"], "CandidateLock.resolved_packages")
    )
    candidates = tuple(
        _candidate_package_from_dict(item) for item in _expect_list(mapping["candidates"], "CandidateLock.candidates")
    )
    return CandidateLock(
        digest=_expect_str(mapping["digest"], "CandidateLock.digest"),
        exclude_newer=_expect_str(mapping["exclude_newer"], "CandidateLock.exclude_newer"),
        resolved_packages=resolved_packages,
        candidates=candidates,
    )


# --- PathRecord / RootManifest -------------------------------------------------


@dataclass(frozen=True, slots=True)
class PathRecord:
    path: str
    kind: str
    disposition: str
    size: int
    mtime_ns: int
    inode: int
    symlink_target: str | None
    content_sha256: str | None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path, "PathRecord.path")
        if self.kind not in _PATH_RECORD_KINDS:
            raise ValueError(f"PathRecord.kind must be one of {sorted(_PATH_RECORD_KINDS)}")
        if self.disposition not in _PATH_RECORD_DISPOSITIONS:
            raise ValueError(f"PathRecord.disposition must be one of {sorted(_PATH_RECORD_DISPOSITIONS)}")
        _validate_int(self.size, "PathRecord.size", minimum=0)
        _validate_int(self.mtime_ns, "PathRecord.mtime_ns")
        _validate_int(self.inode, "PathRecord.inode", minimum=0)
        if self.kind == "symlink":
            _validate_non_empty_str(self.symlink_target, "PathRecord.symlink_target")
        elif self.symlink_target is not None:
            raise ValueError("PathRecord.symlink_target must be None unless kind is symlink")
        if self.content_sha256 is not None:
            _validate_sha256(self.content_sha256, "PathRecord.content_sha256")


_PATH_RECORD_FIELDS = frozenset(
    {"path", "kind", "disposition", "size", "mtime_ns", "inode", "symlink_target", "content_sha256"}
)


def _path_record_to_dict(record: PathRecord) -> dict[str, object]:
    return {
        "path": record.path,
        "kind": record.kind,
        "disposition": record.disposition,
        "size": record.size,
        "mtime_ns": record.mtime_ns,
        "inode": record.inode,
        "symlink_target": record.symlink_target,
        "content_sha256": record.content_sha256,
    }


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _expect_str(value, label)


def _path_record_from_dict(value: object) -> PathRecord:
    mapping = _expect_mapping(value, "PathRecord")
    _closed_fields(mapping, _PATH_RECORD_FIELDS, "PathRecord")
    return PathRecord(
        path=_expect_str(mapping["path"], "PathRecord.path"),
        kind=_expect_str(mapping["kind"], "PathRecord.kind"),
        disposition=_expect_str(mapping["disposition"], "PathRecord.disposition"),
        size=_expect_int(mapping["size"], "PathRecord.size"),
        mtime_ns=_expect_int(mapping["mtime_ns"], "PathRecord.mtime_ns"),
        inode=_expect_int(mapping["inode"], "PathRecord.inode"),
        symlink_target=_optional_str(mapping["symlink_target"], "PathRecord.symlink_target"),
        content_sha256=_optional_str(mapping["content_sha256"], "PathRecord.content_sha256"),
    )


@dataclass(frozen=True, slots=True)
class RootManifest:
    root: str
    kind: str
    source_revision: str | None
    inventory_digest: str
    inventory_count: int
    hashed_paths: tuple[PathRecord, ...]
    metadata_paths: tuple[PathRecord, ...]
    manifest_digest: str

    def __post_init__(self) -> None:
        _validate_absolute_path(self.root, "RootManifest.root")
        if self.kind not in _ROOT_MANIFEST_KINDS:
            raise ValueError(f"RootManifest.kind must be one of {sorted(_ROOT_MANIFEST_KINDS)}")
        if self.kind == "git":
            if not isinstance(self.source_revision, str) or _GIT_REVISION_RE.fullmatch(self.source_revision) is None:
                raise ValueError("RootManifest.source_revision must be a Git commit revision when kind is git")
        elif self.source_revision is not None:
            raise ValueError("RootManifest.source_revision must be None when kind is non_git")
        _validate_sha256(self.inventory_digest, "RootManifest.inventory_digest")
        _validate_sha256(self.manifest_digest, "RootManifest.manifest_digest")
        _validate_int(self.inventory_count, "RootManifest.inventory_count", minimum=0)
        hashed_paths = _validate_tuple(self.hashed_paths, "RootManifest.hashed_paths")
        metadata_paths = _validate_tuple(self.metadata_paths, "RootManifest.metadata_paths")
        for record in hashed_paths:
            if record.content_sha256 is None:
                raise ValueError(f"RootManifest.hashed_paths[{record.path}] must have content_sha256")
        _validate_sorted_unique([record.path for record in hashed_paths], "RootManifest.hashed_paths")
        _validate_sorted_unique([record.path for record in metadata_paths], "RootManifest.metadata_paths")
        all_paths = [record.path for record in (*hashed_paths, *metadata_paths)]
        _validate_unique_names(all_paths, "RootManifest paths")


_ROOT_MANIFEST_FIELDS = frozenset(
    {
        "root",
        "kind",
        "source_revision",
        "inventory_digest",
        "inventory_count",
        "hashed_paths",
        "metadata_paths",
        "manifest_digest",
    }
)


def _root_manifest_to_dict(manifest: RootManifest) -> dict[str, object]:
    return {
        "root": manifest.root,
        "kind": manifest.kind,
        "source_revision": manifest.source_revision,
        "inventory_digest": manifest.inventory_digest,
        "inventory_count": manifest.inventory_count,
        "hashed_paths": [_path_record_to_dict(record) for record in manifest.hashed_paths],
        "metadata_paths": [_path_record_to_dict(record) for record in manifest.metadata_paths],
        "manifest_digest": manifest.manifest_digest,
    }


def _root_manifest_from_dict(value: object) -> RootManifest:
    mapping = _expect_mapping(value, "RootManifest")
    _closed_fields(mapping, _ROOT_MANIFEST_FIELDS, "RootManifest")
    return RootManifest(
        root=_expect_str(mapping["root"], "RootManifest.root"),
        kind=_expect_str(mapping["kind"], "RootManifest.kind"),
        source_revision=_optional_str(mapping["source_revision"], "RootManifest.source_revision"),
        inventory_digest=_expect_str(mapping["inventory_digest"], "RootManifest.inventory_digest"),
        inventory_count=_expect_int(mapping["inventory_count"], "RootManifest.inventory_count"),
        hashed_paths=tuple(
            _path_record_from_dict(item) for item in _expect_list(mapping["hashed_paths"], "RootManifest.hashed_paths")
        ),
        metadata_paths=tuple(
            _path_record_from_dict(item)
            for item in _expect_list(mapping["metadata_paths"], "RootManifest.metadata_paths")
        ),
        manifest_digest=_expect_str(mapping["manifest_digest"], "RootManifest.manifest_digest"),
    )


# --- WriteDelta -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteDelta:
    root: str
    kind: str
    before_manifest_digest: str
    after_manifest_digest: str
    declared: tuple[str, ...]
    unexpected: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_absolute_path(self.root, "WriteDelta.root")
        if self.kind not in _ROOT_MANIFEST_KINDS:
            raise ValueError(f"WriteDelta.kind must be one of {sorted(_ROOT_MANIFEST_KINDS)}")
        _validate_sha256(self.before_manifest_digest, "WriteDelta.before_manifest_digest")
        _validate_sha256(self.after_manifest_digest, "WriteDelta.after_manifest_digest")
        _validate_sorted_unique(_validate_tuple(self.declared, "WriteDelta.declared"), "WriteDelta.declared")
        _validate_sorted_unique(_validate_tuple(self.unexpected, "WriteDelta.unexpected"), "WriteDelta.unexpected")


_WRITE_DELTA_FIELDS = frozenset(
    {"root", "kind", "before_manifest_digest", "after_manifest_digest", "declared", "unexpected"}
)


def _write_delta_to_dict(delta: WriteDelta) -> dict[str, object]:
    return {
        "root": delta.root,
        "kind": delta.kind,
        "before_manifest_digest": delta.before_manifest_digest,
        "after_manifest_digest": delta.after_manifest_digest,
        "declared": list(delta.declared),
        "unexpected": list(delta.unexpected),
    }


def _write_delta_from_dict(value: object) -> WriteDelta:
    mapping = _expect_mapping(value, "WriteDelta")
    _closed_fields(mapping, _WRITE_DELTA_FIELDS, "WriteDelta")
    return WriteDelta(
        root=_expect_str(mapping["root"], "WriteDelta.root"),
        kind=_expect_str(mapping["kind"], "WriteDelta.kind"),
        before_manifest_digest=_expect_str(mapping["before_manifest_digest"], "WriteDelta.before_manifest_digest"),
        after_manifest_digest=_expect_str(mapping["after_manifest_digest"], "WriteDelta.after_manifest_digest"),
        declared=tuple(
            _expect_str(item, "WriteDelta.declared item")
            for item in _expect_list(mapping["declared"], "WriteDelta.declared")
        ),
        unexpected=tuple(
            _expect_str(item, "WriteDelta.unexpected item")
            for item in _expect_list(mapping["unexpected"], "WriteDelta.unexpected")
        ),
    )


# --- AdmissionReceipt ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    schema_version: int
    evaluation_contract_version: str
    evaluation_identity: str
    status: str
    started_at: str
    ended_at: str
    budgets: tuple[PhaseBudget, ...]
    production_identity_before: ProductionIdentity
    production_identity_after: ProductionIdentity
    candidate_lock: CandidateLock
    environments: tuple[EnvironmentIdentity, ...]
    service_configs: tuple[ServiceConfigIdentity, ...]
    root_manifests_before: tuple[RootManifest, ...]
    root_manifests_after: tuple[RootManifest, ...]
    write_deltas: tuple[WriteDelta, ...]
    issues: tuple[str, ...]
    artifact_tree_digest: str
    next_action: str

    def __post_init__(self) -> None:
        if self.schema_version != ADMISSION_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"AdmissionReceipt schema_version must be {ADMISSION_RECEIPT_SCHEMA_VERSION}, "
                f"got {self.schema_version!r}"
            )
        if self.evaluation_contract_version != EVALUATION_CONTRACT_VERSION:
            raise ValueError(
                f"AdmissionReceipt evaluation_contract_version must be {EVALUATION_CONTRACT_VERSION!r}, "
                f"got {self.evaluation_contract_version!r}"
            )
        _validate_non_empty_str(self.evaluation_identity, "AdmissionReceipt.evaluation_identity")
        if self.status not in _ADMISSION_STATUSES:
            raise ValueError(f"AdmissionReceipt.status must be one of {sorted(_ADMISSION_STATUSES)}")
        _validate_non_empty_str(self.started_at, "AdmissionReceipt.started_at")
        _validate_non_empty_str(self.ended_at, "AdmissionReceipt.ended_at")
        budgets = _validate_tuple(self.budgets, "AdmissionReceipt.budgets")
        _validate_sorted_unique([budget.name for budget in budgets], "AdmissionReceipt.budgets")
        environments = _validate_tuple(self.environments, "AdmissionReceipt.environments")
        _validate_sorted_unique([identity.name for identity in environments], "AdmissionReceipt.environments")
        service_configs = _validate_tuple(self.service_configs, "AdmissionReceipt.service_configs")
        _validate_sorted_unique(
            [identity.backend for identity in service_configs], "AdmissionReceipt.service_configs"
        )
        root_manifests_before = _validate_tuple(self.root_manifests_before, "AdmissionReceipt.root_manifests_before")
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_before], "AdmissionReceipt.root_manifests_before"
        )
        root_manifests_after = _validate_tuple(self.root_manifests_after, "AdmissionReceipt.root_manifests_after")
        _validate_sorted_unique(
            [manifest.root for manifest in root_manifests_after], "AdmissionReceipt.root_manifests_after"
        )
        write_deltas = _validate_tuple(self.write_deltas, "AdmissionReceipt.write_deltas")
        _validate_sorted_unique([delta.root for delta in write_deltas], "AdmissionReceipt.write_deltas")
        issues = _validate_tuple(self.issues, "AdmissionReceipt.issues")
        _validate_sorted_unique(issues, "AdmissionReceipt.issues")
        _validate_sha256(self.artifact_tree_digest, "AdmissionReceipt.artifact_tree_digest")
        _validate_non_empty_str(self.next_action, "AdmissionReceipt.next_action")
        if self.status == "pass":
            if self.production_identity_before != self.production_identity_after:
                raise ValueError(
                    "AdmissionReceipt status is pass but production identity changed between before and after"
                )
            before_by_root = {manifest.root: manifest for manifest in root_manifests_before}
            after_by_root = {manifest.root: manifest for manifest in root_manifests_after}
            delta_roots = {delta.root for delta in write_deltas}
            if set(before_by_root) != delta_roots or set(after_by_root) != delta_roots:
                raise ValueError(
                    "AdmissionReceipt status is pass but root manifest roots do not match write_deltas roots"
                )
            for delta in write_deltas:
                before_manifest = before_by_root[delta.root]
                after_manifest = after_by_root[delta.root]
                if delta.before_manifest_digest != before_manifest.manifest_digest:
                    raise ValueError(
                        f"AdmissionReceipt status is pass but write_deltas[{delta.root}]."
                        "before_manifest_digest does not match its root_manifests_before entry"
                    )
                if delta.after_manifest_digest != after_manifest.manifest_digest:
                    raise ValueError(
                        f"AdmissionReceipt status is pass but write_deltas[{delta.root}]."
                        "after_manifest_digest does not match its root_manifests_after entry"
                    )
                if delta.unexpected:
                    raise ValueError(
                        f"AdmissionReceipt status is pass but write_deltas[{delta.root}] has unexpected paths"
                    )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evaluation_contract_version": self.evaluation_contract_version,
            "evaluation_identity": self.evaluation_identity,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "budgets": [_phase_budget_to_dict(budget) for budget in self.budgets],
            "production_identity_before": _production_identity_to_dict(self.production_identity_before),
            "production_identity_after": _production_identity_to_dict(self.production_identity_after),
            "candidate_lock": _candidate_lock_to_dict(self.candidate_lock),
            "environments": [_environment_identity_to_dict(identity) for identity in self.environments],
            "service_configs": [_service_config_identity_to_dict(identity) for identity in self.service_configs],
            "root_manifests_before": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_before],
            "root_manifests_after": [_root_manifest_to_dict(manifest) for manifest in self.root_manifests_after],
            "write_deltas": [_write_delta_to_dict(delta) for delta in self.write_deltas],
            "issues": list(self.issues),
            "artifact_tree_digest": self.artifact_tree_digest,
            "next_action": self.next_action,
        }

    @staticmethod
    def from_dict(value: Mapping[str, object]) -> AdmissionReceipt:
        schema_version = value.get("schema_version")
        if schema_version != ADMISSION_RECEIPT_SCHEMA_VERSION:
            raise ValueError(
                f"AdmissionReceipt schema_version must be {ADMISSION_RECEIPT_SCHEMA_VERSION}, got {schema_version!r}"
            )
        _closed_fields(value, _ADMISSION_RECEIPT_FIELDS, "AdmissionReceipt")
        return AdmissionReceipt(
            schema_version=_expect_int(value["schema_version"], "AdmissionReceipt.schema_version"),
            evaluation_contract_version=_expect_str(
                value["evaluation_contract_version"], "AdmissionReceipt.evaluation_contract_version"
            ),
            evaluation_identity=_expect_str(value["evaluation_identity"], "AdmissionReceipt.evaluation_identity"),
            status=_expect_str(value["status"], "AdmissionReceipt.status"),
            started_at=_expect_str(value["started_at"], "AdmissionReceipt.started_at"),
            ended_at=_expect_str(value["ended_at"], "AdmissionReceipt.ended_at"),
            budgets=tuple(
                _phase_budget_from_dict(item) for item in _expect_list(value["budgets"], "AdmissionReceipt.budgets")
            ),
            production_identity_before=_production_identity_from_dict(value["production_identity_before"]),
            production_identity_after=_production_identity_from_dict(value["production_identity_after"]),
            candidate_lock=_candidate_lock_from_dict(value["candidate_lock"]),
            environments=tuple(
                _environment_identity_from_dict(item)
                for item in _expect_list(value["environments"], "AdmissionReceipt.environments")
            ),
            service_configs=tuple(
                _service_config_identity_from_dict(item)
                for item in _expect_list(value["service_configs"], "AdmissionReceipt.service_configs")
            ),
            root_manifests_before=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_before"], "AdmissionReceipt.root_manifests_before")
            ),
            root_manifests_after=tuple(
                _root_manifest_from_dict(item)
                for item in _expect_list(value["root_manifests_after"], "AdmissionReceipt.root_manifests_after")
            ),
            write_deltas=tuple(
                _write_delta_from_dict(item)
                for item in _expect_list(value["write_deltas"], "AdmissionReceipt.write_deltas")
            ),
            issues=tuple(
                _expect_str(item, "AdmissionReceipt.issues item")
                for item in _expect_list(value["issues"], "AdmissionReceipt.issues")
            ),
            artifact_tree_digest=_expect_str(value["artifact_tree_digest"], "AdmissionReceipt.artifact_tree_digest"),
            next_action=_expect_str(value["next_action"], "AdmissionReceipt.next_action"),
        )


_ADMISSION_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "evaluation_contract_version",
        "evaluation_identity",
        "status",
        "started_at",
        "ended_at",
        "budgets",
        "production_identity_before",
        "production_identity_after",
        "candidate_lock",
        "environments",
        "service_configs",
        "root_manifests_before",
        "root_manifests_after",
        "write_deltas",
        "issues",
        "artifact_tree_digest",
        "next_action",
    }
)
