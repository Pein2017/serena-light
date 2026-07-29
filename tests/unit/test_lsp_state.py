from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from serena_light.lsp.state import DiagnosticsState, LspState


def test_generations_advance_independently() -> None:
    state = LspState()

    state.advance_source_generation()
    state.advance_index_generation()

    assert state.generations.source == 1
    assert state.generations.index == 1
    assert state.generations.diagnostics == 0


def test_empty_publication_is_not_missing_and_becomes_stale() -> None:
    state = LspState()
    path = Path("/data/example.py")
    document = state.update_document(uri="file:///data/example.py", path=path, version=1)
    assert document is not None

    assert state.diagnostics_snapshot(document.uri).state is DiagnosticsState.MISSING
    assert state.publish_diagnostics(
        uri=document.uri, path=path, version=1, generation=document.generation, diagnostics=[]
    )
    clean = state.diagnostics_snapshot(document.uri)
    assert clean.state is DiagnosticsState.CLEAN
    assert clean.diagnostics == ()
    assert clean.diagnostics_generation == 1

    next_document = state.update_document(uri=document.uri, path=path, version=2)
    assert next_document is not None
    assert state.diagnostics_snapshot(document.uri).state is DiagnosticsState.STALE


def test_old_document_or_diagnostic_generation_cannot_replace_newer_state() -> None:
    state = LspState()
    path = Path("/data/example.py")
    first = state.update_document(uri="file:///data/example.py", path=path, version=1)
    assert first is not None
    second = state.update_document(uri=first.uri, path=path, version=2)
    assert second is not None

    assert state.update_document(uri=first.uri, path=path, version=1) is None
    assert not state.publish_diagnostics(
        uri=first.uri, path=path, version=1, generation=first.generation, diagnostics=[{"message": "old"}]
    )
    assert state.publish_diagnostics(
        uri=first.uri, path=path, version=2, generation=second.generation, diagnostics=[{"message": "new"}]
    )
    snapshot = state.diagnostics_snapshot(first.uri)
    assert snapshot.state is DiagnosticsState.FINDINGS
    assert snapshot.diagnostics == ({"message": "new"},)


def test_publication_diagnostics_are_immutable() -> None:
    state = LspState()
    path = Path("/data/example.py")
    document = state.update_document(uri="file:///data/example.py", path=path, version=None)
    assert document is not None
    diagnostic = {"message": "mutable", "related": ["one"]}

    assert state.publish_diagnostics(
        uri=document.uri, path=path, version=None, generation=document.generation, diagnostics=[diagnostic]
    )
    diagnostic["related"].append("two")
    stored = state.diagnostics_snapshot(document.uri).diagnostics[0]
    assert stored["related"] == ("one",)  # type: ignore[index]


def test_concurrent_updates_do_not_lose_or_regress_versions() -> None:
    state = LspState()
    path = Path("/data/example.py")

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda version: state.update_document(uri="file:///data/example.py", path=path, version=version),
                range(1, 101),
            )
        )

    current = state.document("file:///data/example.py")
    assert current is not None
    assert current.version == 100
    assert current.generation == sum(result is not None for result in results)
