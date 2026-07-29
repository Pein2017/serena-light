from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serena_light.bootstrap import repository_root, runtime_paths
from tests.admission.lsp_probe import LspClient, Profile, initialize_params, path_uri
from tests.admission.pyright_scope_probe import (
    SCOPE_INCOMPATIBLE,
    bounded_trust_inventory,
    git_trust_inventory,
    parse_pyright_dependency_output,
    probe_pyright_scope,
)

pytestmark = pytest.mark.timeout(90)


def _git(command: list[str], root: Path) -> None:
    subprocess.run(["git", *command], cwd=root, check=True, capture_output=True)


def _fixture_root(tmp_path: Path, name: str) -> Path:
    source = repository_root() / "tests" / "admission" / "fixtures" / name
    root = tmp_path / name
    shutil.copytree(source, root)
    if name == "pyright-scope-incompatible":
        generated = root / "ignored-generated" / "hidden.py"
        generated.parent.mkdir(exist_ok=True)
        generated.write_text("hidden = True\n", encoding="utf-8")
    _git(["init", "-q"], root)
    _git(["add", "."], root)
    return root


def _probe(root: Path) -> dict[str, Any]:
    locked = runtime_paths(repository_root())
    return probe_pyright_scope(
        root,
        Path("/root/miniconda3/envs/ms/bin/python"),
        locked["node"],
        locked["pyright"],
    )


def test_parser_uses_engine_graph_and_selected_config(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    config = root / "pyrightconfig.json"
    output = f"""\
Loading configuration file at {config}
Found 2 source files
pyright 1.1.403

src/imported.py
 Imports     1 file
    file:///outside/typeshed.pyi
 Imported by 1 file

1 file not explicitly imported
    {root.joinpath("src/root.py").as_uri()}
"""
    parsed = parse_pyright_dependency_output(output, root)
    assert parsed == {
        "selected_config_path": "pyrightconfig.json",
        "project_kind": "configured",
        "configured_source_count": 2,
        "engine_version": "1.1.403",
        "configured_program_paths": ["src/imported.py", "src/root.py"],
    }


def test_native_config_omission_is_allowed_with_file_level_evidence(tmp_path: Path) -> None:
    report = _probe(_fixture_root(tmp_path, "pyright-scope-compatible"))
    assert report["selected_config_path"] == "pyrightconfig.json"
    assert report["project_kind"] == "configured"
    assert report["configured_source_count"] == 2
    assert report["configured_program_paths"] == ["src/helper.py", "src/main.py"]
    assert report["configured_program_count"] == 2
    assert report["configured_program_digest"]
    assert report["trust_inventory_paths"] == ["omitted/trusted.py", "src/helper.py", "src/main.py"]
    assert report["trusted_not_in_configured_program"] == ["omitted/trusted.py"]
    assert report["configured_program_outside_trust"] == []
    assert report["configured_program_evidence"]["comparison_basis"] == "normalized_path_sets"
    assert report["configured_program_evidence"]["count_only_equivalence_rejected"] is True
    assert report["scope_compatible"] is True
    assert report["status"] == "pass"


def test_ignored_native_program_file_is_scope_incompatible(tmp_path: Path) -> None:
    report = _probe(_fixture_root(tmp_path, "pyright-scope-incompatible"))
    assert report["configured_program_paths"] == ["ignored-generated/hidden.py", "src/main.py"]
    assert report["trust_inventory_paths"] == ["src/main.py"]
    assert report["configured_program_outside_trust"] == ["ignored-generated/hidden.py"]
    assert report["difference_reasons"]["configured_program_outside_trust"] == [
        {"path": "ignored-generated/hidden.py", "reason": "git_ignored"}
    ]
    assert report["scope_compatible"] is False
    assert report["error"]["code"] == SCOPE_INCOMPATIBLE
    assert report["status"] == "fail"


def test_git_trust_filters_deleted_ignored_non_regular_and_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "git-root"
    root.mkdir()
    _git(["init", "-q"], root)
    (root / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
    (root / "kept.py").write_text("kept = True\n", encoding="utf-8")
    deleted = root / "deleted.py"
    deleted.write_text("deleted = True\n", encoding="utf-8")
    (root / "ignored.py").write_text("ignored = True\n", encoding="utf-8")
    outside = tmp_path / "outside.py"
    outside.write_text("outside = True\n", encoding="utf-8")
    (root / "escape.py").symlink_to(outside)
    fifo = root / "pipe.py"
    os.mkfifo(fifo)
    _git(["add", ".gitignore", "kept.py", "deleted.py"], root)
    _git(["add", "-f", "escape.py"], root)
    deleted.unlink()

    assert git_trust_inventory(root) == ["kept.py"]


def test_non_git_inventory_is_bounded_and_does_not_follow_symlinks(tmp_path: Path) -> None:
    root = tmp_path / "bounded"
    root.mkdir()
    (root / "visible.py").write_text("visible = True\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "external.py").write_text("external = True\n", encoding="utf-8")
    (root / "escape.py").symlink_to(outside / "external.py")
    (root / "escape-dir").symlink_to(outside, target_is_directory=True)
    hidden = root / ".hidden"
    hidden.mkdir()
    (hidden / "hidden.py").write_text("hidden = True\n", encoding="utf-8")

    assert bounded_trust_inventory(root) == ["visible.py"]


def test_native_config_omission_is_served_path_scoped_without_global_expansion(tmp_path: Path) -> None:
    root = _fixture_root(tmp_path, "pyright-scope-compatible")
    omitted = root / "omitted" / "trusted.py"
    original = omitted.read_bytes()
    locked = runtime_paths(repository_root())
    profile = Profile(
        "fixture",
        "python",
        root,
        "result",
        Path("src/main.py"),
        Path("/root/miniconda3/envs/ms/bin/python"),
    )
    settings = {
        "pythonPath": str(profile.interpreter),
        "analysis": {
            "diagnosticMode": "workspace",
            "autoSearchPaths": True,
            "useLibraryCodeForTypes": True,
        },
    }
    client = LspClient(
        [str(locked["node"]), str(locked["pyright-langserver"]), "--stdio"],
        root,
        settings,
    )
    try:
        client.start()
        client.request("initialize", initialize_params(profile), 10)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": settings})
        before = client.request("workspace/symbol", {"query": "omitted"}, 10) or []
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": path_uri(omitted),
                    "languageId": "python",
                    "version": 1,
                    "text": omitted.read_text(encoding="utf-8"),
                }
            },
        )
        symbols = client.request("textDocument/documentSymbol", {"textDocument": {"uri": path_uri(omitted)}}, 10)
        after = client.request("workspace/symbol", {"query": "omitted"}, 10) or []
    finally:
        client.close()

    assert before == []
    assert any(symbol.get("name") == "omitted" for symbol in symbols)
    assert after == []
    assert omitted.read_bytes() == original
    assert client.cleanup_ok is True
