from __future__ import annotations

from pathlib import Path

import pytest

from serena_light.build_identity import compute_build_identity, validate_build_identity


def _repository(root: Path) -> Path:
    source = root / "src" / "serena_light"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("VERSION = 1\n")
    for name in ("pyproject.toml", "uv.lock", "package.json", "package-lock.json"):
        (root / name).write_text(name)
    return root


def test_build_identity_is_stable_and_covers_source_lock_schema_and_algorithm(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    baseline = compute_build_identity(root)

    assert compute_build_identity(root) == baseline

    source = root / "src" / "serena_light" / "__init__.py"
    source.write_text("VERSION = 2\n")
    assert compute_build_identity(root) != baseline
    source.write_text("VERSION = 1\n")

    (root / "uv.lock").write_text("changed")
    assert compute_build_identity(root) != baseline
    (root / "uv.lock").write_text("uv.lock")

    (root / "pyproject.toml").write_text("[tool.ty]\nchanged = true\n")
    assert compute_build_identity(root) == baseline

    assert compute_build_identity(root, public_tool_schema_version="2") != baseline
    assert compute_build_identity(root, algorithm_version=3) != baseline


def test_build_identity_sorts_source_paths_independently_of_creation_order(tmp_path: Path) -> None:
    first = _repository(tmp_path / "first")
    second = _repository(tmp_path / "second")
    (first / "src" / "serena_light" / "z.py").write_text("z = 1\n")
    (first / "src" / "serena_light" / "a.py").write_text("a = 1\n")
    (second / "src" / "serena_light" / "a.py").write_text("a = 1\n")
    (second / "src" / "serena_light" / "z.py").write_text("z = 1\n")

    assert compute_build_identity(first) == compute_build_identity(second)


@pytest.mark.parametrize("value", ["", "A" * 64, "0" * 63, "g" * 64])
def test_validate_build_identity_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        validate_build_identity(value)


def test_validate_build_identity_accepts_sha256() -> None:
    value = "a" * 64
    assert validate_build_identity(value) == value
