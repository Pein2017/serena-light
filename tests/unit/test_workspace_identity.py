from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from serena_light.workspace.identity import (
    LocationKind,
    PinnedMsRoots,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceKind,
    WorkspacePolicy,
)


def _git(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


def _policy(tmp_path: Path) -> WorkspacePolicy:
    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages"
    for directory in (external, tmp_path / "ms" / "lib" / "python3.12", tmp_path / "ms"):
        directory.mkdir(parents=True, exist_ok=True)
    interpreter = tmp_path / "ms" / "bin" / "python"
    interpreter.parent.mkdir(exist_ok=True)
    interpreter.touch()
    roots = PinnedMsRoots(
        interpreter=interpreter.resolve(),
        stdlib=(tmp_path / "ms" / "lib" / "python3.12").resolve(),
        purelib=external.resolve(),
        platlib=external.resolve(),
        conda_prefix=(tmp_path / "ms").resolve(),
    )
    allowed = external / "transformers"
    allowed.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()
    return WorkspacePolicy(ms_roots=roots, allowed_non_git_root=allowed, data_root=data_root)


def test_activation_requires_absolute_path(tmp_path: Path) -> None:
    policy = _policy(tmp_path)

    with pytest.raises(WorkspaceError, match="absolute") as raised:
        policy.resolve_activation("relative")

    assert raised.value.data.code is WorkspaceErrorCode.INVALID_PATH


def test_git_identity_uses_resolved_nested_top_level_and_deterministic_workdir(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "outer"
    nested = root / "pkg" / "inside"
    nested.mkdir(parents=True)
    _git(root)

    first = policy.resolve_activation(root / "pkg")
    second = policy.resolve_activation(nested)

    assert first.kind is WorkspaceKind.GIT
    assert first.root == root.resolve()
    assert first.registry_key == second.registry_key
    assert first.working_subdirectory == (root / "pkg").resolve()
    assert second.working_subdirectory == nested.resolve()


def test_nested_repository_is_a_distinct_identity(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    outer = tmp_path / "data" / "outer"
    inner = outer / "vendor" / "nested"
    inner.mkdir(parents=True)
    _git(outer)
    _git(inner)

    assert policy.resolve_activation(inner).root == inner.resolve()
    assert policy.resolve_activation(outer).root == outer.resolve()


def test_ignored_linked_worktree_under_coordexp_is_another_workspace(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    main = tmp_path / "data" / "CoordExp"
    linked = main / ".worktrees" / "linked"
    main.mkdir()
    _git(main)
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    (main / ".gitignore").write_text(".worktrees/\n")
    (main / "tracked.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(main), "add", "."], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "--quiet", "-m", "fixture"], check=True)
    subprocess.run(["git", "-C", str(main), "worktree", "add", "--quiet", "-b", "linked", str(linked)], check=True)
    target = linked / "linked.py"
    target.write_text("y = 2\n")

    main_identity = policy.resolve_activation(main)
    linked_identity = policy.resolve_activation(linked)

    assert linked_identity.root == linked.resolve()
    assert linked_identity.registry_key != main_identity.registry_key
    with pytest.raises(WorkspaceError) as raised:
        policy.authorize_path_operand(main_identity, target, [])
    assert raised.value.data.code is WorkspaceErrorCode.OUT_OF_WORKSPACE
    assert raised.value.data.activation_hint == linked.resolve()


def test_only_exact_non_git_root_is_allowed(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    allowed = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "transformers"

    identity = policy.resolve_activation(allowed)
    assert identity.kind is WorkspaceKind.ALLOWLISTED_NON_GIT

    with pytest.raises(WorkspaceError) as raised:
        policy.resolve_activation(allowed.parent)
    assert raised.value.data.code is WorkspaceErrorCode.UNTRUSTED_ROOT


def test_semantic_external_is_read_only_without_becoming_inventory(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "torch.py"
    external.touch()

    location = policy.classify_semantic_location(identity, external)

    assert location.kind is LocationKind.READ_ONLY_EXTERNAL
    assert location.path == external.resolve()


def test_path_outside_active_inventory_has_identity_and_absolute_activation_hint(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    active = tmp_path / "data" / "active"
    other = tmp_path / "data" / "other"
    active.mkdir(parents=True)
    other.mkdir()
    _git(active)
    _git(other)
    target = other / "module.py"
    target.touch()
    identity = policy.resolve_activation(active)

    with pytest.raises(WorkspaceError) as raised:
        policy.authorize_path_operand(identity, target, [])

    data = raised.value.data
    assert data.code is WorkspaceErrorCode.OUT_OF_WORKSPACE
    assert data.current_identity == identity
    assert data.activation_hint is not None
    assert data.activation_hint == other.resolve()
    assert data.activation_hint.is_absolute()


def test_edit_rejects_non_git_conda_and_symlink_escape_before_io(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "external.py"
    external.touch()

    with pytest.raises(WorkspaceError) as conda:
        policy.authorize_edit(identity, external, [external])
    assert conda.value.data.code is WorkspaceErrorCode.READ_ONLY_ROOT

    escaped = root / "escaped.py"
    os.symlink(external, escaped)
    with pytest.raises(WorkspaceError) as symlink:
        policy.authorize_edit(identity, escaped, [escaped])
    assert symlink.value.data.code is WorkspaceErrorCode.READ_ONLY_ROOT


def test_edit_allows_only_resolved_git_inventory_file_below_data(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    target = root / "module.py"
    target.write_text("x = 1\n")
    identity = policy.resolve_activation(root)

    assert policy.authorize_edit(identity, target, [target]) == target.resolve()
    assert policy.authorize_edit(identity, Path("module.py"), [target]) == target.resolve()


def test_pinned_ms_roots_are_resolved_by_the_selected_interpreter() -> None:
    roots = PinnedMsRoots.resolve(Path(sys.executable))

    assert roots.interpreter == Path(sys.executable).resolve()
    assert all(root.is_dir() for root in roots.semantic_roots)
