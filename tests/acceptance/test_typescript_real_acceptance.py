from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from serena_light.bootstrap import runtime_paths
from serena_light.lsp.typescript import TypeScriptAdapterConfig
from serena_light.tools.envelopes import ToolEnvelope
from serena_light.workspace.identity import WorkspacePolicy
from serena_light.workspace.runtime import WorkspaceRuntime

ROOT = Path("/data/CoordExp/external/codexUI")
# ``tsconfig.json`` selects ``src/**/*.ts`` plus ``vite.config.ts``; every other
# Git-trusted JS/TS path is trusted but outside the configured program.
IN_PROGRAM_FILE = "src/commandResolution.ts"
IN_PROGRAM_CONSUMER = "src/cli/index.ts"
OMITTED_MJS_FILE = "scripts/generate-pwa-icons.mjs"
OMITTED_LISTED_FILE = "documentation/app-server-schemas/typescript/AddConversationListenerParams.ts"
UNICODE_FILE = "src/api/normalizers/v2.ts"
UNICODE_SYMBOL = "extractFileAttachments"

pytestmark = [
    pytest.mark.timeout(120),
    pytest.mark.external_repo(
        root=str(ROOT),
        snapshot_env="SERENA_LIGHT_CODEXUI_SNAPSHOT",
    ),
]


@dataclass(frozen=True, slots=True)
class RealTypeScriptAcceptance:
    runtime: WorkspaceRuntime
    config: TypeScriptAdapterConfig


def _locked_lsp_descendants() -> dict[int, str]:
    descendants: dict[int, str] = {}
    for process in psutil.Process().children(recursive=True):
        try:
            command = " ".join(process.cmdline())
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        if "typescript-language-server" in command or "tsserver.js" in command:
            descendants[process.pid] = command
    return descendants


def _stop_and_assert_no_lsp_leaks(runtime: WorkspaceRuntime, before: set[int]) -> None:
    runtime.stop()
    deadline = time.monotonic() + 5.0
    leaked: dict[int, str] = {}
    while time.monotonic() < deadline:
        leaked = {pid: command for pid, command in _locked_lsp_descendants().items() if pid not in before}
        if not leaked:
            break
        time.sleep(0.05)
    assert not leaked, f"Serena Light leaked TypeScript LSP descendants: {leaked}"


@pytest.fixture(scope="module")
def acceptance() -> Iterator[RealTypeScriptAcceptance]:
    before = set(_locked_lsp_descendants())
    policy = WorkspacePolicy()
    runtime = WorkspaceRuntime(policy.resolve_activation(ROOT), path_policy=policy)
    try:
        yield RealTypeScriptAcceptance(runtime, TypeScriptAdapterConfig.locked())
    finally:
        _stop_and_assert_no_lsp_leaks(runtime, before)


def _envelope(value: ToolEnvelope) -> Mapping[str, Any]:
    rendered = value.to_dict()
    assert isinstance(rendered, Mapping)
    return cast(Mapping[str, Any], rendered)


def _success(value: ToolEnvelope) -> Mapping[str, Any]:
    rendered = _envelope(value)
    assert rendered.get("ok") is True, rendered
    data = rendered.get("data")
    assert isinstance(data, Mapping), rendered
    return cast(Mapping[str, Any], data)


def _mapping(value: object) -> Mapping[str, Any]:
    assert isinstance(value, Mapping)
    return cast(Mapping[str, Any], value)


def _sequence(value: object) -> Sequence[Any]:
    assert isinstance(value, Sequence) and not isinstance(value, str | bytes)
    return cast(Sequence[Any], value)


def _walk_symbols(symbols: object) -> Iterator[Mapping[str, Any]]:
    for symbol in _sequence(symbols):
        item = _mapping(symbol)
        yield item
        yield from _walk_symbols(item.get("children", ()))


