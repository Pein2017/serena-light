from __future__ import annotations

import json
from itertools import permutations
from typing import Any

import pytest
from mcp import types

from serena_light.tools.compact import (
    CompactFile,
    CompactNavigationSuccess,
    CompactOverviewSymbol,
    CompactReference,
    CompactSymbolMatch,
    CompactTarget,
    ExactBodyRecovery,
    LocatedCompactRecord,
    canonical_json,
    compact_range,
    compact_raw_lsp_range,
    group_records,
    minimum_required_chars_result,
    ordered_records,
    render_bounded_overview,
    render_bounded_records,
    render_success,
    symbol_kind,
    validate_max_answer_chars,
    validate_max_matches,
    validate_overview_kind_filters,
)
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_RANGE = ((1, 2), (3, 4))


def _text(result: types.CallToolResult) -> str:
    assert len(result.content) == 1
    block = result.content[0]
    assert isinstance(block, types.TextContent)
    return block.text


def _structured(result: types.CallToolResult) -> dict[str, Any]:
    assert isinstance(result.structuredContent, dict)
    return result.structuredContent


def test_canonical_renderer_is_utf8_preserving_and_exactly_matches_structured_content() -> None:
    success = CompactNavigationSuccess(
        "/repo/项目",
        (
            CompactFile(
                "src/café.py",
                "symbols",
                (CompactSymbolMatch("Café/启动", 12, _RANGE),),
            ),
        ),
    )

    result = render_success(success)

    assert _text(result) == (
        '{"ok":true,"data":{"workspace":"/repo/项目","files":[{"path":"src/café.py",'
        '"symbols":[{"name_path":"Café/启动","kind":"function","range":[[1,2],[3,4]]}]}],"omitted":0}}'
    )
    assert "\\u" not in _text(result)
    assert json.loads(_text(result)) == _structured(result) == success.to_dict()
    assert result.isError is False


def test_current_public_range_becomes_compact_without_compatibility_offsets() -> None:
    value = compact_range(
        {
            "start": {"line": 2, "column": 3, "text_offset": 20, "byte_offset": 24},
            "end": {"line": 4, "column": 5, "text_offset": 40, "byte_offset": 48},
        }
    )

    assert value == ((2, 3), (4, 5))
    assert CompactTarget(value, "Thing", 5).to_dict() == {
        "range": [[2, 3], [4, 5]],
        "name_path": "Thing",
        "kind": "class",
    }


def test_lsp_kind_mapping_and_overview_filters_are_stable_and_validated() -> None:
    assert symbol_kind(1) == "file"
    assert symbol_kind(22) == "enum_member"
    assert symbol_kind(26) == "type_parameter"
    assert symbol_kind(99) == "unknown:99"
    assert validate_overview_kind_filters(["method", "class", "method"], ["unknown:99"]) == (
        ("class", "method"),
        ("unknown:99",),
    )
    with pytest.raises(ValueError, match="unknown symbol kind"):
        validate_overview_kind_filters(["Class"], None)
    with pytest.raises(ValueError, match="sequence"):
        validate_overview_kind_filters("class", None)
    with pytest.raises(ValueError, match="unknown symbol kind"):
        validate_overview_kind_filters(["unknown:099"], None)


@pytest.mark.parametrize("value", [512, 12_000, 50_000])
def test_max_answer_chars_accepts_fixed_inclusive_range(value: int) -> None:
    assert validate_max_answer_chars(value) == value


@pytest.mark.parametrize("value", [True, 511, 50_001, 512.0, "12000"])
def test_max_answer_chars_rejects_non_integer_or_out_of_range_values(value: object) -> None:
    with pytest.raises(ValueError, match="max_answer_chars"):
        validate_max_answer_chars(value)


@pytest.mark.parametrize("value", [1, 20, 100])
def test_max_matches_accepts_fixed_inclusive_range(value: int) -> None:
    assert validate_max_matches(value) == value


@pytest.mark.parametrize("value", [True, 0, 101, 20.0, "20"])
def test_max_matches_rejects_non_integer_or_out_of_range_values(value: object) -> None:
    with pytest.raises(ValueError, match="max_matches"):
        validate_max_matches(value)


