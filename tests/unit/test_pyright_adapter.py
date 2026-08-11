from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from serena_light.bootstrap import runtime_paths
from serena_light.lsp.positions import FileSnapshot, LspPosition, PositionEncoding, PositionMapper
from serena_light.lsp.pyright import (
    PYRIGHT_DEFINITION_METHOD,
    PYRIGHT_EXTENSIONS,
    PyrightAttributionError,
    PyrightConfigurationError,
    PyrightFacts,
    _validate_owned_files_report,
)
from serena_light.lsp.python_assignment_recovery import AssignmentRecoveryReason
from serena_light.workspace.identity import LocationKind, SemanticLocation
from serena_light.workspace.scope import DifferenceReason, ProjectKind, ScopeCode

_VARIABLE_KIND = 13
_CONSTANT_KIND = 14


def _snapshot_and_mapper(source: str) -> tuple[FileSnapshot, PositionMapper]:
    snapshot = FileSnapshot.from_bytes(source.encode("utf-8"))
    return snapshot, PositionMapper(snapshot, PositionEncoding.UTF16)


def _identifier_only_symbol(
    mapper: PositionMapper,
    source: str,
    name: str,
    *,
    kind: int = _CONSTANT_KIND,
    occurrence: int = 0,
) -> dict[str, Any]:
    """Build a raw Pyright-shaped DocumentSymbol whose range is identifier-only."""

    start_offset = -1
    for _ in range(occurrence + 1):
        start_offset = source.index(name, start_offset + 1)
    end_offset = start_offset + len(name)
    start = mapper.text_offset_to_lsp(start_offset)
    end = mapper.text_offset_to_lsp(end_offset)
    selection = {
        "start": {"line": start.line, "character": start.character},
        "end": {"line": end.line, "character": end.character},
    }
    return {"name": name, "kind": kind, "range": dict(selection), "selectionRange": dict(selection), "children": []}


def _recovered_text(mapper: PositionMapper, source: str, raw_symbol: Mapping[str, Any]) -> str:
    start = raw_symbol["range"]["start"]
    end = raw_symbol["range"]["end"]
    start_offset = mapper.lsp_to_text_offset(_lsp(start))
    end_offset = mapper.lsp_to_text_offset(_lsp(end))
    return source[start_offset:end_offset]


def _lsp(position: Mapping[str, int]) -> LspPosition:
    return LspPosition(position["line"], position["character"])


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(child, key) for child in value.values())
    if isinstance(value, list):
        return any(_contains_key(child, key) for child in value)
    return False


def test_locked_facts_and_initialize_configuration_are_fixed(tmp_path: Path) -> None:
    facts = PyrightFacts.locked()
    params = facts.initialize_params(tmp_path, process_id=0)
    locked = runtime_paths(Path(__file__).resolve().parents[2])

    assert facts.extensions == PYRIGHT_EXTENSIONS == frozenset({".py", ".pyi"})
    assert facts.language_id == "python"
    assert facts.command == (str(locked["node"]), str(locked["pyright-langserver"]), "--stdio")
    assert facts.definition_method == PYRIGHT_DEFINITION_METHOD == "textDocument/definition"
    shared = facts.adapter_language_facts(tmp_path)
    assert shared.routes("package/module.pyi")
    assert shared.engine.executable == locked["pyright-langserver"]
    assert shared.engine.interpreter == Path("/root/miniconda3/envs/ms/bin/python")
    assert facts.runtime_provider(tmp_path).environment == {
        "PATH": str(locked["node"].parent),
        "NODE_PATH": None,
    }
    assert params["capabilities"]["workspace"]["configuration"] is True
    assert params["initializationOptions"] == {}
    assert not _contains_key(params["initializationOptions"], "pythonPath")


def test_workspace_configuration_answers_known_sections_in_order() -> None:
    facts = PyrightFacts.locked()

    answers = facts.workspace_configuration(
        {
            "items": [
                {"section": "python.analysis"},
                {"section": "python"},
                {"section": "pyright"},
                {"section": "unknown"},
            ]
        }
    )

    assert answers == [
        {
            "diagnosticMode": "workspace",
            "autoSearchPaths": True,
            "useLibraryCodeForTypes": True,
        },
        {"pythonPath": "/root/miniconda3/envs/ms/bin/python"},
        {},
        {},
    ]
    with pytest.raises(PyrightConfigurationError, match="items"):
        facts.workspace_configuration({})


