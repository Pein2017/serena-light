"""Production-identity capture and the byte-identical production invariant."""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

import scripts.backend_eval.production_identity as production_identity_module
from scripts.backend_eval.identity import capture_evaluator_identity
from scripts.backend_eval.models import ProductionIdentity, canonical_json, sha256_bytes
from scripts.backend_eval.process import Deadline, monotonic_clock, run_bounded_bytes, sealed_image
from scripts.backend_eval.production_helper import (
    PRODUCTION_CHILD_PATH,
    PRODUCTION_CHILD_RELPATH,
    ProductionHelperError,
    ProductionHelperTimeout,
    _open_owner_root,
    _read_owned_file,
    production_child_digest,
    run_production_helper,
)
from scripts.backend_eval.production_identity import (
    PRODUCTION_IDENTITY_FILES,
    ProductionIdentityChanged,
    ProductionIdentityError,
    assert_production_identity_unchanged,
    capture_production_identity,
)
from scripts.backend_eval.source_binding import (
    CHILD_EXECUTED_HELPERS,
    EVALUATION_OWNER_ROOT,
    SourceBindingError,
    bind_production_source,
)
from serena_light.bootstrap import runtime_paths
from serena_light.build_identity import compute_build_identity, dependency_lock_digest

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHA_F = "f" * 64


class _FakeClock:
    """A monotonic clock the test advances explicitly."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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
    with pytest.raises(ProductionIdentityError, match="without following a link"):
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


# --- guarded direct reads and the bounded production-helper child ----------------------


def test_capture_production_identity_refuses_a_fifo_input_promptly(tmp_path: Path) -> None:
    """``Path.read_bytes()`` on a FIFO with no writer blocks until one appears.

    The direct lock reads are guarded -- ``O_NOFOLLOW``, ``O_NONBLOCK``, and an ``fstat``
    regular-file proof on the descriptor that was actually opened -- so a FIFO substituted
    for a production lock input fails closed at once instead of hanging the phase.
    """

    root = _stub_root(tmp_path)
    (root / "uv.lock").unlink()
    os.mkfifo(root / "uv.lock")
    before_fds = len(os.listdir("/proc/self/fd"))

    started = time.monotonic()
    with pytest.raises(ProductionIdentityError, match="must be a regular file"):
        capture_production_identity(root)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert len(os.listdir("/proc/self/fd")) == before_fds


def test_capture_production_identity_refuses_an_input_reached_through_a_link(tmp_path: Path) -> None:
    """A symlink whose target holds exactly the expected bytes is still refused.

    A check-then-``read_bytes`` accepts it, because the check and the read are two separate
    resolutions of the same mutable name.  One guarded descriptor cannot be split that way.
    """

    root = _stub_root(tmp_path, symlink="package-lock.json")
    with pytest.raises(ProductionIdentityError, match="cannot open"):
        capture_production_identity(root)


def test_capture_production_identity_equals_the_production_helpers(repo_root: Path) -> None:
    """The bounded child returns exactly what the in-process production helpers return."""

    identity = capture_production_identity(repo_root)
    assert identity.dependency_lock_digest == dependency_lock_digest(repo_root)
    assert identity.build_identity == compute_build_identity(repo_root)
    assert dict(identity.runtime_paths) == {
        name: str(path) for name, path in runtime_paths(repo_root).items()
    }


def test_capture_production_identity_propagates_its_deadline(repo_root: Path) -> None:
    """A phase with no time left fails typed rather than starting an unbounded helper."""

    clock = _FakeClock()
    deadline = Deadline.start(clock, 10.0)
    clock.advance(10.0)

    with pytest.raises(ProductionIdentityError):
        capture_production_identity(repo_root, deadline=deadline)


def test_capture_production_identity_kills_a_hung_helper_process_group(
    repo_root: Path, tmp_path: Path
) -> None:
    """A production helper that blocks costs the remaining budget, not the whole run.

    The child is replaced by an interpreter stub that never exits, which is the observable
    shape of a production helper blocked inside one uninterruptible ``open`` on a FIFO.
    """

    marker = tmp_path / "child.pid"
    stub = tmp_path / "hung-python"
    stub.write_text(
        "#!/bin/sh\n"
        f'echo "$$" > "{marker}"\n'
        "while true; do sleep 1; done\n",
        encoding="utf-8",
    )
    stub.chmod(0o700)
    deadline = Deadline.start(monotonic_clock, 2.0)

    started = time.monotonic()
    with pytest.raises(ProductionHelperTimeout, match="process group was killed"):
        run_production_helper("production_identity", {"root": str(repo_root)}, deadline=deadline, python=stub)
    elapsed = time.monotonic() - started

    assert elapsed < 20.0
    pid = int(marker.read_text().strip())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_the_production_helper_child_binds_the_checkout_it_executed(repo_root: Path) -> None:
    """A child that ran another checkout's helper bytes is refused, not believed."""

    with pytest.raises((SourceBindingError, ProductionHelperError)):
        run_production_helper(
            "production_identity", {"root": str(repo_root)}, owner_root=repo_root.parent
        )


