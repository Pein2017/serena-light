from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

from serena_light.lsp.normalize import Location, Position, Range, reparent
from serena_light.lsp.positions import FileSnapshot, PositionEncoding
from serena_light.tools.envelopes import AdapterMetadata, GenerationMetadata, WorkspaceMetadata
from serena_light.tools.references import (
    ReferenceDocumentInput,
    ReferenceNavigationService,
    ReferenceRequest,
    ReferenceTarget,
)


def _range(start_line: int, start_character: int, end_line: int, end_character: int) -> dict[str, dict[str, int]]:
    return {
        "start": {"line": start_line, "character": start_character},
        "end": {"line": end_line, "character": end_character},
    }


def _location(uri: str, start_line: int, start_character: int, end_line: int, end_character: int) -> Location:
    return Location(uri, Range(Position(start_line, start_character), Position(end_line, end_character)), None)


def _request() -> ReferenceRequest:
    return ReferenceRequest(
        "src/source.py",
        Position(0, 0),
        WorkspaceMetadata("/repo", "git", "/repo"),
        AdapterMetadata("pyright", "python"),
        GenerationMetadata(trust=1, program=2, document=3, index=4),
    )


@dataclass
class _Locations:
    values: list[Location]
    calls: int = 0

    def find_references(self, request: ReferenceRequest) -> list[Location]:
        assert request == _request()
        self.calls += 1
        return self.values


@dataclass
class _Classifier:
    paths: dict[str, tuple[str, str, bool]]

    def classify_reference_location(self, location: Location) -> ReferenceTarget:
        key, display_path, read_only_external = self.paths[location.uri]
        return ReferenceTarget(location, key, display_path, read_only_external)


@dataclass
class _Documents:
    values: dict[str, ReferenceDocumentInput]
    calls: list[str]

    def load_reference_document(self, target: ReferenceTarget) -> ReferenceDocumentInput:
        self.calls.append(target.key)
        return self.values[target.key]


def _service(
    locations: list[Location],
    documents: dict[str, ReferenceDocumentInput],
    paths: dict[str, tuple[str, str, bool]],
) -> tuple[ReferenceNavigationService, _Documents]:
    provider = _Documents(documents, [])
    return ReferenceNavigationService(_Locations(locations), _Classifier(paths), provider), provider


def test_references_map_unicode_crlf_location_to_smallest_symbol_and_bounded_snippet() -> None:
    uri = "file:///repo/src/references.py"
    text = "# 😀\r\nclass Café:\r\n    def call🚀():\r\n      target😀()\r\n"
    document = ReferenceDocumentInput(
        uri,
        FileSnapshot.from_bytes(text.encode()),
        [
            {
                "name": "Café",
                "kind": 5,
                "range": _range(1, 0, 4, 0),
                "children": [
                    {
                        "name": "call🚀",
                        "kind": 6,
                        "range": _range(2, 4, 4, 0),
                    }
                ],
            }
        ],
        PositionEncoding.UTF16,
    )
    service, documents = _service(
        [_location(uri, 3, 6, 3, 12)],
        {"references.py": document},
        {uri: ("references.py", "src/references.py", False)},
    )

    value = service.find_referencing_symbols(_request(), max_snippet_chars=8).to_dict()

    assert value["ok"] is True
    reference = value["data"]["references"][0]
    assert reference["container"] == {"kind": "symbol", "name_path": "Café/call🚀", "symbol_kind": 6}
    assert reference["location"]["start"] == {"line": 4, "column": 7, "text_offset": 42, "byte_offset": 49}
    assert len(reference["snippet"]) <= 8
    assert "tar" in reference["snippet"]
    assert reference["snippet_truncated"] is True
    assert documents.calls == ["references.py"]
    assert value["workspace"]["root"] == "/repo"
    assert value["adapter"] == {"name": "pyright", "language": "python"}
    assert value["generations"] == {"trust": 1, "program": 2, "document": 3, "index": 4}