def test_grouping_exactly_deduplicates_and_sorts_for_every_input_permutation() -> None:
    records = (
        LocatedCompactRecord("src/../src/b.py", CompactSymbolMatch("B", 12, ((8, 0), (9, 0)))),
        LocatedCompactRecord("src/a.py", CompactSymbolMatch("Z", 6, ((4, 2), (4, 5)))),
        LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, ((4, 2), (6, 0)))),
        LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, ((4, 2), (6, 0)))),
    )
    observed = {
        canonical_json(CompactNavigationSuccess("/repo", group_records(permutation)))
        for permutation in permutations(records)
    }

    assert len(observed) == 1
    assert json.loads(observed.pop())["data"]["files"] == [
        {
            "path": "src/a.py",
            "symbols": [
                {"name_path": "Z", "kind": "method", "range": [[4, 2], [4, 5]]},
                {"name_path": "A", "kind": "class", "range": [[4, 2], [6, 0]]},
            ],
        },
        {"path": "src/b.py", "symbols": [{"name_path": "B", "kind": "function", "range": [[8, 0], [9, 0]]}]},
    ]
    assert [item.record.to_dict()["name_path"] for item in ordered_records(records)] == ["Z", "A", "B"]


def test_range_end_precedes_name_in_stable_order() -> None:
    records = [
        LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, ((1, 0), (9, 0)))),
        LocatedCompactRecord("src/a.py", CompactSymbolMatch("Z", 5, ((1, 0), (2, 0)))),
    ]

    assert [item.record.to_dict()["name_path"] for item in ordered_records(records)] == ["Z", "A"]


def test_golden_file_groups_cover_hash_language_read_only_and_no_empty_groups() -> None:
    single = group_records(
        [LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, _RANGE, body="class A: pass"), sha256=_SHA_A)]
    )
    multi_language = group_records(
        [
            LocatedCompactRecord(
                "src/a.py", CompactTarget(_RANGE, "A", 5, body="class A: pass"), language="python", sha256=_SHA_A
            ),
            LocatedCompactRecord(
                "src/a.ts", CompactTarget(_RANGE, "A", 5, body="class A {}"), language="typescript", sha256=_SHA_B
            ),
        ]
    )
    external = group_records(
        [
            LocatedCompactRecord(
                "/opt/pkg/index",
                CompactReference(_RANGE, symbol="module/use", snippet="use(A)"),
                language="python",
                read_only=True,
                sha256=_SHA_A,
            )
        ]
    )

    assert [file.to_dict() for file in single] == [
        {
            "path": "src/a.py",
            "sha256": _SHA_A,
            "symbols": [{"name_path": "A", "kind": "class", "range": [[1, 2], [3, 4]], "body": "class A: pass"}],
        }
    ]
    assert [file.to_dict() for file in multi_language] == [
        {
            "path": "src/a.py",
            "language": "python",
            "sha256": _SHA_A,
            "targets": [{"range": [[1, 2], [3, 4]], "name_path": "A", "kind": "class", "body": "class A: pass"}],
        },
        {
            "path": "src/a.ts",
            "language": "typescript",
            "sha256": _SHA_B,
            "targets": [{"range": [[1, 2], [3, 4]], "name_path": "A", "kind": "class", "body": "class A {}"}],
        },
    ]
    assert [file.to_dict() for file in external] == [
        {
            "path": "/opt/pkg/index",
            "language": "python",
            "read_only": True,
            "references": [{"range": [[1, 2], [3, 4]], "symbol": "module/use", "snippet": "use(A)"}],
        }
    ]
    assert group_records([]) == ()
    assert "sha256" not in external[0].to_dict()


def test_mixed_languages_are_inferred_for_directory_records_without_adapter_repetition() -> None:
    grouped = group_records(
        [
            LocatedCompactRecord("src/value.py", CompactSymbolMatch("VALUE", 14, _RANGE)),
            LocatedCompactRecord("src/value.ts", CompactSymbolMatch("VALUE", 14, _RANGE)),
        ]
    )

    assert [file.to_dict()["language"] for file in grouped] == ["python", "typescript"]


def test_body_bearing_groups_require_one_hash_and_hashless_records_never_emit_one() -> None:
    with pytest.raises(ValueError, match="requires sha256"):
        group_records([LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, _RANGE, body="body"))])
    without_body = group_records([LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, _RANGE), sha256=_SHA_A)])
    assert "sha256" not in without_body[0].to_dict()


