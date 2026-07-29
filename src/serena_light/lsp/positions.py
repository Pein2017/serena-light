"""Lossless conversion between LSP positions and one immutable source snapshot."""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import StrEnum


class PositionEncoding(StrEnum):
    """LSP character-unit encodings supported by the v1 adapters."""

    UTF8 = "utf-8"
    UTF16 = "utf-16"
    UTF32 = "utf-32"


class PositionError(ValueError):
    """Raised when a position is outside the snapshot or splits a code unit."""


@dataclass(frozen=True, slots=True)
class LspPosition:
    """A zero-based LSP line and character position."""

    line: int
    character: int


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    """Decoded source and its lossless UTF-8 physical-file metadata.

    ``text`` deliberately excludes a UTF-8 BOM: it is an encoding signature,
    not source text exposed to an LSP.  Byte offsets remain offsets in
    ``raw_bytes``, so decoded offset zero maps immediately after that BOM.
    """

    raw_bytes: bytes
    text: str
    encoding: str
    bom: bytes
    line_endings: tuple[str, ...]
    _text_to_byte: tuple[int, ...]
    _line_starts: tuple[int, ...]
    _line_content_ends: tuple[int, ...]
    _line_ends: tuple[int, ...]

    @classmethod
    def from_bytes(cls, raw_bytes: bytes) -> FileSnapshot:
        """Build a UTF-8 snapshot, rejecting malformed or unsupported input."""

        bom = b"\xef\xbb\xbf" if raw_bytes.startswith(b"\xef\xbb\xbf") else b""
        payload = raw_bytes[len(bom) :]
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PositionError("v1 position snapshots require valid UTF-8") from error

        text_to_byte = [len(bom)]
        byte_offset = len(bom)
        for character in text:
            byte_offset += len(character.encode("utf-8"))
            text_to_byte.append(byte_offset)

        line_starts = [0]
        content_ends: list[int] = []
        line_ends: list[int] = []
        line_endings: list[str] = []
        index = 0
        while index < len(text):
            if text[index] == "\r" and index + 1 < len(text) and text[index + 1] == "\n":
                content_ends.append(index)
                line_ends.append(index + 2)
                line_endings.append("\r\n")
                line_starts.append(index + 2)
                index += 2
            elif text[index] == "\n" or text[index] == "\r":
                content_ends.append(index)
                line_ends.append(index + 1)
                line_endings.append(text[index])
                line_starts.append(index + 1)
                index += 1
            else:
                index += 1
        content_ends.append(len(text))
        line_ends.append(len(text))

        return cls(
            raw_bytes=raw_bytes,
            text=text,
            encoding="utf-8",
            bom=bom,
            line_endings=tuple(line_endings),
            _text_to_byte=tuple(text_to_byte),
            _line_starts=tuple(line_starts),
            _line_content_ends=tuple(content_ends),
            _line_ends=tuple(line_ends),
        )


@dataclass(frozen=True, slots=True)
class PositionMapper:
    """Convert positions only at exact boundaries of an immutable snapshot."""

    snapshot: FileSnapshot
    encoding: PositionEncoding = PositionEncoding.UTF16

    def lsp_to_text_offset(self, position: LspPosition) -> int:
        """Return a decoded-text offset for a valid LSP position."""

        if position.line < 0 or position.line >= len(self.snapshot._line_starts):
            raise PositionError(f"line {position.line} is outside the snapshot")
        if position.character < 0:
            raise PositionError("LSP character must be non-negative")

        start = self.snapshot._line_starts[position.line]
        end = self.snapshot._line_content_ends[position.line]
        units = 0
        for offset in range(start, end):
            if units == position.character:
                return offset
            units += self._units(self.snapshot.text[offset])
        if units == position.character:
            return end
        raise PositionError("LSP character is past the line end or splits a code unit")

    def text_offset_to_lsp(self, offset: int) -> LspPosition:
        """Return the LSP position for a source-character boundary.

        Newline characters themselves do not have an LSP position.  Their line
        content boundary is represented by the preceding line's end position.
        """

        self._check_text_offset(offset)
        line = bisect_right(self.snapshot._line_starts, offset) - 1
        if offset > self.snapshot._line_content_ends[line]:
            raise PositionError("a newline character has no LSP position")
        start = self.snapshot._line_starts[line]
        return LspPosition(line, sum(self._units(char) for char in self.snapshot.text[start:offset]))

    def text_offset_to_byte_offset(self, offset: int) -> int:
        """Return the raw-file byte offset for a decoded-text boundary."""

        self._check_text_offset(offset)
        return self.snapshot._text_to_byte[offset]

    def byte_offset_to_text_offset(self, offset: int) -> int:
        """Return a decoded-text offset, rejecting BOM and multibyte interiors."""

        if offset < 0 or offset > len(self.snapshot.raw_bytes):
            raise PositionError("byte offset is outside the snapshot")
        try:
            return self.snapshot._text_to_byte.index(offset)
        except ValueError as error:
            raise PositionError("byte offset splits a BOM or UTF-8 code point") from error

    def lsp_to_byte_offset(self, position: LspPosition) -> int:
        return self.text_offset_to_byte_offset(self.lsp_to_text_offset(position))

    def byte_offset_to_lsp(self, offset: int) -> LspPosition:
        return self.text_offset_to_lsp(self.byte_offset_to_text_offset(offset))

    def _check_text_offset(self, offset: int) -> None:
        if offset < 0 or offset > len(self.snapshot.text):
            raise PositionError("decoded-text offset is outside the snapshot")

    def _units(self, character: str) -> int:
        if self.encoding is PositionEncoding.UTF8:
            return len(character.encode("utf-8"))
        if self.encoding is PositionEncoding.UTF16:
            return 2 if ord(character) > 0xFFFF else 1
        if self.encoding is PositionEncoding.UTF32:
            return 1
        raise PositionError(f"unsupported LSP position encoding: {self.encoding!r}")
