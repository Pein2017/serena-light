from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from mcp import types

from serena_light.tools.compact import (
    CompactFile,
    CompactNavigationSuccess,
    CompactOverviewSymbol,
    CompactSymbolMatch,
    LocatedCompactRecord,
    canonical_json,
    group_records,
    minimum_required_chars_result,
)
from serena_light.tools.compact_adapter import compact_navigation_result

_SHA = "a" * 64


def _range(line: int, start: int, end: int) -> dict[str, object]:
    return {
        "start": {"line": line, "column": start, "text_offset": 100, "byte_offset": 104},
        "end": {"line": line, "column": end, "text_offset": 110, "byte_offset": 114},
    }


def _envelope(data: Mapping[str, object], *, omitted: int = 0) -> dict[str, object]:
    return {
        "ok": True,
        "data": dict(data),
        "workspace": {"root": "/repo", "kind": "git", "working_subdirectory": "/repo/sub"},
        "adapter": {"name": "pyright", "language": "python", "phase": "ready"},
        "generations": {"trust": 2, "program": 3, "document": 4, "index": 5},
        "truncation": {"truncated": omitted > 0, "omitted_count": omitted},
    }


def _payload(result: types.CallToolResult) -> dict[str, Any]:
    assert result.isError is False
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    payload = json.loads(block.text)
    assert payload == result.structuredContent
    return cast(dict[str, Any], payload)


def test_symbol_success_groups_files_limits_matches_and_drops_runtime_metadata() -> None:
    items = [
        {
            "name_path": name,
            "kind": 12,
            "relative_path": path,
            "location": {"uri": f"file:///repo/{path}", "range": _range(line, 0, 4)},
            "adapter": {"name": "pyright", "language": "python", "phase": "ready"},
            "generations": {"trust": 2, "program": 3, "document": line, "index": 5},
        }
        for name, path, line in (("z", "src/z.py", 2), ("a", "src/a.py", 1), ("b", "src/a.py", 3))
    ]
    result = compact_navigation_result(
        "find_symbol",
        _envelope({"scope": "configured_program", "adapters": [], "symbols": items}, omitted=2),
        max_matches=2,
    )
    payload = _payload(result)

    assert payload == {
        "ok": True,
        "data": {
            "workspace": "/repo",
            "files": [
                {
                    "path": "src/a.py",
                    "symbols": [
                        {"name_path": "a", "kind": "function", "range": [[1, 0], [1, 4]]},
                        {"name_path": "b", "kind": "function", "range": [[3, 0], [3, 4]]},
                    ],
                }
            ],
            "omitted": 3,
        },
    }
    rendered = cast(types.TextContent, result.content[0]).text
    for forbidden in ("adapter", "generations", "configured_program", "uri", "text_offset", "byte_offset"):
        assert forbidden not in rendered


def test_global_symbol_success_retains_language_once_per_mixed_file_group() -> None:
    symbols = [
        {
            "name_path": "Shared",
            "kind": 5,
            "relative_path": path,
            "location": {"uri": f"file:///repo/{path}", "range": _range(0, 0, 6)},
            "adapter": {"name": adapter, "language": language},
        }
        for path, adapter, language in (
            ("src/shared.py", "pyright", "python"),
            ("src/shared.ts", "typescript", "typescript"),
        )
    ]

    payload = _payload(
        compact_navigation_result(
            "find_symbol",
            _envelope({"scope": "configured_program", "adapters": [], "symbols": symbols}),
        )
    )

    files = payload["data"]["files"]
    assert [file["language"] for file in files] == ["python", "typescript"]
    expected = [{"name_path": "Shared", "kind": "class", "range": [[0, 0], [0, 6]]}]
    assert all(file["symbols"] == expected for file in files)


def test_overview_filters_before_compact_tree_rendering() -> None:
    result = compact_navigation_result(
        "get_symbols_overview",
        _envelope(
            {
                "relative_path": "src/main.py",
                "sha256": _SHA,
                "symbols": [
                    {
                        "name": "kept",
                        "kind": 12,
                        "range": _range(0, 0, 4),
                        "detail": "discard",
                        "children": [{"name": "inner", "kind": 13, "children": []}],
                        "children_truncated": False,
                    },
                    {"name": "dropped", "kind": 5, "children": []},
                ],
            }
        ),
        include_kinds=["function", "variable"],
    )
    payload = _payload(result)

    assert payload["data"] == {
        "workspace": "/repo",
        "files": [
            {
                "path": "src/main.py",
                "symbols": [
                    {
                        "name": "kept",
                        "kind": "function",
                        "children": [{"name": "inner", "kind": "variable"}],
                    }
                ],
            }
        ],
        "omitted": 1,
    }


