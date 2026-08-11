"""Canonicalization and validation tests for the backend evaluation models."""

from __future__ import annotations

import pytest

from scripts.backend_eval.models import (
    DEFAULT_PHASE_BUDGETS,
    EVALUATION_CONTRACT_VERSION,
    AdmissionReceipt,
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    PathRecord,
    PhaseBudget,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    ServiceConfigIdentity,
    WriteDelta,
    canonical_json,
    sha256_bytes,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64
_SHA_D = "d" * 64
_SHA_E = "e" * 64
_GIT_REV = "f" * 40


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


def _environment_identity(name: str = "ms", **overrides: object) -> EnvironmentIdentity:
    fields: dict[str, object] = {
        "name": name,
        "interpreter_path": f"/root/miniconda3/envs/{name}/bin/python",
        "interpreter_realpath": f"/root/miniconda3/envs/{name}/bin/python3.12",
        "version": "3.12.11",
    }
    fields.update(overrides)
    return EnvironmentIdentity(**fields)


def _service_config_identity(backend: str = "ty", **overrides: object) -> ServiceConfigIdentity:
    fields: dict[str, object] = {
        "backend": backend,
        "config_path": f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{backend}/config.json",
        "config_sha256": _SHA_A,
        "home_path": f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{backend}/home",
        "cache_path": f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{backend}/cache",
    }
    fields.update(overrides)
    return ServiceConfigIdentity(**fields)


def _resolved_package(name: str, **overrides: object) -> ResolvedPackage:
    fields: dict[str, object] = {
        "name": name,
        "version": "0.0.1",
        "requirement": f"{name}==0.0.1",
        "artifact_hashes": ((f"{name}-0.0.1.tar.gz", _SHA_A),),
    }
    fields.update(overrides)
    return ResolvedPackage(**fields)


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
        "resolved_packages": (
            _resolved_package("click"),
            _resolved_package("pyrefly"),
            _resolved_package("ty"),
        ),
        "candidates": (_candidate_package("pyrefly"), _candidate_package("ty")),
    }
    fields.update(overrides)
    return CandidateLock(**fields)


def _path_record(path: str = "src/a.py", **overrides: object) -> PathRecord:
    fields: dict[str, object] = {
        "path": path,
        "kind": "file",
        "disposition": "tracked",
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
        "source_revision": _GIT_REV,
        "inventory_digest": _SHA_A,
        "inventory_count": 1,
        "hashed_paths": (_path_record("src/a.py"),),
        "metadata_paths": (_path_record("model_cache/blob.bin", disposition="declared", content_sha256=None),),
        "manifest_digest": _SHA_B,
    }
    fields.update(overrides)
    return RootManifest(**fields)


def _write_delta(**overrides: object) -> WriteDelta:
    fields: dict[str, object] = {
        "root": "/data/CoordExp/serena-light",
        "kind": "git",
        "before_manifest_digest": _SHA_B,
        "after_manifest_digest": _SHA_C,
        "declared": ("src/a.py",),
        "unexpected": (),
    }
    fields.update(overrides)
    return WriteDelta(**fields)


def _admission_receipt(
    *, status: str = "pass", after: ProductionIdentity | None = None, **overrides: object
) -> AdmissionReceipt:
    before = _production_identity()
    fields: dict[str, object] = {
        "schema_version": 1,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "evaluation_identity": "eval-2026-08-11-0001",
        "status": status,
        "started_at": "2026-08-11T00:00:00Z",
        "ended_at": "2026-08-11T00:10:00Z",
        "budgets": (DEFAULT_PHASE_BUDGETS["admission"],),
        "production_identity_before": before,
        "production_identity_after": before if after is None else after,
        "candidate_lock": _candidate_lock(),
        "environments": (_environment_identity("llm-framework-study"), _environment_identity("ms")),
        "service_configs": (
            _service_config_identity("pyrefly"),
            _service_config_identity("pyright"),
            _service_config_identity("ty"),
        ),
        "root_manifests": (_root_manifest(),),
        "write_deltas": (_write_delta(),),
        "artifact_tree_digest": _SHA_C,
        "issues": (),
        "next_action": "proceed to protocol phase",
    }
    fields.update(overrides)
    return AdmissionReceipt(**fields)


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


