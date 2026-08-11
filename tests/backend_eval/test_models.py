"""Canonicalization and validation tests for the backend evaluation models."""

from __future__ import annotations

import pytest

from scripts.backend_eval.models import (
    DEFAULT_PHASE_BUDGETS,
    AdmissionReceipt,
    CandidateLock,
    CandidatePackage,
    PathRecord,
    PhaseBudget,
    ProductionIdentity,
    RootManifest,
    WriteDelta,
    canonical_json,
    sha256_bytes,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64


def _production_identity(**overrides: object) -> ProductionIdentity:
    fields: dict[str, object] = {
        "pyproject_toml_sha256": _SHA_A,
        "uv_lock_sha256": _SHA_B,
        "package_lock_json_sha256": _SHA_C,
        "dependency_lock_digest": _SHA_D,
        "build_identity": _SHA_E,
        "runtime_paths": (("cli", "/data/x/cli.py"), ("server", "/data/x/server.py")),
    }
    fields.update(overrides)
    return ProductionIdentity(**fields)


def _candidate_package(name: str = "ty", **overrides: object) -> CandidatePackage:
    fields: dict[str, object] = {
        "name": name,
        "version": "0.0.1",
        "requirement": f"{name}==0.0.1",
        "artifact_hashes": ((f"{name}-0.0.1.tar.gz", _SHA_A),),
        "executable_relpath": f"bin/{name}",
    }
    fields.update(overrides)
    return CandidatePackage(**fields)


def _candidate_lock(**overrides: object) -> CandidateLock:
    fields: dict[str, object] = {
        "digest": _SHA_A,
        "exclude_newer": "2026-08-11T00:00:00Z",
        "packages": (_candidate_package("ty"), _candidate_package("pyrefly")),
    }
    fields.update(overrides)
    return CandidateLock(**fields)


def _path_record(path: str = "src/a.py", **overrides: object) -> PathRecord:
    fields: dict[str, object] = {
        "path": path,
        "kind": "file",
        "size": 12,
        "mtime_ns": 1,
        "inode": 1,
        "symlink_target": None,
        "content_sha256": _SHA_A,
    }
    fields.update(overrides)
    return PathRecord(**fields)


def _root_manifest(**overrides: object) -> RootManifest:
    fields: dict[str, object] = {
        "root": "/data/CoordExp/serena-light",
        "kind": "git",
        "inventory_digest": _SHA_A,
        "inventory_count": 1,
        "hashed_paths": (_path_record("src/a.py"),),
        "metadata_paths": (_path_record("model_cache/blob.bin", content_sha256=None),),
        "manifest_digest": _SHA_B,
    }
    fields.update(overrides)
    return RootManifest(**fields)


def _write_delta(**overrides: object) -> WriteDelta:
    fields: dict[str, object] = {
        "root": "/data/CoordExp/serena-light",
        "kind": "git",
        "declared": ("src/a.py",),
        "unexpected": (),
    }
    fields.update(overrides)
    return WriteDelta(**fields)


def _admission_receipt(*, status: str = "pass", after: ProductionIdentity | None = None) -> AdmissionReceipt:
    before = _production_identity()
    return AdmissionReceipt(
        schema_version=1,
        evaluation_identity="eval-2026-08-11-0001",
        status=status,
        started_at="2026-08-11T00:00:00Z",
        ended_at="2026-08-11T00:10:00Z",
        budgets=(DEFAULT_PHASE_BUDGETS["admission"],),
        production_identity_before=before,
        production_identity_after=before if after is None else after,
        candidate_lock=_candidate_lock(),
        root_manifests=(_root_manifest(),),
        write_deltas=(_write_delta(),),
        artifact_tree_digest=_SHA_C,
        issues=(),
        next_action="proceed to protocol phase",
    )


def test_canonical_json_is_sorted_utf8_and_newline_terminated() -> None:
    assert canonical_json({"z": 1, "é": [True, None]}) == b'{"z":1,"\xc3\xa9":[true,null]}\n'


def test_sha256_bytes_returns_lowercase_hex_digest() -> None:
    digest = sha256_bytes(b"hello")
    assert digest == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    assert digest == sha256_bytes(b"hello")


def test_admission_receipt_rejects_unknown_schema() -> None:
    with pytest.raises(ValueError, match="schema_version"):
        AdmissionReceipt.from_dict({"schema_version": 999})


def test_phase_budgets_match_openspec() -> None:
    assert DEFAULT_PHASE_BUDGETS == {  # noqa: SIM300
        "admission": PhaseBudget("admission", 30 * 60),
        "protocol": PhaseBudget("protocol", 90 * 60),
        "product_seam": PhaseBudget("product_seam", 3 * 60 * 60),
        "feature": PhaseBudget("feature", 2 * 60 * 60),
        "agent": PhaseBudget("agent", 8 * 60 * 60),
        "total": PhaseBudget("total", 16 * 60 * 60),
    }


def test_phase_budget_rejects_non_positive_seconds() -> None:
    with pytest.raises(ValueError, match="seconds"):
        PhaseBudget("admission", 0)
    with pytest.raises(ValueError, match="seconds"):
        PhaseBudget("admission", -1)


def test_phase_budget_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="name"):
        PhaseBudget("", 1)