def test_reference_at_module_scope_returns_typed_file_container() -> None:
    uri = "file:///repo/src/module.py"
    document = ReferenceDocumentInput(
        uri,
        FileSnapshot.from_bytes(b"target()\n\ndef function():\n    return target()\n"),
        [{"name": "function", "kind": 12, "range": _range(2, 0, 4, 0)}],
    )
    service, _documents = _service(
        [_location(uri, 0, 0, 0, 6)],
        {"module.py": document},
        {uri: ("module.py", "src/module.py", False)},
    )

    value = service.find_referencing_symbols(_request()).to_dict()

    reference = value["data"]["references"][0]
    assert reference["container"] == {"kind": "file", "name_path": "<file>"}
    assert reference["snippet"] == "target()"


def test_external_reference_is_preserved_as_read_only_without_expanding_workspace() -> None:
    uri = "file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/pkg/use.py"
    document = ReferenceDocumentInput(
        uri,
        FileSnapshot.from_bytes(b"def use():\n    return target()\n"),
        [{"name": "use", "kind": 12, "range": _range(0, 0, 2, 0)}],
    )
    service, _documents = _service(
        [_location(uri, 1, 11, 1, 17)],
        {"external": document},
        {uri: ("external", "/root/miniconda3/envs/ms/lib/python3.12/site-packages/pkg/use.py", True)},
    )

    value = service.find_referencing_symbols(_request()).to_dict()

    reference = value["data"]["references"][0]
    assert reference["path"].endswith("site-packages/pkg/use.py")
    assert reference["read_only_external"] is True
    assert reference["container"]["name_path"] == "use"


def test_adapter_owned_containment_recovery_is_used_for_reference_mapping() -> None:
    uri = "file:///repo/src/flat.py"

    def recover(symbols: tuple[Any, ...]) -> tuple[Any, ...]:
        parent, child = symbols
        return (replace(parent, children=(reparent(child, parent.name_path),)),)

    document = ReferenceDocumentInput(
        uri,
        FileSnapshot.from_bytes(b"class Parent:\n    def child():\n        target()\n"),
        [
            {"name": "Parent", "kind": 5, "range": _range(0, 0, 3, 0)},
            {"name": "child", "kind": 6, "range": _range(1, 4, 3, 0)},
        ],
        recover_containment=recover,
    )
    service, _documents = _service(
        [_location(uri, 2, 8, 2, 14)],
        {"flat.py": document},
        {uri: ("flat.py", "src/flat.py", False)},
    )

    value = service.find_referencing_symbols(_request()).to_dict()

    assert value["data"]["references"][0]["container"]["name_path"] == "Parent/child"


def test_reference_results_are_deduplicated_ordered_and_answer_bounded_per_candidate_file() -> None:
    alpha_uri = "file:///repo/src/alpha.py"
    beta_uri = "file:///repo/src/beta.py"
    document = {
        "alpha": ReferenceDocumentInput(alpha_uri, FileSnapshot.from_bytes(b"target()\n"), []),
        "beta": ReferenceDocumentInput(beta_uri, FileSnapshot.from_bytes(b"target()\n"), []),
    }
    service, documents = _service(
        [
            _location(beta_uri, 0, 0, 0, 6),
            _location(alpha_uri, 0, 0, 0, 6),
            _location(beta_uri, 0, 0, 0, 6),
        ],
        document,
        {
            alpha_uri: ("alpha", "src/alpha.py", False),
            beta_uri: ("beta", "src/beta.py", False),
        },
    )

    full = service.find_referencing_symbols(_request()).to_dict()
    bounded = service.find_referencing_symbols(_request(), max_answer_chars=250).to_dict()

    assert [reference["path"] for reference in full["data"]["references"]] == ["src/alpha.py", "src/beta.py"]
    assert full["data"]["reference_count"] == 2
    assert documents.calls == ["alpha", "beta", "alpha", "beta"]
    assert bounded["truncation"]["truncated"] is True
    assert bounded["truncation"]["omitted_count"] >= 1
