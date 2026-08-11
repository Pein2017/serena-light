from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from serena_light.workspace.identity import (
    LocationKind,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspaceKind,
    WorkspacePolicy,
)


def _git(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


def _policy(tmp_path: Path) -> WorkspacePolicy:
    return _flexible_policy(tmp_path)[0]


def _flexible_policy(tmp_path: Path) -> tuple[WorkspacePolicy, Path]:
    envs_root = tmp_path / "conda" / "envs"
    for name in ("ms", "llm-framework-study"):
        interpreter = envs_root / name / "bin" / "python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(Path(sys.executable))
    data_root = tmp_path / "data"
    data_root.mkdir()
    return WorkspacePolicy(conda_envs_root=envs_root, data_root=data_root), envs_root


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


def test_each_non_git_activation_uses_its_own_exact_root(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    package = tmp_path / "external" / "package"
    package.mkdir(parents=True)

    package_identity = policy.resolve_activation(package)
    parent_identity = policy.resolve_activation(package.parent)

    assert package_identity.kind is WorkspaceKind.NON_GIT_READ_ONLY
    assert package_identity.root == package.resolve()
    assert parent_identity.root == package.parent.resolve()
    assert package_identity.registry_key != parent_identity.registry_key


def test_any_existing_non_git_directory_uses_exact_read_only_identity(tmp_path: Path) -> None:
    policy, _envs_root = _flexible_policy(tmp_path)
    package = tmp_path / "external" / "arbitrary-package"
    package.mkdir(parents=True)

    identity = policy.resolve_activation(package)

    assert identity.root == package.resolve()
    assert identity.working_subdirectory == package.resolve()
    assert identity.kind is WorkspaceKind.NON_GIT_READ_ONLY
    assert identity.python_environment == "ms"


def test_entire_site_packages_directory_is_a_valid_non_git_root(tmp_path: Path) -> None:
    policy, envs_root = _flexible_policy(tmp_path)
    site_packages = envs_root / "llm-framework-study" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    identity = policy.resolve_activation(site_packages, python_environment="llm-framework-study")

    assert identity.root == site_packages.resolve()
    assert identity.kind is WorkspaceKind.NON_GIT_READ_ONLY
    assert identity.python_environment == "llm-framework-study"
    assert identity.python_interpreter == envs_root / "llm-framework-study" / "bin" / "python"


def test_environment_selection_is_part_of_registry_identity(tmp_path: Path) -> None:
    policy, envs_root = _flexible_policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir(parents=True)
    _git(root)

    default = policy.resolve_activation(root)
    selected = policy.resolve_activation(root, python_environment="llm-framework-study")

    assert default.python_environment == "ms"
    assert default.python_interpreter == envs_root / "ms" / "bin" / "python"
    assert selected.python_environment == "llm-framework-study"
    assert selected.registry_key != default.registry_key


@pytest.mark.parametrize("name", ["", "../ms", "nested/ms", "/absolute"])
def test_invalid_environment_name_fails_closed(tmp_path: Path, name: str) -> None:
    policy, _envs_root = _flexible_policy(tmp_path)
    root = tmp_path / "non-git"
    root.mkdir()

    with pytest.raises(WorkspaceError) as raised:
        policy.resolve_activation(root, python_environment=name)

    assert raised.value.data.code is WorkspaceErrorCode.INVALID_PATH


def test_missing_environment_fails_closed(tmp_path: Path) -> None:
    policy, _envs_root = _flexible_policy(tmp_path)
    root = tmp_path / "non-git"
    root.mkdir()

    with pytest.raises(WorkspaceError) as raised:
        policy.resolve_activation(root, python_environment="missing")

    assert raised.value.data.code is WorkspaceErrorCode.INVALID_PATH


def test_any_existing_external_semantic_location_is_read_only(tmp_path: Path) -> None:
    policy, _envs_root = _flexible_policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir(parents=True)
    _git(root)
    identity = policy.resolve_activation(root)
    external = tmp_path / "outside-every-configured-root" / "module.py"
    external.parent.mkdir()
    external.write_text("answer = 42\n", encoding="utf-8")

    location = policy.classify_semantic_location(identity, external)

    assert location.kind is LocationKind.READ_ONLY_EXTERNAL
    assert location.path == external.resolve()


def test_semantic_external_is_read_only_without_becoming_inventory(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "torch.py"
    external.parent.mkdir(parents=True)
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


def test_edit_rejects_out_of_workspace_and_symlink_escape_before_io(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    external = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "external.py"
    external.parent.mkdir(parents=True)
    external.touch()

    with pytest.raises(WorkspaceError) as outside:
        policy.authorize_edit(identity, external, [external])
    assert outside.value.data.code is WorkspaceErrorCode.OUT_OF_WORKSPACE

    # The escape is refused as a symlink before any resolution, so the target it
    # would have named never contributes to the decision.
    escaped = root / "escaped.py"
    os.symlink(external, escaped)
    with pytest.raises(WorkspaceError) as symlink:
        policy.authorize_edit(identity, escaped, [escaped])
    assert symlink.value.data.code is WorkspaceErrorCode.INVALID_PATH


def test_edit_rejects_inventoried_path_replaced_by_symlink_to_ignored_in_root_file(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    tracked = root / "module.py"
    tracked.write_text("x = 1\n")
    ignored = root / "ignored.py"
    ignored.write_text("y = 2\n")
    inventory = [tracked]

    assert policy.authorize_edit(identity, tracked, inventory) == tracked
    tracked.unlink()
    os.symlink(ignored, tracked)

    # Resolution would give the symlink the ignored file's identity, which is
    # still in-root; only the lexical membership plus O_NOFOLLOW walk refuses it.
    with pytest.raises(WorkspaceError) as raised:
        policy.authorize_edit(identity, tracked, inventory)
    assert raised.value.data.code is WorkspaceErrorCode.INVALID_PATH
    assert ignored.read_text() == "y = 2\n"


def test_edit_rejects_a_symlinked_parent_directory_component(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "data" / "repo"
    root.mkdir()
    _git(root)
    identity = policy.resolve_activation(root)
    real = root / "real"
    real.mkdir()
    target = real / "module.py"
    target.write_text("x = 1\n")
    linked = root / "linked"
    os.symlink(real, linked)

    with pytest.raises(WorkspaceError) as raised:
        policy.authorize_edit(identity, linked / "module.py", [linked / "module.py"])
    assert raised.value.data.code is WorkspaceErrorCode.INVALID_PATH


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
