from pathlib import Path

import pytest

from serena_light import bootstrap
from serena_light.bootstrap import BootstrapError, _assert_owned, _install_or_find_service_python, lock_digest


def test_lock_digest_is_stable_and_sensitive_only_to_resolved_locks(tmp_path: Path) -> None:
    for name in ("pyproject.toml", "uv.lock", "package.json", "package-lock.json"):
        (tmp_path / name).write_text(name)
    before = lock_digest(tmp_path)

    (tmp_path / "pyproject.toml").write_text("developer-tool config changed")
    (tmp_path / "package.json").write_text("changed")
    assert lock_digest(tmp_path) == before

    (tmp_path / "package-lock.json").write_text("changed")
    assert lock_digest(tmp_path) != before


def test_runtime_path_must_exist_below_runtime(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("x")
    with pytest.raises(BootstrapError, match="escaped"):
        _assert_owned(outside, runtime)


def test_service_python_install_is_pinned_below_shared_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = tmp_path / "runtime" / "python"
    interpreter = install_root / "cpython-3.12.12-test" / "bin" / "python3.12"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")
    commands: list[list[str]] = []

    def fake_run(command: list[str], *, env: dict[str, str] | None = None) -> str:
        commands.append(command)
        assert env is not None and env["UV_PYTHON_INSTALL_DIR"] == str(install_root)
        return str(interpreter)

    monkeypatch.setattr(bootstrap, "SERVICE_PYTHON_ROOT", install_root)
    monkeypatch.setattr(bootstrap, "_run", fake_run)

    assert _install_or_find_service_python(Path("/usr/bin/uv"), install=True) == interpreter.resolve()
    assert commands[0] == [
        "/usr/bin/uv",
        "python",
        "install",
        "--install-dir",
        str(install_root),
        "3.12.12",
    ]
    assert len(commands) == 1


def test_existing_service_python_resolution_does_not_require_uv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_root = tmp_path / "runtime" / "python"
    interpreter = install_root / "cpython-3.12.12-test" / "bin" / "python3.12"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("")

    monkeypatch.setattr(bootstrap, "SERVICE_PYTHON_ROOT", install_root)
    monkeypatch.setattr(
        bootstrap,
        "_find_uv",
        lambda: (_ for _ in ()).throw(AssertionError("runtime inspection must not resolve uv")),
    )

    assert _install_or_find_service_python() == interpreter.resolve()
