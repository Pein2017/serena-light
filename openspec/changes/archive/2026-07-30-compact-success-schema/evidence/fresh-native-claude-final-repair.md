# Fresh Native Claude Code Final-Repair Acceptance

- Date: 2026-07-30 UTC
- Claude Code: 2.1.220
- Model/effort: `claude-sonnet-5`, `high`
- Session: `e375322b-bbaf-43bd-a050-ef1831ec7a98`
- API duration: 57.793 seconds; end-to-end duration: 71.387 seconds
- MCP boundary: `--strict-mcp-config` with only `serena-light`, empty setting
  sources, `dontAsk`, and an explicit six-tool read-only Serena Light allowlist
- Permission denials, shell, file, web, canonical Serena, and subagent calls: 0
- Build identity: `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
- Verdict: PASS

The fresh tool listing independently confirmed every schema bound and the
absence of a public `compact` flag. Runtime status exposed the expected build;
it does not expose the public-schema-version constant as a status field.

The ambiguity probe returned two candidates, `truncated=true`, and
`omitted_count=851`. The overview retained two class ancestors with method
children and reported `omitted=287`. TypeScript implementation lookup returned
one unfiltered target, `ConcreteRunner` with class kind for `[5]`, and
`files=[]/omitted=1` for `[6]`. The indivisible-body probe returned typed
`INVALID_INPUT`, `minimum_required_chars=4520`, and retained workspace, adapter,
and generations.

Normal release returned `active_holders=0`, `released=true`, `bound=false`,
`runtime_stop_pending=false`, and left the shared daemon in its documented warm
state (`runtime_stopped=false`). The pre-existing `.serena/` directory predates
this run, so it is not evidence of a canonical-Serena call by this isolated
client.
