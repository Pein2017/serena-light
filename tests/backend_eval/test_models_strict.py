"""Adversarial parsing and structural strictness of the Phase 1 receipt models."""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from scripts.backend_eval.admission import ADMISSION_BUDGET_SECONDS
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    DEFAULT_PHASE_BUDGETS,
    EVALUATION_CONTRACT_VERSION,
    NEXT_ACTION_HOLD,
    NEXT_ACTION_PASS,
    AdmissionReceipt,
    BootstrapEnvironmentIdentity,
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    EvaluatorIdentity,
    LockEvidence,
    PathRecord,
    PhaseBudget,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    RuntimeBinding,
    ServiceConfigIdentity,
    WriteDelta,
    default_phase_budgets,
)

LOCK_DIGEST = "1" * 64
RUNTIME_ROOT = f"/data/runtime/backend-eval/{LOCK_DIGEST}"


# --- builders ---------------------------------------------------------------------


def _production_identity(*, build_identity: str = "b" * 64) -> ProductionIdentity:
    return ProductionIdentity(
        pyproject_toml_sha256="c" * 64,
        uv_lock_sha256="d" * 64,
        package_lock_json_sha256="e" * 64,
        dependency_lock_digest="f" * 64,
        build_identity=build_identity,
        runtime_paths=(("python", "/data/runtime/python"),),
    )


def _evaluator() -> EvaluatorIdentity:
    return EvaluatorIdentity.build(
        source_files=(("admission.py", "a" * 64), ("models.py", "b" * 64)),
        source_commit="9" * 40,
        source_clean=True,
        production_root="/data/CoordExp/serena-light/src",
        production_files=(("src/serena_light/workspace/inventory.py", "d" * 64),),
        production_clean=True,
        host_python_path="/root/miniconda3/envs/ms/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256="c" * 64,
        host_python_version="3.12.11",
    )


def _bootstrap() -> BootstrapEnvironmentIdentity:
    return BootstrapEnvironmentIdentity(
        inherited_keys=("HTTPS_PROXY", "NO_PROXY"),
        inherited_value_digests=(("HTTPS_PROXY", "1" * 64), ("NO_PROXY", "2" * 64)),
        service_keys=("HOME", "PATH", "TMPDIR"),
        refused_keys=("PIP_INDEX_URL", "PYTHONPATH"),
    )


def _lock() -> CandidateLock:
    resolved = (
        ResolvedPackage(name="pyrefly", version="1.2.0", requirement="pyrefly==1.2.0", artifact_hashes=("3" * 64,)),
        ResolvedPackage(name="ty", version="0.0.70", requirement="ty==0.0.70", artifact_hashes=("4" * 64, "5" * 64)),
    )
    candidates = tuple(
        CandidatePackage(
            name=package.name,
            version=package.version,
            requirement=package.requirement,
            artifact_hashes=package.artifact_hashes,
            executable_relpath=f"bin/{package.name}",
        )
        for package in resolved
    )
    return CandidateLock(
        digest=LOCK_DIGEST,
        exclude_newer="2026-08-11T00:00:00Z",
        resolved_packages=resolved,
        candidates=candidates,
        lock_evidence=LockEvidence.build(raw_sha256=LOCK_DIGEST, raw_size=512, resolved_packages=resolved),
    )


def _record(path: str, *, digest: str | None = "9" * 64, kind: str = "file") -> PathRecord:
    return PathRecord(
        path=path,
        kind=kind,
        disposition="tracked",
        size=7,
        mtime_ns=11,
        inode=13,
        symlink_target=None,
        content_sha256=digest,
    )


def _manifest(root: str, *, hashed: tuple[PathRecord, ...] = ()) -> RootManifest:
    hashed = hashed or (_record("pyproject.toml"),)
    return RootManifest.build(
        root=root,
        kind="git",
        source_revision="a" * 40,
        inventory_digest="5" * 64,
        inventory_paths=("pyproject.toml",),
        excluded_paths=(".git",),
        hashed_paths=hashed,
        metadata_paths=(_record("docs", digest=None, kind="directory"),),
    )


