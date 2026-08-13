"""Exact immutable Phase 1 parent binding for the Phase 2 protocol gate."""

from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from scripts.backend_eval import protocol_parent
from scripts.backend_eval.models import (
    ADMISSION_RECEIPT_SCHEMA_VERSION,
    EVALUATION_CONTRACT_VERSION,
    NEXT_ACTION_HOLD,
    NEXT_ACTION_PASS,
    AdmissionBinding,
    AdmissionReceipt,
    AdmissionRootWitness,
    BootstrapEnvironmentIdentity,
    CandidateLock,
    CandidatePackage,
    EnvironmentIdentity,
    EvaluatorIdentity,
    LockEvidence,
    PathRecord,
    ProductionIdentity,
    ResolvedPackage,
    RootManifest,
    RuntimeBinding,
    ServiceConfigIdentity,
    WriteDelta,
    canonical_json,
    default_phase_budgets,
    sha256_bytes,
)
from scripts.backend_eval.process import Deadline, DeadlineExceeded, monotonic_clock
from scripts.backend_eval.protocol_parent import (
    ParentAdmissionError,
    ParentAdmissionExpectation,
)
from scripts.backend_eval.protocol_parent import (
    load_parent_admission as _load_parent_admission,
)

_EVALUATION_ID = "a" * 64
_RUN_ID = "b" * 64
_LOCK_DIGEST = "c" * 64
_RUNTIME_MANIFEST_DIGEST = "d" * 64
_ARTIFACT_TREE_DIGEST = "e" * 64
_DEPENDENCY_LOCK_DIGEST = "f" * 64
_BUILD_IDENTITY = "0" * 64
_PRODUCTION_REVISION = "1" * 40
_EVALUATOR_REVISION = "2" * 40
_PRODUCTION_ROOT = "/data/CoordExp/serena-light"


def load_parent_admission(
    expectation: ParentAdmissionExpectation,
) -> protocol_parent.LoadedParentAdmission:
    return _load_parent_admission(
        expectation,
        deadline=Deadline.start(monotonic_clock, 30.0),
    )


class _InjectedDeadline:
    def __init__(self, fail_step: str) -> None:
        self.fail_step = fail_step
        self.steps: list[str] = []

    def check(self, step: str) -> None:
        self.steps.append(step)
        if step == self.fail_step:
            raise DeadlineExceeded(f"step={step} injected deadline")


def _resolved_package(name: str) -> ResolvedPackage:
    return ResolvedPackage(
        name=name,
        version="0.0.1",
        requirement=f"{name}==0.0.1",
        artifact_hashes=("3" * 64,),
    )


def _candidate_package(name: str) -> CandidatePackage:
    return CandidatePackage(
        name=name,
        version="0.0.1",
        requirement=f"{name}==0.0.1",
        artifact_hashes=("3" * 64,),
        executable_relpath=f"bin/{name}",
    )


def _candidate_lock() -> CandidateLock:
    packages = (
        _resolved_package("click"),
        _resolved_package("pyrefly"),
        _resolved_package("ty"),
    )
    return CandidateLock(
        digest=_LOCK_DIGEST,
        exclude_newer="2026-08-12T00:00:00Z",
        resolved_packages=packages,
        candidates=(_candidate_package("pyrefly"), _candidate_package("ty")),
        lock_evidence=LockEvidence.build(
            raw_sha256=_LOCK_DIGEST,
            raw_size=512,
            resolved_packages=packages,
        ),
    )


def _production_identity() -> ProductionIdentity:
    return ProductionIdentity(
        pyproject_toml_sha256="4" * 64,
        uv_lock_sha256="5" * 64,
        package_lock_json_sha256="6" * 64,
        dependency_lock_digest=_DEPENDENCY_LOCK_DIGEST,
        build_identity=_BUILD_IDENTITY,
        runtime_paths=(
            ("cli", f"{_PRODUCTION_ROOT}/src/serena_light/cli.py"),
            ("server", f"{_PRODUCTION_ROOT}/src/serena_light/server.py"),
        ),
    )


