"""Concise, source-owned MCP initialize guidance for every Serena Light client."""


AGENT_INSTRUCTIONS = (
    "Python/JS/TS semantic navigation and diagnostics. Shell cd does not rebind; call "
    "activate_workspace with an absolute root to switch. Overview unfamiliar files before "
    "exact lookup; use host tools for lexical search."
)


__all__ = ["AGENT_INSTRUCTIONS"]
