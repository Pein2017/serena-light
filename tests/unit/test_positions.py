from collections.abc import Callable

import pytest

from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionError, PositionMapper


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
