# Fresh CC Agent Final-Repair Acceptance

- Date: 2026-07-30 UTC
- Client/model/effort: fresh CC Agent, `claude-sonnet-5`, `high`
- Access boundary: Serena Light MCP first and exclusively for semantic calls;
  read-only shell only for status and character-count corroboration
- Build identity: `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
- Daemon ID: `145cfe14-a52f-4d4c-b8b3-3690f5193408`
- Verdict: PASS

The fresh public listing matched schema 3: `find_symbol.max_matches` is bounded
1 through 100, overview kind filters are lowercase strings, declaration answer
budget is 512 through 50,000, implementation kind filters are integer arrays,
and no public `compact` flag exists.

The 512-character ambiguity probe returned two candidates with
`truncated=true` and `omitted_count=851`; its compact JSON text measured 924
characters rather than returning all 853 candidates. The overview returned two
class ancestors, each with retained method children, and `omitted=287`.
TypeScript implementation filters returned `ConcreteRunner` with class kind and
name path for `[5]`, and `files=[]/omitted=1` for `[6]`. The minimum-body-budget
error reported 4,520 required characters and preserved workspace, Pyright
authority, and path generations.

Release returned `active_holders=0`, `released=true`, `bound=false`,
`runtime_stopped=true`, and `runtime_stop_pending=false`. No files were changed.