def test_production_identity_rejects_noncanonical_sha256() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _production_identity(build_identity="not-a-digest")
    with pytest.raises(ValueError, match="SHA-256"):
        _production_identity(build_identity=_SHA_A.upper())


def test_production_identity_rejects_duplicate_runtime_path_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _production_identity(runtime_paths=(("cli", "/data/x/cli.py"), ("cli", "/data/x/other.py")))


def test_production_identity_rejects_unsorted_runtime_paths() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _production_identity(runtime_paths=(("server", "/data/x/server.py"), ("cli", "/data/x/cli.py")))


def test_candidate_package_rejects_absolute_executable_relpath() -> None:
    with pytest.raises(ValueError, match="relative"):
        _candidate_package(executable_relpath="/bin/ty")


def test_candidate_package_rejects_duplicate_artifact_hash_filenames() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _candidate_package(artifact_hashes=(("ty-0.0.1.tar.gz", _SHA_A), ("ty-0.0.1.tar.gz", _SHA_B)))


def test_candidate_package_rejects_noncanonical_artifact_hash() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _candidate_package(artifact_hashes=(("ty-0.0.1.tar.gz", "deadbeef"),))


def test_candidate_lock_rejects_duplicate_package_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _candidate_lock(packages=(_candidate_package("ty"), _candidate_package("ty")))


def test_candidate_lock_rejects_empty_packages() -> None:
    with pytest.raises(ValueError, match="empty"):
        _candidate_lock(packages=())


def test_path_record_requires_symlink_target_for_symlink_kind() -> None:
    with pytest.raises(ValueError, match="symlink_target"):
        _path_record(kind="symlink", symlink_target=None, content_sha256=None)


def test_path_record_rejects_symlink_target_for_file_kind() -> None:
    with pytest.raises(ValueError, match="symlink_target"):
        _path_record(kind="file", symlink_target="../elsewhere")


def test_path_record_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        _path_record(kind="directory")


def test_root_manifest_rejects_non_absolute_root() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _root_manifest(root="relative/path")


def test_root_manifest_rejects_duplicate_paths_across_hashed_and_metadata() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _root_manifest(
            hashed_paths=(_path_record("src/a.py"),),
            metadata_paths=(_path_record("src/a.py", content_sha256=None),),
        )


def test_write_delta_rejects_duplicate_declared_paths() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _write_delta(declared=("src/a.py", "src/a.py"))


def test_admission_receipt_rejects_pass_status_with_changed_production_identity() -> None:
    with pytest.raises(ValueError, match="production identity"):
        _admission_receipt(status="pass", after=_production_identity(build_identity=_SHA_C))


def test_admission_receipt_allows_hold_status_with_changed_production_identity() -> None:
    receipt = _admission_receipt(status="hold", after=_production_identity(build_identity=_SHA_C))
    assert receipt.status == "hold"


def test_admission_receipt_round_trips_through_to_dict_and_from_dict() -> None:
    receipt = _admission_receipt()
    assert AdmissionReceipt.from_dict(receipt.to_dict()) == receipt


def test_admission_receipt_to_dict_is_canonical_json_serializable() -> None:
    receipt = _admission_receipt()
    encoded = canonical_json(receipt.to_dict())
    assert encoded.endswith(b"\n")
    assert AdmissionReceipt.from_dict(receipt.to_dict()) == receipt


def test_admission_receipt_from_dict_rejects_unknown_field() -> None:
    payload = _admission_receipt().to_dict()
    payload["unexpected_field"] = "surprise"
    with pytest.raises(ValueError, match="unknown"):
        AdmissionReceipt.from_dict(payload)


def test_admission_receipt_from_dict_rejects_missing_field() -> None:
    payload = _admission_receipt().to_dict()
    del payload["next_action"]
    with pytest.raises(ValueError, match="missing"):
        AdmissionReceipt.from_dict(payload)
