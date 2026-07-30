from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionMapper
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
from serena_light.lsp.typescript_assignment_recovery import (
    TypeScriptAssignmentRecoveryReason,
    UnresolvedAssignmentSymbol,
)
from serena_light.workspace.scope import DifferenceReason, ProjectKind, ScopeCode

_VARIABLE_KIND = 13
_CONSTANT_KIND = 14


def _snapshot_and_mapper(source: str) -> tuple[FileSnapshot, PositionMapper]:
    snapshot = FileSnapshot.from_bytes(source.encode("utf-8"))
    return snapshot, PositionMapper(snapshot, PositionEncoding.UTF16)


def _identifier_only_symbol(
    mapper: PositionMapper,
    source: str,
    name: str,
    *,
    kind: int = _CONSTANT_KIND,
    occurrence: int = 0,
) -> dict[str, Any]:
    """Build a raw tsserver-shaped DocumentSymbol whose range is identifier-only."""

    start_offset = -1
    for _ in range(occurrence + 1):
        start_offset = source.index(name, start_offset + 1)
    end_offset = start_offset + len(name)
    start = mapper.text_offset_to_lsp(start_offset)
    end = mapper.text_offset_to_lsp(end_offset)
    selection = {
        "start": {"line": start.line, "character": start.character},
        "end": {"line": end.line, "character": end.character},
    }
    return {"name": name, "kind": kind, "range": dict(selection), "selectionRange": dict(selection), "children": []}


def _recovered_text(mapper: PositionMapper, source: str, raw_symbol: Mapping[str, Any]) -> str:
    start = raw_symbol["range"]["start"]
    end = raw_symbol["range"]["end"]
    start_offset = mapper.lsp_to_text_offset(_lsp(start))
    end_offset = mapper.lsp_to_text_offset(_lsp(end))
    return source[start_offset:end_offset]


def _lsp(position: dict[str, int]) -> LspPosition:
    return LspPosition(position["line"], position["character"])


def _selection_chain(
    mapper: PositionMapper,
    source: str,
    name: str,
    recovered_body: str,
) -> dict[str, Any]:
    """Build server-shaped syntax evidence from an explicit expected span."""

    identifier_start = source.index(name)
    body_start = source.index(recovered_body)
    body_end = body_start + len(recovered_body)
    statement_end = body_end + (source[body_end : body_end + 1] == ";")

    def raw_range(start_offset: int, end_offset: int) -> dict[str, dict[str, int]]:
        start = mapper.text_offset_to_lsp(start_offset)
        end = mapper.text_offset_to_lsp(end_offset)
        return {
            "start": {"line": start.line, "character": start.character},
            "end": {"line": end.line, "character": end.character},
        }

    binding_end = max(recovered_body.rfind("] ="), recovered_body.rfind("} =")) + 1
    if binding_end <= 0:
        raise ValueError("expected body does not contain a destructured binding assignment")
    binding = recovered_body[:binding_end]
    return {
        "range": raw_range(identifier_start, identifier_start + len(name)),
        "parent": {
            "range": raw_range(body_start, body_start + len(binding)),
            "parent": {"range": raw_range(0, statement_end)},
        },
    }