def test_evaluation_contract_version_is_pinned() -> None:
    assert EVALUATION_CONTRACT_VERSION == "python-backend-evaluation-v1"


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


def test_production_identity_rejects_non_tuple_runtime_paths() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _production_identity(runtime_paths=[("cli", "/data/x/cli.py")])


def test_environment_identity_rejects_relative_interpreter_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _environment_identity(interpreter_path="relative/python")


def test_environment_identity_rejects_relative_interpreter_realpath() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _environment_identity(interpreter_realpath="relative/python3.12")


def test_environment_identity_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _environment_identity(name="")


def test_environment_identity_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        _environment_identity(version="")


def test_service_config_identity_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="backend"):
        _service_config_identity(backend="mypy")


def test_service_config_identity_rejects_noncanonical_config_sha256() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _service_config_identity(config_sha256="deadbeef")


def test_service_config_identity_rejects_relative_config_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _service_config_identity(config_path="relative/config.json")


def test_service_config_identity_rejects_relative_home_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _service_config_identity(home_path="relative/home")


def test_resolved_package_rejects_noncanonical_artifact_hash() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _resolved_package("ty", artifact_hashes=(("ty-0.0.1.tar.gz", "deadbeef"),))


def test_resolved_package_rejects_duplicate_artifact_hash_filenames() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _resolved_package("ty", artifact_hashes=(("ty-0.0.1.tar.gz", _SHA_A), ("ty-0.0.1.tar.gz", _SHA_B)))


def test_resolved_package_rejects_unsorted_artifact_hashes() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _resolved_package("ty", artifact_hashes=(("z.tar.gz", _SHA_A), ("a.tar.gz", _SHA_B)))


def test_resolved_package_rejects_non_tuple_artifact_hashes() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _resolved_package("ty", artifact_hashes=[("ty-0.0.1.tar.gz", _SHA_A)])


def test_candidate_package_rejects_absolute_executable_relpath() -> None:
    with pytest.raises(ValueError, match="relative"):
        _candidate_package(executable_relpath="/bin/ty")


def test_candidate_package_rejects_duplicate_artifact_hash_filenames() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _candidate_package(artifact_hashes=(("ty-0.0.1.tar.gz", _SHA_A), ("ty-0.0.1.tar.gz", _SHA_B)))


def test_candidate_package_rejects_noncanonical_artifact_hash() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _candidate_package(artifact_hashes=(("ty-0.0.1.tar.gz", "deadbeef"),))


def test_candidate_package_rejects_unsorted_artifact_hashes() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _candidate_package(artifact_hashes=(("z.tar.gz", _SHA_A), ("a.tar.gz", _SHA_B)))


def test_candidate_package_rejects_non_tuple_artifact_hashes() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _candidate_package(artifact_hashes=[("ty-0.0.1.tar.gz", _SHA_A)])


def test_candidate_lock_rejects_duplicate_resolved_package_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _candidate_lock(
            resolved_packages=(_resolved_package("ty"), _resolved_package("ty"), _resolved_package("pyrefly"))
        )


def test_candidate_lock_rejects_empty_resolved_packages() -> None:
    with pytest.raises(ValueError, match="empty"):
        _candidate_lock(resolved_packages=())


def test_candidate_lock_rejects_unsorted_resolved_packages() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _candidate_lock(resolved_packages=(_resolved_package("ty"), _resolved_package("pyrefly")))


def test_candidate_lock_rejects_non_tuple_resolved_packages() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _candidate_lock(resolved_packages=[_resolved_package("pyrefly"), _resolved_package("ty")])


def test_candidate_lock_requires_both_ty_and_pyrefly_candidates() -> None:
    with pytest.raises(ValueError, match="candidates"):
        _candidate_lock(candidates=(_candidate_package("ty"),))


