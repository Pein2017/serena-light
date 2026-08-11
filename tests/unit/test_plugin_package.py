from __future__ import annotations

import json
from pathlib import Path
from typing import cast

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text())
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


def test_plugin_manifest_is_thin_branded_mcp_packaging() -> None:
    manifest = _object(REPOSITORY_ROOT / ".codex-plugin" / "plugin.json")
    assert manifest["name"] == REPOSITORY_ROOT.name == "serena-light"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert "skills" not in manifest
    assert "apps" not in manifest

    interface = cast(dict[str, object], manifest["interface"])
    assert interface["displayName"] == "Serena Light"
    assert interface["brandColor"] == "#6D5EF7"
    assert "Experimental" in cast(str, interface["shortDescription"])
    prompts = cast(list[str], interface["defaultPrompt"])
    assert 0 < len(prompts) <= 3
    assert all(0 < len(prompt) <= 128 for prompt in prompts)
    for field in ("composerIcon", "logo"):
        asset = cast(str, interface[field])
        assert asset.startswith("./assets/")
        assert (REPOSITORY_ROOT / asset).is_file()


def test_plugin_mcp_preserves_startup_cwd_workspace_binding() -> None:
    payload = _object(REPOSITORY_ROOT / ".mcp.json")
    servers = cast(dict[str, object], payload["mcpServers"])
    server = cast(dict[str, object], servers["serena-light"])

    assert server["type"] == "stdio"
    assert server["args"] == []
    assert server["supports_parallel_tool_calls"] is True
    assert "cwd" not in server
    assert cast(str, server["command"]).endswith("/python/bin/serena-light")
