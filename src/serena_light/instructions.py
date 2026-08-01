"""Concise, source-owned MCP initialize guidance for every Serena Light client."""


AGENT_INSTRUCTIONS = (
    "Serena Light provides current semantic navigation and diagnostics for Python and "
    "JavaScript/TypeScript. It auto-binds the startup cwd; shell cd does not rebind. "
    "When switching repositories, call activate_workspace with an absolute directory path. "
    "Prefer file or directory query scope when known. Symbol overview starts at depth 0; reference "
    "snippets are opt-in. Call diagnostics explicitly after a meaningful edit group. "
    "Use runtime status only for debugging, build, or readiness questions. Use host "
    "shell/file tools for lexical file enumeration and text search."
)


__all__ = ["AGENT_INSTRUCTIONS"]
