"""The command entrypoint seals the complete evaluator before semantic imports.

The regression here is deliberately a fresh-process test.  Importing an evaluator module in
this pytest process would already have fixed its compiled bytes and could not reproduce the
defect: a transient source can execute in the command process, restore its disk bytes, and
leave a later identity capture naming the restored file rather than the bytes that ran.
"""

from __future__ import annotations

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

import scripts.backend_eval.admission as admission

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALUATOR_ROOT = REPO_ROOT / "scripts" / "backend_eval"


def _copy_evaluator(tmp_path: Path) -> Path:
    owner = tmp_path / "owner"
    (owner / "scripts").mkdir(parents=True)
    (owner / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(REPO_ROOT / "scripts" / "backend_eval_bootstrap.py", owner / "scripts")
    shutil.copytree(EVALUATOR_ROOT, owner / "scripts" / "backend_eval")
    shutil.copytree(REPO_ROOT / "src" / "serena_light", owner / "src" / "serena_light")
    for name in ("pyproject.toml", "uv.lock", "package-lock.json"):
        shutil.copy2(REPO_ROOT / name, owner / name)
    return owner


def _inject_import_and_restore_probe(owner: Path, report: Path) -> tuple[bytes, bytes]:
    """Make ``models.py`` observable while restoring its path during its own import."""

    target = owner / "scripts" / "backend_eval" / "models.py"
    pristine = target.read_bytes()
    marker = "# transient-import-restore-probe"
    pristine_b64 = b64encode(pristine).decode("ascii")
    probe = f'''\n{marker}
import base64 as _probe_base64, json as _probe_json, os as _probe_os, sys as _probe_sys, zipfile as _probe_zipfile
from pathlib import Path as _ProbePath
_probe_target = _ProbePath({str(target)!r})
_probe_target.write_bytes(_probe_base64.b64decode({pristine_b64!r}))
_probe_image_has_transient = False
try:
    with _probe_zipfile.ZipFile(
        _probe_os.environ.get("SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_PATH", _probe_sys.path[0])
    ) as _probe_archive:
        _probe_image_has_transient = {marker!r}.encode() in _probe_archive.read(
            "scripts/backend_eval/models.py"
        )
except (FileNotFoundError, IsADirectoryError, _probe_zipfile.BadZipFile):
    pass
_ProbePath({str(report)!r}).write_text(
    _probe_json.dumps(
        {{
            "pid": _probe_os.getpid(),
            "image_has_transient": _probe_image_has_transient,
            "disk_has_transient": {marker!r} in _probe_target.read_text(encoding="utf-8"),
        }}
    ),
    encoding="utf-8",
)
'''.encode()
    future = b"from __future__ import annotations\n"
    assert future in pristine
    hostile = pristine.replace(future, future + probe, 1)
    target.write_bytes(hostile)
    return pristine, hostile


def test_command_imports_transient_semantic_bytes_only_from_the_sealed_child(
    tmp_path: Path,
) -> None:
    """Old behavior executes the probe in ``process.pid`` with no source image.

    The repaired command first snapshots the closure, then imports ``models.py`` in a distinct
    isolated child.  The probe restores the pathname before any identity capture could re-read
    it, but its own report proves that the bytes it executed remain in the sealed image.
    """

    owner = _copy_evaluator(tmp_path)
    report = tmp_path / "report.json"
    pristine, hostile = _inject_import_and_restore_probe(owner, report)
    shadow_marker = tmp_path / "ambient-shadow-ran"
    shadow = tmp_path / "shadow" / "scripts"
    shadow.mkdir(parents=True)
    (shadow / "__init__.py").write_text(
        f"from pathlib import Path\nPath({str(shadow_marker)!r}).write_text('shadowed')\n",
        encoding="utf-8",
    )

    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "scripts/backend_eval_bootstrap.py", "--help"],
        cwd=owner,
        env={
            **os.environ,
            "PYTHONPATH": str(tmp_path / "shadow"),
            # Internal-looking ambient values cannot make the disk command skip its bootstrap.
            "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE": "1",
            "SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT": str(tmp_path / "shadow-owner"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = process.communicate(timeout=30)

    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    assert stdout.startswith(b"usage: python -I -S -B scripts/backend_eval_bootstrap.py")
    observed = json.loads(report.read_text(encoding="utf-8"))
    assert observed == {
        "pid": observed["pid"],
        "image_has_transient": True,
        "disk_has_transient": False,
    }
    assert observed["pid"] != process.pid
    assert not shadow_marker.exists()
    assert (owner / "scripts" / "backend_eval" / "models.py").read_bytes() == pristine != hostile


def test_package_module_entrypoint_is_not_receipt_producing(tmp_path: Path) -> None:
    owner = _copy_evaluator(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "scripts.backend_eval.admission", "--help"],
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert b"not a receipt-producing entrypoint" in result.stderr


@pytest.mark.parametrize(
    "flags",
    [
        ("-S", "-B"),
        ("-I", "-B"),
        ("-I", "-S"),
    ],
)
def test_outer_bootstrap_refuses_when_any_closed_startup_flag_is_missing(
    tmp_path: Path, flags: tuple[str, ...]
) -> None:
    owner = _copy_evaluator(tmp_path)
    result = subprocess.run(
        [sys.executable, *flags, "scripts/backend_eval_bootstrap.py", "--help"],
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert b"closed CPython startup" in result.stderr
    assert b"usage:" not in result.stdout


@pytest.mark.parametrize("mode", ["direct", "runpy"])
def test_admission_transport_refuses_without_direct_shim_provenance(
    tmp_path: Path, mode: str
) -> None:
    owner = _copy_evaluator(tmp_path)
    admission_path = owner / "scripts" / "backend_eval" / "admission.py"
    command = (
        [sys.executable, "-I", "-S", "-B", str(admission_path), "--help"]
        if mode == "direct"
        else [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            f"import runpy; runpy.run_path({str(admission_path)!r}, run_name='__main__')",
        ]
    )
    result = subprocess.run(
        command,
        cwd=owner,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert b"direct bootstrap provenance" in result.stderr
    assert b"usage:" not in result.stdout


def test_closed_outer_startup_ignores_hostile_site_and_python_paths(tmp_path: Path) -> None:
    owner = _copy_evaluator(tmp_path)
    marker = tmp_path / "outer-startup-marker"
    hostile = tmp_path / "hostile-site"
    hostile.mkdir()
    payload = f"import pathlib; pathlib.Path({str(marker)!r}).write_text('executed')\n"
    (hostile / "sitecustomize.py").write_text(payload, encoding="utf-8")
    (hostile / "hostile.pth").write_text(payload, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "scripts/backend_eval_bootstrap.py", "--help"],
        cwd=owner,
        env={**os.environ, "PYTHONPATH": str(hostile), "PYTHONUSERBASE": str(hostile)},
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert result.stdout.startswith(b"usage: python -I -S -B scripts/backend_eval_bootstrap.py")
    assert not marker.exists()


def test_source_image_contains_the_complete_evaluator_package(tmp_path: Path) -> None:
    owner = _copy_evaluator(tmp_path)
    builder = getattr(admission, "_build_evaluator_source_image", None)
    assert builder is not None, "the command needs a pre-import evaluator source-image builder"

    image = builder(owner)

    archive_path = tmp_path / "image.pyz"
    archive_path.write_bytes(image)
    with zipfile.ZipFile(archive_path) as archive:
        evaluator_entries = {
            name for name in archive.namelist() if name.startswith("scripts/backend_eval/")
        }
        archive_names = archive.namelist()
    expected = {
        f"scripts/backend_eval/{path.name}"
        for path in (owner / "scripts" / "backend_eval").glob("*.py")
    }
    assert evaluator_entries == expected
    assert "scripts/__init__.py" in archive_names


def test_package_initializers_execute_only_from_and_are_bound_by_the_sealed_image(
    tmp_path: Path,
) -> None:
    owner = _copy_evaluator(tmp_path)
    package = owner / "scripts" / "backend_eval"
    scripts_report = tmp_path / "scripts-init.pid"
    evaluator_report = tmp_path / "backend-init.pid"
    scripts_init = (
        "import os, pathlib\n"
        f"pathlib.Path({str(scripts_report)!r}).write_text(str(os.getpid()))\n"
    ).encode()
    backend_init = (
        "import hashlib, json, os, pathlib\n"
        "from scripts.backend_eval.source_image import evaluator_source_files\n"
        "_observed = {'pid': os.getpid(), 'digests': {name: hashlib.sha256(payload).hexdigest() "
        "for name, payload in evaluator_source_files()}}\n"
        f"pathlib.Path({str(evaluator_report)!r}).write_text(json.dumps(_observed))\n"
    ).encode()
    (owner / "scripts" / "__init__.py").write_bytes(scripts_init)
    (package / "__init__.py").write_bytes(backend_init)
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", "scripts/backend_eval_bootstrap.py", "--help"],
        cwd=owner,
        env={
            **os.environ,
            "SERENA_LIGHT_BACKEND_EVAL_SOURCE_IMAGE_ACTIVE": "1",
            "SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT": str(tmp_path / "ambient-owner"),
            "PYTHONPATH": str(tmp_path / "ambient-src"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(evaluator_report.read_text())
    assert observed["digests"]["scripts/__init__.py"] == sha256(scripts_init).hexdigest()
    assert observed["digests"]["__init__.py"] == sha256(backend_init).hexdigest()
    assert int(scripts_report.read_text()) == observed["pid"]
    assert observed["pid"] != process.pid


def test_sealed_child_preserves_stdout_and_exit_code_and_ignores_ambient_shadow(
    tmp_path: Path,
) -> None:
    owner = tmp_path / "owner"
    package = owner / "scripts" / "backend_eval"
    package.mkdir(parents=True)
    (owner / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "admission.py").write_text(
        "import sys\n"
        "def main():\n"
        "    sys.stdout.buffer.write(b'canonical\\x00stdout\\n')\n"
        "    sys.stderr.buffer.write(b'canonical-stderr\\n')\n"
        "    return 7\n",
        encoding="utf-8",
    )
    shadow = tmp_path / "shadow" / "scripts" / "backend_eval"
    shadow.mkdir(parents=True)
    (shadow.parent / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "__init__.py").write_text("", encoding="utf-8")
    (shadow / "admission.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    runner = getattr(admission, "_run_sealed_evaluator", None)
    assert runner is not None, "the command needs a bounded sealed-evaluator runner"

    returncode, stdout, stderr = runner(
        owner,
        (),
        timeout=5.0,
        environ={**os.environ, "PYTHONPATH": str(tmp_path / "shadow")},
    )

    assert returncode == 7
    assert stdout == b"canonical\x00stdout\n"
    assert stderr == b"canonical-stderr\n"


def test_sealed_child_timeout_kills_and_reaps_its_process_group(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    package = owner / "scripts" / "backend_eval"
    package.mkdir(parents=True)
    (owner / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    pid_path = tmp_path / "grandchild.pid"
    (package / "admission.py").write_text(
        "import pathlib, subprocess, sys, time\n"
        "def main():\n"
        "    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"    pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
        "    time.sleep(60)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    runner = getattr(admission, "_run_sealed_evaluator", None)
    timeout_error = getattr(admission, "EvaluatorBootstrapTimeout", None)
    assert runner is not None and timeout_error is not None

    with pytest.raises(timeout_error):
        runner(owner, (), timeout=0.5, environ=os.environ)

    grandchild = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 3.0
    while Path(f"/proc/{grandchild}").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not Path(f"/proc/{grandchild}").exists()


def test_identity_hashes_the_import_and_restore_bytes_from_the_source_image(
    tmp_path: Path,
) -> None:
    owner = _copy_evaluator(tmp_path)
    report = tmp_path / "restore-report.json"
    pristine, hostile = _inject_import_and_restore_probe(owner, report)
    admission_path = owner / "scripts" / "backend_eval" / "admission.py"
    admission_path.write_text(
        "from __future__ import annotations\n"
        "import json, sys\n"
        "from scripts.backend_eval.identity import capture_evaluator_identity\n"
        "from scripts.backend_eval.models import sha256_bytes\n"
        "def main():\n"
        "    identity = capture_evaluator_identity()\n"
        "    print(json.dumps({'recorded': dict(identity.source_files)['models.py'], "
        "'disk': sha256_bytes((identity_source := "
        f"__import__('pathlib').Path({str(owner)!r}) "
        "/ 'scripts/backend_eval/models.py').read_bytes())}))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    runner = getattr(admission, "_run_sealed_evaluator", None)
    assert runner is not None

    returncode, stdout, stderr = runner(owner, (), timeout=30.0, environ=os.environ)

    assert returncode == 0, stderr.decode("utf-8", "replace")
    observed = json.loads(stdout)
    assert observed["recorded"] == sha256(hostile).hexdigest()
    assert observed["disk"] == sha256(pristine).hexdigest()
    assert observed["recorded"] != observed["disk"]


def test_real_admission_refuses_an_unsealed_in_process_entrypoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only injected test services may exercise orchestration outside the sealed command."""

    request = admission.AdmissionRequest(
        repo_root=tmp_path,
        artifact_root=tmp_path / ".admission-artifacts" / "backend-eval",
        runtime_base=tmp_path / "runtime",
        uv=Path(sys.executable),
        python=Path(sys.executable),
        exclude_newer="2026-08-12T00:00:00Z",
    )

    explicit = admission.ProductionAdmissionServices()

    def forbidden_services() -> None:
        raise AssertionError("production services were constructed before the sealed-entrypoint refusal")

    monkeypatch.setitem(admission.run_admission.__globals__, "ProductionAdmissionServices", forbidden_services)

    with pytest.raises(admission.AdmissionError) as error:
        admission.run_admission(request, services=explicit)
    assert error.value.failure.code == "unsealed_evaluator_entrypoint"


def test_fake_services_and_main_cannot_publish_outside_the_sealed_image(tmp_path: Path) -> None:
    request = admission.AdmissionRequest(
        repo_root=tmp_path,
        artifact_root=tmp_path / ".admission-artifacts" / "backend-eval",
        runtime_base=tmp_path / "runtime",
        uv=Path(sys.executable),
        python=Path(sys.executable),
        exclude_newer="2026-08-12T00:00:00Z",
    )
    fake = object()
    with pytest.raises(admission.AdmissionError, match="unsealed_evaluator_entrypoint"):
        admission.run_admission(request, services=fake)  # type: ignore[arg-type]
    assert not request.artifact_root.exists()
    result = admission.main(
        [
            "--repo-root", str(request.repo_root), "--artifact-root", str(request.artifact_root),
            "--runtime-base", str(request.runtime_base), "--uv", str(request.uv),
            "--python", str(request.python), "--exclude-newer", request.exclude_newer,
        ],
        services=fake,  # type: ignore[arg-type]
    )
    assert result == 2
    assert not request.artifact_root.exists()


def test_sealed_child_has_closed_startup_and_only_deliberate_external_inputs(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    package = owner / "scripts" / "backend_eval"
    package.mkdir(parents=True)
    (owner / "scripts" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    marker = tmp_path / "startup-marker"
    shadow = tmp_path / "site"
    shadow.mkdir()
    (shadow / "sitecustomize.py").write_text(
        f"open({str(marker)!r}, 'w').write('sitecustomize')\n", encoding="utf-8"
    )
    (shadow / "hostile.pth").write_text(
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('pth')\n", encoding="utf-8"
    )
    (package / "admission.py").write_text(
        "import hashlib, json, os, sys\n"
        "def main():\n"
        "    print(json.dumps({'site': 'site' in sys.modules, 'keys': sorted(os.environ), "
        "'proxy': hashlib.sha256(os.environ['HTTPS_PROXY'].encode()).hexdigest(), "
        "'locale': hashlib.sha256(os.environ['LANG'].encode()).hexdigest()}))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    proxy = "http://credential-do-not-print.invalid"
    locale = "C.UTF-8"
    returncode, stdout, stderr = admission._run_sealed_evaluator(
        owner, (), timeout=5.0,
        environ={
            "HTTPS_PROXY": proxy, "LANG": locale, "PYTHONPATH": str(shadow),
            "PYTHONHOME": str(shadow),
            "SERENA_LIGHT_BACKEND_EVAL_OWNER_ROOT": str(tmp_path / "hostile-owner"),
        },
    )
    assert returncode == 0, stderr.decode("utf-8", "replace")
    assert json.loads(stdout) == {
        "site": False, "keys": ["HTTPS_PROXY", "LANG"],
        "proxy": sha256(proxy.encode()).hexdigest(), "locale": sha256(locale.encode()).hexdigest(),
    }
    assert proxy.encode() not in stdout + stderr
    assert not marker.exists()