def _evaluator_identity() -> EvaluatorIdentity:
    return EvaluatorIdentity.build(
        source_files=(("admission.py", "7" * 64), ("models.py", "8" * 64)),
        source_commit=_EVALUATOR_REVISION,
        source_clean=True,
        production_root=f"{_PRODUCTION_ROOT}/src",
        production_files=(("src/serena_light/build_identity.py", "9" * 64),),
        production_clean=True,
        host_python_path="/root/miniconda3/envs/ms/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256="a" * 64,
        host_python_version="3.12.11",
    )


def _runtime_binding() -> RuntimeBinding:
    root = f"/data/CoordExp/.codex/runtime/serena-light/backend-eval/{_LOCK_DIGEST}"
    return RuntimeBinding(
        root=root,
        lock_digest=_LOCK_DIGEST,
        manifest_path=f"{root}/runtime-manifest.json",
        manifest_sha256=_RUNTIME_MANIFEST_DIGEST,
    )


def _path_record() -> PathRecord:
    return PathRecord(
        path="src/a.py",
        kind="file",
        disposition="tracked",
        size=12,
        mtime_ns=1,
        inode=1,
        symlink_target=None,
        content_sha256="b" * 64,
    )


def _root_manifest() -> RootManifest:
    return RootManifest.build(
        root=_PRODUCTION_ROOT,
        kind="git",
        source_revision=_PRODUCTION_REVISION,
        inventory_digest="c" * 64,
        inventory_paths=("src/a.py",),
        excluded_paths=(".git",),
        hashed_paths=(_path_record(),),
        metadata_paths=(),
    )


def _admission_receipt(**overrides: object) -> AdmissionReceipt:
    production = _production_identity()
    manifest = _root_manifest()
    fields: dict[str, object] = {
        "schema_version": ADMISSION_RECEIPT_SCHEMA_VERSION,
        "evaluation_contract_version": EVALUATION_CONTRACT_VERSION,
        "evaluation_identity": _EVALUATION_ID,
        "run_identity": _RUN_ID,
        "status": "pass",
        "started_at": "2026-08-12T00:00:00Z",
        "ended_at": "2026-08-12T00:00:17Z",
        "budgets": default_phase_budgets(),
        "evaluator": _evaluator_identity(),
        "bootstrap_environment": BootstrapEnvironmentIdentity(
            inherited_keys=("HTTPS_PROXY",),
            inherited_value_digests=(("HTTPS_PROXY", "d" * 64),),
            service_keys=("HOME", "PATH"),
            refused_keys=("PIP_INDEX_URL",),
        ),
        "runtime_binding": _runtime_binding(),
        "production_identity_before": production,
        "production_identity_after": production,
        "candidate_lock": _candidate_lock(),
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
                config_path=f"{_runtime_binding().root}/config/{backend}/config",
                config_sha256=character * 64,
                home_path=f"{_runtime_binding().root}/home",
                cache_path=f"{_runtime_binding().root}/cache",
            )
            for backend, character in (("pyrefly", "e"), ("pyright", "f"), ("ty", "0"))
        ),
        "root_manifests_before": (manifest,),
        "root_manifests_after": (manifest,),
        "write_deltas": (
            WriteDelta(
                root=manifest.root,
                kind=manifest.kind,
                before_manifest_digest=manifest.manifest_digest,
                after_manifest_digest=manifest.manifest_digest,
                declared=(),
                unexpected=(),
                control_changes=(),
            ),
        ),
        "issues": (),
        "artifact_tree_digest": _ARTIFACT_TREE_DIGEST,
        "next_action": NEXT_ACTION_PASS,
    }
    fields.update(overrides)
    return AdmissionReceipt(**cast("dict[str, Any]", fields))


def _receipt_path(artifact_root: Path) -> Path:
    return artifact_root / _EVALUATION_ID / "receipts" / f"{_RUN_ID}.json"


