import re
from collections.abc import Callable
from pathlib import Path

import pytest

from serena_light.lsp.positions import (
    FileSnapshot,
    LspPosition,
    PositionEncoding,
    PositionError,
    PositionMapper,
    PublicPositionRenderer,
)


@pytest.mark.parametrize(
    ("encoding", "character"),
    [
        (PositionEncoding.UTF8, 9),
        (PositionEncoding.UTF16, 8),
        (PositionEncoding.UTF32, 8),
    ],
)
def test_python_astral_prefix_converts_all_coordinate_kinds(encoding: PositionEncoding, character: int) -> None:
    snapshot = FileSnapshot.from_bytes("# 😀\r\ndef café():\r\n    return '🚀'\r\n".encode())
    mapper = PositionMapper(snapshot, encoding)
    symbol = LspPosition(1, character)

    text_offset = mapper.lsp_to_text_offset(symbol)
    byte_offset = mapper.lsp_to_byte_offset(symbol)

    assert snapshot.text[text_offset:] == "():\r\n    return '🚀'\r\n"
    assert mapper.byte_offset_to_text_offset(byte_offset) == text_offset
    assert mapper.text_offset_to_lsp(text_offset) == symbol
    assert mapper.byte_offset_to_lsp(byte_offset) == symbol


@pytest.mark.parametrize("encoding", list(PositionEncoding))
def test_mjs_astral_inside_symbol_and_eol_round_trip(encoding: PositionEncoding) -> None:
    snapshot = FileSnapshot.from_bytes("export const café🚀 = 'é';\n".encode())
    mapper = PositionMapper(snapshot, encoding)
    inside_rocket = snapshot.text.index("🚀")
    eol = snapshot.text.index("\n")

    rocket_position = mapper.text_offset_to_lsp(inside_rocket)
    assert mapper.lsp_to_text_offset(rocket_position) == inside_rocket
    assert mapper.lsp_to_text_offset(mapper.text_offset_to_lsp(eol)) == eol
    assert mapper.lsp_to_byte_offset(mapper.text_offset_to_lsp(eol)) == len(snapshot.raw_bytes) - 1


def test_utf8_bom_and_mixed_newline_metadata_are_preserved() -> None:
    raw = b"\xef\xbb\xbfalpha\r\nbeta\ngamma"
    snapshot = FileSnapshot.from_bytes(raw)
    mapper = PositionMapper(snapshot, PositionEncoding.UTF16)

    assert snapshot.encoding == "utf-8"
    assert snapshot.bom == b"\xef\xbb\xbf"
    assert snapshot.line_endings == ("\r\n", "\n")
    assert mapper.text_offset_to_byte_offset(0) == 3
    assert mapper.lsp_to_byte_offset(LspPosition(0, 5)) == 8
    assert mapper.lsp_to_byte_offset(LspPosition(1, 4)) == 14
    assert mapper.lsp_to_text_offset(LspPosition(2, 5)) == len(snapshot.text)


@pytest.mark.parametrize(
    "operation",
    [
        lambda mapper: mapper.lsp_to_text_offset(LspPosition(-1, 0)),
        lambda mapper: mapper.lsp_to_text_offset(LspPosition(0, -1)),
        lambda mapper: mapper.lsp_to_text_offset(LspPosition(2, 0)),
        lambda mapper: mapper.lsp_to_text_offset(LspPosition(0, 1)),
        lambda mapper: mapper.byte_offset_to_text_offset(1),
        lambda mapper: mapper.byte_offset_to_text_offset(4),
        lambda mapper: mapper.text_offset_to_lsp(3),
    ],
)
def test_invalid_coordinates_fail_fast(operation: Callable[[PositionMapper], object]) -> None:
    mapper = PositionMapper(FileSnapshot.from_bytes(b"\xef\xbb\xbf\xf0\x9f\x98\x80\n"), PositionEncoding.UTF16)
    with pytest.raises(PositionError):
        operation(mapper)


