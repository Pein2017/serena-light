"""Task 1.5/3.3 evidence for pinned-server variable-statement recovery.

This probe connects to the real, locked ``typescript-language-server`` (no
mocking) and asks ``textDocument/documentSymbol`` for accepted ``.ts``,
``.tsx``, ``.js``, and ``.mjs`` fixtures.  Plain, annotated, and multiline
declarations come back with ranges that begin at the binding identifier,
omitting ``export`` and ``const``.  Destructured bindings are identifier-only.
The tests retain both raw behaviors as server evidence, then assert that the
same selection-range ancestry expands each form to the complete variable
statement against the exact same real file content.  The terminal semicolon is
the one intentionally omitted character.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionMapper
from serena_light.lsp.typescript import TypeScriptAdapterConfig
from serena_light.lsp.typescript_assignment_recovery import assignment_recovery_positions
from serena_light.processes import LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback

ROOT = Path(__file__).resolve().parent / "fixtures" / "assignment_probe" / "typescript"

pytestmark = pytest.mark.timeout(60)


def _path_uri(path: Path) -> str:
    return path.resolve().as_uri()


@pytest.fixture(scope="module")
def config() -> TypeScriptAdapterConfig:
    return TypeScriptAdapterConfig.locked()


@pytest.fixture(scope="module")
def real_lsp(config: TypeScriptAdapterConfig) -> Iterator[SyncLspClient]:
    process = LanguageServerSubprocessLauncher.get_instance().launch(
        config.command,
        cwd=ROOT,
        env={"PATH": str(config.node_path.parent)},
    )
    assert process.stdout is not None
    assert process.stdin is not None
    client = SyncLspClient(
        process.stdout,
        process.stdin,
        request_timeout=20.0,
        request_handlers={
            "client/registerCapability": lambda _params: None,
            "window/workDoneProgress/create": lambda _params: None,
            "workspace/configuration": lambda params: [{} for _item in (params or {}).get("items", [])],
            "workspace/executeClientCommand": lambda _params: [],
            "workspace/applyEdit": lambda _params: {
                "applied": False,
                "failureReason": "assignment-range probe is read-only",
            },
        },
    )
    try:
        client.start()
        initialize_result = client.request("initialize", config.initialize_params(ROOT), timeout=15.0)
        assert isinstance(initialize_result, Mapping)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        yield client
    finally:
        client.shutdown(timeout=3.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree_with_kill_fallback(
                process,
                2.0,
                "TypeScript assignment-range probe",
                kill_timeout=2.0,
            )


def _document_symbols(client: SyncLspClient, config: TypeScriptAdapterConfig, relative: str) -> list[Mapping[str, Any]]:
    path = ROOT / relative
    uri = _path_uri(path)
    client.notify(
        "textDocument/didOpen",
        {
            "textDocument": {
                "uri": uri,
                "languageId": config.language_id(path),
                "version": 1,
                "text": path.read_text(encoding="utf-8"),
            }
        },
    )
    raw_symbols: list[Mapping[str, Any]] | None = None
    for _attempt in range(20):
        raw_symbols = client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
            timeout=15.0,
        )
        if raw_symbols:
            break
        time.sleep(0.5)
    assert raw_symbols, f"no document symbols returned for {relative}"
    return raw_symbols


def _by_name(symbols: list[Mapping[str, Any]], name: str) -> Mapping[str, Any]:
    return next(symbol for symbol in symbols if symbol["name"] == name)


def _selection_ranges(
    client: SyncLspClient,
    relative: str,
    symbols: list[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    raw = client.request(
        "textDocument/selectionRange",
        {
            "textDocument": {"uri": _path_uri(ROOT / relative)},
            "positions": list(assignment_recovery_positions(symbols)),
        },
        timeout=15.0,
    )
    assert isinstance(raw, list) and all(isinstance(item, Mapping) for item in raw)
    return raw


def _symbol_body(snapshot: FileSnapshot, symbol: Mapping[str, Any]) -> str:
    mapper = PositionMapper(snapshot, PositionEncoding.UTF16)
    start = symbol["range"]["start"]
    end = symbol["range"]["end"]
    start_offset = mapper.lsp_to_text_offset(LspPosition(start["line"], start["character"]))
    end_offset = mapper.lsp_to_text_offset(LspPosition(end["line"], end["character"]))
    return snapshot.text[start_offset:end_offset]


@pytest.mark.parametrize(
    "relative",
    ["src/top_level.ts", "src/top_level.js", "src/top_level.mjs", "src/top_level.tsx"],
)
def test_simple_and_multiline_top_level_declarations_start_at_the_identifier(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
    relative: str,
) -> None:
    """Evidence: non-destructured server ranges omit declaration modifiers."""

    symbols = _document_symbols(real_lsp, config, relative)
    for name in ("simpleConst", "multiline"):
        symbol = _by_name(symbols, name)
        assert symbol["range"] != symbol["selectionRange"], (relative, name, symbol)
        assert symbol["range"]["start"] == symbol["selectionRange"]["start"], (relative, name, symbol)


@pytest.mark.parametrize("relative", ["src/top_level.ts", "src/top_level.tsx"])
def test_annotated_top_level_declarations_start_at_the_identifier(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
    relative: str,
) -> None:
    """Evidence: annotation syntax is present but declaration modifiers are not."""

    symbols = _document_symbols(real_lsp, config, relative)
    symbol = _by_name(symbols, "annotated")
    assert symbol["range"] != symbol["selectionRange"], (relative, symbol)
    assert symbol["range"]["start"] == symbol["selectionRange"]["start"], (relative, symbol)


@pytest.mark.parametrize(
    ("relative", "name", "expected_body"),
    [
        ("src/top_level.ts", "simpleConst", "export const simpleConst = 1;"),
        ("src/top_level.ts", "annotated", "export const annotated: number = 4;"),
        ("src/top_level.ts", "multiline", "export const multiline = (\n  1 +\n  2\n);"),
        ("src/top_level.ts", "mutable", "export let mutable = 5;"),
        ("src/top_level.ts", "legacy", "export var legacy = 6;"),
        ("src/top_level.ts", "declared", "export declare const declared: number;"),
        ("src/top_level.js", "simpleConst", "const simpleConst = 1;"),
        ("src/top_level.js", "multiline", "const multiline = (\n  1 +\n  2\n);"),
        ("src/top_level.js", "mutable", "let mutable = 5;"),
        ("src/top_level.js", "legacy", "var legacy = 6;"),
        ("src/top_level.mjs", "simpleConst", "const simpleConst = 1;"),
        ("src/top_level.mjs", "multiline", "const multiline = (\n  1 +\n  2\n);"),
        ("src/top_level.mjs", "mutable", "let mutable = 5;"),
        ("src/top_level.mjs", "legacy", "var legacy = 6;"),
        ("src/top_level.tsx", "simpleConst", "export const simpleConst = 1;"),
        ("src/top_level.tsx", "annotated", "export const annotated: number = 4;"),
        ("src/top_level.tsx", "multiline", "export const multiline = (\n  1 +\n  2\n);"),
        ("src/top_level.tsx", "mutable", "export let mutable = 5;"),
        ("src/top_level.tsx", "legacy", "export var legacy = 6;"),
        ("src/top_level.tsx", "declared", "export declare const declared: number;"),
    ],
)
def test_plain_top_level_bindings_recover_the_complete_variable_statement(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
    relative: str,
    name: str,
    expected_body: str,
) -> None:
    symbols = _document_symbols(real_lsp, config, relative)
    original_selection = dict(_by_name(symbols, name)["selectionRange"])
    snapshot = FileSnapshot.from_bytes((ROOT / relative).read_bytes())

    result = config.recover_assignment_document_symbols(
        symbols,
        selection_ranges=_selection_ranges(real_lsp, relative, symbols),
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    recovered = next(symbol for symbol in result.raw_symbols if symbol["name"] == name)
    assert result.incomplete_range_reason(name=name, selection_range=original_selection) is None
    assert recovered["selectionRange"] == original_selection
    assert _symbol_body(snapshot, recovered) == expected_body


@pytest.mark.parametrize(
    "relative",
    ["src/top_level.ts", "src/top_level.js", "src/top_level.mjs", "src/top_level.tsx"],
)
def test_destructured_top_level_bindings_are_identifier_only(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
    relative: str,
) -> None:
    """Demonstrated gap: array- and object-destructured top-level ``const``
    bindings receive an identifier-only range (``range == selectionRange``)
    from the pinned TypeScript server, unlike plain declarations.  This
    assertion is kept (rather than removed once recovery exists) so a future
    upstream fix to the language server would show up as a behavior change
    here, not silently."""

    symbols = _document_symbols(real_lsp, config, relative)
    for name in ("tupleA", "tupleB", "objA", "objB"):
        symbol = _by_name(symbols, name)
        assert symbol["range"] == symbol["selectionRange"], (
            relative,
            name,
            "identifier-only range demonstrates the TypeScript destructuring recovery gap",
            symbol,
        )


@pytest.mark.parametrize(
    ("relative", "name", "expected_body"),
    [
        ("src/top_level.ts", "tupleA", "export const [tupleA, tupleB] = [1, 2];"),
        ("src/top_level.ts", "tupleB", "export const [tupleA, tupleB] = [1, 2];"),
        ("src/top_level.ts", "objA", "export const { objA, objB } = { objA: 1, objB: 2 };"),
        ("src/top_level.ts", "objB", "export const { objA, objB } = { objA: 1, objB: 2 };"),
        ("src/top_level.js", "tupleA", "const [tupleA, tupleB] = [1, 2];"),
        ("src/top_level.js", "objA", "const { objA, objB } = { objA: 1, objB: 2 };"),
        ("src/top_level.mjs", "tupleA", "const [tupleA, tupleB] = [1, 2];"),
        ("src/top_level.mjs", "objA", "const { objA, objB } = { objA: 1, objB: 2 };"),
        ("src/top_level.tsx", "tupleA", "export const [tupleA, tupleB] = [1, 2];"),
        ("src/top_level.tsx", "objA", "export const { objA, objB } = { objA: 1, objB: 2 };"),
    ],
)
def test_destructured_top_level_bindings_recover_the_complete_variable_statement(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
    relative: str,
    name: str,
    expected_body: str,
) -> None:
    """Task 3.3: adapter-owned recovery expands the real server's
    identifier-only destructured range to the complete variable statement,
    across every accepted extension, while preserving the identifier as the
    selection range."""

    symbols = _document_symbols(real_lsp, config, relative)
    original_selection = dict(_by_name(symbols, name)["selectionRange"])
    snapshot = FileSnapshot.from_bytes((ROOT / relative).read_bytes())

    result = config.recover_assignment_document_symbols(
        symbols,
        selection_ranges=_selection_ranges(real_lsp, relative, symbols),
        snapshot=snapshot,
        position_encoding=PositionEncoding.UTF16,
    )

    assert result.incomplete_range_reason(name=name, selection_range=original_selection) is None, (relative, name)
    recovered = next(symbol for symbol in result.raw_symbols if symbol["name"] == name)
    assert recovered["selectionRange"] == original_selection, "identifier stays the selection range anchor"
    assert _symbol_body(snapshot, recovered) == expected_body, (relative, name)


def test_selection_range_chain_exposes_binding_pattern_and_statement(
    real_lsp: SyncLspClient,
    config: TypeScriptAdapterConfig,
) -> None:
    symbols = _document_symbols(real_lsp, config, "src/top_level.ts")
    symbol = _by_name(symbols, "tupleA")
    raw = _selection_ranges(real_lsp, "src/top_level.ts", symbols)
    positions = list(assignment_recovery_positions(symbols))
    first = raw[positions.index(symbol["selectionRange"]["start"])]
    ranges: list[Mapping[str, Any]] = []
    current: Mapping[str, Any] | None = first
    while current is not None:
        ranges.append(current["range"])
        parent = current.get("parent")
        current = parent if isinstance(parent, Mapping) else None
    assert symbol["selectionRange"] == ranges[0]
    assert {"start": {"line": 8, "character": 13}, "end": {"line": 8, "character": 29}} in ranges
    assert {"start": {"line": 8, "character": 0}, "end": {"line": 8, "character": 39}} in ranges