def _plain_declaration_symbol_and_chain(
    mapper: PositionMapper,
    source: str,
    name: str,
    server_body: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build the pinned server's identifier-start range and syntax ancestry."""

    identifier_start = source.index(name)
    body_start = source.index(server_body)
    assert body_start == identifier_start
    body_end = body_start + len(server_body)
    statement_end = len(source.rstrip("\n"))

    def raw_range(start_offset: int, end_offset: int) -> dict[str, dict[str, int]]:
        start = mapper.text_offset_to_lsp(start_offset)
        end = mapper.text_offset_to_lsp(end_offset)
        return {
            "start": {"line": start.line, "character": start.character},
            "end": {"line": end.line, "character": end.character},
        }

    selection = raw_range(identifier_start, identifier_start + len(name))
    symbol = {
        "name": name,
        "kind": _CONSTANT_KIND,
        "range": raw_range(body_start, body_end),
        "selectionRange": selection,
        "children": [],
    }
    return symbol, {
        "range": selection,
        "parent": {"range": raw_range(0, statement_end)},
    }


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


@pytest.mark.parametrize(
    ("source", "name", "server_body", "expected_body"),
    [
        (
            "export const multiline = (\n  1 +\n  2\n);\n",
            "multiline",
            "multiline = (\n  1 +\n  2\n)",
            "export const multiline = (\n  1 +\n  2\n);",
        ),
        (
            "declare const declared: number;\n",
            "declared",
            "declared: number",
            "declare const declared: number;",
        ),
        (
            "export declare let mutable: string;\n",
            "mutable",
            "mutable: string",
            "export declare let mutable: string;",
        ),
        ("var legacy = 1;\n", "legacy", "legacy = 1", "var legacy = 1;"),
        ("let mutable = 1\n", "mutable", "mutable = 1", "let mutable = 1"),
    ],
)
def test_typescript_adapter_recovers_identifier_start_variable_statements(
    tmp_path: Path,
    source: str,
    name: str,
    server_body: str,
    expected_body: str,
) -> None:
    """A server range beginning at the binding omits required declaration syntax."""

    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol, chain = _plain_declaration_symbol_and_chain(mapper, source, name, server_body)
    original_selection = dict(raw_symbol["selectionRange"])

    result = _config(tmp_path).recover_assignment_document_symbols(
        [raw_symbol],
        selection_ranges=[chain],
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert not result.unresolved
    (recovered,) = result.raw_symbols
    assert _recovered_text(mapper, source, recovered) == expected_body
    assert recovered["selectionRange"] == original_selection


def test_typescript_identifier_start_recovery_fails_body_closed_without_syntax_evidence(tmp_path: Path) -> None:
    source = "export const multiline = (\n  1 +\n  2\n);\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol, _chain = _plain_declaration_symbol_and_chain(
        mapper,
        source,
        "multiline",
        "multiline = (\n  1 +\n  2\n)",
    )

    result = _config(tmp_path).recover_assignment_document_symbols(
        [raw_symbol],
        selection_ranges=None,
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert result.raw_symbols == (raw_symbol,)
    assert result.body_incomplete_reason(raw_symbol) == TypeScriptAssignmentRecoveryReason.SELECTION_RANGE_UNAVAILABLE


@pytest.mark.parametrize(
    ("case_id", "source", "name", "expected_body"),
    [
        ("array-simple", "const [a, b] = [1, 2];\n", "a", "[a, b] = [1, 2];"),
        ("array-second", "const [a, b] = [1, 2];\n", "b", "[a, b] = [1, 2];"),
        (
            "object-shorthand",
            "const { objA, objB } = { objA: 1, objB: 2 };\n",
            "objA",
            "{ objA, objB } = { objA: 1, objB: 2 };",
        ),
        ("no-semicolon-asi", "const [a, b] = [1, 2]\nconsole.log(a);\n", "a", "[a, b] = [1, 2]"),
        (
            "multi-declarator-comma",
            "const [a, b] = [1, 2], c = 3;\n",
            "a",
            "[a, b] = [1, 2], c = 3;",
        ),
        (
            "trailing-line-comment",
            "const [a, b] = [1, 2]; // has ] and ; inside a comment\n",
            "a",
            "[a, b] = [1, 2];",
        ),
        (
            "string-with-brackets-and-semicolon",
            'const [a, b] = ["];", 2];\n',
            "a",
            '[a, b] = ["];", 2];',
        ),
        (
            "template-literal-interpolation",
            "const [a, b] = [`x${1 + 2}y`, 2];\n",
            "a",
            "[a, b] = [`x${1 + 2}y`, 2];",
        ),
        (
            "block-comment-inside-pattern",
            "const [/* first */ a, b] = [1, 2];\n",
            "a",
            "[/* first */ a, b] = [1, 2];",
        ),
        (
            "object-default-values",
            "const { a = 1, b = 2 } = {};\n",
            "a",
            "{ a = 1, b = 2 } = {};",
        ),
        ("let-keyword", "let [a, b] = [1, 2];\n", "a", "[a, b] = [1, 2];"),
        ("var-keyword", "var [x, y] = [1, 2];\n", "x", "[x, y] = [1, 2];"),
    ],
)
def test_typescript_adapter_recovers_identifier_only_destructured_ranges(
    tmp_path: Path,
    case_id: str,
    source: str,
    name: str,
    expected_body: str,
) -> None:
    """Task 3.3: an identifier-only top-level destructured binding range is
    expanded to the exact complete variable statement, never a truncated
    or comment/string-absorbing slice, while the identifier stays the
    selection range and name-path anchor."""

    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol = _identifier_only_symbol(mapper, source, name)
    original_selection = dict(raw_symbol["selectionRange"])

    result = _config(tmp_path).recover_assignment_document_symbols(
        [raw_symbol],
        selection_ranges=[_selection_chain(mapper, source, name, expected_body)],
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert not result.unresolved, case_id
    (recovered,) = result.raw_symbols
    declarator_end = source.index(expected_body) + len(expected_body)
    assert _recovered_text(mapper, source, recovered) == source[:declarator_end], case_id
    assert recovered["selectionRange"] == original_selection, "identifier stays the selection range anchor"
    assert result.incomplete_range_reason(name=name, selection_range=original_selection) is None


@pytest.mark.parametrize(
    ("case_id", "source", "name", "expected_reason"),
    [
        (
            "for-of-loop-is-not-a-declarator",
            "for (const [a, b] of pairs) {\n  console.log(a);\n}\n",
            "a",
            TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT,
        ),
        (
            "nested-pattern-is-out-of-scope",
            "const [[a], b] = [[1], 2];\n",
            "a",
            TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT,
        ),
        (
            "unterminated-string-fails-closed",
            'const [a, b] = ["oops, 2];\n',
            "a",
            TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT,
        ),
        (
            "not-inside-any-bracket",
            "const answer = 1;\n",
            "answer",
            TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT,
        ),
    ],
)
def test_typescript_adapter_assignment_recovery_fails_closed(
    tmp_path: Path,
    case_id: str,
    source: str,
    name: str,
    expected_reason: str,
) -> None:
    """Task 3.3: unsupported forms fail closed instead of guessing a range,
    keeping the original identifier-only range as an accurately labelled
    (if incomplete) location for ordinary lookup."""

    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol = _identifier_only_symbol(mapper, source, name)
    original_selection = dict(raw_symbol["selectionRange"])

    result = _config(tmp_path).recover_assignment_document_symbols(
        [raw_symbol],
        selection_ranges=[{"range": original_selection}],
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert result.unresolved[0].reason == expected_reason, case_id
    (unchanged,) = result.raw_symbols
    assert unchanged["range"] == unchanged["selectionRange"] == original_selection, case_id
    assert result.incomplete_range_reason(name=name, selection_range=original_selection) == expected_reason


def test_typescript_adapter_assignment_recovery_is_ambiguous_when_two_names_claim_the_same_span(
    tmp_path: Path,
) -> None:
    """Task 3.3: two differently named symbols reported for the exact same
    identifier span is an inconsistent/duplicate server response; recovery
    refuses to guess which name really owns that position."""

    source = "const [a, b] = [1, 2];\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    conflicting = dict(_identifier_only_symbol(mapper, source, "a")["selectionRange"])
    raw_symbols = [
        {
            "name": "a",
            "kind": _CONSTANT_KIND,
            "range": dict(conflicting),
            "selectionRange": dict(conflicting),
            "children": [],
        },
        {
            "name": "unexpected",
            "kind": _CONSTANT_KIND,
            "range": dict(conflicting),
            "selectionRange": dict(conflicting),
            "children": [],
        },
    ]

    result = _config(tmp_path).recover_assignment_document_symbols(
        raw_symbols,
        selection_ranges=None,
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert {item.reason for item in result.unresolved} == {TypeScriptAssignmentRecoveryReason.AMBIGUOUS}
    assert {item.name for item in result.unresolved} == {"a", "unexpected"}
    for recovered in result.raw_symbols:
        assert recovered["range"] == recovered["selectionRange"]


def test_typescript_adapter_plain_recovery_rejects_name_anchored_on_a_different_binding(
    tmp_path: Path,
) -> None:
    """A candidate whose ``name`` does not match the text at its own
    selection position is not real evidence of which binding it is: the
    server-reported name is untrusted and must never be trusted over the
    verified snapshot text. Recovery must fail closed instead of returning
    the unrelated ``other`` statement's body under the ``good`` name."""

    source = "const good = 1;\nconst other = 2;\n"
    snapshot, mapper = _snapshot_and_mapper(source)

    def raw_range(start_offset: int, end_offset: int) -> dict[str, dict[str, int]]:
        start = mapper.text_offset_to_lsp(start_offset)
        end = mapper.text_offset_to_lsp(end_offset)
        return {
            "start": {"line": start.line, "character": start.character},
            "end": {"line": end.line, "character": end.character},
        }

    identifier_start = source.index("other")
    identifier_end = identifier_start + len("other")
    server_body_end = identifier_start + len("other = 2")
    statement_start = source.index("const other")
    statement_end = statement_start + len("const other = 2;")

    selection = raw_range(identifier_start, identifier_end)
    mislabelled = {
        "name": "good",
        "kind": _CONSTANT_KIND,
        "range": raw_range(identifier_start, server_body_end),
        "selectionRange": selection,
        "children": [],
    }
    chain = {"range": dict(selection), "parent": {"range": raw_range(statement_start, statement_end)}}
    original_selection = dict(mislabelled["selectionRange"])

    result = _config(tmp_path).recover_assignment_document_symbols(
        [mislabelled],
        selection_ranges=[chain],
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert result.unresolved == (
        UnresolvedAssignmentSymbol(
            "good", original_selection, TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
        ),
    )
    assert result.raw_symbols == (mislabelled,)
    assert (
        result.incomplete_range_reason(name="good", selection_range=original_selection)
        == TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    )


def test_typescript_adapter_destructured_recovery_rejects_name_anchored_on_a_different_binding(
    tmp_path: Path,
) -> None:
    """Same anchor-mismatch guarantee for a destructured top-level binding:
    a candidate claiming to be ``a`` but positioned at ``b``'s identifier
    must not recover (or later expose) ``b``'s statement under ``a``."""

    source = "const [a, b] = [1, 2];\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol = _identifier_only_symbol(mapper, source, "b")
    mislabelled = dict(raw_symbol)
    mislabelled["name"] = "a"
    original_selection = dict(mislabelled["selectionRange"])
    chain = _selection_chain(mapper, source, "b", "[a, b] = [1, 2];")

    result = _config(tmp_path).recover_assignment_document_symbols(
        [mislabelled],
        selection_ranges=[chain],
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert result.unresolved == (
        UnresolvedAssignmentSymbol("a", original_selection, TypeScriptAssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT),
    )
    (unchanged,) = result.raw_symbols
    assert unchanged["range"] == unchanged["selectionRange"] == original_selection


def test_typescript_adapter_assignment_recovery_leaves_already_complete_ranges_untouched(tmp_path: Path) -> None:
    """A symbol whose ``range`` already differs from its ``selectionRange``
    is left alone: the server already reported a complete body for it
    (true for every non-destructured top-level form per the probe evidence)."""

    source = "const answer = 1;\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    selection = _identifier_only_symbol(mapper, source, "answer")["selectionRange"]
    start = mapper.text_offset_to_lsp(0)
    end = mapper.text_offset_to_lsp(len(source.rstrip("\n")) - 1)
    already_complete = {
        "name": "answer",
        "kind": _CONSTANT_KIND,
        "range": {
            "start": {"line": start.line, "character": start.character},
            "end": {"line": end.line, "character": end.character},
        },
        "selectionRange": selection,
        "children": [],
    }

    result = _config(tmp_path).recover_assignment_document_symbols(
        [already_complete],
        selection_ranges=None,
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert not result.unresolved
    assert result.raw_symbols[0]["range"] == already_complete["range"]


def test_typescript_adapter_assignment_recovery_leaves_nested_and_non_variable_symbols_untouched(
    tmp_path: Path,
) -> None:
    """Recovery only inspects top-level entries: nested ``children`` and
    non-variable kinds (e.g. a function) pass through unchanged."""

    source = "function f() {\n  return 1;\n}\n"
    snapshot, _mapper = _snapshot_and_mapper(source)
    function_symbol = {
        "name": "f",
        "kind": 12,  # SymbolKind.Function
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 2, "character": 1}},
        "selectionRange": {"start": {"line": 0, "character": 9}, "end": {"line": 0, "character": 10}},
        "children": [
            {
                "name": "inner",
                "kind": _CONSTANT_KIND,
                "range": {"start": {"line": 1, "character": 9}, "end": {"line": 1, "character": 9}},
                "selectionRange": {"start": {"line": 1, "character": 9}, "end": {"line": 1, "character": 9}},
                "children": [],
            }
        ],
    }

    result = _config(tmp_path).recover_assignment_document_symbols(
        [function_symbol],
        selection_ranges=None,
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert not result.unresolved
    assert result.raw_symbols == (function_symbol,)
