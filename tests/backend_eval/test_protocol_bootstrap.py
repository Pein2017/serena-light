"""The protocol phase has one explicit sealed command entry and no disk fallback."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from base64 import b64encode
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.backend_eval_bootstrap as bootstrap
from scripts.backend_eval.source_binding import (
    CHILD_EXECUTED_HELPERS,
    PRODUCTION_CHILD_NAME,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _protocol_owner(tmp_path: Path, protocol_source: str) -> Path:
    owner = tmp_path / "owner"
    package = owner / "scripts" / "backend_eval"
    package.mkdir(parents=True)
    (owner / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "scripts" / "backend_eval_bootstrap.py", owner / "scripts")
    shutil.copytree(REPO_ROOT / "scripts" / "backend_eval", package, dirs_exist_ok=True)
    (package / "protocol_phase.py").write_text(protocol_source, encoding="utf-8")
    shutil.copytree(REPO_ROOT / "src" / "serena_light", owner / "src" / "serena_light")
    for name in ("pyproject.toml", "uv.lock", "package-lock.json"):
        shutil.copy2(REPO_ROOT / name, owner / name)
    return owner


def test_protocol_entry_is_explicit_and_keeps_admission_budget_and_arguments() -> None:
    admission = bootstrap._select_entrypoint(("--repo-root", "/repo"))
    protocol = bootstrap._select_entrypoint(("protocol-phase", "--repo-root", "/repo"))

    assert admission == (
        "scripts.backend_eval.admission",
        ("--repo-root", "/repo"),
        1800.0,
    )
    assert protocol == (
        "scripts.backend_eval.protocol_phase",
        ("--repo-root", "/repo"),
        5400.0,
    )


def test_protocol_dependency_binding_never_falls_back_to_an_ambient_site(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = tmp_path / "host" / "bin" / "python"
    host.parent.mkdir(parents=True)
    host.write_bytes(b"")
    monkeypatch.setattr(bootstrap.sys, "executable", str(host))

    with pytest.raises(bootstrap.EvaluatorBootstrapError, match="host interpreter environment"):
        bootstrap._bound_psutil_sources()


def test_protocol_image_contains_only_the_reachable_evaluator_and_bound_runtime_closures(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "from serena_light.processes import LanguageServerSubprocessLauncher\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    return 0\n",
    )

    image = bootstrap._build_protocol_source_image(owner)

    archive_path = tmp_path / "protocol.pyz"
    archive_path.write_bytes(image.archive)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
    assert {
        "scripts/__init__.py",
        "scripts/backend_eval/__init__.py",
        "scripts/backend_eval/protocol_phase.py",
        "scripts/backend_eval/source_image.py",
    } <= names
    assert "scripts/backend_eval/admission.py" not in names
    assert {"serena_light/__init__.py", "serena_light/processes.py"} <= names
    assert {
        "psutil/__init__.py",
        "psutil/_common.py",
        "psutil/_pslinux.py",
        "psutil/_psposix.py",
    } <= names
    assert tuple(name for name, _payload in image.extensions) == (
        "psutil._psutil_linux",
        "psutil._psutil_posix",
    )


def test_protocol_image_prebinds_nonimported_production_helper_execution_closure(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import psutil\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_binding import HelperExpectation\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    HelperExpectation.from_identity(capture_evaluator_identity())\n"
        "    return 0\n",
    )

    image = bootstrap._build_protocol_source_image(owner)

    with zipfile.ZipFile(io.BytesIO(image.archive)) as archive:
        names = set(archive.namelist())
    assert f"scripts/backend_eval/{PRODUCTION_CHILD_NAME}" in names
    assert {relative.removeprefix("src/") for relative in CHILD_EXECUTED_HELPERS} <= names


def test_sealed_protocol_preflight_builds_helper_expectation_before_any_backend(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_binding import (\n"
        "    CHILD_EXECUTED_HELPERS, HelperExpectation, PRODUCTION_CHILD_NAME,\n"
        ")\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    identity = capture_evaluator_identity()\n"
        "    expectation = HelperExpectation.from_identity(identity)\n"
        "    print(json.dumps({\n"
        "        'child': PRODUCTION_CHILD_NAME in dict(identity.source_files),\n"
        "        'helpers': [name for name, _digest in expectation.closure],\n"
        "    }))\n"
        "    return 0\n",
    )

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "scripts/backend_eval_bootstrap.py",
            "protocol-phase",
        ],
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert json.loads(result.stdout) == {
        "child": True,
        "helpers": list(CHILD_EXECUTED_HELPERS),
    }


def test_protocol_identity_keeps_transient_production_child_bytes_after_restore(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(tmp_path, "")
    child = owner / "scripts" / "backend_eval" / PRODUCTION_CHILD_NAME
    pristine = child.read_bytes()
    hostile = b"# transient protocol child bytes\n" + pristine
    child.write_bytes(hostile)
    (owner / "scripts" / "backend_eval" / "protocol_phase.py").write_text(
        "import base64, json, pathlib, psutil\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_binding import PRODUCTION_CHILD_NAME\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        f"    path = pathlib.Path({str(child)!r})\n"
        f"    path.write_bytes(base64.b64decode({b64encode(pristine).decode()!r}))\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps({\n"
        "        'recorded': dict(identity.source_files)[PRODUCTION_CHILD_NAME],\n"
        "        'clean': identity.source_clean,\n"
        "    }))\n"
        "    return 0\n",
        encoding="utf-8",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(stdout) == {
        "recorded": sha256(hostile).hexdigest(),
        "clean": False,
    }


def test_protocol_child_runs_production_and_psutil_only_from_bound_images(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "ambient-shadow-ran"
    owner = _protocol_owner(
        tmp_path,
        "import json, os, psutil\n"
        "import serena_light.processes as processes\n"
        "from scripts.backend_eval.source_image import (\n"
        "    require_protocol_execution, source_image_deadline_seconds,\n"
        ")\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    print(json.dumps({\n"
        "        'budget': source_image_deadline_seconds(),\n"
        "        'keys': sorted(os.environ),\n"
        "        'psutil_version': psutil.__version__,\n"
        "        'psutil_origin': psutil.__file__,\n"
        "        'psutil_extension_origin': __import__('psutil._psutil_linux', fromlist=['x']).__file__,\n"
        "        'production_origin': processes.__file__,\n"
        "    }))\n"
        "    return 0\n",
    )
    shadow = tmp_path / "shadow" / "psutil"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('shadow')\n",
        encoding="utf-8",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner,
        (),
        timeout=5.0,
        environ={**os.environ, "PYTHONPATH": str(tmp_path / "shadow")},
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert observed["budget"] == 5400.0
    assert observed["keys"] == sorted(
        key for key in bootstrap._INHERITED_KEYS if os.environ.get(key)
    )
    assert observed["psutil_version"] == "7.0.0"
    assert observed["psutil_origin"].startswith("/proc/self/fd/")
    assert observed["psutil_extension_origin"].startswith("/proc/self/fd/")
    assert observed["production_origin"] == str(owner / "src" / "serena_light" / "processes.py")
    assert not marker.exists()


def test_protocol_executes_sealed_production_bytes_with_owner_checkout_path_semantics(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil\n"
        "import serena_light.bootstrap as production_bootstrap\n"
        "import serena_light.lsp.pyright as pyright\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    print(json.dumps({\n"
        "        'repository_root': str(production_bootstrap.repository_root()),\n"
        "        'probe': str(__import__('pathlib').Path(pyright.__file__).with_name(\n"
        "            'pyright_owned_files_probe.mjs'\n"
        "        )),\n"
        "    }))\n"
        "    return 0\n",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(stdout) == {
        "repository_root": str(owner),
        "probe": str(owner / "src" / "serena_light" / "lsp" / "pyright_owned_files_probe.mjs"),
    }


def test_protocol_identity_names_the_executed_production_and_psutil_bytes(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil\n"
        "import serena_light.processes\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps({\n"
        "        'source': dict(identity.source_files),\n"
        "        'production': dict(identity.production_files),\n"
        "        'source_clean': identity.source_clean,\n"
        "        'production_clean': identity.production_clean,\n"
        "    }))\n"
        "    return 0\n",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert {
        "dependencies/psutil/__init__.py",
        "dependencies/psutil/_common.py",
        "dependencies/psutil/_pslinux.py",
        "dependencies/psutil/_psposix.py",
        "dependencies/psutil/_psutil_linux.so",
        "dependencies/psutil/_psutil_posix.so",
    } <= set(observed["source"])
    assert {
        "src/serena_light/__init__.py",
        "src/serena_light/processes.py",
    } <= set(observed["production"])
    assert all(len(digest) == 64 for digest in observed["source"].values())
    assert all(len(digest) == 64 for digest in observed["production"].values())
    assert observed["source_clean"] is False
    assert observed["production_clean"] is False


def test_protocol_identity_keeps_transient_production_bytes_after_the_path_is_restored(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json\n"
        "import serena_light.processes\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.models import sha256_bytes\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    identity = capture_evaluator_identity()\n"
        "    path = __import__('pathlib').Path(identity.production_root) / 'serena_light/processes.py'\n"
        "    print(json.dumps({\n"
        "        'recorded': dict(identity.production_files)['src/serena_light/processes.py'],\n"
        "        'disk': sha256_bytes(path.read_bytes()),\n"
        "        'clean': identity.production_clean,\n"
        "    }))\n"
        "    return 0\n",
    )
    target = owner / "src" / "serena_light" / "processes.py"
    pristine = target.read_bytes()
    future = b"from __future__ import annotations\n"
    restore = (
        "import base64 as _b64, pathlib as _pathlib\n"
        f"_pathlib.Path({str(target)!r}).write_bytes(_b64.b64decode({b64encode(pristine).decode()!r}))\n"
    ).encode()
    hostile = pristine.replace(future, future + restore, 1)
    target.write_bytes(hostile)

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert observed == {
        "recorded": sha256(hostile).hexdigest(),
        "disk": sha256(pristine).hexdigest(),
        "clean": False,
    }


def test_protocol_identity_binds_nested_production_package_initializers(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil\n"
        "import serena_light.lsp.adapter\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps(sorted(dict(identity.production_files))))\n"
        "    return 0\n",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    assert "src/serena_light/lsp/__init__.py" in json.loads(stdout)


def test_protocol_identity_prebinds_reachable_delayed_production_imports(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def delayed():\n"
        "    import psutil\n"
        "    import serena_light.tools.navigation\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    assert 'psutil' not in __import__('sys').modules\n"
        "    assert 'serena_light.tools.navigation' not in __import__('sys').modules\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps({\n"
        "        'production': sorted(dict(identity.production_files)),\n"
        "        'source': sorted(dict(identity.source_files)),\n"
        "    }))\n"
        "    return 0\n",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert "src/serena_light/tools/navigation.py" in observed["production"]
    assert "dependencies/psutil/_psutil_linux.so" in observed["source"]


def test_explicit_protocol_cli_forwards_output_and_uses_the_frozen_origin(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil, sys\n"
        "import serena_light.processes\n"
        "from scripts.backend_eval.source_image import (\n"
        "    require_protocol_execution, source_image_deadline_seconds, source_image_started,\n"
        ")\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    print(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'budget': source_image_deadline_seconds(),\n"
        "        'started': source_image_started(),\n"
        "    }))\n"
        "    return 7\n",
    )
    before = time.monotonic()

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "scripts/backend_eval_bootstrap.py",
            "protocol-phase",
            "--sentinel",
        ],
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )
    after = time.monotonic()

    assert result.returncode == 7, result.stderr.decode("utf-8", "replace")
    observed = json.loads(result.stdout)
    assert observed["argv"] == ["--sentinel"]
    assert observed["budget"] == 5400.0
    assert before <= observed["started"] <= after


@pytest.mark.parametrize(
    "alternate",
    [
        ("-m", "scripts.backend_eval.protocol_phase"),
        (
            "-c",
            "import runpy; runpy.run_module("
            "'scripts.backend_eval.protocol_phase', run_name='__main__')",
        ),
        (
            "-c",
            "import runpy; runpy.run_path("
            "'scripts/backend_eval/protocol_phase.py', run_name='__main__')",
        ),
    ],
)
def test_protocol_disk_module_runpy_and_path_entries_cannot_reach_semantics(
    tmp_path: Path,
    alternate: tuple[str, str],
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    print('receipt-producing semantics reached')\n"
        "    return 0\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n",
    )

    result = subprocess.run(
        [sys.executable, *alternate],
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert b"sealed evaluator bootstrap context is absent" in result.stderr
    assert b"receipt-producing semantics reached" not in result.stdout


def test_protocol_runtime_module_injected_without_an_image_origin_is_refused(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import psutil, sys, types\n"
        "import serena_light.processes\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    sys.modules['serena_light.fileless'] = types.ModuleType('serena_light.fileless')\n"
        "    capture_evaluator_identity()\n"
        "    return 0\n",
    )

    returncode, _stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=10.0, environ=os.environ
    )

    assert returncode != 0
    assert b"has no sealed-image origin" in stderr


def test_protocol_timeout_kills_and_reaps_its_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    owner = _protocol_owner(
        tmp_path,
        "import pathlib, psutil, subprocess, sys, time\n"
        "import serena_light.processes\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"    pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
        "    time.sleep(60)\n"
        "    return 0\n",
    )

    with pytest.raises(bootstrap.EvaluatorBootstrapTimeout):
        bootstrap._run_sealed_protocol(owner, (), timeout=0.5, environ=os.environ)

    grandchild = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3.0
    while Path(f"/proc/{grandchild}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{grandchild}").exists()


def test_protocol_recomputes_remaining_after_dependency_sealing_and_child_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every transport step spends the one outer budget; no stale timeout is reused."""

    monotonic_values = iter((100.0, 101.0, 103.0, 104.0))
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(
        bootstrap,
        "_build_protocol_source_image",
        lambda _owner: bootstrap.ProtocolSourceImage(
            archive=b"archive",
            extensions=(("psutil._psutil_linux", b"linux"), ("psutil._psutil_posix", b"posix")),
        ),
    )
    monkeypatch.setattr(
        bootstrap,
        "_sealed_evaluator_image",
        lambda _payload: os.open("/dev/null", os.O_RDONLY),
    )
    observed: dict[str, float] = {}

    class _Process:
        returncode = 0

        def communicate(self, *, timeout: float) -> tuple[bytes, bytes]:
            observed["timeout"] = timeout
            return b"out", b"err"

    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *_args, **_kwargs: _Process())

    result = bootstrap._run_sealed_protocol(
        Path("/declared-owner"), (), timeout=10.0, environ={}
    )

    assert result == (0, b"out", b"err")
    assert observed["timeout"] == 6.0