def test_navigation_success_structurally_forbids_verbose_navigation_metadata() -> None:
    value = CompactNavigationSuccess(
        "/repo",
        group_records(
            [
                LocatedCompactRecord(
                    "src/a.py",
                    CompactSymbolMatch("A", 5, _RANGE, body="class A: pass", info="class A"),
                    sha256=_SHA_A,
                )
            ]
        ),
    ).to_dict()
    forbidden = {
        "adapter",
        "adapter_phase",
        "generations",
        "configured_program",
        "query",
        "uri",
        "text_offset",
        "byte_offset",
        "selection_range",
        "detail",
        "children_truncated",
        "has_children",
    }

    def keys(node: object) -> set[str]:
        if isinstance(node, dict):
            return {str(key) for key in node}.union(*(keys(child) for child in node.values()))
        if isinstance(node, list):
            return set().union(*(keys(child) for child in node))
        return set()

    assert set(value) == {"ok", "data"}
    assert set(value["data"]) == {"workspace", "files", "omitted"}
    assert keys(value).isdisjoint(forbidden)
    serialized = canonical_json(value)
    assert serialized.count("src/a.py") == 1
    assert serialized.count(_SHA_A) == 1


def test_reference_coverage_is_copied_once_in_canonical_data_order() -> None:
    coverage: dict[str, Any] = {
        "adapter": "pyright",
        "language": "python",
        "uncovered_sample": ["tests/outside.py"],
    }
    files = group_records([LocatedCompactRecord("src/a.py", CompactReference(_RANGE))])
    success = CompactNavigationSuccess("/repo", files, coverage=coverage)
    coverage["adapter"] = "mutated"

    assert canonical_json(success) == (
        '{"ok":true,"data":{"workspace":"/repo","files":[{"path":"src/a.py",'
        '"references":[{"range":[[1,2],[3,4]]}]}],"coverage":{"adapter":"pyright",'
        '"language":"python","uncovered_sample":["tests/outside.py"]},"omitted":0}}'
    )
    assert canonical_json(success).count('"coverage"') == 1


def test_raw_external_target_keeps_explicit_lsp_basis() -> None:
    raw_range, basis = compact_raw_lsp_range(
        {
            "basis": "lsp_zero_based_line_utf16_code_unit_character",
            "start": {"line": 4, "character": 2},
            "end": {"line": 4, "character": 9},
        }
    )
    target = CompactTarget(None, raw_range=raw_range, position_basis=basis)

    assert target.to_dict() == {
        "raw_range": [[4, 2], [4, 9]],
        "position_basis": "lsp_zero_based_line_utf16_code_unit_character",
    }
    with pytest.raises(ValueError, match="only for reference"):
        CompactNavigationSuccess(
            "/repo",
            group_records([LocatedCompactRecord("src/a.py", CompactSymbolMatch("A", 5, _RANGE))]),
            coverage={"adapter": "pyright"},
        )


def test_whole_record_budget_pruning_is_exact_and_never_slices_atomic_content() -> None:
    records = [
        LocatedCompactRecord("src/a.py", CompactReference(((1, 0), (1, 1)), snippet="界" * 180)),
        LocatedCompactRecord("src/b.py", CompactReference(((2, 0), (2, 1)), snippet="雪" * 180)),
    ]
    one = render_bounded_records("/repo", records[:1], max_answer_chars=512)
    exact_boundary = len(_text(one))
    assert exact_boundary == 512 or exact_boundary < 512
    full = render_bounded_records("/repo", records, max_answer_chars=512)

    assert len(_text(full)) <= 512
    payload = _structured(full)
    assert payload["data"]["omitted"] == 1
    assert [file["path"] for file in payload["data"]["files"]] == ["src/a.py"]
    assert payload["data"]["files"][0]["references"][0]["snippet"] == "界" * 180
    assert "雪" not in _text(full)


def test_indivisible_body_returns_measured_invalid_input_without_partial_body() -> None:
    body = "def enormous():\n" + "    return '完整'\n" * 80
    record = LocatedCompactRecord(
        "src/huge.py",
        CompactSymbolMatch("enormous", 12, ((0, 0), (81, 0)), body=body),
        sha256=_SHA_A,
    )
    minimum = len(canonical_json(CompactNavigationSuccess("/repo", group_records([record]))))

    result = render_bounded_records("/repo", [record], max_answer_chars=512)

    assert result.isError is False
    assert len(_text(result)) <= 512
    assert _structured(result)["error"]["code"] == "INVALID_INPUT"
    assert _structured(result)["error"]["details"] == {
        "field": "max_answer_chars",
        "minimum_required_chars": minimum,
    }
    assert body not in _text(result)
    assert "def enormous" not in _text(result)