def test_overview_filters_retain_ancestor_paths_and_count_only_removed_nodes() -> None:
    data = {
        "relative_path": "src/tree.py",
        "symbols": [
            {
                "name": "Container",
                "kind": 5,
                "children": [
                    {"name": "kept_method", "kind": 6, "children": []},
                    {"name": "dropped_field", "kind": 8, "children": []},
                    {"name": "unknown", "kind": 99, "children": []},
                ],
            },
            {"name": "dropped_function", "kind": 12, "children": []},
        ],
    }

    included = _payload(compact_navigation_result("get_symbols_overview", _envelope(data), include_kinds=["method"]))
    excluded = _payload(compact_navigation_result("get_symbols_overview", _envelope(data), exclude_kinds=["class"]))
    overlap = _payload(
        compact_navigation_result(
            "get_symbols_overview",
            _envelope(data),
            include_kinds=["method", "unknown:99"],
            exclude_kinds=["unknown:99"],
        )
    )

    assert included["data"] == {
        "workspace": "/repo",
        "files": [
            {
                "path": "src/tree.py",
                "symbols": [
                    {
                        "name": "Container",
                        "kind": "class",
                        "children": [{"name": "kept_method", "kind": "method"}],
                    }
                ],
            }
        ],
        "omitted": 3,
    }
    assert excluded["data"]["omitted"] == 0
    assert excluded["data"]["files"][0]["symbols"][0]["kind"] == "class"
    assert overlap["data"] == included["data"]

    filtered_empty = _payload(
        compact_navigation_result(
            "get_symbols_overview",
            _envelope({"relative_path": "src/empty.py", "symbols": [{"name": "Only", "kind": 5}]}),
            include_kinds=["method"],
        )
    )
    assert filtered_empty["data"] == {"workspace": "/repo", "files": [], "omitted": 1}


def test_overview_combines_depth_envelope_and_budget_omissions() -> None:
    suffix = "x" * 180
    result = compact_navigation_result(
        "get_symbols_overview",
        _envelope(
            {
                "relative_path": "src/tree.py",
                "depth_omitted_count": 2,
                "symbols": [
                    {
                        "name": "Root",
                        "kind": 5,
                        "children": [
                            {"name": "first" + suffix, "kind": 12, "children": []},
                            {"name": "second" + suffix, "kind": 12, "children": []},
                            {"name": "third" + suffix, "kind": 12, "children": []},
                        ],
                    }
                ],
            },
            omitted=3,
        ),
        max_answer_chars=512,
    )
    payload = _payload(result)

    assert payload["data"]["omitted"] == 7
    children = payload["data"]["files"][0]["symbols"][0]["children"]
    assert [child["name"] for child in children] == ["first" + suffix]


def test_overview_budget_removes_newly_childless_structural_ancestors_and_counts_them() -> None:
    result = compact_navigation_result(
        "get_symbols_overview",
        _envelope(
            {
                "relative_path": "src/tree.py",
                "symbols": [
                    {"name": "kept", "kind": 6, "children": []},
                    {
                        "name": "Container" + "x" * 180,
                        "kind": 5,
                        "children": [{"name": "tail" + "y" * 180, "kind": 6, "children": []}],
                    },
                ],
            }
        ),
        include_kinds=["method"],
        max_answer_chars=512,
    )
    payload = _payload(result)

    assert payload["data"] == {
        "workspace": "/repo",
        "files": [{"path": "src/tree.py", "symbols": [{"name": "kept", "kind": "method"}]}],
        "omitted": 2,
    }


def test_descendant_only_overview_budget_never_returns_a_lone_structural_ancestor() -> None:
    result = compact_navigation_result(
        "get_symbols_overview",
        _envelope(
            {
                "relative_path": "src/tree.py",
                "symbols": [
                    {
                        "name": "Container" + "x" * 240,
                        "kind": 5,
                        "children": [{"name": "match" + "y" * 240, "kind": 6, "children": []}],
                    }
                ],
            }
        ),
        include_kinds=["method"],
        max_answer_chars=512,
    )
    payload = _payload(result)

    assert payload["ok"] is False
    minimum = payload["error"]["details"]["minimum_required_chars"]
    assert minimum > 512
    retried = _payload(
        compact_navigation_result(
            "get_symbols_overview",
            _envelope(
                {
                    "relative_path": "src/tree.py",
                    "symbols": [
                        {
                            "name": "Container" + "x" * 240,
                            "kind": 5,
                            "children": [{"name": "match" + "y" * 240, "kind": 6, "children": []}],
                        }
                    ],
                }
            ),
            include_kinds=["method"],
            max_answer_chars=minimum,
        )
    )
    assert retried["ok"] is True
    assert retried["data"]["files"][0]["symbols"][0]["children"][0]["kind"] == "method"