def test_candidate_lock_rejects_extra_candidate() -> None:
    with pytest.raises(ValueError, match="candidates"):
        _candidate_lock(
            resolved_packages=(
                _resolved_package("click"),
                _resolved_package("pyrefly"),
                _resolved_package("ty"),
            ),
            candidates=(
                _candidate_package("click"),
                _candidate_package("pyrefly"),
                _candidate_package("ty"),
            ),
        )


def test_candidate_lock_rejects_unsorted_candidates() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _candidate_lock(candidates=(_candidate_package("ty"), _candidate_package("pyrefly")))


def test_candidate_lock_rejects_candidate_inconsistent_with_resolved_entry() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _candidate_lock(candidates=(_candidate_package("pyrefly"), _candidate_package("ty", version="9.9.9")))


def test_path_record_requires_symlink_target_for_symlink_kind() -> None:
    with pytest.raises(ValueError, match="symlink_target"):
        _path_record(kind="symlink", symlink_target=None, content_sha256=None)


def test_path_record_rejects_symlink_target_for_file_kind() -> None:
    with pytest.raises(ValueError, match="symlink_target"):
        _path_record(kind="file", symlink_target="../elsewhere")


def test_path_record_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        _path_record(kind="directory")


def test_path_record_rejects_unknown_disposition() -> None:
    with pytest.raises(ValueError, match="disposition"):
        _path_record(disposition="mutated")


def test_root_manifest_rejects_non_absolute_root() -> None:
    with pytest.raises(ValueError, match="absolute"):
        _root_manifest(root="relative/path")


def test_root_manifest_rejects_duplicate_paths_across_hashed_and_metadata() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _root_manifest(
            hashed_paths=(_path_record("src/a.py"),),
            metadata_paths=(_path_record("src/a.py", disposition="declared", content_sha256=None),),
        )


def test_root_manifest_requires_git_source_revision_when_kind_is_git() -> None:
    with pytest.raises(ValueError, match="source_revision"):
        _root_manifest(source_revision=None)


def test_root_manifest_rejects_malformed_git_source_revision() -> None:
    with pytest.raises(ValueError, match="source_revision"):
        _root_manifest(source_revision="not-a-revision")


def test_root_manifest_rejects_source_revision_when_kind_is_non_git() -> None:
    with pytest.raises(ValueError, match="source_revision"):
        _root_manifest(kind="non_git", source_revision=_GIT_REV)


def test_root_manifest_rejects_hashed_path_without_content_sha256() -> None:
    with pytest.raises(ValueError, match="content_sha256"):
        _root_manifest(hashed_paths=(_path_record("src/a.py", content_sha256=None),))


def test_root_manifest_allows_metadata_path_with_content_sha256() -> None:
    manifest = _root_manifest(
        metadata_paths=(_path_record("model_cache/blob.bin", disposition="declared", content_sha256=_SHA_C),)
    )
    assert manifest.metadata_paths[0].content_sha256 == _SHA_C


def test_root_manifest_rejects_unsorted_hashed_paths() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _root_manifest(hashed_paths=(_path_record("z.py"), _path_record("a.py")))


def test_root_manifest_rejects_non_tuple_hashed_paths() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _root_manifest(hashed_paths=[_path_record("src/a.py")])


def test_write_delta_rejects_duplicate_declared_paths() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _write_delta(declared=("src/a.py", "src/a.py"))


def test_write_delta_rejects_unsorted_declared_paths() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _write_delta(declared=("z.py", "a.py"))


def test_write_delta_rejects_non_tuple_declared() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _write_delta(declared=["src/a.py"])


def test_write_delta_rejects_noncanonical_before_manifest_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _write_delta(before_manifest_digest="deadbeef")


def test_write_delta_rejects_noncanonical_after_manifest_digest() -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        _write_delta(after_manifest_digest="deadbeef")


def test_admission_receipt_rejects_pass_status_with_changed_production_identity() -> None:
    with pytest.raises(ValueError, match="production identity"):
        _admission_receipt(status="pass", after=_production_identity(build_identity=_SHA_C))


