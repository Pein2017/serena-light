from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.navigation import (
    DocumentNavigation,
    DocumentNavigationService,
    DocumentSymbolInput,
    find_symbol,
    get_symbols_overview,
)


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _document(*, mjs: bool = False) -> DocumentSymbolInput:
    text = (
        "// 😀\r\nexport class Café {\r\n  launch🚀() { return 'ok'; }\r\n}\r\n"
        if mjs
        else "# 😀\r\nclass Café:\r\n    def launch🚀(self):\r\n        return 'ok'\r\n"
    )
    raw = [
        {
            "name": "Café",
            "kind": 5,
            "range": _range(1, 0, 4 if not mjs else 3, 0),
            "selectionRange": _range(1, 6 if not mjs else 13, 1, 10 if not mjs else 17),
            "detail": "class detail",
            "children": [
                {
                    "name": "launch🚀",
                    "kind": 6,
                    "range": _range(2, 4 if not mjs else 2, 3 if not mjs else 2, 0 if not mjs else 29),
                    "selectionRange": _range(2, 8 if not mjs else 2, 2, 16 if not mjs else 10),
                    "detail": "method detail",
                }
            ],
        }
    ]
    return DocumentSymbolInput(
        "src/example.mjs" if mjs else "src/example.py",
        "file:///repo/src/example.mjs" if mjs else "file:///repo/src/example.py",
        FileSnapshot.from_bytes(text.encode()),
        raw,
        PositionEncoding.UTF16,
        WorkspaceMetadata("/repo", "git", "/repo"),
        AdapterMetadata("typescript" if mjs else "pyright", "typescript" if mjs else "python"),
        GenerationMetadata(trust=1, program=2, document=3, index=4),
    )


def _mutable_raw() -> list[dict[str, Any]]:
    raw = _document().raw_symbols
    assert raw is not None
    return cast(list[dict[str, Any]], deepcopy(raw))


def test_python_overview_defaults_to_root_depth_and_uses_one_normalized_tree() -> None:
    document = DocumentNavigation.from_input(_document())

    value = get_symbols_overview(document).to_dict()

    assert value["ok"] is True
    assert value["data"]["max_depth"] == 0
    assert value["data"]["depth_truncated"] is True
    symbol = value["data"]["symbols"][0]
    assert symbol["name_path"] == "Café"
    assert symbol["range"]["start"] == {"line": 1, "column": 0, "text_offset": 5, "byte_offset": 8}
    assert symbol["children"] == []
    assert symbol["children_truncated"] is True
    assert value["generations"] == {"trust": 1, "program": 2, "document": 3, "index": 4}


def test_mjs_find_symbol_uses_utf16_astral_offsets_body_info_and_file_hash() -> None:
    document = DocumentNavigation.from_input(_document(mjs=True))

    value = find_symbol(document, "Café/launch🚀", include_body=True, include_info=True).to_dict()

    assert value["ok"] is True
    result = value["data"]
    assert len(result["sha256"]) == 64
    assert result["symbol"]["body"] == "launch🚀() { return 'ok'; }"
    assert result["symbol"]["info"]["detail"] == "method detail"
    # The LSP body end uses UTF-16 character 29 while source columns remain
    # decoded Unicode code-point columns; CRLF still has two raw bytes.
    assert result["symbol"]["range"]["end"] == {"line": 2, "column": 28, "text_offset": 55, "byte_offset": 62}
    assert result["symbol"]["info"]["selection_range"]["end"]["column"] == 9


