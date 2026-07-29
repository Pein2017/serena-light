from __future__ import annotations

import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest

from serena_light.lsp.client import SyncLspClient
from serena_light.lsp.normalize import normalize_document_symbols
from serena_light.lsp.typescript import (
    TypeScriptAdapterConfig,
    attribute_native_program,
    probe_inferred_path_support,
)
from serena_light.processes import LanguageServerSubprocessLauncher, terminate_process_tree_with_kill_fallback
from serena_light.workspace.inventory import git_trust_inventory

ROOT = Path("/data/CoordExp/cc-plugin-codex")

pytestmark = [
    pytest.mark.timeout(90),
    pytest.mark.external_repo(
        root=str(ROOT),
        snapshot_env="SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT",
    ),
]


def _path_uri(path: Path) -> str:
    return path.resolve().as_uri()


def _uri_path(location: Mapping[str, Any]) -> Path:
    parsed = urlparse(str(location["uri"]))
    assert parsed.scheme == "file"
    return Path(unquote(parsed.path))


def _position_of(source: str, needle: str, *, after: str = "", inside: int = 0) -> dict[str, int]:
    start = source.index(after) + len(after) if after else 0
    offset = source.index(needle, start) + inside
    prefix = source[:offset]
    return {"line": prefix.count("\n"), "character": len(prefix.rsplit("\n", 1)[-1])}


@pytest.fixture(scope="module")
def config() -> TypeScriptAdapterConfig:
    return TypeScriptAdapterConfig.locked()


@pytest.fixture(scope="module")
def real_lsp(config: TypeScriptAdapterConfig) -> Iterator[tuple[SyncLspClient, Mapping[str, Any]]]:
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
                "failureReason": "real adapter test is read-only",
            },
        },
    )
    try:
        client.start()
        initialize_result = client.request("initialize", config.initialize_params(ROOT), timeout=15.0)
        assert isinstance(initialize_result, Mapping)
        client.notify("initialized", {})
        client.notify("workspace/didChangeConfiguration", {"settings": {}})
        for relative in ("runtime/cli.mjs", "runtime/args.mjs"):
            path = ROOT / relative
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": _path_uri(path),
                        "languageId": config.language_id(path),
                        "version": 1,
                        "text": path.read_text(encoding="utf-8"),
                    }
                },
            )
        yield client, initialize_result
    finally:
        client.shutdown(timeout=3.0)
        try:
            process.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            terminate_process_tree_with_kill_fallback(
                process,
                2.0,
                "real TypeScript adapter test",
                kill_timeout=2.0,
            )


def test_real_runtime_mjs_overview_and_capability_facts(
    config: TypeScriptAdapterConfig,
    real_lsp: tuple[SyncLspClient, Mapping[str, Any]],
) -> None:
    client, initialize_result = real_lsp
    cli = ROOT / "runtime/cli.mjs"
    raw_symbols = client.request(
        "textDocument/documentSymbol",
        {"textDocument": {"uri": _path_uri(cli)}},
        timeout=15.0,
    )
    assert isinstance(raw_symbols, list)
    symbols = normalize_document_symbols(raw_symbols, document_uri=_path_uri(cli))
    names = {symbol.name for root in symbols for symbol in root.iter_depth_first()}

    assert {"main", "parse", "spawnAgent", "followupTask"} <= names
    facts = config.capability_facts(initialize_result)
    assert facts.raw_providers["definitionProvider"] is True
    assert facts.raw_providers["declarationProvider"] is False
    assert facts.raw_providers["implementationProvider"] is True
    assert facts.derived_tools["find_declaration"] is True
    assert facts.derived_tools["find_implementations"] is True
    assert facts.position_encoding == "utf-16"


def test_real_cli_parse_args_definition_references_and_implementation(
    real_lsp: tuple[SyncLspClient, Mapping[str, Any]],
) -> None:
    client, _initialize_result = real_lsp
    cli = ROOT / "runtime/cli.mjs"
    source = cli.read_text(encoding="utf-8")
    position = _position_of(source, "parseArgs", after="function parse(", inside=2)
    text_document = {"uri": _path_uri(cli)}

    definitions = client.request(
        "textDocument/definition",
        {"textDocument": text_document, "position": position},
        timeout=15.0,
    )
    references = client.request(
        "textDocument/references",
        {"textDocument": text_document, "position": position, "context": {"includeDeclaration": True}},
        timeout=15.0,
    )
    implementations = client.request(
        "textDocument/implementation",
        {"textDocument": text_document, "position": position},
        timeout=15.0,
    )

    assert isinstance(definitions, list) and len(definitions) == 1
    assert _uri_path(definitions[0]) == ROOT / "runtime/args.mjs"
    assert isinstance(references, list)
    assert {_uri_path(location) for location in references} == {
        ROOT / "runtime/cli.mjs",
        ROOT / "runtime/args.mjs",
    }
    assert len(references) >= 3
    assert isinstance(implementations, list) and len(implementations) == 1
    assert _uri_path(implementations[0]) == ROOT / "runtime/args.mjs"


def test_real_status_scope_facts_and_omitted_engine_owned_inferred_path(
    config: TypeScriptAdapterConfig,
) -> None:
    inventory = git_trust_inventory(ROOT)
    attributed = attribute_native_program(
        config,
        ROOT,
        trust_inventory_paths=inventory.paths,
        entry_path="runtime/agent-runtime.mjs",
    )
    projection = attributed.require_compatible()
    status = attributed.status_facts()
    fixed = config.fixed_facts()

    expected_trust = tuple(path for path in inventory.paths if Path(path).suffix.lower() in config.extensions)
    assert fixed["language_server_version"] == "5.1.3"
    assert fixed["typescript_engine_version"] == "5.9.3"
    assert Path(str(fixed["typescript_engine_path"])) == config.tsserver_path
    assert status["selected_config_path"] == "tsconfig.json"
    assert status["project_kind"] == "configured"
    assert status["scope_compatible"] is True
    assert status["overlay_generated"] is False
    assert projection.trust_inventory.paths == expected_trust
    assert status["trust_inventory_count"] == len(expected_trust)
    assert status["configured_program_count"] == len(projection.configured_program.paths) > 0
    assert projection.configured_program.paths
    assert not projection.configured_program_outside_trust
    assert {item.path for item in projection.trusted_not_in_configured_program} == (
        set(projection.trust_inventory.paths) - set(projection.configured_program.paths)
    )
    omitted_status = status["trusted_not_in_configured_program"]
    assert omitted_status["total"] == len(projection.trusted_not_in_configured_program)
    assert omitted_status["omitted_count"] == max(0, omitted_status["total"] - 50)
    assert all(item["reason"] == "excluded_by_native_config" for item in omitted_status["items"])

    omitted = next(
        path for path in projection.trusted_not_in_configured_program if Path(path.path).suffix.lower() == ".mjs"
    )
    inferred = probe_inferred_path_support(
        config,
        ROOT,
        configured_entry_path="runtime/agent-runtime.mjs",
        candidate_path=omitted.path,
    )
    assert inferred.path == omitted.path
    assert inferred.project_kind == "inferred"
    assert inferred.selected_config_path is None
    assert inferred.engine_owned is True
    assert inferred.service_supported is True
    assert inferred.configured_program_before == projection.configured_program.paths
    assert inferred.configured_program_after == projection.configured_program.paths
    assert inferred.supported_without_scope_expansion is True
