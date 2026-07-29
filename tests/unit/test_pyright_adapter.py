from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from serena_light.bootstrap import runtime_paths
from serena_light.lsp.pyright import (
    PYRIGHT_DEFINITION_METHOD,
    PYRIGHT_EXTENSIONS,
    PyrightAttributionError,
    PyrightConfigurationError,
    PyrightFacts,
    _validate_owned_files_report,
)
from serena_light.workspace.identity import LocationKind, SemanticLocation
from serena_light.workspace.scope import DifferenceReason, ProjectKind, ScopeCode


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