def _receipt(**overrides: object) -> AdmissionReceipt:
    before = _manifest("/data/CoordExp/serena-light")
    after = _manifest("/data/CoordExp/serena-light")
    fields: dict[str, object] = {
        "schema_version": ADMISSION_RECEIPT_SCHEMA_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "evaluation_identity": "e" * 64,
        "run_identity": "7" * 64,
        "status": "pass",
        "started_at": "2026-08-11T21:31:13Z",
        "ended_at": "2026-08-11T21:31:19Z",
        "budgets": default_phase_budgets(),
        "evaluator": _evaluator(),
        "bootstrap_environment": _bootstrap(),
        "runtime_binding": RuntimeBinding(
            root=RUNTIME_ROOT,
            lock_digest=LOCK_DIGEST,
            manifest_path=f"{RUNTIME_ROOT}/runtime-manifest.json",
            manifest_sha256="6" * 64,
        ),
        "production_identity_before": _production_identity(),
        "production_identity_after": _production_identity(),
        "candidate_lock": _lock(),
        "environments": (
            EnvironmentIdentity(
                name="llm-framework-study",
                interpreter_path="/root/miniconda3/envs/llm-framework-study/bin/python",
                interpreter_realpath="/root/miniconda3/envs/llm-framework-study/bin/python3.12",
                version="3.12.13",
            ),
            EnvironmentIdentity(
                name="ms",
                interpreter_path="/root/miniconda3/envs/ms/bin/python",
                interpreter_realpath="/root/miniconda3/envs/ms/bin/python3.12",
                version="3.12.11",
            ),
        ),
        "service_configs": tuple(
            ServiceConfigIdentity(
                backend=backend,
                config_path=f"{RUNTIME_ROOT}/config/{backend}/config",
                config_sha256="8" * 64,
                home_path=f"{RUNTIME_ROOT}/home",
                cache_path=f"{RUNTIME_ROOT}/cache",
            )
            for backend in ("pyrefly", "pyright", "ty")
        ),
        "root_manifests_before": (before,),
        "root_manifests_after": (after,),
        "write_deltas": (
            WriteDelta(
                root="/data/CoordExp/serena-light",
                kind="git",
                before_manifest_digest=before.manifest_digest,
                after_manifest_digest=after.manifest_digest,
                declared=(),
                unexpected=(),
                control_changes=(),
            ),
        ),
        "issues": (),
        "artifact_tree_digest": "a" * 64,
        "next_action": NEXT_ACTION_PASS,
    }
    fields.update(overrides)
    return AdmissionReceipt(**cast("dict[str, Any]", fields))


# --- the canonical PASS round trip -------------------------------------------------


def test_a_canonical_pass_receipt_round_trips(tmp_path_factory: pytest.TempPathFactory) -> None:
    del tmp_path_factory
    receipt = _receipt()
    assert AdmissionReceipt.from_dict(receipt.to_dict()) == receipt
    assert receipt.run_identity != receipt.evaluation_identity


# --- C.2 the manifest digest is recomputed -----------------------------------------


def test_root_manifest_digest_is_recomputed_from_its_canonical_fields() -> None:
    manifest = _manifest("/data/ms-swift")
    with pytest.raises(ValueError, match="manifest_digest"):
        replace(manifest, manifest_digest="0" * 64)


def test_forged_inventory_count_fails_even_with_a_retained_digest() -> None:
    manifest = _manifest("/data/ms-swift")
    payload = manifest.to_dict()
    payload["inventory_count"] = 99
    with pytest.raises(ValueError, match="inventory_count"):
        RootManifest.from_dict(payload)


def test_forged_path_record_fails_even_with_a_retained_digest() -> None:
    manifest = _manifest("/data/ms-swift")
    payload = manifest.to_dict()
    records = cast("list[dict[str, Any]]", payload["hashed_paths"])
    records[0]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="manifest_digest"):
        RootManifest.from_dict(payload)


