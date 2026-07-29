"""End-to-end boundary assertions for semantic-tool path operands and results."""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from serena_light.tools.envelopes import ToolEnvelope
from serena_light.workspace.identity import (
    LocationKind,
    PinnedMsRoots,
    WorkspaceError,
    WorkspaceErrorCode,
    WorkspacePolicy,
)
from serena_light.workspace.inventory import SupportedPathTree, TrustInventory
from serena_light.workspace.runtime import WorkspaceRuntime


def _git(path: Path) -> None:
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)


def _policy(tmp_path: Path) -> tuple[WorkspacePolicy, Path]:
    data_root = tmp_path / "data"
    data_root.mkdir()
    prefix = tmp_path / "ms"
    stdlib = prefix / "lib" / "python3.12"
    purelib = stdlib / "site-packages"
    transformers = purelib / "transformers"
    transformers.mkdir(parents=True)
    interpreter = prefix / "bin" / "python"
    interpreter.parent.mkdir()
    interpreter.touch()
    return (
        WorkspacePolicy(
            ms_roots=PinnedMsRoots(
                interpreter=interpreter.resolve(),
                stdlib=stdlib.resolve(),
                purelib=purelib.resolve(),
                platlib=purelib.resolve(),
                conda_prefix=prefix.resolve(),
            ),
            allowed_non_git_root=transformers,
            data_root=data_root,
        ),
        data_root,
    )


def _linked_worktree(main: Path) -> Path:
    subprocess.run(["git", "-C", str(main), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(main), "config", "user.name", "Test"], check=True)
    (main / ".gitignore").write_text(".worktrees/\n")
    (main / "tracked.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(main), "add", "."], check=True)
    subprocess.run(["git", "-C", str(main), "commit", "--quiet", "-m", "fixture"], check=True)
    linked = main / ".worktrees" / "linked"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "--quiet", "-b", "linked", str(linked)], check=True)
    return linked


def _empty_runtime(policy: WorkspacePolicy, active: Path) -> WorkspaceRuntime:
    identity = policy.resolve_activation(active)
    inventory = TrustInventory(
        root=identity.root,
        paths=(),
        rejected=(),
        digest=hashlib.sha256(b"").hexdigest(),
        tree=SupportedPathTree.from_paths(()),
        kind="test",
    )
    return WorkspaceRuntime(identity, path_policy=policy, inventory=inventory)


def test_semantic_result_boundary_matrix_uses_active_nested_identity(tmp_path: Path) -> None:
    policy, data_root = _policy(tmp_path)
    outer = data_root / "outer"
    active = outer / "vendor" / "nested"
    active.mkdir(parents=True)
    _git(outer)
    _git(active)
    active_file = active / "active.py"
    active_file.write_text("value = 1\n")

    other = data_root / "other"
    other.mkdir()
    _git(other)
    other_file = other / "other.py"
    other_file.write_text("value = 2\n")

    coordexp = data_root / "CoordExp"
    coordexp.mkdir()
    _git(coordexp)
    linked = _linked_worktree(coordexp)
    linked_file = linked / "linked.py"
    linked_file.write_text("value = 3\n")

    allowed = tmp_path / "ms" / "lib" / "python3.12" / "site-packages" / "package.py"
    allowed.write_text("value = 4\n")
    untrusted = tmp_path / "outside" / "package.py"
    untrusted.parent.mkdir()
    untrusted.write_text("value = 5\n")

    identity = policy.resolve_activation(active)
    assert policy.classify_semantic_location(identity, active_file).kind is LocationKind.WORKSPACE
    assert policy.classify_semantic_location(identity, other_file).kind is LocationKind.READ_ONLY_EXTERNAL
    assert policy.classify_semantic_location(identity, linked_file).kind is LocationKind.READ_ONLY_EXTERNAL
    assert policy.classify_semantic_location(identity, allowed).kind is LocationKind.READ_ONLY_EXTERNAL
    with pytest.raises(WorkspaceError) as raised:
        policy.classify_semantic_location(identity, untrusted)
    assert raised.value.data.code is WorkspaceErrorCode.UNTRUSTED_ROOT

    for foreign in (other_file, linked_file):
        with pytest.raises(WorkspaceError) as rejected:
            policy.authorize_path_operand(identity, foreign, [active_file])
        assert rejected.value.data.code is WorkspaceErrorCode.OUT_OF_WORKSPACE
        assert rejected.value.data.current_identity == identity
        assert rejected.value.data.activation_hint == foreign.parent.resolve()


@pytest.mark.parametrize(
    "invoke",
    [
        lambda runtime, path: runtime.get_symbols_overview(path),
        lambda runtime, path: runtime.find_symbol("target", relative_path=path),
        lambda runtime, path: runtime.find_declaration(path, r"(target)"),
        lambda runtime, path: runtime.find_implementations("target", path),
        lambda runtime, path: runtime.find_referencing_symbols(path, "target"),
        lambda runtime, path: runtime.get_diagnostics_for_file(path),
        lambda runtime, path: runtime.get_diagnostics_for_symbol(path, "target"),
        lambda runtime, path: runtime.replace_symbol_body("target", path, "def target(): pass", "0" * 64),
    ],
    ids=(
        "get_symbols_overview",
        "find_symbol",
        "find_declaration",
        "find_implementations",
        "find_referencing_symbols",
        "get_diagnostics_for_file",
        "get_diagnostics_for_symbol",
        "replace_symbol_body",
    ),
)
@pytest.mark.parametrize("foreign_kind", ("other_workspace", "linked_worktree"))
def test_every_path_taking_tool_preserves_out_of_workspace_for_other_data_workspace(
    tmp_path: Path, invoke: Callable[[WorkspaceRuntime, str], ToolEnvelope], foreign_kind: str
) -> None:
    policy, data_root = _policy(tmp_path)
    outer = data_root / "outer"
    active = outer / "vendor" / "active"
    active.mkdir(parents=True)
    _git(outer)
    _git(active)
    if foreign_kind == "other_workspace":
        foreign_root = data_root / "other"
        foreign_root.mkdir()
        _git(foreign_root)
    else:
        coordexp = data_root / "CoordExp"
        coordexp.mkdir()
        _git(coordexp)
        foreign_root = _linked_worktree(coordexp)
    foreign_file = foreign_root / "foreign.py"
    foreign_file.write_text("def target(): pass\n")
    runtime = _empty_runtime(policy, active)
    try:
        result = invoke(runtime, os.path.relpath(foreign_file, active)).to_dict()
    finally:
        runtime.stop()

    assert result["error"]["code"] == "OUT_OF_WORKSPACE"
    assert result["workspace"]["root"] == str(active.resolve())
    assert result["error"]["details"]["activation_hint"] == str(foreign_root.resolve())
