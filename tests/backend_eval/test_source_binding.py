"""The executed production helpers are bound to this checkout, by origin and by bytes.

The evaluator runs more than ``scripts/backend_eval``: manifests, the write guard, and the
production-identity capture execute ``serena_light`` helpers that a CLI host virtual
environment resolves through whatever editable ``.pth`` installed it.  These tests are
adversarial about exactly that seam -- they change a helper's bytes and repoint a helper's
path *without touching ``scripts/backend_eval`` at all*, and require the published identity
to change or the run to be refused.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

from scripts.backend_eval.admission import AdmissionRequest, evaluation_identity
from scripts.backend_eval.identity import IdentityError, capture_evaluator_identity
from scripts.backend_eval.models import EvaluatorIdentity, ProductionIdentity
from scripts.backend_eval.source_binding import (
    EVALUATION_OWNER_ROOT,
    PRODUCTION_PACKAGE,
    PRODUCTION_SOURCE_ROOT,
    SourceBindingError,
    _read_regular_file,
    bind_production_source,
)

_REPO_ROOT = EVALUATION_OWNER_ROOT


def _checkout(root: Path, *, body: str) -> ModuleType:
    """A synthetic ``serena_light.workspace.inventory`` below one checkout root."""

    package = root / "src" / "serena_light" / "workspace"
    package.mkdir(parents=True, exist_ok=True)
    (root / "src" / "serena_light" / "__init__.py").write_text("")
    (package / "__init__.py").write_text("")
    module_path = package / "inventory.py"
    module_path.write_text(body)
    module = ModuleType("serena_light.workspace.inventory")
    module.__file__ = str(module_path)
    return module


# --- the real process ----------------------------------------------------------------


def test_the_evaluator_binds_the_production_helpers_of_its_own_checkout() -> None:
    bound = dict(bind_production_source())
    assert bound, "no production helper was bound"
    for relative in bound:
        assert relative.startswith("src/serena_light/")
        assert (_REPO_ROOT / relative).is_file()
    # The helpers the manifests, write guard, and production identity actually import.
    assert {
        "src/serena_light/build_identity.py",
        "src/serena_light/workspace/identity.py",
        "src/serena_light/workspace/inventory.py",
    } <= set(bound)


def test_the_evaluator_identity_carries_the_bound_production_closure() -> None:
    identity = capture_evaluator_identity()
    assert identity.production_root == str(PRODUCTION_SOURCE_ROOT)
    assert dict(identity.production_files) == dict(bind_production_source())
    assert identity.production_digest == EvaluatorIdentity.build(
        source_files=identity.source_files,
        source_commit=identity.source_commit,
        source_clean=identity.source_clean,
        production_root=identity.production_root,
        production_files=identity.production_files,
        production_clean=identity.production_clean,
        host_python_path=identity.host_python_path,
        host_python_realpath=identity.host_python_realpath,
        host_python_sha256=identity.host_python_sha256,
        host_python_version=identity.host_python_version,
    ).production_digest


def test_every_loaded_serena_light_module_resolves_inside_this_checkout() -> None:
    for name, module in tuple(sys.modules.items()):
        if name != "serena_light" and not name.startswith("serena_light."):
            continue
        origin = getattr(module, "__file__", None)
        if origin is None:
            continue
        assert Path(origin).resolve().is_relative_to(PRODUCTION_PACKAGE), (
            f"{name} is executed from {origin}, outside {PRODUCTION_PACKAGE}"
        )


def test_the_admission_cli_loads_no_unbound_non_stdlib_helper() -> None:
    """The bound closure is *complete*: nothing else non-stdlib is executed by the CLI."""

    program = (
        "import sys; import scripts.backend_eval.admission;"
        "top = sorted({name.split('.')[0] for name in sys.modules"
        " if not name.startswith('_') and name.split('.')[0] not in sys.stdlib_module_names});"
        "print(' '.join(top))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], cwd=_REPO_ROOT, capture_output=True, text=True, timeout=120, check=True
    )
    assert result.stdout.split() == ["scripts", "serena_light"]


def test_read_regular_file_refuses_a_fifo_promptly(tmp_path: Path) -> None:
    """``O_RDONLY`` on a FIFO with no writer blocks until one appears.

    Every executed production helper is read through this same guarded open, so a FIFO left
    where a helper file belongs must be refused promptly rather than hanging the bind.
    """

    fifo = tmp_path / "inventory.py"
    os.mkfifo(fifo)
    before_fds = len(os.listdir("/proc/self/fd"))

    started = time.monotonic()
    with pytest.raises(SourceBindingError, match="regular file"):
        _read_regular_file(fifo)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    assert len(os.listdir("/proc/self/fd")) == before_fds


# --- adversarial: changed bytes, repointed path --------------------------------------


def test_changed_helper_bytes_change_the_bound_digest(tmp_path: Path) -> None:
    """``scripts/backend_eval`` is untouched; only the executed helper's bytes move."""

    owner = tmp_path / "owner"
    module = _checkout(owner, body="VALUE = 1\n")
    before = bind_production_source(modules={module.__name__: module}, owner_root=owner)
    assert before == (("src/serena_light/workspace/inventory.py", before[0][1]),)

    Path(str(module.__file__)).write_text("VALUE = 2\n")
    after = bind_production_source(modules={module.__name__: module}, owner_root=owner)
    assert after[0][0] == before[0][0]
    assert after[0][1] != before[0][1]