def test_oversized_navigation_results_preserve_rich_authority_only_on_error() -> None:
    body = "x" * 900
    record = LocatedCompactRecord(
        "src/huge.py",
        CompactSymbolMatch("huge", 12, ((0, 0), (0, 4)), body=body),
        sha256=_SHA,
    )
    record_minimum = len(canonical_json(CompactNavigationSuccess("/repo", group_records([record]))))
    overview = CompactFile("src/huge.py", "symbols", (CompactOverviewSymbol("Root" + body, 5),))
    overview_minimum = len(canonical_json(CompactNavigationSuccess("/repo", (overview,))))
    record_error = _payload(
        compact_navigation_result(
            "find_symbol",
            _envelope(
                {
                    "relative_path": "src/huge.py",
                    "symbol": {"name_path": "huge", "kind": 12, "range": _range(0, 0, 4), "body": body},
                    "sha256": _SHA,
                }
            ),
            max_answer_chars=512,
        )
    )
    overview_error = _payload(
        compact_navigation_result(
            "get_symbols_overview",
            _envelope({"relative_path": "src/huge.py", "symbols": [{"name": "Root" + body, "kind": 5}]}),
            max_answer_chars=512,
        )
    )

    for payload, expected_minimum in ((record_error, record_minimum), (overview_error, overview_minimum)):
        assert payload["ok"] is False
        assert payload["error"]["details"]["field"] == "max_answer_chars"
        assert payload["error"]["details"]["minimum_required_chars"] == expected_minimum
        assert payload["workspace"] == {"root": "/repo", "kind": "git", "working_subdirectory": "/repo/sub"}
        assert payload["adapter"] == {"name": "pyright", "language": "python"}
        assert payload["generations"] == {"trust": 2, "program": 3, "document": 4, "index": 5}


def test_multi_adapter_minimum_budget_error_preserves_all_item_authorities() -> None:
    symbols = [
        {
            "name_path": "PythonHuge",
            "kind": 12,
            "relative_path": "src/a.py",
            "location": {"uri": "file:///repo/src/a.py", "range": _range(0, 0, 10)},
            "adapter": {"name": "pyright", "language": "python"},
            "generations": {"trust": 2, "program": 3, "document": 4, "index": 5},
            "body": "p" * 900,
            "sha256": "a" * 64,
        },
        {
            "name_path": "TypeScriptHuge",
            "kind": 12,
            "relative_path": "src/b.ts",
            "location": {"uri": "file:///repo/src/b.ts", "range": _range(0, 0, 14)},
            "adapter": {"name": "typescript", "language": "typescript"},
            "generations": {"trust": 2, "program": 7, "document": 8, "index": 9},
            "body": "t" * 900,
            "sha256": "b" * 64,
        },
    ]
    envelope = _envelope({"scope": "configured_program", "adapters": [], "symbols": symbols})
    envelope.pop("adapter")
    envelope.pop("generations")

    payload = _payload(compact_navigation_result("find_symbol", envelope, max_answer_chars=512))

    assert payload["ok"] is False
    assert "adapter" not in payload and "generations" not in payload
    assert payload["error"]["details"]["authorities"] == [
        {
            "adapter": {"name": "pyright", "language": "python"},
            "generations": {"trust": 2, "program": 3, "document": 4, "index": 5},
        },
        {
            "adapter": {"name": "typescript", "language": "typescript"},
            "generations": {"trust": 2, "program": 7, "document": 8, "index": 9},
        },
    ]


def test_references_group_by_file_and_retain_coverage_once() -> None:
    coverage = {"adapter": "pyright", "language": "python", "scope_kind": "configured_program"}
    result = compact_navigation_result(
        "find_referencing_symbols",
        _envelope(
            {
                "references": [
                    {
                        "path": "src/main.py",
                        "read_only_external": False,
                        "location": _range(4, 2, 8),
                        "container": {"kind": "symbol", "name_path": "run", "symbol_kind": 12},
                        "snippet": "run()",
                        "snippet_truncated": False,
                    }
                ],
                "coverage": coverage,
            }
        ),
        include_snippets=True,
    )
    payload = _payload(result)

    assert payload["data"]["coverage"] == coverage
    assert payload["data"]["files"] == [
        {
            "path": "src/main.py",
            "references": [{"range": [[4, 2], [4, 8]], "symbol": "run", "snippet": "run()"}],
        }
    ]
    assert cast(types.TextContent, result.content[0]).text.count('"coverage"') == 1