def test_cold_first_semantic_call_resolves_cross_file_owner_and_references() -> None:
    """No prior overview or diagnostics call may be required for complete TS answers."""

    before = set(_locked_lsp_descendants())
    policy = WorkspacePolicy()
    runtime = WorkspaceRuntime(policy.resolve_activation(ROOT), path_policy=policy)
    try:
        _assert_definition_references_and_implementation(
            RealTypeScriptAcceptance(runtime, TypeScriptAdapterConfig.locked())
        )
    finally:
        _stop_and_assert_no_lsp_leaks(runtime, before)


def test_cold_first_reference_call_returns_the_complete_cross_file_set() -> None:
    """Reference completeness must not inherit warm state from another tool call."""

    before = set(_locked_lsp_descendants())
    policy = WorkspacePolicy()
    runtime = WorkspaceRuntime(policy.resolve_activation(ROOT), path_policy=policy)
    try:
        references = _success(runtime.find_referencing_symbols(IN_PROGRAM_FILE, "resolveCodexCommand"))
        assert int(references["reference_count"]) >= 3
        reference_paths = {_mapping(item)["path"] for item in _sequence(references["references"])}
        assert {IN_PROGRAM_CONSUMER, "src/server/codexAppServerBridge.ts"} <= reference_paths
        assert IN_PROGRAM_FILE not in reference_paths
    finally:
        _stop_and_assert_no_lsp_leaks(runtime, before)


