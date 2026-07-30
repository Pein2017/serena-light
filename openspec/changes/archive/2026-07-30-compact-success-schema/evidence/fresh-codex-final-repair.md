# Fresh Codex Final-Repair Acceptance

- Date: 2026-07-30 UTC
- Client/model/effort: fresh Codex subagent, `gpt-5.6-terra`, `high`
- Access boundary: Serena Light semantic tools only; read-only
- Build identity: `92b2618eb6030d50260b9885a63feb358f94f05823e545e0d5f72f9f3b380242`
- Daemon ID: `145cfe14-a52f-4d4c-b8b3-3690f5193408`
- Verdict: PASS

The fresh tool listing exposed `find_symbol.max_matches` as 1 through 100,
lowercase-string overview kind filters, declaration `max_answer_chars` as 512
through 50,000, integer implementation kind filters, and no public `compact`
flag.

The 512-character file-scoped ambiguity probe returned two bounded candidates,
`truncated=true`, and `omitted_count=851` instead of the former approximately
424,683-character response. The compact overview retained only class ancestors
that still had method children and reported `omitted=287`. TypeScript `Runner`
implementation lookup retained `ConcreteRunner` with class kind and name path
when requested; this adapter returned no separate semantic-detail field. The
method-only filter returned `files=[]` and `omitted=1`. The 512-character
indivisible-body probe returned typed
`INVALID_INPUT`, `minimum_required_chars=4520`, and retained workspace,
adapter, and generation authority.

Release returned `released=true` and `bound=false`. One other holder existed at
that instant, so this client correctly did not stop the shared runtime. A
post-release status showed no binding or runtime owned by this lease. No files
were changed.