def test_verified_body_target_hash_and_raw_external_target_are_lossless() -> None:
    verified = compact_navigation_result(
        "find_declaration",
        _envelope(
            {
                "locations": [
                    {
                        "absolute_path": "/repo/src/value.py",
                        "relative_path": "src/value.py",
                        "location_kind": "workspace",
                        "kind": 13,
                        "range": _range(0, 0, 9),
                        "body": "value = 1",
                        "sha256": _SHA,
                    }
                ]
            }
        ),
    )
    assert _payload(verified)["data"]["files"] == [
        {
            "path": "src/value.py",
            "sha256": _SHA,
            "targets": [{"range": [[0, 0], [0, 9]], "kind": "variable", "body": "value = 1"}],
        }
    ]

    external = compact_navigation_result(
        "find_declaration",
        _envelope(
            {
                "locations": [
                    {
                        "absolute_path": "/opt/site-packages/pkg/value.py",
                        "location_kind": "read_only_external",
                        "read_only_external": True,
                        "raw_lsp_range": {
                            "basis": "lsp_zero_based_line_utf16_code_unit_character",
                            "start": {"line": 5, "character": 1},
                            "end": {"line": 5, "character": 6},
                        },
                    }
                ]
            }
        ),
    )
    assert _payload(external)["data"]["files"] == [
        {
            "path": "/opt/site-packages/pkg/value.py",
            "read_only": True,
            "targets": [
                {
                    "raw_range": [[5, 1], [5, 6]],
                    "position_basis": "lsp_zero_based_line_utf16_code_unit_character",
                }
            ],
        }
    ]


def test_malformed_success_fails_loudly_instead_of_becoming_empty() -> None:
    result = compact_navigation_result("find_symbol", _envelope({"symbols": [{}]}))
    payload = _payload(result)

    assert result.isError is False
    assert payload["ok"] is False
    assert payload["error"]["code"] == "UNSUPPORTED"
    assert payload["error"]["details"]["reason"] == "malformed_navigation_success"
    assert payload["workspace"] == {"root": "/repo", "kind": "git", "working_subdirectory": "/repo/sub"}
    assert payload["adapter"] == {"name": "pyright", "language": "python"}
    assert payload["generations"] == {"trust": 2, "program": 3, "document": 4, "index": 5}


def test_malformed_success_preserves_independently_valid_authority_fields() -> None:
    envelope = _envelope({"symbols": []})
    envelope["adapter"] = {"name": 42}

    payload = _payload(compact_navigation_result("find_symbol", envelope))

    assert payload["ok"] is False
    assert payload["workspace"] == {"root": "/repo", "kind": "git", "working_subdirectory": "/repo/sub"}
    assert "adapter" not in payload
    assert payload["generations"] == {"trust": 2, "program": 3, "document": 4, "index": 5}


def test_compact_owned_failures_use_authoritative_error_envelope_transport() -> None:
    minimum = minimum_required_chars_result(734)
    malformed = compact_navigation_result("find_symbol", _envelope({"symbols": [{}]}))

    for result in (minimum, malformed):
        assert result.isError is False
        assert json.loads(cast(types.TextContent, result.content[0]).text) == result.structuredContent
        assert result.structuredContent is not None
        assert result.structuredContent["ok"] is False


def test_all_range_bearing_navigation_tools_preserve_repaired_unicode_coordinates() -> None:
    """Compaction removes offsets without changing the snapshot-mapped public range."""

    repaired_range = {
        "start": {"line": 7, "column": 14, "text_offset": 80, "byte_offset": 87},
        "end": {"line": 7, "column": 19, "text_offset": 85, "byte_offset": 92},
    }
    cases = (
        (
            "find_symbol",
            {
                "relative_path": "src/unicode.py",
                "sha256": _SHA,
                "symbol": {"name_path": "VALUE", "kind": 14, "range": repaired_range},
            },
            "symbols",
        ),
        (
            "find_referencing_symbols",
            {
                "references": [
                    {
                        "path": "src/unicode.py",
                        "read_only_external": False,
                        "location": repaired_range,
                        "container": {"kind": "file", "name_path": "<file>"},
                    }
                ],
                "coverage": {"adapter": "pyright", "language": "python"},
            },
            "references",
        ),
        (
            "find_declaration",
            {
                "locations": [
                    {
                        "absolute_path": "/repo/src/unicode.py",
                        "relative_path": "src/unicode.py",
                        "location_kind": "workspace",
                        "range": repaired_range,
                    }
                ]
            },
            "targets",
        ),
        (
            "find_implementations",
            {
                "locations": [
                    {
                        "absolute_path": "/repo/src/unicode.py",
                        "relative_path": "src/unicode.py",
                        "location_kind": "workspace",
                        "range": repaired_range,
                    }
                ]
            },
            "targets",
        ),
    )

    for operation, data, record_key in cases:
        payload = _payload(compact_navigation_result(operation, _envelope(data)))
        record = payload["data"]["files"][0][record_key][0]
        assert record["range"] == [[7, 14], [7, 19]]
        assert "text_offset" not in json.dumps(payload)
        assert "byte_offset" not in json.dumps(payload)