def test_changed_helper_bytes_change_the_evaluation_identity(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    module = _checkout(owner, body="VALUE = 1\n")
    first = bind_production_source(modules={module.__name__: module}, owner_root=owner)
    Path(str(module.__file__)).write_text("VALUE = 2\n")
    second = bind_production_source(modules={module.__name__: module}, owner_root=owner)

    identities = tuple(
        evaluation_identity(_request(), _production_identity(), _evaluator(production_files=files))
        for files in (first, second)
    )
    assert identities[0] != identities[1]


def test_a_helper_from_another_checkout_is_refused(tmp_path: Path) -> None:
    """Repointing the editable ``.pth`` at a second worktree fails the run closed."""

    owner = tmp_path / "owner"
    _checkout(owner, body="VALUE = 1\n")
    other = _checkout(tmp_path / "other", body="VALUE = 1\n")
    with pytest.raises(SourceBindingError, match="outside this evaluator's own production source"):
        bind_production_source(modules={other.__name__: other}, owner_root=owner)


def test_a_repointed_helper_fails_the_evaluator_identity_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt refuses to name an evaluator whose helpers it cannot bind."""

    other = _checkout(tmp_path / "other", body="VALUE = 1\n")
    monkeypatch.setitem(sys.modules, "serena_light.workspace.inventory", other)
    with pytest.raises(IdentityError, match="outside this evaluator's own production source"):
        capture_evaluator_identity()


def test_a_namespace_helper_outside_the_owner_is_refused(tmp_path: Path) -> None:
    owner = tmp_path / "owner"
    _checkout(owner, body="VALUE = 1\n")
    namespace = ModuleType("serena_light.workspace")
    namespace.__path__ = [str(tmp_path / "other" / "src" / "serena_light" / "workspace")]
    (tmp_path / "other" / "src" / "serena_light" / "workspace").mkdir(parents=True)
    with pytest.raises(SourceBindingError, match="outside this evaluator's own production source"):
        bind_production_source(modules={namespace.__name__: namespace}, owner_root=owner)


def test_binding_nothing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SourceBindingError, match="no production helper is loaded"):
        bind_production_source(modules={}, owner_root=tmp_path)


def test_a_symlinked_helper_outside_the_owner_is_refused(tmp_path: Path) -> None:
    """Realpath, not the configured path, decides ownership."""

    owner = tmp_path / "owner"
    _checkout(owner, body="VALUE = 1\n")
    outside = tmp_path / "outside.py"
    outside.write_text("VALUE = 3\n")
    link = owner / "src" / "serena_light" / "linked.py"
    link.symlink_to(outside)
    module = ModuleType("serena_light.linked")
    module.__file__ = str(link)
    with pytest.raises(SourceBindingError, match="outside this evaluator's own production source"):
        bind_production_source(modules={module.__name__: module}, owner_root=owner)


# --- fixtures -------------------------------------------------------------------------


def _evaluator(*, production_files: tuple[tuple[str, str], ...]) -> EvaluatorIdentity:
    return EvaluatorIdentity.build(
        source_files=(("admission.py", "a" * 64),),
        source_commit="9" * 40,
        source_clean=True,
        production_root="/data/CoordExp/serena-light/src",
        production_files=production_files,
        production_clean=True,
        host_python_path="/root/miniconda3/envs/ms/bin/python",
        host_python_realpath="/root/miniconda3/envs/ms/bin/python3.12",
        host_python_sha256="c" * 64,
        host_python_version="3.12.11",
    )


def _production_identity() -> ProductionIdentity:
    return ProductionIdentity(
        pyproject_toml_sha256="c" * 64,
        uv_lock_sha256="d" * 64,
        package_lock_json_sha256="e" * 64,
        dependency_lock_digest="f" * 64,
        build_identity="b" * 64,
        runtime_paths=(("runtime", "/data/runtime"),),
    )


def _request() -> AdmissionRequest:
    return AdmissionRequest(
        repo_root=Path("/data/CoordExp/serena-light"),
        artifact_root=Path("/data/CoordExp/serena-light/.admission-artifacts/backend-eval"),
        runtime_base=Path("/data/CoordExp/.codex/runtime/serena-light/backend-eval"),
        uv=Path("/root/miniconda3/envs/ms/bin/uv"),
        python=Path("/root/miniconda3/envs/ms/bin/python"),
        exclude_newer="2026-08-11T00:00:00Z",
    )