def test_invalid_utf8_fails_fast() -> None:
    with pytest.raises(PositionError, match="valid UTF-8"):
        FileSnapshot.from_bytes(b"\xff")


@pytest.mark.parametrize(
    ("encoding", "position", "expected"),
    [
        (PositionEncoding.UTF8, LspPosition(0, 4), (0, 1, 1, 7)),
        (PositionEncoding.UTF16, LspPosition(0, 2), (0, 1, 1, 7)),
        (PositionEncoding.UTF32, LspPosition(0, 1), (0, 1, 1, 7)),
        (PositionEncoding.UTF8, LspPosition(0, 5), (0, 2, 2, 8)),
        (PositionEncoding.UTF16, LspPosition(0, 3), (0, 2, 2, 8)),
        (PositionEncoding.UTF32, LspPosition(0, 2), (0, 2, 2, 8)),
        (PositionEncoding.UTF16, LspPosition(1, 0), (1, 0, 4, 10)),
        (PositionEncoding.UTF16, LspPosition(2, 0), (2, 0, 5, 11)),
        (PositionEncoding.UTF16, LspPosition(2, 1), (2, 1, 6, 13)),
    ],
)
def test_public_renderer_maps_all_encodings_bom_newlines_empty_lines_and_eol(
    encoding: PositionEncoding,
    position: LspPosition,
    expected: tuple[int, int, int, int],
) -> None:
    snapshot = FileSnapshot.from_bytes(b"\xef\xbb\xbf" + "😀x\r\n\né\n".encode())
    renderer = PublicPositionRenderer.from_snapshot(snapshot, encoding)

    rendered = renderer.position(position)

    assert rendered == dict(zip(("line", "column", "text_offset", "byte_offset"), expected, strict=True))
    assert renderer.mapper.byte_offset_to_text_offset(rendered["byte_offset"]) == rendered["text_offset"]
    assert snapshot.text[snapshot._line_starts[rendered["line"]] : rendered["text_offset"]].__len__() == rendered[
        "column"
    ]


@pytest.mark.parametrize(
    ("encoding", "character"),
    [
        (PositionEncoding.UTF8, 1),
        (PositionEncoding.UTF8, 2),
        (PositionEncoding.UTF8, 3),
        (PositionEncoding.UTF16, 1),
        (PositionEncoding.UTF32, 2),
    ],
)
def test_public_renderer_rejects_invalid_character_unit_boundaries(
    encoding: PositionEncoding,
    character: int,
) -> None:
    renderer = PublicPositionRenderer.from_snapshot(FileSnapshot.from_bytes("😀\n".encode()), encoding)

    with pytest.raises(PositionError, match="past the line end or splits a code unit"):
        renderer.position(LspPosition(0, character))


def test_public_range_and_body_share_one_exact_snapshot_mapper() -> None:
    snapshot = FileSnapshot.from_bytes(b"\xef\xbb\xbf" + "😀name\r\n".encode())
    renderer = PublicPositionRenderer.from_snapshot(snapshot, PositionEncoding.UTF16)

    rendered = renderer.range(LspPosition(0, 2), LspPosition(0, 6))

    assert rendered == {
        "start": {"line": 0, "column": 1, "text_offset": 1, "byte_offset": 7},
        "end": {"line": 0, "column": 5, "text_offset": 5, "byte_offset": 11},
    }
    assert renderer.text(LspPosition(0, 2), LspPosition(0, 6)) == "name"


def test_tool_renderers_do_not_apply_local_line_or_column_base_arithmetic() -> None:
    tools_root = Path(__file__).parents[2] / "src" / "serena_light" / "tools"
    rendered_field_arithmetic = re.compile(r'"(?:line|column)"\s*:\s*[^\n,]+\s[+-]\s1')

    for name in ("navigation.py", "global_symbols.py", "declarations.py", "diagnostics.py"):
        assert rendered_field_arithmetic.search((tools_root / name).read_text()) is None
