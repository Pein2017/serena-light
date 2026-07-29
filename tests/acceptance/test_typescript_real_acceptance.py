from __future__ import annotations

import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psutil
import pytest

from serena_light.lsp.typescript import TypeScriptAdapterConfig
from serena_light.tools.envelopes import ToolEnvelope
from serena_light.workspace.identity import PinnedMsRoots, WorkspacePolicy
from serena_light.workspace.runtime import WorkspaceRuntime

ROOT = Path("/data/CoordExp/cc-plugin-codex")
OMITTED_UNICODE_FILE = "tests/runtime/agent-completion-projection.test.mjs"
UNICODE_SYMBOL_FRAGMENT = "preserves legacy truncation provenance"

pytestmark = pytest.mark.timeout(120)


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


@pytest.fixture(scope="module")
def acceptance() -> Iterator[RealTypeScriptAcceptance]:
    before = set(_locked_lsp_descendants())
    policy = WorkspacePolicy(ms_roots=PinnedMsRoots.resolve())
    runtime = WorkspaceRuntime(policy.resolve_activation(ROOT), path_policy=policy)
    try:
        yield RealTypeScriptAcceptance(runtime, TypeScriptAdapterConfig.locked())
    finally:
        runtime.stop()
        deadline = time.monotonic() + 5.0
        leaked: dict[int, str] = {}
        while time.monotonic() < deadline:
            leaked = {pid: command for pid, command in _locked_lsp_descendants().items() if pid not in before}
            if not leaked:
                break
            time.sleep(0.05)
        assert not leaked, f"Serena Light leaked TypeScript LSP descendants: {leaked}"


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
    assert "runtime/args.mjs" not in omitted
    assert "eslint.config.mjs" in omitted


def test_diagnostics_are_current_and_disclose_advisory_authority(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    rendered = _envelope(
        acceptance.runtime.get_diagnostics_for_file(
            "runtime/args.mjs",
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
    if native.get("authority") != "authoritative" or native.get("command") != "npm run typecheck":
        failures.append(f"native typecheck authority/command is incomplete: {engine}")
    assert not failures, "\n".join(failures)


def test_mjs_overview_and_exact_body_range(acceptance: RealTypeScriptAcceptance) -> None:
    overview = _success(acceptance.runtime.get_symbols_overview("runtime/args.mjs", max_depth=2))
    symbols = {str(item["name_path"]): item for item in _walk_symbols(overview["symbols"])}
    assert {"parseArgs", "splitRawArgumentString"} <= symbols.keys()

    found = _success(
        acceptance.runtime.find_symbol(
            "parseArgs",
            relative_path="runtime/args.mjs",
            include_body=True,
            include_info=True,
        )
    )
    symbol = _mapping(found["symbol"])
    body = str(symbol["body"])
    source = (ROOT / "runtime/args.mjs").read_text(encoding="utf-8")
    symbol_range = _mapping(symbol["range"])
    start = int(_mapping(symbol_range["start"])["text_offset"])
    end = int(_mapping(symbol_range["end"])["text_offset"])
    assert body == source[start:end]
    assert body.startswith("export function parseArgs(")
    assert found["sha256"] == overview["sha256"]


def test_definition_references_and_implementation_use_public_semantics(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    declaration = _success(
        acceptance.runtime.find_declaration(
            "runtime/cli.mjs",
            r'import \{ (parseArgs), splitRawArgumentString \} from "\./args\.mjs";',
            include_body=True,
            include_info=True,
        )
    )
    declaration_locations = _sequence(declaration["locations"])
    assert len(declaration_locations) == 1
    assert _mapping(declaration_locations[0])["relative_path"] == "runtime/args.mjs"

    references = _success(acceptance.runtime.find_referencing_symbols("runtime/args.mjs", "parseArgs"))
    assert int(references["reference_count"]) >= 3
    reference_paths = {_mapping(item)["path"] for item in _sequence(references["references"])}
    assert {"runtime/args.mjs", "runtime/cli.mjs"} <= reference_paths
    assert any(
        _mapping(_mapping(item)["container"])["name_path"] == "parse"
        for item in _sequence(references["references"])
    )

    implementation = _success(
        acceptance.runtime.find_implementations(
            "parseArgs",
            "runtime/args.mjs",
            include_info=True,
        )
    )
    implementation_locations = _sequence(implementation["locations"])
    assert len(implementation_locations) == 1
    assert _mapping(implementation_locations[0])["relative_path"] == "runtime/args.mjs"


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
    assert OMITTED_UNICODE_FILE in omitted

    overview_envelope = _envelope(
        acceptance.runtime.get_symbols_overview(
            OMITTED_UNICODE_FILE,
            max_depth=1,
            max_answer_chars=200_000,
        )
    )
    assert overview_envelope["ok"] is True, overview_envelope
    assert _mapping(overview_envelope["generations"])["scope"] == "path"
    overview = _mapping(overview_envelope["data"])
    target = next(item for item in _walk_symbols(overview["symbols"]) if UNICODE_SYMBOL_FRAGMENT in str(item["name"]))
    target_range = _mapping(target["range"])
    start = _mapping(target_range["start"])
    source = (ROOT / OMITTED_UNICODE_FILE).read_text(encoding="utf-8")
    text_offset = int(start["text_offset"])
    byte_offset = int(start["byte_offset"])
    assert len(source[:text_offset].encode("utf-8")) == byte_offset
    assert byte_offset > text_offset
    assert int(start["line"]) == source[:text_offset].count("\n") + 1

    after = acceptance.runtime.status()
    adapter_after = _mapping(_mapping(after["adapters"])["typescript"])
    assert _mapping(adapter_after["configured_program"]) == configured_before
    assert adapter_after["selected_native_config"] == adapter_before["selected_native_config"]
    assert adapter_after["project_kind"] == adapter_before["project_kind"]


def test_repository_native_typescript_7_typecheck_is_authoritative(
    acceptance: RealTypeScriptAcceptance,
) -> None:
    version = subprocess.run(
        [str(ROOT / "node_modules/.bin/tsc"), "--version"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    native = subprocess.run(
        ["npm", "run", "typecheck"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == "Version 7.0.2"
    assert acceptance.config.typescript_version == "5.9.3"
    assert native.returncode == 0, native.stdout + native.stderr