def test_locked_facts_use_binding_selected_interpreter(tmp_path: Path) -> None:
    interpreter = tmp_path / "conda" / "envs" / "llm-framework-study" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.touch()

    facts = PyrightFacts.locked(interpreter=interpreter)

    assert facts.interpreter == interpreter
    assert facts.adapter_language_facts(tmp_path).engine.interpreter == interpreter
    assert facts.workspace_configuration({"items": [{"section": "python"}]}) == [
        {"pythonPath": str(interpreter)}
    ]


def test_raw_provider_facts_are_separate_from_derived_tools() -> None:
    facts = PyrightFacts.locked()
    fixture = json.loads(
        (Path(__file__).parents[1] / "admission/fixtures/initialize/python.json").read_text(encoding="utf-8")
    )

    providers = facts.provider_facts(fixture["response"])

    assert providers.raw.definition
    assert providers.raw.declaration
    assert not providers.raw.implementation
    assert providers.derived.find_declaration
    assert not providers.derived.find_implementations
    assert providers.derived.find_referencing_symbols
    with pytest.raises(PyrightConfigurationError, match="definitionProvider"):
        facts.provider_facts({"capabilities": {}})


def test_definition_location_classification_is_an_injected_policy_seam(tmp_path: Path) -> None:
    facts = PyrightFacts.locked()
    target = tmp_path / "external.py"
    target.write_text("answer = 42\n", encoding="utf-8")
    observed: list[Path] = []

    def classify(path: Path) -> SemanticLocation:
        observed.append(path)
        return SemanticLocation(path.resolve(), LocationKind.READ_ONLY_EXTERNAL)

    locations = facts.classify_definition_locations(
        {
            "uri": target.as_uri(),
            "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 6}},
        },
        classify=classify,
    )

    assert observed == [target]
    assert locations[0].location.path == str(target)
    assert locations[0].semantic_location.read_only_external
    assert facts.classify_definition_locations(None, classify=classify) == ()


