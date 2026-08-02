from __future__ import annotations

from pathlib import Path
from typing import cast

from serena_light.build_identity import (
    PUBLIC_TOOL_SCHEMA_VERSION,
    compute_build_identity,
)
from serena_light.connector import Connector, build_proxy_server
from serena_light.instructions import AGENT_INSTRUCTIONS

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_schema4_is_a_build_identity_input() -> None:
    assert PUBLIC_TOOL_SCHEMA_VERSION == "4"
    assert compute_build_identity(REPOSITORY_ROOT) != compute_build_identity(
        REPOSITORY_ROOT,
        public_tool_schema_version="3",
    )


def test_agent_instructions_are_concise_and_cover_the_fixed_workflow() -> None:
    assert len(AGENT_INSTRUCTIONS.encode()) <= 220
    for required in (
        "Python/JS/TS",
        "semantic navigation",
        "diagnostics",
        "does not rebind",
        "activate_workspace",
        "absolute root",
        "Overview unfamiliar files",
        "host tools",
        "lexical search",
    ):
        assert required in AGENT_INSTRUCTIONS
    assert "hook" not in AGENT_INSTRUCTIONS.lower()


def test_stdio_proxy_publishes_the_source_owned_instructions() -> None:
    server = build_proxy_server(cast(Connector, object()))

    assert server.create_initialization_options().instructions == AGENT_INSTRUCTIONS
