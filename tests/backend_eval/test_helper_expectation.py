"""The identity a receipt publishes is the expectation every child is held to.

The defect these tests exist for: the evaluator captured its identity -- including the child
program and the executed production-helper closure -- and then let the *first* child use pin
whatever bytes happened to be on disk at that moment.  A helper or child program substituted
between the capture and the first use therefore executed successfully and was only re-read
afterwards, which a transient substitution survives.

The repair is structural.  :class:`~scripts.backend_eval.source_binding.HelperExpectation` is
derived from the captured identity, passed explicitly into every production-helper call, and
compared *before* anything runs; the verified bytes are then the bytes the child imports,
because they are handed over in a sealed in-memory image rather than re-read from disk.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.backend_eval.identity import capture_evaluator_identity
from scripts.backend_eval.models import EvaluatorIdentity, sha256_bytes
from scripts.backend_eval.process import CommandBytesResult
from scripts.backend_eval.production_helper import (
    PRODUCTION_CHILD_PATH,
    ProductionHelperError,
    run_production_helper,
)
from scripts.backend_eval.production_identity import PRODUCTION_IDENTITY_FILES
from scripts.backend_eval.source_binding import (
    CHILD_EXECUTED_HELPERS,
    OPERATION_HELPER_CLOSURES,
    PRODUCTION_CHILD_NAME,
    PRODUCTION_CHILD_RELPATH,
    HelperExpectation,
    SourceBindingError,
)
from serena_light.build_identity import compute_build_identity, dependency_lock_digest
from tests.backend_eval.support import expectation_for, real_expectation

REPO_ROOT = Path(__file__).resolve().parents[2]
_BUILD_IDENTITY_RELPATH = "src/serena_light/build_identity.py"


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


def _owner_copy(tmp_path: Path, repo_root: Path, name: str = "owner") -> Path:
    """A self-contained evaluator checkout: the child program plus a production source tree."""

    owner = tmp_path / name
    (owner / "scripts" / "backend_eval").mkdir(parents=True)
    shutil.copy2(PRODUCTION_CHILD_PATH, owner / PRODUCTION_CHILD_RELPATH)
    (owner / "src").mkdir()
    shutil.copytree(repo_root / "src" / "serena_light", owner / "src" / "serena_light")
    for name_ in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(repo_root / name_, owner / name_)
    return owner


# --- the expectation is the captured identity, not an observation ------------------------


def test_the_expectation_is_derived_from_the_identity_the_receipt_publishes() -> None:
    """Enforcing the expectation is enforcing the receipt: every digest comes from it."""

    identity = capture_evaluator_identity()
    expectation = HelperExpectation.from_identity(identity)

    assert expectation.child_digest == dict(identity.source_files)[PRODUCTION_CHILD_NAME]
    assert expectation.closure == tuple(
        (relative, dict(identity.production_files)[relative]) for relative in CHILD_EXECUTED_HELPERS
    )


def test_an_identity_that_does_not_name_the_child_program_cannot_authorize_a_child() -> None:
    without_child = _identity_without(source_name=PRODUCTION_CHILD_NAME)

    with pytest.raises(SourceBindingError, match=PRODUCTION_CHILD_NAME):
        HelperExpectation.from_identity(without_child)


def test_an_identity_missing_a_child_executed_helper_cannot_authorize_a_child() -> None:
    narrowed = _identity_without(production_relpath=_BUILD_IDENTITY_RELPATH)

    with pytest.raises(SourceBindingError, match="does not name every child-executed helper"):
        HelperExpectation.from_identity(narrowed)


def _identity_without(
    *, source_name: str | None = None, production_relpath: str | None = None
) -> EvaluatorIdentity:
    """The captured identity with one recorded file removed, digests recomputed."""

    identity = capture_evaluator_identity()
    return EvaluatorIdentity.build(
        source_files=tuple(entry for entry in identity.source_files if entry[0] != source_name),
        source_commit=identity.source_commit,
        source_clean=identity.source_clean,
        production_root=identity.production_root,
        production_files=tuple(
            entry for entry in identity.production_files if entry[0] != production_relpath
        ),
        production_clean=identity.production_clean,
        host_python_path=identity.host_python_path,
        host_python_realpath=identity.host_python_realpath,
        host_python_sha256=identity.host_python_sha256,
        host_python_version=identity.host_python_version,
    )


def test_each_operation_declares_an_exact_allowed_subset_of_the_declared_closure() -> None:
    """Per operation an exact subset; across the supported operations exactly the union."""

    expectation = real_expectation()
    union: set[str] = set()
    for operation, declared in OPERATION_HELPER_CLOSURES.items():
        modules = expectation.modules_for(operation)
        assert tuple(relative for relative, _digest in modules) == declared
        assert set(declared) < set(CHILD_EXECUTED_HELPERS) or set(declared) == set(CHILD_EXECUTED_HELPERS)
        union |= set(declared)
    assert union == set(CHILD_EXECUTED_HELPERS)


# --- substitution between the identity capture and the first use -------------------------


def test_a_child_program_swapped_after_the_capture_never_reaches_its_first_use(
    tmp_path: Path, repo_root: Path
) -> None:
    """The reproduced defect: the first use used to pin whatever was on disk *then*.

    No child has run yet when the program is replaced here, so nothing has been pinned and an
    after-the-fact re-read would have nothing to compare against.  The expectation captured
    before the swap is what refuses it.
    """

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)

    hostile = owner / PRODUCTION_CHILD_RELPATH
    hostile.write_text("import sys\nsys.stdout.write('{}\\n')\n", encoding="utf-8")

    with pytest.raises(SourceBindingError, match="not the .* this evaluator's identity names"):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)


def test_a_helper_swapped_after_the_capture_never_reaches_its_first_use(
    tmp_path: Path, repo_root: Path
) -> None:
    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)

    (owner / _BUILD_IDENTITY_RELPATH).write_text(
        "def compute_build_identity(root):\n    return 'd' * 64\n", encoding="utf-8"
    )

    with pytest.raises(SourceBindingError, match="not the .* this evaluator's identity names"):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)


def test_a_helper_swapped_inside_the_import_window_cannot_execute(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The window an after-the-fact re-read cannot see: between the read and the import.

    The substitution lands after the parent has verified and sealed the helper bytes and
    before the child would have imported them from disk.  A digest bracket would have to catch
    it afterwards; here there is nothing to catch, because the child never consults the disk --
    it imports the sealed image the parent built from the verified bytes.
    """

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)
    expected_lock = dependency_lock_digest(owner)
    target = owner / _BUILD_IDENTITY_RELPATH
    pristine = target.read_bytes()
    hostile = (
        b"def compute_build_identity(root):\n    return 'd' * 64\n"
        b"def dependency_lock_digest(root):\n    return 'e' * 64\n"
        b"def runtime_source_files(root):\n    return ()\n"
    )
    real_runner = run_production_helper.__globals__["run_bounded_bytes"]
    substituted = False

    def substituting_runner(*args: Any, **kwargs: Any) -> CommandBytesResult:
        nonlocal substituted
        target.write_bytes(hostile)
        substituted = True
        return real_runner(*args, **kwargs)

    monkeypatch.setitem(run_production_helper.__globals__, "run_bounded_bytes", substituting_runner)

    result = run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)

    assert substituted
    assert target.read_bytes() == hostile != pristine
    # The hostile bytes are on disk and were never executed.  The lock digest is untouched by
    # the swap, and the build identity is the one the *pristine* helper computes over the tree
    # as it now stands -- production's own scan reads that tree, which is production semantics
    # preserved exactly -- and is emphatically not the constant the hostile module returns.
    assert result["dependency_lock_digest"] == expected_lock != "e" * 64
    assert result["build_identity"] == compute_build_identity(owner) != "d" * 64