def test_the_production_helper_child_receives_no_ambient_import_path(
    repo_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ambient ``PYTHONPATH`` shadow cannot reach the child."""

    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "serena_light").mkdir()
    (shadow / "serena_light" / "__init__.py").write_text("raise SystemExit(99)\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(shadow))

    identity = capture_production_identity(repo_root)

    assert identity.build_identity == compute_build_identity(repo_root)


def test_the_production_helper_child_refuses_an_unknown_operation(repo_root: Path) -> None:
    with pytest.raises(ProductionHelperError, match="unknown production helper operation"):
        run_production_helper("not_a_helper", {"root": str(repo_root)})


def test_a_non_regular_runtime_source_changes_the_identity_rather_than_hanging(
    tmp_path: Path, repo_root: Path
) -> None:
    """Production's own scan skips a non-regular source file; the guard catches the change.

    ``runtime_source_files`` filters by ``Path.is_file()``, so a FIFO planted below
    ``src/serena_light`` is silently excluded from the closure rather than read.  Preserving
    that exact production semantics is the point of running production's bytes, so the
    evaluation does not paper over it: the resulting identity simply differs, and
    :func:`assert_production_identity_unchanged` refuses the run.
    """

    copied = tmp_path / "copy"
    copied.mkdir()
    for name in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(repo_root / name, copied / name)
    shutil.copytree(repo_root / "src", copied / "src")
    before = capture_production_identity(copied)
    target = copied / "src" / "serena_light" / "build_identity.py"
    target.unlink()
    os.mkfifo(target)

    after = capture_production_identity(copied)

    assert after.build_identity != before.build_identity
    with pytest.raises(ProductionIdentityChanged, match="build_identity"):
        assert_production_identity_unchanged(before, after)


def test_a_substitution_after_the_guarded_read_fails_typed_inside_the_ceiling(
    tmp_path: Path, repo_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The post-validation window between the guarded read and the production helpers.

    The three lock digests are taken here through guarded descriptors; the dependency-lock
    digest and build identity are then computed by production's own bytes, which re-open the
    same names.  A node substituted in that window is exactly the race production cannot
    close, so the evaluation bounds it instead: the child fails typed under the deadline
    rather than blocking the phase, and no partial identity is returned.
    """

    copied = tmp_path / "copy"
    copied.mkdir()
    for name in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(repo_root / name, copied / name)
    shutil.copytree(repo_root / "src", copied / "src")
    real_inputs = production_identity_module._read_identity_inputs
    substituted = False

    def substituting_inputs(root: Path) -> dict[str, bytes]:
        nonlocal substituted
        payloads = real_inputs(root)
        if not substituted:
            substituted = True
            (root / "uv.lock").unlink()
            os.mkfifo(root / "uv.lock")
        return payloads

    monkeypatch.setattr(production_identity_module, "_read_identity_inputs", substituting_inputs)

    started = time.monotonic()
    with pytest.raises(ProductionIdentityError, match="cannot capture production identity"):
        capture_production_identity(copied, deadline=Deadline.start(monotonic_clock, 30.0))
    assert time.monotonic() - started < 20.0
    assert substituted


# --- the two source-binding seams of the bounded child ----------------------------------


def _owner_copy(tmp_path: Path, repo_root: Path) -> Path:
    """A self-contained evaluator checkout: the child program plus a production source tree."""

    owner = tmp_path / "owner"
    (owner / "scripts" / "backend_eval").mkdir(parents=True)
    shutil.copy2(PRODUCTION_CHILD_PATH, owner / PRODUCTION_CHILD_RELPATH)
    (owner / "src").mkdir()
    shutil.copytree(repo_root / "src" / "serena_light", owner / "src" / "serena_light")
    for name in PRODUCTION_IDENTITY_FILES:
        shutil.copy2(repo_root / name, owner / name)
    return owner


def _read_owned(owner: Path, relative: str) -> bytes:
    owner_fd = _open_owner_root(owner, "test")
    try:
        return _read_owned_file(owner_fd, relative, owner, "test")
    finally:
        os.close(owner_fd)


def test_the_parent_reread_refuses_a_symlinked_intermediate_component(
    tmp_path: Path, repo_root: Path
) -> None:
    """``O_NOFOLLOW`` on the whole relative path guards only its last component.

    A symlinked ``src/serena_light`` would let the parent "confirm" a helper's bytes from
    another tree entirely and accept them as this checkout's own.  Every component is opened
    from its parent's descriptor instead.  The child refuses this layout too, by realpath, so
    the parent's own guard is exercised directly here rather than through the child.
    """

    owner = _owner_copy(tmp_path, repo_root)
    elsewhere = tmp_path / "elsewhere" / "serena_light"
    elsewhere.mkdir(parents=True)
    shutil.copytree(owner / "src" / "serena_light", elsewhere, dirs_exist_ok=True)
    shutil.rmtree(owner / "src" / "serena_light")
    (owner / "src" / "serena_light").symlink_to(elsewhere)

    with pytest.raises(SourceBindingError, match="without following a link"):
        _read_owned(owner, "src/serena_light/build_identity.py")

    # End to end the same layout is refused, by whichever guard sees it first.
    with pytest.raises((SourceBindingError, ProductionHelperError)):
        run_production_helper("production_identity", {"root": str(owner)}, owner_root=owner)


def test_the_parent_reread_refuses_a_symlinked_leaf(tmp_path: Path, repo_root: Path) -> None:
    owner = _owner_copy(tmp_path, repo_root)
    target = owner / "src" / "serena_light" / "build_identity.py"
    decoy = tmp_path / "decoy_build_identity.py"
    shutil.copy2(target, decoy)
    target.unlink()
    target.symlink_to(decoy)

    with pytest.raises(SourceBindingError, match="without following a link"):
        _read_owned(owner, "src/serena_light/build_identity.py")

    with pytest.raises((SourceBindingError, ProductionHelperError)):
        run_production_helper("production_identity", {"root": str(owner)}, owner_root=owner)


def test_the_parent_reread_refuses_a_blocking_special_node(tmp_path: Path, repo_root: Path) -> None:
    """A FIFO where a helper module was must fail fast, not block the re-read."""

    owner = _owner_copy(tmp_path, repo_root)
    target = owner / "src" / "serena_light" / "build_identity.py"
    target.unlink()
    os.mkfifo(target)

    started = time.monotonic()
    with pytest.raises(SourceBindingError):
        _read_owned(owner, "src/serena_light/build_identity.py")
    assert time.monotonic() - started < 5.0


def test_the_child_program_is_executed_from_a_sealed_image_not_a_pathname(
    tmp_path: Path, repo_root: Path
) -> None:
    """A mid-run substitution of the child program is refused, not executed.

    The program is read once through the confined walk, pinned by digest, and executed from a
    sealed ``memfd``; the pathname is never handed to the interpreter.  Swapping the file
    after the first use therefore cannot get hostile bytes executed -- the digest pin refuses
    the run instead, without waiting for an after-the-fact ``source_clean`` observation.
    """

    owner = _owner_copy(tmp_path, repo_root)
    first = run_production_helper("production_identity", {"root": str(owner)}, owner_root=owner)
    assert first["dependency_lock_digest"] == dependency_lock_digest(owner)

    hostile = owner / PRODUCTION_CHILD_RELPATH
    hostile.write_text("import sys\nsys.stdout.write('{}\\n')\n", encoding="utf-8")

    with pytest.raises(SourceBindingError, match="changed during this run"):
        run_production_helper("production_identity", {"root": str(owner)}, owner_root=owner)


def test_the_child_program_digest_is_the_one_the_evaluator_identity_names(repo_root: Path) -> None:
    """The bytes that run and the bytes the receipt names are the same bytes."""

    identity = capture_evaluator_identity()
    recorded = dict(identity.source_files)

    assert recorded["production_child.py"] == production_child_digest()


def test_the_child_program_read_is_confined_to_the_owner_root(tmp_path: Path, repo_root: Path) -> None:
    """A symlinked ``scripts/backend_eval`` cannot supply the program that is executed."""

    owner = _owner_copy(tmp_path, repo_root)
    elsewhere = tmp_path / "elsewhere-scripts"
    elsewhere.mkdir()
    (elsewhere / "production_child.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    shutil.rmtree(owner / "scripts" / "backend_eval")
    (owner / "scripts" / "backend_eval").symlink_to(elsewhere)

    with pytest.raises(SourceBindingError, match="without following a link"):
        run_production_helper("production_identity", {"root": str(owner)}, owner_root=owner)


def test_the_declared_child_closure_covers_what_the_child_actually_loads(repo_root: Path) -> None:
    """The receipt must keep naming the bytes of every helper it carries an answer from.

    Those helpers run in the bounded child, so ``sys.modules`` cannot see them and the bound
    closure declares them instead.  A declaration is only evidence while it matches reality,
    so the child's own reported closure -- for every operation it supports -- must be a subset
    of it, and a helper that starts importing something new fails here rather than silently
    leaving the receipt.
    """

    reported: set[str] = set()
    requests: tuple[tuple[str, dict[str, Any]], ...] = (
        ("production_identity", {"root": str(repo_root)}),
        ("observe_file_digests", {"paths": []}),
    )
    for operation, payload in requests:
        response = _child_response(operation, payload)
        loaded = {entry[0] for entry in response["production_files"]}
        assert loaded, f"the child reported no production closure for {operation}"
        reported |= loaded

    assert reported <= set(CHILD_EXECUTED_HELPERS)
    assert set(CHILD_EXECUTED_HELPERS) == reported


def test_the_bound_production_closure_names_every_child_executed_helper() -> None:
    """The published closure covers the child's helpers even though this process never loads them."""

    bound = dict(bind_production_source())

    for relative in CHILD_EXECUTED_HELPERS:
        assert relative in bound
        assert bound[relative] == sha256_bytes((EVALUATION_OWNER_ROOT / relative).read_bytes())


def _child_response(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the child exactly as the helper does and return its whole structured response."""

    owner = EVALUATION_OWNER_ROOT
    owner_fd = _open_owner_root(owner, "test")
    try:
        program = _read_owned_file(owner_fd, PRODUCTION_CHILD_RELPATH, owner, "test")
    finally:
        os.close(owner_fd)
    with sealed_image("test-production-child", program) as image_fd:
        result = run_bounded_bytes(
            [sys.executable, "-I", "-B", f"/proc/self/fd/{image_fd}", str(owner), str(owner / "src")],
            cwd=owner,
            env={"LC_ALL": "C"},
            timeout=120.0,
            stdin=canonical_json({"op": operation, **payload}),
            pass_fds=(image_fd,),
        )
    assert result.returncode == 0, result.stdout
    return cast("dict[str, Any]", json.loads(result.stdout))
