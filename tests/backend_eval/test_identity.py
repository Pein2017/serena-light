"""Evaluator, host, and bootstrap-environment binding."""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

from scripts.backend_eval.identity import (
    BOOTSTRAP_SERVICE_KEYS,
    EVALUATOR_PACKAGE,
    IdentityError,
    _read_regular_file,
    bootstrap_environment,
    bootstrap_environment_identity,
    bootstrap_service_values,
    capture_evaluator_identity,
)
from scripts.backend_eval.models import sha256_bytes

# --- evaluator identity --------------------------------------------------------------


def test_evaluator_identity_binds_the_executed_source_closure_and_host() -> None:
    identity = capture_evaluator_identity()
    recorded = dict(identity.source_files)
    assert "admission.py" in recorded
    assert "models.py" in recorded
    assert "scripts/__init__.py" in recorded
    assert recorded["models.py"] == sha256_bytes((EVALUATOR_PACKAGE / "models.py").read_bytes())
    assert identity.host_python_path == sys.executable
    assert Path(identity.host_python_realpath).is_file()
    assert identity.host_python_version == ".".join(str(part) for part in sys.version_info[:3])
    assert len(identity.source_digest) == 64


def test_evaluator_identity_records_the_commit_only_with_cleanliness() -> None:
    identity = capture_evaluator_identity()
    assert identity.source_commit is None or len(identity.source_commit) in {40, 64}
    if identity.source_commit is None:
        assert identity.source_clean is False


def test_evaluator_identity_refuses_a_shadowed_module(monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = types.ModuleType("scripts.backend_eval.shadow")
    shadow.__file__ = "/tmp/elsewhere/shadow.py"
    monkeypatch.setitem(sys.modules, "scripts.backend_eval.shadow", shadow)
    with pytest.raises(IdentityError, match="not part of the recorded closure"):
        capture_evaluator_identity()


def test_evaluator_identity_refuses_a_fileless_module(monkeypatch: pytest.MonkeyPatch) -> None:
    shadow = types.ModuleType("scripts.backend_eval.fileless")
    monkeypatch.setitem(sys.modules, shadow.__name__, shadow)
    with pytest.raises(IdentityError, match="has no origin"):
        capture_evaluator_identity()


def test_read_regular_file_refuses_a_fifo_promptly(tmp_path: Path) -> None:
    """``O_RDONLY`` on a FIFO with no writer blocks until one appears.

    The evaluator source and production closures are read through this same guarded open, so
    a FIFO left where a source file belongs must be refused promptly rather than hanging the
    whole evaluator-identity capture.
    """

    fifo = tmp_path / "source.py"
    os.mkfifo(fifo)
    before_fds = len(os.listdir("/proc/self/fd"))

    started = time.monotonic()
    with pytest.raises(IdentityError, match="regular file"):
        _read_regular_file(fifo)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_evaluator_source_digest_changes_with_any_source_byte() -> None:
    first = capture_evaluator_identity()
    second = capture_evaluator_identity()
    assert first.source_digest == second.source_digest
    mutated = tuple(
        (name, "0" * 64) if name == "models.py" else (name, digest) for name, digest in first.source_files
    )
    assert mutated != first.source_files


# --- bootstrap environment ------------------------------------------------------------


def _service(tmp_path: Path) -> dict[str, str]:
    return bootstrap_service_values(
        home=tmp_path / "home",
        tmp=tmp_path / "tmp",
        cache=tmp_path / "cache",
        config=tmp_path / "config",
        uv_cache=tmp_path / "uv",
    )


def test_bootstrap_environment_keeps_the_proxy_and_sheds_everything_else(tmp_path: Path) -> None:
    ambient = {
        "HTTPS_PROXY": "http://127.0.0.1:9090",
        "no_proxy": "localhost,127.0.0.1",
        "SSL_CERT_FILE": "/etc/ssl/certs/ca.pem",
        "LANG": "C.UTF-8",
        "UV_INDEX_URL": "https://mirror.invalid/simple",
        "PIP_INDEX_URL": "https://mirror.invalid/simple",
        "PYTHONPATH": "/data/verl",
        "PATH": "/opt/evil/bin",
        "CONDA_PREFIX": "/root/miniconda3/envs/ms",
        "SOMETHING_ELSE": "kept out",
    }
    env = bootstrap_environment(_service(tmp_path), environ=ambient)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9090"
    assert env["no_proxy"] == "localhost,127.0.0.1"
    assert env["SSL_CERT_FILE"] == "/etc/ssl/certs/ca.pem"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["UV_NO_CONFIG"] == "1"
    assert "UV_INDEX_URL" not in env
    assert "PIP_INDEX_URL" not in env
    assert "PYTHONPATH" not in env
    assert "CONDA_PREFIX" not in env
    assert "SOMETHING_ELSE" not in env
    assert set(env) == set(BOOTSTRAP_SERVICE_KEYS) | {"HTTPS_PROXY", "no_proxy", "SSL_CERT_FILE", "LANG"}


def test_bootstrap_identity_publishes_key_names_and_digests_only(tmp_path: Path) -> None:
    del tmp_path
    secret = "http://user:supersecret@127.0.0.1:9090"
    ambient = {"HTTPS_PROXY": secret, "UV_INDEX_URL": "https://mirror.invalid", "PYTHONPATH": "/data/verl"}
    identity = bootstrap_environment_identity(environ=ambient)
    payload = identity.to_dict()
    assert identity.inherited_keys == ("HTTPS_PROXY",)
    assert identity.inherited_value_digests == (("HTTPS_PROXY", sha256_bytes(secret.encode())),)
    assert identity.refused_keys == ("PYTHONPATH", "UV_INDEX_URL")
    assert identity.service_keys == BOOTSTRAP_SERVICE_KEYS
    assert secret not in repr(payload)
    assert "supersecret" not in repr(payload)


def test_bootstrap_environment_guard_fires_if_the_allowlist_ever_admits_a_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The structural self-check, not the allowlist, is the last line of defence."""

    import scripts.backend_eval.identity as identity_module

    monkeypatch.setattr(identity_module, "BOOTSTRAP_INHERITED_KEYS", ("HTTPS_PROXY", "PIP_INDEX_URL"))
    with pytest.raises(IdentityError, match="prohibited control"):
        bootstrap_environment(_service(tmp_path), environ={"PIP_INDEX_URL": "https://mirror.invalid"})


def test_bootstrap_service_values_reject_an_incomplete_service_half(tmp_path: Path) -> None:
    import scripts.backend_eval.identity as identity_module

    original = identity_module.BOOTSTRAP_SERVICE_KEYS
    identity_module.BOOTSTRAP_SERVICE_KEYS = (*original, "EXTRA_KEY")
    try:
        with pytest.raises(IdentityError, match="service-owned bootstrap keys"):
            _service(tmp_path)
    finally:
        identity_module.BOOTSTRAP_SERVICE_KEYS = original


def test_bootstrap_service_values_declare_the_frozen_key_set(tmp_path: Path) -> None:
    assert tuple(sorted(_service(tmp_path))) == BOOTSTRAP_SERVICE_KEYS


def test_bootstrap_identity_of_the_live_environment_is_publishable() -> None:
    identity = bootstrap_environment_identity()
    assert set(identity.inherited_keys).isdisjoint(identity.refused_keys)
    assert all(len(digest) == 64 for _key, digest in identity.inherited_value_digests)
