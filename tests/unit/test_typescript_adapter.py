from __future__ import annotations

import json
from pathlib import Path

import pytest

from serena_light.lsp.typescript import (
    LANGUAGE_IDS,
    TYPESCRIPT_EXTENSIONS,
    TypeScriptAdapterConfig,
    TypeScriptAdapterError,
    TypeScriptCapabilityFacts,
    TypeScriptScopeError,
    project_info_to_scope,
    select_default_entry,
)
from serena_light.workspace.scope import DifferenceReason, ProjectKind, ScopeCode


def _config(tmp_path: Path) -> TypeScriptAdapterConfig:
    node = tmp_path / "node" / "bin" / "node"
    language_server = tmp_path / "node-packages" / "node_modules" / "typescript-language-server" / "lib" / "cli.mjs"
    tsserver = tmp_path / "node-packages" / "node_modules" / "typescript" / "lib" / "tsserver.js"
    for path in (node, language_server, tsserver):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    return TypeScriptAdapterConfig(
        node_path=node,
        language_server_path=language_server,
        tsserver_path=tsserver,
        language_server_version="5.1.3",
        typescript_version="5.9.3",
        lock_digest="unit-lock",
    )


def test_fixed_extensions_language_ids_command_and_engine_ownership(tmp_path: Path) -> None:
    config = _config(tmp_path)

    assert (
        config.extensions
        == TYPESCRIPT_EXTENSIONS
        == (
            ".js",
            ".jsx",
            ".mjs",
            ".cjs",
            ".ts",
            ".tsx",
            ".mts",
            ".cts",
        )
    )
    assert (
        dict(config.language_ids)
        == dict(LANGUAGE_IDS)
        == {
            ".js": "javascript",
            ".jsx": "javascriptreact",
            ".mjs": "javascript",
            ".cjs": "javascript",
            ".ts": "typescript",
            ".tsx": "typescriptreact",
            ".mts": "typescript",
            ".cts": "typescript",
        }
    )
    assert config.command == (str(config.node_path), str(config.language_server_path), "--stdio")
    assert config.language_id("runtime/main.MJS") == "javascript"
    assert config.tsserver_path == config.language_server_path.parents[2] / "typescript/lib/tsserver.js"
    assert config.fixed_facts()["typescript_engine_version"] == "5.9.3"
    assert config.fixed_facts()["diagnostic_authority"] == "advisory"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = config.adapter_language_facts(workspace)
    assert shared.language_id_for("view.tsx") == "typescriptreact"
    assert shared.language_id_for("runtime.mjs") == "javascript"
    assert shared.engine.executable == config.language_server_path
    assert config.runtime_provider(workspace).environment == {
        "PATH": str(config.node_path.parent),
        "NODE_PATH": None,
    }

    with pytest.raises(TypeScriptAdapterError, match="unsupported"):
        config.language_id("main.py")