def test_inventory_paths_must_be_carried_by_the_hashed_records() -> None:
    with pytest.raises(ValueError, match="inventory_paths"):
        RootManifest.build(
            root="/data/ms-swift",
            kind="git",
            source_revision="a" * 40,
            inventory_digest="5" * 64,
            inventory_paths=("absent.py",),
            excluded_paths=(),
            hashed_paths=(_record("pyproject.toml"),),
            metadata_paths=(),
        )


def test_directory_and_symlink_records_carry_no_content_hash() -> None:
    with pytest.raises(ValueError, match="content_sha256"):
        _record("docs", kind="directory")


def test_manifest_counts_are_derived_from_the_carried_records() -> None:
    manifest = _manifest("/data/ms-swift")
    assert manifest.in_scope_count == len(manifest.hashed_paths) + len(manifest.metadata_paths)
    assert manifest.excluded_count == 1
    assert manifest.observed_count == manifest.in_scope_count + manifest.excluded_count


# --- C.3 candidate lock structure ---------------------------------------------------


def test_candidate_lock_requires_a_raw_lock_digest_witness() -> None:
    lock = _lock()
    with pytest.raises(ValueError, match="lock_evidence"):
        replace(lock, lock_evidence=replace(lock.lock_evidence, raw_sha256="0" * 64))


def test_candidate_lock_rejects_a_mismatched_candidate_structure() -> None:
    lock = _lock()
    mismatched = tuple(
        replace(candidate, artifact_hashes=("7" * 64,)) if candidate.name == "ty" else candidate
        for candidate in lock.candidates
    )
    with pytest.raises(ValueError, match="does not match its resolved_packages entry"):
        replace(lock, candidates=mismatched)


def test_candidate_lock_rejects_lock_evidence_that_contradicts_the_resolution() -> None:
    lock = _lock()
    forged = replace(lock.lock_evidence, requirement_lines=("ty==0.0.71 sha256:" + "4" * 64,))
    with pytest.raises(ValueError, match="requirement_lines"):
        replace(lock, lock_evidence=forged)