def _publish_fixture(
    artifact_root: Path,
    receipt: AdmissionReceipt,
    *,
    payload: bytes | None = None,
    mode: int = 0o600,
) -> tuple[Path, bytes]:
    path = _receipt_path(artifact_root)
    path.parent.mkdir(parents=True)
    exact = canonical_json(receipt.to_dict()) if payload is None else payload
    path.write_bytes(exact)
    path.chmod(mode)
    return path, exact


def _expectation(artifact_root: Path, payload: bytes, **overrides: object) -> ParentAdmissionExpectation:
    fields: dict[str, object] = {
        "artifact_root": artifact_root,
        "evaluation_identity": _EVALUATION_ID,
        "run_identity": _RUN_ID,
        "receipt_sha256": sha256_bytes(payload),
        "artifact_tree_digest": _ARTIFACT_TREE_DIGEST,
        "candidate_lock_digest": _LOCK_DIGEST,
        "runtime_manifest_sha256": _RUNTIME_MANIFEST_DIGEST,
        "production_root": Path(_PRODUCTION_ROOT),
        "production_source_revision": _PRODUCTION_REVISION,
        "production_dependency_lock_digest": _DEPENDENCY_LOCK_DIGEST,
        "production_build_identity": _BUILD_IDENTITY,
    }
    fields.update(overrides)
    return ParentAdmissionExpectation(**cast("dict[str, Any]", fields))


def _binding(receipt_path: Path) -> AdmissionBinding:
    return AdmissionBinding(
        admission_evaluation_identity=_EVALUATION_ID,
        admission_run_identity=_RUN_ID,
        receipt_path=str(receipt_path),
        receipt_sha256="2" * 64,
        artifact_tree_digest=_ARTIFACT_TREE_DIGEST,
        candidate_lock_digest=_LOCK_DIGEST,
        runtime_root=_runtime_binding().root,
        runtime_manifest_sha256=_RUNTIME_MANIFEST_DIGEST,
        production_root=_PRODUCTION_ROOT,
        production_source_revision=_PRODUCTION_REVISION,
        production_dependency_lock_digest=_DEPENDENCY_LOCK_DIGEST,
        production_build_identity=_BUILD_IDENTITY,
        parent_root_manifests=(
            AdmissionRootWitness(
                root=_PRODUCTION_ROOT,
                kind="git",
                source_revision=_PRODUCTION_REVISION,
                manifest_digest=_root_manifest().manifest_digest,
            ),
        ),
    )


def test_admission_binding_round_trips_canonically(tmp_path: Path) -> None:
    binding = _binding(_receipt_path(tmp_path))

    parsed = AdmissionBinding.from_dict(json.loads(canonical_json(binding.to_dict())))

    assert parsed == binding
    assert canonical_json(parsed.to_dict()) == canonical_json(binding.to_dict())


def test_admission_binding_rejects_paths_that_do_not_name_its_exact_ids(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="receipt_path"):
        replace(_binding(_receipt_path(tmp_path)), receipt_path=str(tmp_path / "latest.json"))


@pytest.mark.parametrize(
    "field",
    ["receipt_path", "runtime_root", "production_root"],
)
def test_admission_binding_rejects_noncanonical_absolute_paths(
    tmp_path: Path, field: str
) -> None:
    binding = _binding(_receipt_path(tmp_path))
    value = cast("str", getattr(binding, field))

    with pytest.raises(ValueError, match="canonical absolute path"):
        replace(binding, **{field: f"//{value.removeprefix('/')}"})