def test_native_attribution_preserves_selected_config_and_builds_file_projection(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "omitted").mkdir()
    (tmp_path / "src/main.py").write_text("from .helper import answer\n", encoding="utf-8")
    (tmp_path / "src/helper.py").write_text("answer = 42\n", encoding="utf-8")
    (tmp_path / "omitted/trusted.py").write_text("trusted = True\n", encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text('{"include":["src"]}\n', encoding="utf-8")
    facts = PyrightFacts.locked()

    projection = facts.attribute_program(
        tmp_path,
        ("src/main.py", "src/helper.py", "omitted/trusted.py"),
    )

    assert projection.project_kind is ProjectKind.CONFIGURED
    assert projection.selected_config_path == "pyrightconfig.json"
    assert projection.configured_program.paths == ("src/helper.py", "src/main.py")
    assert projection.trusted_not_in_configured_program[0].path == "omitted/trusted.py"
    assert projection.trusted_not_in_configured_program[0].reason is DifferenceReason.EXCLUDED_BY_NATIVE_CONFIG
    assert projection.compatible
    assert not projection.overlay_generated


def test_scope_projection_rejects_engine_owned_python_outside_trust(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "ignored-generated").mkdir()
    (tmp_path / "src/main.py").write_text("answer = 42\n", encoding="utf-8")
    (tmp_path / "ignored-generated/hidden.py").write_text("hidden = True\n", encoding="utf-8")
    (tmp_path / "pyrightconfig.json").write_text('{"include":["**/*.py"]}\n', encoding="utf-8")

    projection = PyrightFacts.locked().attribute_program(
        tmp_path,
        ("src/main.py",),
        outside_trust_reasons={"ignored-generated/hidden.py": DifferenceReason.GIT_IGNORED},
    )

    assert not projection.compatible
    assert projection.error is not None
    assert projection.error.code is ScopeCode.SCOPE_INCOMPATIBLE
    assert projection.configured_program_outside_trust[0].reason is DifferenceReason.GIT_IGNORED
    assert not projection.overlay_generated


def test_owned_files_report_validation_fails_closed_on_version_and_digest_drift() -> None:
    facts = PyrightFacts.locked()
    base = {
        "schema_version": 1,
        "engine": {"version": facts.engine_version, "cli_entrypoint": str(facts.engine_path)},
        "project": {"project_kind": "workspace_default", "selected_config_path": None},
        "owned_files": ["/tmp/a.py"],
        "owned_file_count": 1,
        "owned_files_sha256": "wrong",
        "bundle": {},
    }

    with pytest.raises(PyrightAttributionError, match="digest"):
        _validate_owned_files_report(base, expected_cli=facts.engine_path)
    drifted = {**base, "engine": {**base["engine"], "version": "1.1.404"}}
    with pytest.raises(PyrightAttributionError, match="version drift"):
        _validate_owned_files_report(drifted, expected_cli=facts.engine_path)


def test_production_node_probe_exports_fail_closed_bundle_and_module_checks() -> None:
    facts = PyrightFacts.locked()
    node = facts.command[0]
    probe = Path(__file__).parents[2] / "src/serena_light/lsp/pyright_owned_files_probe.mjs"
    program = f"""
      import {{ EXPECTED_BUNDLE, ProbeError, assertRequiredBundleModules, validateBundleEvidence }}
        from {json.dumps(probe.as_uri())};
      let checks = 0;
      try {{
        validateBundleEvidence({{
          packageJsonBytes: Buffer.from('{{\"version\":\"1.1.404\"}}'),
          pyrightJsBytes: Buffer.alloc(0),
          pyrightInternalJsBytes: Buffer.alloc(0),
        }});
      }} catch (error) {{
        if (error instanceof ProbeError && error.code === 'PYRIGHT_VERSION_MISMATCH') checks++;
      }}
      try {{ assertRequiredBundleModules(() => ({{}})); }} catch (error) {{
        if (error instanceof ProbeError && error.code === 'PYRIGHT_MODULE_STRUCTURE_MISMATCH') checks++;
      }}
      if (EXPECTED_BUNDLE.version !== '1.1.403' || checks !== 2) process.exit(9);
    """

    completed = subprocess.run(
        [node, "--input-type=module", "-e", program],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    ("case_id", "source", "name", "kind", "expected_body"),
    [
        ("simple", "answer = 42\n", "answer", _CONSTANT_KIND, "answer = 42"),
        ("annotated", "answer: int = 42\n", "answer", _CONSTANT_KIND, "answer: int = 42"),
        (
            "multiline",
            "answer = (\n    40 +\n    2\n)\n",
            "answer",
            _CONSTANT_KIND,
            "answer = (\n    40 +\n    2\n)",
        ),
        ("tuple_first", "first, second = 1, 2\n", "first", _CONSTANT_KIND, "first, second = 1, 2"),
        ("tuple_second", "first, second = 1, 2\n", "second", _CONSTANT_KIND, "first, second = 1, 2"),
        ("chained", "left = right = 1\n", "right", _VARIABLE_KIND, "left = right = 1"),
        (
            "unicode_identifier",
            "café = '日本語'\n",
            "café",
            _CONSTANT_KIND,
            "café = '日本語'",
        ),
        (
            "if_elif_else",
            "cond = True\nif cond:\n    a = 1\nelif not cond:\n    b = 2\nelse:\n    c = 3\n",
            "b",
            _CONSTANT_KIND,
            "b = 2",
        ),
        (
            "try_except_else_finally",
            "try:\n    a = 1\nexcept Exception:\n    b = 2\nelse:\n    c = 3\nfinally:\n    d = 4\n",
            "d",
            _CONSTANT_KIND,
            "d = 4",
        ),
        (
            "with_suite",
            "with open('f') as fh:\n    zzz = 1\n",
            "zzz",
            _CONSTANT_KIND,
            "zzz = 1",
        ),
        (
            "for_suite",
            "for item in range(3):\n    zzz = 1\n",
            "zzz",
            _CONSTANT_KIND,
            "zzz = 1",
        ),
        (
            "while_suite",
            "while True:\n    b = 2\n    break\n",
            "b",
            _CONSTANT_KIND,
            "b = 2",
        ),
        (
            "match_case",
            "match value:\n    case 1:\n        zzz = 1\n    case _:\n        b = 2\n",
            "zzz",
            _CONSTANT_KIND,
            "zzz = 1",
        ),
    ],
)
def test_pyright_adapter_recovers_identifier_only_module_assignment_ranges(
    case_id: str,
    source: str,
    name: str,
    kind: int,
    expected_body: str,
) -> None:
    """Task 1.4/3.1: an identifier-only module variable/constant range is
    expanded to the exact complete assignment statement, never a truncated
    or comment-absorbing slice, while the identifier stays the selection
    range and name-path anchor."""

    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol = _identifier_only_symbol(mapper, source, name, kind=kind)
    original_selection = dict(raw_symbol["selectionRange"])

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert not result.unresolved, case_id
    (recovered,) = result.raw_symbols
    assert _recovered_text(mapper, source, recovered) == expected_body, case_id
    assert recovered["selectionRange"] == original_selection, "identifier stays the selection range anchor"
    assert recovered["name"] == name
    assert result.incomplete_range_reason(name=name, selection_range=original_selection) is None


def test_pyright_adapter_assignment_recovery_is_ambiguous_when_name_matches_two_statements() -> None:
    """Task 1.4/3.2: a redefined module-level name that cannot be pinned to
    one exact target span fails closed as ambiguous rather than guessing."""

    source = "x = 1\nx = 2\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    zero_width = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    raw_symbol = {
        "name": "x",
        "kind": _VARIABLE_KIND,
        "range": dict(zero_width),
        "selectionRange": dict(zero_width),
        "children": [],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert result.unresolved[0].name == "x"
    assert result.unresolved[0].reason == AssignmentRecoveryReason.AMBIGUOUS
    (unchanged,) = result.raw_symbols
    assert unchanged["range"] == unchanged["selectionRange"] == zero_width
    assert (
        result.incomplete_range_reason(name="x", selection_range=zero_width) == AssignmentRecoveryReason.AMBIGUOUS
    )


def test_pyright_adapter_assignment_recovery_rejects_name_only_mismatched_selection() -> None:
    """A unique name must not override a server selection anchored elsewhere."""

    source = "good = 1\nother = 2\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    wrong_anchor = _identifier_only_symbol(mapper, source, "other", kind=_CONSTANT_KIND)["selectionRange"]
    raw_symbol = {
        "name": "good",
        "kind": _CONSTANT_KIND,
        "range": dict(wrong_anchor),
        "selectionRange": dict(wrong_anchor),
        "children": [],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert len(result.unresolved) == 1
    assert result.unresolved[0].name == "good"
    assert result.unresolved[0].range == wrong_anchor
    assert result.unresolved[0].reason == AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    assert result.raw_symbols == (raw_symbol,)


def test_pyright_adapter_assignment_recovery_fails_closed_on_syntax_error_and_unsupported_target() -> None:
    """Task 1.4/3.2: syntax-invalid source and a non-assignment binding (a
    ``for`` loop variable) both fail closed instead of guessing a range."""

    invalid_source = "answer = (\n"
    snapshot, mapper = _snapshot_and_mapper(invalid_source)
    raw_symbol = _identifier_only_symbol(mapper, invalid_source, "answer")

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert result.unresolved[0].reason == AssignmentRecoveryReason.SYNTAX_INVALID
    (unchanged,) = result.raw_symbols
    assert unchanged["range"] == unchanged["selectionRange"]

    loop_source = "for item in range(3):\n    pass\n"
    loop_snapshot, loop_mapper = _snapshot_and_mapper(loop_source)
    loop_symbol = _identifier_only_symbol(loop_mapper, loop_source, "item", kind=_VARIABLE_KIND)

    loop_result = PyrightFacts.locked().recover_assignment_document_symbols(
        [loop_symbol], snapshot=loop_snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert loop_result.unresolved[0].reason == AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT


@pytest.mark.parametrize(
    ("case_id", "source"),
    [
        ("nested_function_local", "def outer():\n    zzz = 1\n"),
        ("nested_class_attribute", "class Outer:\n    zzz = 1\n"),
    ],
)
def test_pyright_adapter_assignment_recovery_excludes_nested_function_and_class_locals(
    case_id: str,
    source: str,
) -> None:
    """Task: a compound-suite recovery walk must not cross into a nested
    function or class body; those locals are not module-executed scope and
    must fail closed rather than be recovered as a module assignment."""

    snapshot, mapper = _snapshot_and_mapper(source)
    raw_symbol = _identifier_only_symbol(mapper, source, "zzz")

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert result.unresolved[0].reason == AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT, case_id
    (unchanged,) = result.raw_symbols
    assert unchanged["range"] == unchanged["selectionRange"], case_id


def test_pyright_adapter_assignment_recovery_fails_closed_when_selection_escapes_a_suite_target() -> None:
    """A server selection that does not fall inside any module-executed
    assignment target in a compound suite must fail closed rather than be
    pinned to an unrelated statement by name alone."""

    source = "if True:\n    zzz = 1\nelse:\n    other = 2\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    # A selection anchored on "other" (a real assignment target) but
    # reported under the name "zzz" is an inconsistent selection/name pair.
    wrong_anchor = _identifier_only_symbol(mapper, source, "other")["selectionRange"]
    raw_symbol = {
        "name": "zzz",
        "kind": _CONSTANT_KIND,
        "range": dict(wrong_anchor),
        "selectionRange": dict(wrong_anchor),
        "children": [],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert result.unresolved[0].name == "zzz"
    assert result.unresolved[0].reason == AssignmentRecoveryReason.NO_ENCLOSING_ASSIGNMENT
    assert result.raw_symbols == (raw_symbol,)


def test_pyright_adapter_assignment_recovery_leaves_already_complete_ranges_untouched() -> None:
    """A symbol whose ``range`` already differs from its ``selectionRange``
    is left alone: Pyright already reported a complete body for it."""

    source = "answer = 42\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    start = mapper.text_offset_to_lsp(0)
    end = mapper.text_offset_to_lsp(len(source.rstrip("\n")))
    selection = _identifier_only_symbol(mapper, source, "answer")["selectionRange"]
    already_complete = {
        "name": "answer",
        "kind": _CONSTANT_KIND,
        "range": {
            "start": {"line": start.line, "character": start.character},
            "end": {"line": end.line, "character": end.character},
        },
        "selectionRange": selection,
        "children": [],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [already_complete], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert not result.unresolved
    assert result.raw_symbols[0]["range"] == already_complete["range"]


def test_pyright_adapter_assignment_recovery_leaves_nested_and_non_variable_symbols_untouched() -> None:
    """Recovery is module-level only: nested children and non-variable kinds
    (e.g. a decorated function) pass through unchanged, per design decision 3."""

    source = "def f():\n    pass\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    function_symbol = {
        "name": "f",
        "kind": 12,  # SymbolKind.Function
        "range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 8}},
        "selectionRange": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 5}},
        "children": [
            {
                "name": "inner",
                "kind": _CONSTANT_KIND,
                "range": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 4}},
                "selectionRange": {"start": {"line": 1, "character": 4}, "end": {"line": 1, "character": 4}},
                "children": [],
            }
        ],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [function_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )

    assert not result.unresolved
    assert result.raw_symbols == (function_symbol,)


def test_pyright_adapter_include_body_contract_never_advertises_incomplete_text_as_complete() -> None:
    """Task 3.2: the recovery result gives a caller everything it needs to
    keep an accurately labelled identifier range for ordinary lookup while
    refusing to attach an ``include_body=true`` body for an unresolved symbol.

    ``typescript.py``/``navigation.py`` integration note: a caller rendering
    ``include_body=True`` MUST call ``incomplete_range_reason`` with the
    symbol's name and *original* selection range before slicing a body from
    ``raw_symbol["range"]``; a non-``None`` result means the range is still
    identifier-only and must become a typed ``UNSUPPORTED`` failure instead
    of a successful body.
    """

    source = "x = 1\nx = 2\n"
    snapshot, mapper = _snapshot_and_mapper(source)
    zero_width = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 0}}
    raw_symbol = {
        "name": "x",
        "kind": _VARIABLE_KIND,
        "range": dict(zero_width),
        "selectionRange": dict(zero_width),
        "children": [],
    }

    result = PyrightFacts.locked().recover_assignment_document_symbols(
        [raw_symbol], snapshot=snapshot, position_encoding=PositionEncoding.UTF16
    )
    (rendered,) = result.raw_symbols

    def include_body_would_be_served(symbol: Mapping[str, Any]) -> bool:
        reason = result.incomplete_range_reason(name=symbol["name"], selection_range=symbol["selectionRange"])
        return reason is None

    # Ordinary lookup (include_body=False) may still show the labelled
    # identifier range: it is truthful, just not a complete statement.
    assert rendered["range"] == rendered["selectionRange"] == zero_width
    # include_body=True must not be served from this range.
    assert not include_body_would_be_served(rendered)