# --- C.1 structurally strict PASS ----------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"issues": ("warning: something",)}, "issues"),
        ({"next_action": NEXT_ACTION_HOLD}, "next_action"),
        ({"started_at": "2026-08-11T21:31:99Z"}, "started_at"),
        ({"started_at": "2026-08-11T21:31:20Z"}, "ended_at"),
        ({"budgets": ()}, "budgets"),
        ({"evaluator": None}, "evaluator"),
        ({"bootstrap_environment": None}, "bootstrap_environment"),
        ({"runtime_binding": None}, "runtime_binding"),
        ({"environments": ()}, "environments"),
        ({"service_configs": ()}, "service_configs"),
        ({"root_manifests_before": ()}, "root manifest"),
        (
            {"production_identity_after": _production_identity(build_identity="0" * 64)},
            "production identity",
        ),
    ],
)
def test_pass_requires_complete_evidence(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _receipt(**overrides)


def test_pass_rejects_a_declared_mutation_in_admission() -> None:
    before = _manifest("/data/CoordExp/serena-light")
    with pytest.raises(ValueError, match="declared"):
        _receipt(
            write_deltas=(
                WriteDelta(
                    root="/data/CoordExp/serena-light",
                    kind="git",
                    before_manifest_digest=before.manifest_digest,
                    after_manifest_digest=before.manifest_digest,
                    declared=("fixture.py",),
                    unexpected=(),
                    control_changes=(),
                ),
            )
        )


def test_pass_rejects_a_changed_manifest_control() -> None:
    before = _manifest("/data/CoordExp/serena-light")
    with pytest.raises(ValueError, match="control_changes"):
        _receipt(
            write_deltas=(
                WriteDelta(
                    root="/data/CoordExp/serena-light",
                    kind="git",
                    before_manifest_digest=before.manifest_digest,
                    after_manifest_digest=before.manifest_digest,
                    declared=(),
                    unexpected=(),
                    control_changes=("inventory_digest",),
                ),
            )
        )


def test_pass_requires_the_exact_phase_budget_set() -> None:
    budgets = tuple(budget for budget in default_phase_budgets() if budget.name != "agent")
    with pytest.raises(ValueError, match="budgets"):
        _receipt(budgets=budgets)


@pytest.mark.parametrize("name", sorted(DEFAULT_PHASE_BUDGETS))
@pytest.mark.parametrize("delta", [1, -1, 3600])
def test_pass_requires_every_budget_to_equal_its_frozen_seconds(name: str, delta: int) -> None:
    """The frozen ceilings are the contract: a pass may not widen or narrow any of them."""

    mutated = tuple(
        PhaseBudget(budget.name, budget.seconds + delta) if budget.name == name else budget
        for budget in default_phase_budgets()
    )
    with pytest.raises(ValueError, match="budgets are not the frozen Phase 1 set"):
        _receipt(budgets=mutated)


def test_a_parsed_pass_receipt_rejects_a_widened_admission_ceiling() -> None:
    """The same mutation is refused when it arrives as published bytes, not as a dataclass."""

    payload = _receipt().to_dict()
    budgets = cast("list[dict[str, object]]", payload["budgets"])
    for budget in budgets:
        if budget["name"] == "admission":
            budget["seconds"] = ADMISSION_BUDGET_SECONDS * 2
    with pytest.raises(ValueError, match="budgets are not the frozen Phase 1 set"):
        AdmissionReceipt.from_dict(payload)


def test_a_pass_receipt_carries_exactly_the_frozen_budgets() -> None:
    assert _receipt().budgets == default_phase_budgets()
    assert {budget.name: budget.seconds for budget in default_phase_budgets()} == {
        name: budget.seconds for name, budget in DEFAULT_PHASE_BUDGETS.items()
    }


def test_pass_requires_the_exact_environment_and_service_config_names() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="environments"):
        _receipt(environments=receipt.environments[:1])
    with pytest.raises(ValueError, match="service_configs"):
        _receipt(service_configs=receipt.service_configs[:2])


def test_hold_receipts_may_carry_incomplete_evidence() -> None:
    receipt = _receipt(
        status="hold",
        next_action=NEXT_ACTION_HOLD,
        issues=("unexpected_evaluation_writes: sample",),
        evaluator=None,
        bootstrap_environment=None,
        runtime_binding=None,
        environments=(),
        service_configs=(),
        root_manifests_before=(),
        root_manifests_after=(),
        write_deltas=(),
    )
    assert receipt.status == "hold"
    assert AdmissionReceipt.from_dict(receipt.to_dict()) == receipt


# --- evaluator and bootstrap identity ------------------------------------------------


def test_evaluator_source_digest_is_recomputed_from_its_file_digests() -> None:
    evaluator = _evaluator()
    with pytest.raises(ValueError, match="source_digest"):
        replace(evaluator, source_digest="0" * 64)
    other = EvaluatorIdentity.build(
        source_files=(("admission.py", "a" * 64), ("models.py", "0" * 64)),
        source_commit="9" * 40,
        source_clean=True,
        production_root=evaluator.production_root,
        production_files=evaluator.production_files,
        production_clean=evaluator.production_clean,
        host_python_path=evaluator.host_python_path,
        host_python_realpath=evaluator.host_python_realpath,
        host_python_sha256=evaluator.host_python_sha256,
        host_python_version=evaluator.host_python_version,
    )
    assert other.source_digest != evaluator.source_digest


def test_bootstrap_environment_records_only_key_names_and_digests() -> None:
    identity = _bootstrap()
    payload = identity.to_dict()
    assert payload["inherited_keys"] == ["HTTPS_PROXY", "NO_PROXY"]
    assert all(len(digest) == 64 for _key, digest in identity.inherited_value_digests)
    with pytest.raises(ValueError, match="inherited"):
        replace(identity, inherited_keys=("HTTPS_PROXY",))
    with pytest.raises(ValueError, match="refused_keys"):
        replace(identity, refused_keys=("HTTPS_PROXY",))
