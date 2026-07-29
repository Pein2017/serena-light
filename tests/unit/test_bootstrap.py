from pathlib import Path

import pytest

from serena_light.bootstrap import BootstrapError, _assert_owned, lock_digest


def test_lock_digest_is_stable_and_sensitive(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "uv.lock", "package.json", "package-lock.json"):
        (tmp_path / name).write_text(name)
    before = lock_digest(tmp_path)
    (tmp_path / "package.json").write_text("changed")
    assert lock_digest(tmp_path) != before


def test_runtime_path_must_exist_below_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("x")
    with pytest.raises(BootstrapError, match="escaped"):
        _assert_owned(outside, runtime)