def test_oversized_container_recommends_child_navigation_without_partial_body() -> None:
    body = "class Container:\n" + "    value = 1\n" * 80
    record = LocatedCompactRecord(
        "src/huge.py",
        CompactSymbolMatch("Container", 5, ((0, 0), (81, 0)), body=body),
        sha256=_SHA_A,
    )

    result = render_bounded_records(
        "/repo",
        [record],
        max_answer_chars=512,
        exact_body_recovery=ExactBodyRecovery(has_children=True, relative_path="src/huge.py", name_path="Container"),
    )

    details = _structured(result)["error"]["details"]
    assert details["next_action"] == "overview_then_find_child_symbol"
    assert details["minimum_required_chars"] > 512
    assert len(_text(result)) <= 512
    assert body not in _text(result)


def test_oversized_leaf_with_legal_minimum_recommends_exact_budget_retry() -> None:
    body = "def leaf():\n" + "    return 1\n" * 80
    record = LocatedCompactRecord(
        "src/huge.py",
        CompactSymbolMatch("leaf", 12, ((0, 0), (81, 0)), body=body),
        sha256=_SHA_A,
    )

    result = render_bounded_records(
        "/repo",
        [record],
        max_answer_chars=512,
        exact_body_recovery=ExactBodyRecovery(has_children=False, relative_path="src/huge.py", name_path="leaf"),
    )

    details = _structured(result)["error"]["details"]
    assert 512 < details["minimum_required_chars"] <= 50_000
    assert details["next_action"] == "retry_with_minimum_answer_chars"
    assert len(_text(result)) <= 512
    assert body not in _text(result)


def test_oversized_leaf_above_public_maximum_recommends_exact_range_read() -> None:
    body = "def enormous():\n" + "x" * 50_000
    record = LocatedCompactRecord(
        "src/enormous.py",
        CompactSymbolMatch("enormous", 12, ((0, 0), (1, 50_000)), body=body),
        sha256=_SHA_A,
    )

    result = render_bounded_records(
        "/repo",
        [record],
        max_answer_chars=512,
        exact_body_recovery=ExactBodyRecovery(
            has_children=False,
            relative_path="src/enormous.py",
            name_path="enormous",
        ),
    )

    details = _structured(result)["error"]["details"]
    assert details["minimum_required_chars"] > 50_000
    assert details["next_action"] == "find_symbol_location_then_exact_file_read"
    assert len(_text(result)) <= 512
    assert body not in _text(result)


def test_fitting_exact_body_ignores_internal_recovery_fact() -> None:
    body = "def leaf():\n    return 1\n"
    record = LocatedCompactRecord(
        "src/leaf.py",
        CompactSymbolMatch("leaf", 12, ((0, 0), (2, 0)), body=body),
        sha256=_SHA_A,
    )

    result = render_bounded_records(
        "/repo",
        [record],
        max_answer_chars=512,
        exact_body_recovery=ExactBodyRecovery(has_children=False, relative_path="src/leaf.py", name_path="leaf"),
    )

    assert _structured(result)["ok"] is True
    assert _structured(result)["data"]["files"][0]["symbols"][0]["body"] == body
    assert "has_children" not in _text(result)
    assert "next_action" not in _text(result)


def test_flat_minimum_budget_uses_the_first_stable_record_not_a_smaller_later_record() -> None:
    records = [
        LocatedCompactRecord(
            "src/a.py",
            CompactSymbolMatch("large", 12, _RANGE, body="x" * 900),
            sha256=_SHA_A,
        ),
        LocatedCompactRecord("src/z.py", CompactSymbolMatch("small", 12, _RANGE)),
    ]
    first_prefix = CompactNavigationSuccess("/repo", group_records(records[:1]), omitted=1)
    first_minimum = len(canonical_json(first_prefix))

    too_small = render_bounded_records("/repo", records, max_answer_chars=512)

    assert _structured(too_small)["error"]["details"]["minimum_required_chars"] == first_minimum
    retried = render_bounded_records("/repo", records, max_answer_chars=first_minimum)
    assert _structured(retried) == first_prefix.to_dict()