def test_admission_binding_from_dict_rejects_missing_and_unknown_fields(tmp_path: Path) -> None:
    payload = _binding(_receipt_path(tmp_path)).to_dict()
    missing = dict(payload)
    del missing["receipt_sha256"]
    with pytest.raises(ValueError, match="missing required fields"):
        AdmissionBinding.from_dict(missing)

    unknown = dict(payload)
    unknown["latest"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        AdmissionBinding.from_dict(unknown)


def test_admission_binding_from_dict_rejects_parent_identity_path_mismatch(
    tmp_path: Path,
) -> None:
    payload = _binding(_receipt_path(tmp_path)).to_dict()
    payload["admission_run_identity"] = "4" * 64

    with pytest.raises(ValueError, match="receipt_path"):
        AdmissionBinding.from_dict(payload)


def test_load_parent_admission_binds_the_exact_explicit_receipt(tmp_path: Path) -> None:
    path, payload = _publish_fixture(tmp_path, _admission_receipt())

    loaded = load_parent_admission(_expectation(tmp_path, payload))

    assert loaded.receipt == _admission_receipt()
    assert loaded.binding == replace(_binding(path), receipt_sha256=sha256_bytes(payload))


def test_admission_binding_requires_parent_production_root_witness(tmp_path: Path) -> None:
    binding = _binding(_receipt_path(tmp_path))
    unrelated = AdmissionRootWitness(
        root="/data/unrelated",
        kind="git",
        source_revision=_PRODUCTION_REVISION,
        manifest_digest="4" * 64,
    )

    with pytest.raises(ValueError, match="exact Git production root"):
        replace(binding, parent_root_manifests=(unrelated,))


def test_parent_expectation_rejects_noncanonical_artifact_root() -> None:
    with pytest.raises(ValueError, match="canonical absolute path"):
        _expectation(Path("//data/CoordExp/serena-light"), b"receipt")


def test_load_parent_admission_keeps_evaluator_helper_origin_distinct_from_production_corpus(
    tmp_path: Path,
) -> None:
    evaluator = replace(
        _evaluator_identity(),
        production_root="/data/CoordExp/.worktrees/backend-evaluator/src",
    )
    receipt = _admission_receipt(evaluator=evaluator)
    _, payload = _publish_fixture(tmp_path, receipt)

    loaded = load_parent_admission(_expectation(tmp_path, payload))

    assert loaded.receipt.evaluator == evaluator
    assert loaded.binding.production_root == _PRODUCTION_ROOT


def test_load_parent_admission_never_scans_for_a_latest_receipt(tmp_path: Path) -> None:
    other_run = "3" * 64
    other = replace(_admission_receipt(), run_identity=other_run)
    path = tmp_path / _EVALUATION_ID / "receipts" / f"{other_run}.json"
    path.parent.mkdir(parents=True)
    payload = canonical_json(other.to_dict())
    path.write_bytes(payload)
    path.chmod(0o600)

    with pytest.raises(ParentAdmissionError, match="exact parent admission receipt"):
        load_parent_admission(_expectation(tmp_path, payload))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("receipt_sha256", "4" * 64, "receipt SHA-256"),
        ("artifact_tree_digest", "4" * 64, "artifact tree"),
        ("candidate_lock_digest", "4" * 64, "candidate lock"),
        ("runtime_manifest_sha256", "4" * 64, "runtime manifest"),
        ("production_source_revision", "4" * 40, "production source revision"),
        ("production_dependency_lock_digest", "4" * 64, "dependency lock"),
        ("production_build_identity", "4" * 64, "build identity"),
    ],
)
def test_load_parent_admission_rejects_every_expected_identity_mismatch(
    tmp_path: Path,
    field: str,
    value: str,
    message: str,
) -> None:
    _, payload = _publish_fixture(tmp_path, _admission_receipt())

    with pytest.raises(ParentAdmissionError, match=message):
        load_parent_admission(_expectation(tmp_path, payload, **{field: value}))


