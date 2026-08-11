"""Concise, source-owned MCP initialize guidance for every Serena Light client."""


AGENT_INSTRUCTIONS = (
    "Experimental Python/JS/TS. Shell cd won't rebind; activate_workspace needs an absolute "
    "path; Conda defaults to ms. Use rg/find for text, Light for symbols/references/diagnostics. "
    "Ranges are 0-based. Report friction."
)


__all__ = ["AGENT_INSTRUCTIONS"]