def test_admission_receipt_allows_hold_status_with_changed_production_identity() -> None:
    receipt = _admission_receipt(status="hold", after=_production_identity(build_identity=_SHA_C))
    assert receipt.status == "hold"


def test_admission_receipt_rejects_wrong_evaluation_contract_version() -> None:
    with pytest.raises(ValueError, match="evaluation_contract_version"):
        _admission_receipt(evaluation_contract_version="python-backend-evaluation-v2")


def test_admission_receipt_rejects_non_tuple_budgets() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _admission_receipt(budgets=[DEFAULT_PHASE_BUDGETS["admission"]])


def test_admission_receipt_rejects_unsorted_budgets() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(budgets=(DEFAULT_PHASE_BUDGETS["protocol"], DEFAULT_PHASE_BUDGETS["admission"]))


def test_admission_receipt_rejects_non_tuple_environments() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _admission_receipt(environments=[_environment_identity("ms"), _environment_identity("llm-framework-study")])


def test_admission_receipt_rejects_unsorted_environments() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(environments=(_environment_identity("ms"), _environment_identity("llm-framework-study")))


def test_admission_receipt_rejects_duplicate_environment_names() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _admission_receipt(environments=(_environment_identity("ms"), _environment_identity("ms")))


def test_admission_receipt_rejects_non_tuple_service_configs() -> None:
    with pytest.raises(ValueError, match="tuple"):
        _admission_receipt(service_configs=[_service_config_identity("ty")])


def test_admission_receipt_rejects_unsorted_service_configs() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(
            service_configs=(
                _service_config_identity("ty"),
                _service_config_identity("pyright"),
                _service_config_identity("pyrefly"),
            )
        )


def test_admission_receipt_rejects_duplicate_service_config_backends() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _admission_receipt(service_configs=(_service_config_identity("ty"), _service_config_identity("ty")))


def test_admission_receipt_rejects_unsorted_root_manifests() -> None:
    first = _root_manifest(root="/data/ms-swift", manifest_digest=_SHA_D)
    second = _root_manifest()
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(status="hold", root_manifests=(first, second))


def test_admission_receipt_rejects_duplicate_root_manifest_roots() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _admission_receipt(
            status="hold", root_manifests=(_root_manifest(), _root_manifest(manifest_digest=_SHA_D))
        )


def test_admission_receipt_rejects_unsorted_write_deltas() -> None:
    first = _write_delta(root="/data/ms-swift")
    second = _write_delta()
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(status="hold", write_deltas=(first, second))


def test_admission_receipt_rejects_duplicate_write_delta_roots() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _admission_receipt(
            status="hold", write_deltas=(_write_delta(), _write_delta(after_manifest_digest=_SHA_D))
        )


def test_admission_receipt_rejects_unsorted_issues() -> None:
    with pytest.raises(ValueError, match="sorted"):
        _admission_receipt(issues=("zeta issue", "alpha issue"))


def test_admission_receipt_rejects_duplicate_issues() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        _admission_receipt(issues=("same issue", "same issue"))


def test_admission_receipt_pass_requires_root_manifest_roots_match_write_delta_roots() -> None:
    other_manifest = _root_manifest(root="/data/ms-swift", manifest_digest=_SHA_D)
    with pytest.raises(ValueError, match="root_manifests"):
        _admission_receipt(root_manifests=(other_manifest,))


def test_admission_receipt_pass_requires_before_manifest_digest_matches_root_manifest() -> None:
    with pytest.raises(ValueError, match="before_manifest_digest"):
        _admission_receipt(write_deltas=(_write_delta(before_manifest_digest=_SHA_D),))


def test_admission_receipt_allows_incomplete_status_with_mismatched_manifest_roots() -> None:
    other_manifest = _root_manifest(root="/data/ms-swift", manifest_digest=_SHA_D)
    receipt = _admission_receipt(status="incomplete", root_manifests=(other_manifest,))
    assert receipt.status == "incomplete"


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