def test_minimum_budget_errors_forward_rich_authority_without_leaking_it_to_success() -> None:
    workspace = WorkspaceMetadata("/repo", "git", "/repo/sub")
    adapter = AdapterMetadata("pyright", "python")
    generations = GenerationMetadata(trust=2, program=3, document=4, index=5)
    record = LocatedCompactRecord(
        "src/huge.py",
        CompactSymbolMatch("enormous", 12, ((0, 0), (81, 0)), body="x" * 900),
        sha256=_SHA_A,
    )
    minimum = len(canonical_json(CompactNavigationSuccess("/repo", group_records([record]))))

    record_error = render_bounded_records(
        "/repo",
        [record],
        max_answer_chars=512,
        error_workspace=workspace,
        error_adapter=adapter,
        error_generations=generations,
    )
    overview = CompactFile("src/huge.py", "symbols", (CompactOverviewSymbol("Root" + "x" * 900, 5),))
    overview_minimum = len(canonical_json(CompactNavigationSuccess("/repo", (overview,))))
    overview_error = render_bounded_overview(
        "/repo",
        [overview],
        max_answer_chars=512,
        error_workspace=workspace,
        error_adapter=adapter,
        error_generations=generations,
    )

    for result, expected_minimum in ((record_error, minimum), (overview_error, overview_minimum)):
        payload = _structured(result)
        assert result.isError is False
        assert payload["error"]["details"]["minimum_required_chars"] == expected_minimum
        assert payload["workspace"] == {"root": "/repo", "kind": "git", "working_subdirectory": "/repo/sub"}
        assert payload["adapter"] == {"name": "pyright", "language": "python"}
        assert payload["generations"] == {"trust": 2, "program": 3, "document": 4, "index": 5}

    compact_success = render_bounded_records(
        "/repo",
        [LocatedCompactRecord("src/ok.py", CompactSymbolMatch("ok", 12, _RANGE))],
        error_workspace=workspace,
        error_adapter=adapter,
        error_generations=generations,
    )
    assert set(_structured(compact_success)) == {"ok", "data"}


def test_overview_pruning_preserves_preorder_parents_and_exact_boundary() -> None:
    tree = CompactFile(
        "src/tree.py",
        "symbols",
        (
            CompactOverviewSymbol(
                "Root" + "x" * 240,
                5,
                (
                    CompactOverviewSymbol("first" + "y" * 120, 6),
                    CompactOverviewSymbol("second" + "z" * 120, 6),
                ),
            ),
        ),
    )
    full = render_bounded_overview("/repo", [tree], max_answer_chars=50_000)
    full_length = len(_text(full))
    at_boundary = render_bounded_overview("/repo", [tree], max_answer_chars=full_length)
    below_boundary = render_bounded_overview("/repo", [tree], max_answer_chars=full_length - 1)

    assert _structured(at_boundary)["data"]["omitted"] == 0
    below = _structured(below_boundary)["data"]
    assert below["omitted"] == 1
    retained_root = below["files"][0]["symbols"][0]
    assert [child["name"] for child in retained_root["children"]] == ["first" + "y" * 120]
    assert "second" not in _text(below_boundary)


def test_minimum_required_chars_helper_uses_existing_rich_error_shape() -> None:
    result = minimum_required_chars_result(734)

    assert result.isError is False
    assert json.loads(_text(result)) == _structured(result)
    assert _structured(result) == {
        "ok": False,
        "error": {
            "code": "INVALID_INPUT",
            "message": "input is invalid",
            "retry": None,
            "details": {"field": "max_answer_chars", "minimum_required_chars": 734},
        },
    }


@pytest.mark.parametrize(
    "next_action",
    (
        "overview_then_find_child_symbol",
        "retry_with_minimum_answer_chars",
        "find_symbol_location_then_exact_file_read",
    ),
)
def test_minimum_required_chars_error_sheds_long_optional_workspace_but_keeps_recovery(
    next_action: str,
) -> None:
    root = "/" + "/".join("workspace" for _ in range(80))
    result = minimum_required_chars_result(
        734,
        workspace=WorkspaceMetadata(root, "git", f"{root}/subdir"),
        adapter=AdapterMetadata("pyright", "python"),
        generations=GenerationMetadata(trust=2, program=3, document=4, index=5),
        authorities=(
            {
                "adapter": {"name": "pyright", "language": "python"},
                "generations": {"trust": 2, "program": 3, "document": 4, "index": 5},
            },
        ),
        next_action=next_action,  # type: ignore[arg-type]
        max_answer_chars=512,
    )

    payload = _structured(result)

    assert len(_text(result)) <= 512
    assert json.loads(_text(result)) == payload
    assert payload["error"]["details"]["minimum_required_chars"] == 734
    assert payload["error"]["details"]["next_action"] == next_action
    assert "workspace" not in payload