def test_load_parent_admission_rejects_receipt_ids_that_disagree_with_the_exact_path(
    tmp_path: Path,
) -> None:
    receipt = replace(_admission_receipt(), run_identity="4" * 64)
    path, payload = _publish_fixture(tmp_path, receipt)
    assert path.name == f"{_RUN_ID}.json"

    with pytest.raises(ParentAdmissionError, match="run identity"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_non_pass_authority(tmp_path: Path) -> None:
    receipt = replace(
        _admission_receipt(),
        status="hold",
        issues=("held",),
        next_action=NEXT_ACTION_HOLD,
    )
    _, payload = _publish_fixture(tmp_path, receipt)

    with pytest.raises(ParentAdmissionError, match="status.*pass"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_dirty_evaluator_authority(tmp_path: Path) -> None:
    receipt = replace(
        _admission_receipt(),
        evaluator=replace(_evaluator_identity(), source_clean=False),
    )
    _, payload = _publish_fixture(tmp_path, receipt)

    with pytest.raises(ParentAdmissionError, match="clean evaluator"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_noncanonical_receipt_bytes(tmp_path: Path) -> None:
    receipt = _admission_receipt()
    payload = (json.dumps(receipt.to_dict(), sort_keys=True, indent=2) + "\n").encode()
    _publish_fixture(tmp_path, receipt, payload=payload)

    with pytest.raises(ParentAdmissionError, match="canonical"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_malformed_receipt_bytes(tmp_path: Path) -> None:
    payload = b"{not-json}\n"
    _publish_fixture(tmp_path, _admission_receipt(), payload=payload)

    with pytest.raises(ParentAdmissionError, match="decode"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_open_filesystem_root_preserves_typed_unsafe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse_open(_path: str, _flags: int) -> int:
        raise OSError("root refused")

    monkeypatch.setattr(protocol_parent.os, "open", refuse_open)

    with pytest.raises(ParentAdmissionError, match="parent_receipt_unsafe.*filesystem root"):
        protocol_parent._open_filesystem_root(
            deadline=Deadline.start(monotonic_clock, 30.0)
        )


def test_load_parent_admission_propagates_deadline_during_chunked_read(
    tmp_path: Path,
) -> None:
    _, payload = _publish_fixture(tmp_path, _admission_receipt())
    injected = _InjectedDeadline("parent receipt read after")

    with pytest.raises(DeadlineExceeded, match="parent receipt read after"):
        _load_parent_admission(
            _expectation(tmp_path, payload),
            deadline=cast("Deadline", injected),
        )

    assert "parent receipt read before" in injected.steps


def test_load_parent_admission_rejects_symlink_receipt(tmp_path: Path) -> None:
    path, payload = _publish_fixture(tmp_path, _admission_receipt())
    target = tmp_path / "outside.json"
    path.replace(target)
    path.symlink_to(target)

    with pytest.raises(ParentAdmissionError, match="without following a link"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_fifo_without_blocking(tmp_path: Path) -> None:
    path, payload = _publish_fixture(tmp_path, _admission_receipt())
    path.unlink()
    os.mkfifo(path, 0o600)

    started = time.monotonic()
    with pytest.raises(ParentAdmissionError, match="regular file"):
        load_parent_admission(_expectation(tmp_path, payload))
    assert time.monotonic() - started < 1.0


def test_load_parent_admission_rejects_symlinked_ancestor(tmp_path: Path) -> None:
    path, payload = _publish_fixture(tmp_path, _admission_receipt())
    evaluation = path.parents[1]
    outside = tmp_path / "outside"
    evaluation.replace(outside)
    evaluation.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ParentAdmissionError, match="without following a link"):
        load_parent_admission(_expectation(tmp_path, payload))


@pytest.mark.parametrize("mode", [0o400, 0o640, 0o644])
def test_load_parent_admission_rejects_unexpected_file_authority(
    tmp_path: Path,
    mode: int,
) -> None:
    _, payload = _publish_fixture(tmp_path, _admission_receipt(), mode=mode)

    with pytest.raises(ParentAdmissionError, match="mode 0600"):
        load_parent_admission(_expectation(tmp_path, payload))


def test_load_parent_admission_rejects_multiply_linked_receipt_authority(
    tmp_path: Path,
) -> None:
    path, payload = _publish_fixture(tmp_path, _admission_receipt())
    os.link(path, tmp_path / "other-receipt-name.json")

    with pytest.raises(ParentAdmissionError, match="unexpected authority"):
        load_parent_admission(_expectation(tmp_path, payload))