def test_production_runtime_selects_the_configured_program_and_pinned_engine(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    status = acceptance.runtime.status()
    adapter = _mapping(_mapping(status["adapters"])["typescript"])
    engine = _mapping(adapter["engine"])
    omitted_status = _mapping(adapter["trusted_not_in_configured_program"])
    omitted = {_mapping(item)["path"] for item in _sequence(omitted_status["items"])}

    assert acceptance.config.language_server_version == "5.1.3"
    assert acceptance.config.typescript_version == "5.9.3"
    assert Path(str(engine["executable"])) == acceptance.config.language_server_path
    assert adapter["selected_native_config"] == "tsconfig.json"
    assert adapter["project_kind"] == "configured"
    assert adapter["scope_compatible"] is True
    assert int(_mapping(adapter["configured_program"])["count"]) > 1
    assert IN_PROGRAM_FILE not in omitted
    assert OMITTED_LISTED_FILE in omitted
    # The bounded status list is a sample of a much larger omission set.
    total = int(omitted_status["total"])
    assert total > len(omitted)
    assert int(omitted_status["omitted_count"]) == total - len(omitted)


def test_diagnostics_are_current_and_disclose_advisory_authority(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    overview = _envelope(acceptance.runtime.get_symbols_overview(IN_PROGRAM_FILE))
    assert overview["ok"] is True, overview
    rendered = _envelope(
        acceptance.runtime.get_diagnostics_for_file(
            IN_PROGRAM_FILE,
            timeout_seconds=20.0,
            maximum_severity=2,
        )
    )
    repeated = _envelope(
        acceptance.runtime.get_diagnostics_for_file(
            IN_PROGRAM_FILE,
            timeout_seconds=20.0,
            maximum_severity=2,
        )
    )
    if rendered.get("ok") is True:
        diagnostic_state = str(_mapping(rendered["data"])["state"])
        engine = _mapping(_mapping(rendered["data"])["engine"])
    else:
        error = _mapping(rendered["error"])
        details = _mapping(error["details"])
        diagnostic_state = str(details.get("state"))
        engine = _mapping(details["engine"])

    failures: list[str] = []
    if rendered.get("ok") is not True:
        failures.append(f"current-generation diagnostics did not complete: {rendered}")
    if repeated.get("ok") is not True:
        failures.append(f"unchanged repeated diagnostics did not complete: {repeated}")
    elif (
        rendered.get("ok") is True
        and _mapping(repeated["data"])["diagnostics_generation"] != _mapping(rendered["data"])["diagnostics_generation"]
    ):
        failures.append("unchanged repeated diagnostics manufactured a new generation")
    if diagnostic_state not in {"findings", "clean"}:
        failures.append(f"diagnostic state is {diagnostic_state!r}, expected findings or clean")
    if engine.get("authority") != "advisory":
        failures.append(f"LSP diagnostic authority is not advisory: {engine}")
    if _mapping(engine.get("authority_distinction", {})).get("repository_native_typecheck") != "authoritative":
        failures.append(f"repository-native authority is not explicit: {engine}")
    pinned = _mapping(engine.get("pinned_engine", {}))
    if pinned.get("name") != "typescript" or pinned.get("version") != "5.9.3":
        failures.append(f"pinned TypeScript 5.9.3 is not disclosed: {engine}")
    native = _mapping(engine.get("native_typecheck", {}))
    if native.get("authority") != "authoritative":
        failures.append(f"native typecheck authority is not authoritative: {engine}")
    # This root declares no ``typecheck`` script, so the disclosure must claim
    # authority without inventing a command the repository does not own.
    if "command" in native:
        failures.append(f"native typecheck named a command this root does not declare: {engine}")
    assert not failures, "\n".join(failures)


def test_overview_and_exact_body_range(acceptance: RealTypeScriptAcceptance) -> None:
    overview = _success(
        acceptance.runtime.get_symbols_overview(
            IN_PROGRAM_FILE,
            max_depth=2,
            max_answer_chars=200_000,
        )
    )
    symbols = {str(item["name_path"]): item for item in _walk_symbols(overview["symbols"])}
    assert {"resolveCodexCommand", "canRunCommand"} <= symbols.keys()

    found = _success(
        acceptance.runtime.find_symbol(
            "resolveCodexCommand",
            relative_path=IN_PROGRAM_FILE,
            include_body=True,
            include_info=True,
        )
    )
    symbol = _mapping(found["symbol"])
    body = str(symbol["body"])
    source = (ROOT / IN_PROGRAM_FILE).read_text(encoding="utf-8")
    symbol_range = _mapping(symbol["range"])
    start = int(_mapping(symbol_range["start"])["text_offset"])
    end = int(_mapping(symbol_range["end"])["text_offset"])
    assert body == source[start:end]
    assert body.startswith("export function resolveCodexCommand(")
    assert found["sha256"] == overview["sha256"]


def test_definition_references_and_implementation_use_public_semantics(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    _assert_definition_references_and_implementation(acceptance)


def _assert_definition_references_and_implementation(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    declaration = _success(
        acceptance.runtime.find_declaration(
            IN_PROGRAM_CONSUMER,
            r"(?m)^\s*(resolveCodexCommand),$",
            include_body=True,
            include_info=True,
        )
    )
    declaration_locations = _sequence(declaration["locations"])
    assert len(declaration_locations) == 1
    assert _mapping(declaration_locations[0])["relative_path"] == IN_PROGRAM_FILE

    references = _success(acceptance.runtime.find_referencing_symbols(IN_PROGRAM_FILE, "resolveCodexCommand"))
    assert int(references["reference_count"]) >= 3
    reference_paths = {_mapping(item)["path"] for item in _sequence(references["references"])}
    assert {IN_PROGRAM_CONSUMER, "src/server/codexAppServerBridge.ts"} <= reference_paths
    assert IN_PROGRAM_FILE not in reference_paths

    implementation = _success(
        acceptance.runtime.find_implementations(
            "resolveCodexCommand",
            IN_PROGRAM_FILE,
            include_info=True,
        )
    )
    implementation_locations = _sequence(implementation["locations"])
    assert len(implementation_locations) == 1
    assert _mapping(implementation_locations[0])["relative_path"] == IN_PROGRAM_FILE


def test_omitted_file_is_path_scoped_and_unicode_ranges_are_exact(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    before = acceptance.runtime.status()
    adapter_before = _mapping(_mapping(before["adapters"])["typescript"])
    configured_before = _mapping(adapter_before["configured_program"])
    omitted = {
        _mapping(item)["path"]
        for item in _sequence(_mapping(adapter_before["trusted_not_in_configured_program"])["items"])
    }
    assert OMITTED_LISTED_FILE in omitted

    overview_envelope = _envelope(
        acceptance.runtime.get_symbols_overview(
            OMITTED_MJS_FILE,
            max_depth=1,
            max_answer_chars=200_000,
        )
    )
    assert overview_envelope["ok"] is True, overview_envelope
    assert _mapping(overview_envelope["generations"])["scope"] == "path"
    overview = _mapping(overview_envelope["data"])
    assert any(str(item["name"]) == "jobs" for item in _walk_symbols(overview["symbols"]))

    unicode_overview = _success(
        acceptance.runtime.get_symbols_overview(
            UNICODE_FILE,
            max_depth=1,
            max_answer_chars=200_000,
        )
    )
    target = next(item for item in _walk_symbols(unicode_overview["symbols"]) if item["name"] == UNICODE_SYMBOL)
    target_range = _mapping(target["range"])
    start = _mapping(target_range["start"])
    source = (ROOT / UNICODE_FILE).read_text(encoding="utf-8")
    text_offset = int(start["text_offset"])
    byte_offset = int(start["byte_offset"])
    assert len(source[:text_offset].encode("utf-8")) == byte_offset
    assert byte_offset > text_offset
    assert int(start["line"]) == source[:text_offset].count("\n")

    first_diagnostics = _envelope(
        acceptance.runtime.get_diagnostics_for_file(
            OMITTED_MJS_FILE,
            timeout_seconds=20.0,
            maximum_severity=2,
        )
    )
    repeated_diagnostics = _envelope(
        acceptance.runtime.get_diagnostics_for_file(
            OMITTED_MJS_FILE,
            timeout_seconds=20.0,
            maximum_severity=2,
        )
    )
    assert first_diagnostics["ok"] is True, first_diagnostics
    assert repeated_diagnostics["ok"] is True, repeated_diagnostics
    assert _mapping(first_diagnostics["data"])["state"] in {"clean", "findings"}
    assert (
        _mapping(repeated_diagnostics["data"])["diagnostics_generation"]
        == _mapping(first_diagnostics["data"])["diagnostics_generation"]
    )

    after = acceptance.runtime.status()
    adapter_after = _mapping(_mapping(after["adapters"])["typescript"])
    assert _mapping(adapter_after["configured_program"]) == configured_before
    assert adapter_after["selected_native_config"] == adapter_before["selected_native_config"]
    assert adapter_after["project_kind"] == adapter_before["project_kind"]


def test_repository_native_typecheck_is_authoritative(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    paths = runtime_paths(Path(__file__).resolve().parents[2])
    locked_node = paths["node"]
    typescript_cli = ROOT / "node_modules/typescript/bin/tsc"
    vue_tsc_cli = ROOT / "node_modules/vue-tsc/bin/vue-tsc.js"
    assert locked_node.is_file()
    assert typescript_cli.is_file()
    assert vue_tsc_cli.is_file()
    assert locked_node.resolve().is_relative_to(paths["runtime"].resolve())
    version = subprocess.run(
        [str(locked_node), str(typescript_cli), "--version"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    native = subprocess.run(
        [str(locked_node), str(vue_tsc_cli), "--noEmit", "-p", "tsconfig.json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "Version 5.9.3"
    assert version.args == [str(locked_node), str(typescript_cli), "--version"]
    assert native.args == [str(locked_node), str(vue_tsc_cli), "--noEmit", "-p", "tsconfig.json"]
    assert acceptance.config.typescript_version == "5.9.3"
    assert native.returncode == 0, native.stdout + native.stderr
