from __future__ import annotations

from pathlib import Path

from serena_light.workspace.runtime import _native_typecheck_command


def test_native_typecheck_command_requires_a_declared_package_script(tmp_path: Path) -> None:
    package = tmp_path / "package.json"

    assert _native_typecheck_command(tmp_path) is None
    package.write_text('{"scripts":{"test":"pytest"}}', encoding="utf-8")
    assert _native_typecheck_command(tmp_path) is None
    package.write_text('{"scripts":{"typecheck":"tsc -p tsconfig.json"}}', encoding="utf-8")
    assert _native_typecheck_command(tmp_path) == "npm run typecheck"


def test_native_typecheck_command_fails_closed_on_malformed_or_empty_scripts(tmp_path: Path) -> None:
    package = tmp_path / "package.json"

    package.write_text("not-json", encoding="utf-8")
    assert _native_typecheck_command(tmp_path) is None
    package.write_text('{"scripts":{"typecheck":"  "}}', encoding="utf-8")
    assert _native_typecheck_command(tmp_path) is None