def test_protocol_image_imports_the_real_probe_and_lifecycle_closure_without_site_packages(
    tmp_path: Path,
) -> None:
    owner = _protocol_owner(
        tmp_path,
        "import json, psutil\n"
        "from scripts.backend_eval import protocol_lifecycle, pyrefly_probe, pyright_probe, ty_probe\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.source_image import require_protocol_execution\n"
        "def main():\n"
        "    require_protocol_execution()\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps({\n"
        "        'source': sorted(dict(identity.source_files)),\n"
        "        'production': sorted(dict(identity.production_files)),\n"
        "        'psutil': psutil.__version__,\n"
        "    }))\n"
        "    return 0\n",
    )

    returncode, stdout, stderr = bootstrap._run_sealed_protocol(
        owner, (), timeout=15.0, environ=os.environ
    )

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert observed["psutil"] == "7.0.0"
    assert {
        "protocol.py",
        "protocol_lifecycle.py",
        "pyright_probe.py",
        "ty_probe.py",
        "pyrefly_probe.py",
    } <= set(observed["source"])
    assert {
        "src/serena_light/lsp/adapter.py",
        "src/serena_light/lsp/client.py",
        "src/serena_light/lsp/normalize.py",
        "src/serena_light/lsp/pyright.py",
        "src/serena_light/processes.py",
    } <= set(observed["production"])
