"""Concise, source-owned MCP initialize guidance for every Serena Light client."""


AGENT_INSTRUCTIONS = (
    "Experimental Python/JS/TS semantic navigation. Shell cd won't rebind; "
    "activate_workspace needs absolute path. Use rg/find for files/text; prefer "
    "Serena Light for symbols/references/diagnostics. Report issues to user."
)


__all__ = ["AGENT_INSTRUCTIONS"]
