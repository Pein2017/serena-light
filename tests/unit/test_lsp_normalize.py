from __future__ import annotations

import pytest

from serena_light.lsp.normalize import (
    Location,
    NormalizationError,
    Position,
    Range,
    containing_symbol,
    normalize_document_symbols,
    normalize_location,
    reparent,
)


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def test_document_symbol_tree_gets_locations_name_paths_and_overload_indices() -> None:
    raw = [
        {
            "name": "C",
            "kind": 5,
            "range": _range(0, 0, 6, 0),
            "selectionRange": _range(0, 6, 0, 7),
            "children": [
                {"name": "f", "kind": 6, "range": _range(1, 2, 2, 0)},
                {"name": "f", "kind": 6, "range": _range(3, 2, 4, 0)},
            ],
        }
    ]

    roots = normalize_document_symbols(raw, document_uri="file:///repo/a.py")

    assert roots[0].location.path == "/repo/a.py"
    assert roots[0].selection_range == Range(Position(0, 6), Position(0, 7))
    assert [child.name_path for child in roots[0].children] == [("C", "f[0]"), ("C", "f[1]")]


def test_flat_symbol_information_stays_flat_without_adapter_recovery() -> None:
    raw = [
        {
            "name": "method",
            "kind": 6,
            "containerName": "Class",
            "location": {"uri": "file:///repo/a.ts", "range": _range(2, 0, 3, 0)},
        }
    ]

    roots = normalize_document_symbols(raw, document_uri="file:///repo/a.ts")

    assert roots[0].name_path == ("method",)
    assert roots[0].children == ()


def test_adapter_owns_containment_recovery() -> None:
    raw = [{"name": "method", "kind": 6, "location": {"uri": "file:///a.py", "range": _range(1, 0, 2, 0)}}]
    called = False

    def recover(symbols):
        nonlocal called
        called = True
        return [reparent(symbols[0], ("AdapterRecoveredClass",))]

    recovered = normalize_document_symbols(raw, document_uri="file:///a.py", recover_containment=recover)

    assert called
    assert recovered[0].name_path == ("AdapterRecoveredClass", "method")


def test_name_normalization_is_injected_not_language_specific() -> None:
    raw = [{"name": "f(x: int)", "kind": 12, "range": _range(0, 0, 1, 0)}]

    roots = normalize_document_symbols(
        raw,
        document_uri="file:///a.py",
        normalize_name=lambda name: name.partition("(")[0],
    )

    assert roots[0].name == "f"
    assert roots[0].name_path == ("f",)


def test_containing_symbol_uses_narrowest_same_uri_range() -> None:
    raw = [
        {
            "name": "outer",
            "kind": 12,
            "range": _range(0, 0, 10, 0),
            "children": [{"name": "inner", "kind": 12, "range": _range(2, 0, 4, 0)}],
        }
    ]
    roots = normalize_document_symbols(raw, document_uri="file:///a.py")
    reference = Location("file:///a.py", Range(Position(3, 1), Position(3, 2)), "/a.py")

    assert containing_symbol(roots, reference) is roots[0].children[0]
    other = Location("file:///b.py", reference.range, "/b.py")
    assert containing_symbol(roots, other) is None

    end_boundary = Location(
        "file:///a.py",
        Range(Position(4, 0), Position(4, 1)),
        "/a.py",
    )
    assert containing_symbol(roots, end_boundary) is roots[0]


def test_external_non_file_location_remains_normalized_without_path_authorization() -> None:
    location = normalize_location({"uri": "untitled:external", "range": _range(0, 0, 0, 1)})
    assert location.path is None


@pytest.mark.parametrize(
    "raw",
    [
        [{"name": "", "kind": 1, "range": _range(0, 0, 0, 0)}],
        [{"name": "x", "kind": True, "range": _range(0, 0, 0, 0)}],
        [{"name": "x", "kind": 1, "range": _range(1, 0, 0, 0)}],
        [{"name": "x", "kind": 1}],
    ],
)
def test_invalid_symbol_shapes_fail_fast(raw: list[dict]) -> None:
    with pytest.raises(NormalizationError):
        normalize_document_symbols(raw, document_uri="file:///a.py")