# --- exact closure membership, enforced at runtime ---------------------------------------


def test_an_unexpected_extra_imported_helper_refuses_the_run(
    tmp_path: Path, repo_root: Path
) -> None:
    """A helper that starts importing another production module cannot silently succeed.

    The extra module is not in the operation's declared closure, so it is not in the sealed
    image, so the import has nowhere to come from: the child refuses instead of reaching to
    disk for bytes the receipt would not name.
    """

    owner = _owner_copy(tmp_path, repo_root)
    target = owner / _BUILD_IDENTITY_RELPATH
    source = target.read_text(encoding="utf-8")
    future = "from __future__ import annotations\n"
    assert future in source
    target.write_text(
        source.replace(future, future + "import serena_light.workspace.inventory\n", 1), encoding="utf-8"
    )
    expectation = expectation_for(owner)

    with pytest.raises(ProductionHelperError) as error:
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)
    assert "serena_light.workspace" in str(error.value)


def test_a_missing_expected_helper_refuses_the_run(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Membership is exact in both directions: a declared module that never loads is a refusal."""

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)
    monkeypatch.setitem(
        HelperExpectation.modules_for.__globals__["OPERATION_HELPER_CLOSURES"],
        "production_identity",
        (*OPERATION_HELPER_CLOSURES["production_identity"], "src/serena_light/workspace/__init__.py"),
    )

    with pytest.raises(ProductionHelperError, match="not the declared closure"):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)


def test_a_helper_reaching_disk_instead_of_the_image_is_an_origin_escape(
    tmp_path: Path, repo_root: Path
) -> None:
    """Origin is proven by loader identity, not by the pathname a module reports.

    The child program is rewritten to put the real ``src`` root back on ``sys.path`` and import
    a production module from it before the sealed image is installed.  Its ``__file__`` looks
    exactly like an image-loaded module's, and it is refused anyway.
    """

    owner = _owner_copy(tmp_path, repo_root)
    program = owner / PRODUCTION_CHILD_RELPATH
    source = program.read_text(encoding="utf-8")
    marker = "    payload = sys.stdin.buffer.read()"
    assert marker in source
    program.write_text(
        source.replace(
            marker,
            '    sys.path.insert(0, os.path.join(owner_root, "src"))\n'
            "    import serena_light.build_identity  # noqa\n" + marker,
        ),
        encoding="utf-8",
    )
    expectation = expectation_for(owner)

    with pytest.raises(ProductionHelperError, match="rather than from the sealed production image"):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)


def test_a_helper_swapped_after_the_capture_cannot_serve_the_corpus_inventory(
    tmp_path: Path, repo_root: Path
) -> None:
    """The corpus inventory helpers are held to the expectation like every other one.

    They used to be imported into the evaluator process, where the compiled bytes were fixed
    before the identity that names them was captured.  Now they execute in the child under the
    same expectation, so a post-capture swap refuses instead of answering.
    """

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)
    target = owner / "src" / "serena_light" / "workspace" / "inventory.py"
    target.write_text("def bounded_non_git_trust_inventory(root):\n    return None\n", encoding="utf-8")

    for operation, payload in (
        ("bounded_non_git_inventory", {"root": str(owner)}),
        ("git_inventory_from_bytes", {"root": str(owner), "candidates_b64": ""}),
        ("observe_file_digests", {"paths": []}),
    ):
        with pytest.raises(SourceBindingError, match="not the .* this evaluator's identity names"):
            run_production_helper(operation, payload, expectation=expectation)


def test_the_corpus_inventory_helpers_run_under_the_same_sealed_image(
    tmp_path: Path, repo_root: Path
) -> None:
    """Both new operations execute, and report exactly their declared closure."""

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)

    bounded = run_production_helper(
        "bounded_non_git_inventory", {"root": str(owner / "src")}, expectation=expectation
    )
    assert bounded["kind"] == "bounded_no_symlink"
    assert "serena_light/build_identity.py" in bounded["paths"]

    empty = run_production_helper(
        "git_inventory_from_bytes", {"root": str(owner), "candidates_b64": ""}, expectation=expectation
    )
    assert empty["kind"] == "git"
    assert empty["paths"] == []


# --- no process-global truth --------------------------------------------------------------


def test_two_checkouts_in_one_process_never_contaminate_each_others_truth(
    tmp_path: Path, repo_root: Path
) -> None:
    """The removed defect: a process-global first-use pin, keyed by owner root.

    Each call is bound only by the expectation it was given, so a second checkout with
    different helper bytes runs its own bytes, and an expectation from one checkout can never
    authorize the other.
    """

    first = _owner_copy(tmp_path, repo_root, "first")
    second = _owner_copy(tmp_path, repo_root, "second")
    (second / "uv.lock").write_bytes((second / "uv.lock").read_bytes() + b"\n# second\n")

    first_expectation = expectation_for(first)
    second_expectation = expectation_for(second)

    first_result = run_production_helper(
        "production_identity", {"root": str(first)}, expectation=first_expectation
    )
    second_result = run_production_helper(
        "production_identity", {"root": str(second)}, expectation=second_expectation
    )

    assert first_result["dependency_lock_digest"] == dependency_lock_digest(first)
    assert second_result["dependency_lock_digest"] == dependency_lock_digest(second)
    assert first_result["dependency_lock_digest"] != second_result["dependency_lock_digest"]

    # And one checkout's expectation cannot authorize the other's bytes.  The helper closures
    # have to actually differ for that to mean anything, so make them differ.
    (second / _BUILD_IDENTITY_RELPATH).write_text(
        (second / _BUILD_IDENTITY_RELPATH).read_text(encoding="utf-8") + "\n# second checkout\n",
        encoding="utf-8",
    )
    diverged = expectation_for(second)
    assert dict(diverged.closure) != dict(first_expectation.closure)

    crossed = replace(first_expectation, owner_root=second)
    with pytest.raises(SourceBindingError, match="not the .* this evaluator's identity names"):
        run_production_helper("production_identity", {"root": str(second)}, expectation=crossed)

    # The second checkout still runs perfectly well under its *own* expectation.
    assert run_production_helper(
        "production_identity", {"root": str(second)}, expectation=diverged
    )["dependency_lock_digest"] == second_result["dependency_lock_digest"]


def test_no_process_global_pin_survives_in_the_production_helper() -> None:
    """The pin itself is gone, not merely bypassed."""

    import scripts.backend_eval.production_helper as module

    assert not hasattr(module, "_PINNED_CHILD_DIGESTS")
    assert not [name for name in vars(module) if "PINNED" in name.upper()]


# --- normal operation is unchanged ----------------------------------------------------------


def test_the_expected_bytes_are_the_bytes_that_answer_a_normal_operation(repo_root: Path) -> None:
    """The whole point: exact production semantics, from exactly the named bytes."""

    expectation = real_expectation()
    result = run_production_helper(
        "production_identity", {"root": str(repo_root)}, expectation=expectation
    )

    assert result["build_identity"] == compute_build_identity(repo_root)
    assert result["dependency_lock_digest"] == dependency_lock_digest(repo_root)
    for relative, digest in expectation.closure:
        assert digest == sha256_bytes((repo_root / relative).read_bytes())


def test_a_symlinked_helper_component_is_refused_before_any_child_starts(
    tmp_path: Path, repo_root: Path
) -> None:
    """The pre-execution read is confined, so a symlinked ``src/serena_light`` cannot supply bytes."""

    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)
    elsewhere = tmp_path / "elsewhere" / "serena_light"
    elsewhere.mkdir(parents=True)
    shutil.copytree(owner / "src" / "serena_light", elsewhere, dirs_exist_ok=True)
    shutil.rmtree(owner / "src" / "serena_light")
    (owner / "src" / "serena_light").symlink_to(elsewhere)

    with pytest.raises(SourceBindingError, match="without following a link"):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)


def test_a_blocking_special_node_where_a_helper_was_fails_fast(tmp_path: Path, repo_root: Path) -> None:
    owner = _owner_copy(tmp_path, repo_root)
    expectation = expectation_for(owner)
    target = owner / _BUILD_IDENTITY_RELPATH
    target.unlink()
    os.mkfifo(target)

    with pytest.raises(SourceBindingError):
        run_production_helper("production_identity", {"root": str(owner)}, expectation=expectation)