def test_mjs_file_scoped_symbol_miss_uses_the_same_overview_recovery_action() -> None:
    document = DocumentNavigation.from_input(_document(mjs=True))

    value = find_symbol(document, "missing").to_dict()

    assert value["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert value["error"]["details"]["next_action"] == "get_symbols_overview"


def test_serena_name_path_suffix_absolute_and_last_segment_substring_matching() -> None:
    raw = _mutable_raw()
    nested_class = raw[0]
    nested_class["name"] = "Package"
    nested_class["children"][0]["name"] = "Café"
    nested_class["children"][0]["children"] = [
        {"name": "launch🚀", "kind": 6, "range": _range(2, 4, 3, 0)},
    ]
    raw.append(
        {
            "name": "Café",
            "kind": 5,
            "range": _range(1, 0, 4, 0),
            "children": [{"name": "launch🚀", "kind": 6, "range": _range(2, 4, 3, 0)}],
        }
    )
    input_value = DocumentSymbolInput("src/example.py", "file:///repo/src/example.py", _document().snapshot, raw)

    @dataclass
    class Provider:
        calls: list[str]

        def load_document_symbols(self, relative_path: str) -> DocumentSymbolInput:
            self.calls.append(relative_path)
            return input_value

    provider = Provider([])
    service = DocumentNavigationService(provider)
    simple = service.find_symbol("src/example.py", "launch🚀").to_dict()
    relative = service.find_symbol("src/example.py", "Café/launch🚀").to_dict()
    absolute = service.find_symbol("src/example.py", "/Café/launch🚀").to_dict()
    last_segment_substring = service.find_symbol("src/example.py", "Café/aunch", substring_matching=True).to_dict()
    nonfinal_substring = service.find_symbol("src/example.py", "afé/aunch", substring_matching=True).to_dict()

    for matched in (simple, relative, last_segment_substring):
        assert matched["error"]["code"] == "AMBIGUOUS_SYMBOL"
        assert [item["name_path"] for item in matched["error"]["details"]["candidates"]] == [
            "Café/launch🚀",
            "Package/Café/launch🚀",
        ]
    assert absolute["ok"] is True
    assert absolute["data"]["symbol"]["name_path"] == "Café/launch🚀"
    assert nonfinal_substring["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert nonfinal_substring["error"]["details"]["next_action"] == "get_symbols_overview"
    assert provider.calls == ["src/example.py"] * 5


def test_path_scoped_provider_loads_one_selected_file_without_workspace_walk() -> None:
    raw = _mutable_raw()
    raw[0]["children"].append({"name": "launch_pad", "kind": 6, "range": _range(3, 4, 4, 0)})
    input_value = DocumentSymbolInput("src/example.py", "file:///repo/src/example.py", _document().snapshot, raw)

    @dataclass
    class Provider:
        calls: list[str]

        def load_document_symbols(self, relative_path: str) -> DocumentSymbolInput:
            self.calls.append(relative_path)
            return input_value

    provider = Provider([])
    value = DocumentNavigationService(provider).find_symbol(
        "src/example.py",
        "Café/launch",
        substring_matching=True,
    ).to_dict()

    assert value["error"]["code"] == "AMBIGUOUS_SYMBOL"
    assert [item["name_path"] for item in value["error"]["details"]["candidates"]] == [
        "Café/launch🚀",
        "Café/launch_pad",
    ]
    assert provider.calls == ["src/example.py"]


def test_directory_scope_searches_only_the_explicit_inventory_selection() -> None:
    base = _document()

    def document(path: str) -> DocumentSymbolInput:
        return DocumentSymbolInput(
            path,
            f"file:///repo/{path}",
            base.snapshot,
            base.raw_symbols,
            base.position_encoding,
            base.workspace,
            base.adapter,
            base.generations,
        )

    documents = {
        "src/a.py": document("src/a.py"),
        "src/nested/b.py": document("src/nested/b.py"),
        "sibling/c.py": document("sibling/c.py"),
    }

    @dataclass
    class Provider:
        calls: list[str]

        def load_document_symbols(self, relative_path: str) -> DocumentSymbolInput:
            self.calls.append(relative_path)
            return documents[relative_path]

    provider = Provider([])
    value = DocumentNavigationService(provider).find_symbol_in_documents(
        ("src/nested/b.py", "src/a.py"),
        "/Café/launch🚀",
        relative_scope="src",
        include_body=True,
    ).to_dict()

    assert value["ok"] is True
    assert [item["relative_path"] for item in value["data"]["symbols"]] == [
        "src/a.py",
        "src/nested/b.py",
    ]
    assert all("def launch🚀" in item["symbol"]["body"] for item in value["data"]["symbols"])
    assert provider.calls == ["src/a.py", "src/nested/b.py"]

    missing = DocumentNavigationService(provider).find_symbol_in_documents(
        ("src/nested/b.py", "src/a.py"),
        "missing",
        relative_scope="src",
    ).to_dict()
    assert missing["error"]["code"] == "SYMBOL_NOT_FOUND"
    assert "next_action" not in missing["error"]["details"]


def test_overview_and_ambiguity_are_deterministically_bounded() -> None:
    document = DocumentNavigation.from_input(_document())
    overview = get_symbols_overview(document, max_depth=2, max_answer_chars=10).to_dict()

    assert overview["ok"] is True
    assert overview["data"]["symbols"] == []
    assert overview["truncation"] == {"truncated": True, "omitted_count": 1}


def test_ambiguity_candidates_use_private_error_budget_not_success_budget() -> None:
    names = [f"ambiguous_candidate_{index:03d}_{'x' * 48}" for index in range(80)]
    snapshot = FileSnapshot.from_bytes("".join(f"{name}\n" for name in names).encode())
    document = DocumentNavigation.from_input(
        DocumentSymbolInput(
            "src/ambiguous.py",
            "file:///repo/src/ambiguous.py",
            snapshot,
            [
                {
                    "name": name,
                    "kind": 12,
                    "range": _range(index, 0, index, len(name)),
                    "selectionRange": _range(index, 0, index, len(name)),
                }
                for index, name in enumerate(names)
            ],
            PositionEncoding.UTF16,
            WorkspaceMetadata("/repo", "git", "/repo"),
        )
    )

    narrow = find_symbol(
        document,
        "ambiguous",
        substring_matching=True,
        max_answer_chars=2_147_483_647,
        _error_max_answer_chars=512,
    ).to_dict()
    wide = find_symbol(
        document,
        "ambiguous",
        substring_matching=True,
        max_answer_chars=2_147_483_647,
        _error_max_answer_chars=12_000,
    ).to_dict()

    narrow_details = narrow["error"]["details"]
    wide_details = wide["error"]["details"]
    assert narrow_details["truncated"] is True
    assert narrow_details["omitted_count"] > 0
    assert len(narrow_details["candidates"]) < len(wide_details["candidates"])
    assert len(narrow_details["candidates"]) < len(names)
