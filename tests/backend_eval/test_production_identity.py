"""Production-identity capture and the byte-identical production invariant."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.backend_eval.models import ProductionIdentity, sha256_bytes
from scripts.backend_eval.production_identity import (
    PRODUCTION_IDENTITY_FILES,
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)
from serena_light.bootstrap import runtime_paths
from serena_light.build_identity import compute_build_identity, dependency_lock_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA_F = "f" * 64


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def _stub_root(tmp_path: Path, *, omit: str | None = None, symlink: str | None = None) -> Path:
    root = tmp_path / "root"
    root.mkdir()
    for name in PRODUCTION_IDENTITY_FILES:
        if name == omit:
            continue
        if name == symlink:
            target = tmp_path / f"external-{name}"
            target.write_bytes(b"payload\n")
            (root / name).symlink_to(target)
            continue
        (root / name).write_bytes(f"{name}\n".encode())
    return root


def test_capture_production_identity_matches_runtime_functions(repo_root: Path) -> None:
    identity = capture_production_identity(repo_root)
    assert identity.dependency_lock_digest == dependency_lock_digest(repo_root)
    assert identity.build_identity == compute_build_identity(repo_root)
    assert dict(identity.runtime_paths) == {
        key: str(value) for key, value in sorted(runtime_paths(repo_root).items())
    }


def test_capture_production_identity_hashes_each_input_separately(repo_root: Path) -> None:
    identity = capture_production_identity(repo_root)
    assert identity.pyproject_toml_sha256 == sha256_bytes((repo_root / "pyproject.toml").read_bytes())
    assert identity.uv_lock_sha256 == sha256_bytes((repo_root / "uv.lock").read_bytes())
    assert identity.package_lock_json_sha256 == sha256_bytes((repo_root / "package-lock.json").read_bytes())
    assert len({identity.pyproject_toml_sha256, identity.uv_lock_sha256, identity.package_lock_json_sha256}) == 3


def test_production_identity_files_cover_the_three_production_inputs() -> None:
    assert PRODUCTION_IDENTITY_FILES == ("package-lock.json", "pyproject.toml", "uv.lock")


def test_capture_production_identity_is_deterministic_and_canonical(repo_root: Path) -> None:
    first = capture_production_identity(repo_root)
    second = capture_production_identity(repo_root)
    assert first == second
    names = [name for name, _ in first.runtime_paths]
    assert names == sorted(names)
    assert all(path.startswith("/") for _, path in first.runtime_paths)
    assert dict(first.runtime_paths)["python"].endswith("/python/bin/python")


def test_capture_production_identity_reads_the_requested_root_only(tmp_path: Path, repo_root: Path) -> None:
    copied = tmp_path / "copy"
    copied.mkdir()
    for name in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(repo_root / name, copied / name)
    shutil.copytree(repo_root / "src", copied / "src")
    identity = capture_production_identity(copied)
    assert identity == capture_production_identity(repo_root)


def test_capture_production_identity_rejects_a_missing_input(tmp_path: Path) -> None:
    root = _stub_root(tmp_path, omit="uv.lock")
    with pytest.raises(ProductionIdentityError, match="uv.lock"):
        capture_production_identity(root)


def test_capture_production_identity_rejects_a_symlinked_input(tmp_path: Path) -> None:
    root = _stub_root(tmp_path, symlink="package-lock.json")
    with pytest.raises(ProductionIdentityError, match="symlink"):
        capture_production_identity(root)


def test_capture_production_identity_rejects_a_root_without_runtime_sources(tmp_path: Path) -> None:
    root = _stub_root(tmp_path)
    with pytest.raises(ProductionIdentityError, match="runtime sources"):
        capture_production_identity(root)


def _identity() -> ProductionIdentity:
    return ProductionIdentity(
        pyproject_toml_sha256="a" * 64,
        uv_lock_sha256="b" * 64,
        package_lock_json_sha256="c" * 64,
        dependency_lock_digest="d" * 64,
        build_identity="e" * 64,
        runtime_paths=(("python", "/data/runtime/python/bin/python"), ("runtime", "/data/runtime")),
    )


def test_identity_guard_accepts_an_unchanged_identity() -> None:
    before = _identity()
    assert_production_identity_unchanged(before, replace(before))


def test_identity_guard_rejects_any_lock_or_runtime_change() -> None:
    before = _identity()
    with pytest.raises(ProductionIdentityChanged, match="uv.lock"):
        assert_production_identity_unchanged(before, replace(before, uv_lock_sha256="f" * 64))


@pytest.mark.parametrize(
    ("field", "value", "label"),
    [
        ("pyproject_toml_sha256", _SHA_F, "pyproject.toml"),
        ("uv_lock_sha256", _SHA_F, "uv.lock"),
        ("package_lock_json_sha256", _SHA_F, "package-lock.json"),
        ("dependency_lock_digest", _SHA_F, "dependency_lock_digest"),
        ("build_identity", _SHA_F, "build_identity"),
        ("runtime_paths", (("python", "/data/other/python"), ("runtime", "/data/runtime")), "runtime_paths"),
    ],
)
def test_identity_guard_rejects_each_field(field: str, value: object, label: str) -> None:
    before = _identity()
    after = replace(before, **{field: value})
    with pytest.raises(ProductionIdentityChanged) as excinfo:
        assert_production_identity_unchanged(before, after)
    assert label in str(excinfo.value)


def test_identity_guard_reports_every_changed_field() -> None:
    before = _identity()
    after = replace(before, uv_lock_sha256=_SHA_F, build_identity=_SHA_F)
    with pytest.raises(ProductionIdentityChanged) as excinfo:
        assert_production_identity_unchanged(before, after)
    message = str(excinfo.value)
    assert "uv.lock" in message
    assert "build_identity" in message
    assert "pyproject.toml" not in message


def test_identity_guard_is_a_production_identity_error() -> None:
    assert issubclass(ProductionIdentityChanged, ProductionIdentityError)