def test_initialize_params_are_native_and_use_only_the_locked_tsserver(tmp_path: Path) -> None:
    config = _config(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    params = config.initialize_params(workspace, process_id=17)

    assert params["processId"] == 17
    assert params["rootPath"] == str(workspace)
    assert params["rootUri"] == workspace.as_uri()
    assert params["workspaceFolders"] == [{"uri": workspace.as_uri(), "name": "workspace"}]
    assert params["capabilities"]["workspace"]["configuration"] is True
    assert params["capabilities"]["textDocument"]["implementation"] == {"dynamicRegistration": True}
    assert params["capabilities"]["general"]["positionEncodings"] == ["utf-16", "utf-8", "utf-32"]
    assert params["initializationOptions"] == {
        "preferences": {"disableAutomaticTypingAcquisition": True},
        "tsserver": {"path": str(config.tsserver_path)},
    }
    assert "exclude" not in params["initializationOptions"]
    assert "plugins" not in params["initializationOptions"]


def test_raw_providers_are_separate_from_derived_tools_and_default_to_utf16() -> None:
    facts = TypeScriptCapabilityFacts.from_initialize_result(
        {
            "capabilities": {
                "definitionProvider": True,
                "implementationProvider": {"documentSelector": None},
                "referencesProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
            }
        }
    )

    assert dict(facts.raw_providers) == {
        "definitionProvider": True,
        "declarationProvider": False,
        "implementationProvider": True,
        "referencesProvider": True,
        "documentSymbolProvider": True,
        "workspaceSymbolProvider": True,
    }
    assert facts.derived_tools["find_declaration"] is True
    assert facts.derived_tools["find_implementations"] is True
    assert facts.derived_tools["find_referencing_symbols"] is True
    assert facts.position_encoding == "utf-16"


def test_invalid_position_encoding_fails_fast() -> None:
    with pytest.raises(TypeScriptAdapterError, match="position encoding"):
        TypeScriptCapabilityFacts.from_initialize_result({"capabilities": {"positionEncoding": "utf-7"}})


def test_project_info_uses_native_config_and_reports_stable_scope_differences(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    for relative in ("src/main.ts", "src/helper.ts", "ignored-generated/hidden.ts", "omitted/trusted.ts"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export const value = true;\n", encoding="utf-8")

    attributed = project_info_to_scope(
        root,
        trust_inventory_paths=("src/main.ts", "src/helper.ts", "omitted/trusted.ts"),
        entry_path="src/main.ts",
        project_info_body={
            "configFileName": str(root / "tsconfig.json"),
            "fileNames": [
                str(root / "src/main.ts"),
                str(root / "src/helper.ts"),
                str(root / "ignored-generated/hidden.ts"),
                "/external/typescript/lib/lib.es2022.d.ts",
            ],
        },
    )

    projection = attributed.projection
    assert projection.project_kind is ProjectKind.CONFIGURED
    assert projection.selected_config_path == "tsconfig.json"
    assert projection.trust_inventory.paths == ("omitted/trusted.ts", "src/helper.ts", "src/main.ts")
    assert projection.configured_program.paths == (
        "ignored-generated/hidden.ts",
        "src/helper.ts",
        "src/main.ts",
    )
    assert [(item.path, item.reason) for item in projection.trusted_not_in_configured_program] == [
        ("omitted/trusted.ts", DifferenceReason.EXCLUDED_BY_NATIVE_CONFIG)
    ]
    assert [(item.path, item.reason) for item in projection.configured_program_outside_trust] == [
        ("ignored-generated/hidden.ts", DifferenceReason.ABSENT_FROM_GIT_TRUST_INVENTORY)
    ]
    assert projection.overlay_generated is False
    assert projection.error is not None
    assert projection.error.code is ScopeCode.SCOPE_INCOMPATIBLE
    assert attributed.status_facts()["scope_compatible"] is False
    assert attributed.status_facts()["overlay_generated"] is False
    with pytest.raises(TypeScriptScopeError) as raised:
        attributed.require_compatible()
    assert raised.value.code == "SCOPE_INCOMPATIBLE"
    assert raised.value.paths == ("ignored-generated/hidden.ts",)


def test_rejected_symlink_never_enters_projection_and_still_fails_scope(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    source = root / "src/main.ts"
    source.parent.mkdir()
    source.write_text("export const value = true;\n", encoding="utf-8")
    link = root / "src/link.ts"
    link.symlink_to("main.ts")

    attributed = project_info_to_scope(
        root,
        trust_inventory_paths=("src/main.ts",),
        entry_path="src/main.ts",
        project_info_body={
            "configFileName": str(root / "tsconfig.json"),
            "fileNames": [str(source), str(link)],
        },
    )

    assert attributed.projection.configured_program.paths == ("src/main.ts",)
    assert [(item.path, item.reason) for item in attributed.rejected_configured_paths] == [
        ("src/link.ts", DifferenceReason.SYMLINK_OR_ESCAPE)
    ]
    assert attributed.scope_compatible is False
    with pytest.raises(TypeScriptScopeError) as raised:
        attributed.require_compatible()
    assert raised.value.paths == ("src/link.ts",)


def test_inferred_project_info_has_no_selected_native_config(tmp_path: Path) -> None:
    source = tmp_path / "loose.mjs"
    source.write_text("export const loose = true;\n", encoding="utf-8")

    attributed = project_info_to_scope(
        tmp_path,
        trust_inventory_paths=("loose.mjs",),
        entry_path="loose.mjs",
        project_info_body={
            "configFileName": "/dev/null/inferredProject1*",
            "fileNames": [str(source)],
        },
    )

    assert attributed.projection.project_kind is ProjectKind.WORKSPACE_DEFAULT
    assert attributed.projection.selected_config_path is None
    assert attributed.scope_compatible is True


def test_select_default_entry_prefers_native_config_include_over_alphabetical_config_file(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "tsconfig.json").write_text(
        json.dumps({"include": ["runtime/**/*.mjs"], "exclude": ["node_modules", "tests"]}),
        encoding="utf-8",
    )
    (root / "eslint.config.mjs").write_text("export default [];\n", encoding="utf-8")
    (root / "runtime").mkdir()
    (root / "runtime/args.mjs").write_text("export const args = [];\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests/args.test.mjs").write_text("export const t = 1;\n", encoding="utf-8")

    entry = select_default_entry(root, ("eslint.config.mjs", "runtime/args.mjs", "tests/args.test.mjs"))

    assert entry == "runtime/args.mjs"


def test_select_default_entry_falls_back_to_first_path_without_a_root_native_config(tmp_path: Path) -> None:
    entry = select_default_entry(tmp_path.resolve(), ("b.ts", "a.ts"))

    assert entry == "a.ts"


def test_select_default_entry_rejects_an_empty_path_set(tmp_path: Path) -> None:
    with pytest.raises(TypeScriptAdapterError, match="no TypeScript-family paths"):
        select_default_entry(tmp_path.resolve(), ())


def test_engine_owned_library_files_are_excluded_but_other_outside_trust_paths_still_fail(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    source = root / "src/main.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = true;\n", encoding="utf-8")
    hidden = root / "generated/hidden.ts"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("export const generated = true;\n", encoding="utf-8")
    engine_lib = root / ".codex/runtime/serena-light/deps/lockhash/typescript/lib"
    engine_lib.mkdir(parents=True)
    lib_dts = engine_lib / "lib.es2022.d.ts"
    lib_dts.write_text("// lib\n", encoding="utf-8")

    attributed = project_info_to_scope(
        root,
        trust_inventory_paths=("src/main.ts",),
        entry_path="src/main.ts",
        project_info_body={
            "configFileName": str(root / "tsconfig.json"),
            "fileNames": [str(source), str(lib_dts), str(hidden)],
        },
        engine_library_dir=engine_lib,
    )

    projection = attributed.projection
    assert projection.configured_program.paths == ("generated/hidden.ts", "src/main.ts")
    assert [(item.path, item.reason) for item in projection.configured_program_outside_trust] == [
        ("generated/hidden.ts", DifferenceReason.ABSENT_FROM_GIT_TRUST_INVENTORY)
    ]
    assert attributed.scope_compatible is False


def test_real_locked_engine_library_file_is_excluded_from_configured_program(tmp_path: Path) -> None:
    config = TypeScriptAdapterConfig.locked()
    root = tmp_path.resolve()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    source = root / "src/main.ts"
    source.parent.mkdir(parents=True)
    source.write_text("export const value = true;\n", encoding="utf-8")
    real_lib_dts = config.tsserver_path.parent / "lib.es2022.d.ts"
    assert real_lib_dts.is_file()

    attributed = project_info_to_scope(
        root,
        trust_inventory_paths=("src/main.ts",),
        entry_path="src/main.ts",
        project_info_body={
            "configFileName": str(root / "tsconfig.json"),
            "fileNames": [str(source), str(real_lib_dts)],
        },
        engine_library_dir=config.tsserver_path.resolve(strict=True).parent,
    )

    assert attributed.projection.configured_program.paths == ("src/main.ts",)
    assert attributed.scope_compatible is True
