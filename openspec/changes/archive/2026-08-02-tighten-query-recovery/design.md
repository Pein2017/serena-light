## Context

Schema 4 already minimizes successful navigation and diagnostics payloads, but
the first Luna V2 paired code-archaeology benchmark exposed a different cost:
the Agent guessed four nonexistent symbols and made three queries against the
wrong active root. None was a language-server, freshness, or daemon failure.
The same live client surface also showed that the 562-character source
instruction appears as a 564-character common prefix on all 11 Serena Light
tools: 12,646 total description characters, including about 5,640 characters
that are repeated after the first copy.

Official Serena was not semantically more accurate in that benchmark. Its arm
used overview more consistently and stopped after its first wrong-root failure;
therefore this change improves recovery behavior without introducing official
Serena's broader tool surface or treating one stochastic run as a model gate.

The current contracts already provide explicit per-lease workspace binding,
typed deterministic failures, compact ambiguity candidates, host-owned lexical
search, and versioned build rollover. This design must preserve those owners.

## Goals / Non-Goals

**Goals:**

- Reduce repeated Agent metadata while retaining all decision-bearing workflow
  guidance across initialization and existing tool descriptions.
- Turn the two observed deterministic failure classes into one safe, bounded
  next action without dispatching another semantic operation.
- Teach overview-before-guessing, qualified ambiguity retry, and explicit
  reactivation after a root switch at the point where each choice is made.
- Keep every success, semantic authority, freshness, diagnostics, editing,
  lease, and build-isolation contract unchanged.

**Non-Goals:**

- Fuzzy or lexical symbol fallback, guessed aliases, or automatic candidate
  selection.
- Automatic root discovery or activation from a failed relative path.
- New MCP tools, hooks, batching, diagnostics injection, RTK integration, or
  lexical discovery.
- Removing `structuredContent`, flattening successful envelopes, changing the
  schema-4 input surface, or optimizing official Serena/client configuration.
- A matched official-versus-Light ablation or a hard stochastic call-count
  threshold.

## Decisions

### 1. Keep one short global instruction and move local choices to tool metadata

`AGENT_INSTRUCTIONS` will use this exact candidate text unless implementation
uncovers a client parsing constraint:

```text
Python/JS/TS semantic navigation and diagnostics. Shell cd does not rebind; call activate_workspace with an absolute root to switch. Overview unfamiliar files before exact lookup; use host tools for lexical search.
```

It is 214 characters and retains language scope, the non-rebinding rule,
explicit activation, overview-first lookup, and host-owned lexical search. The
outer connector and inner daemon will still publish byte-identical bytes.

Existing owning tool descriptions will carry advice that need not be repeated
globally:

- `activate_workspace`: startup cwd is auto-bound; shell `cd` does not change
  the lease; pass an absolute path to switch or return to a root.
- `get_symbols_overview`: use depth 0 first for an unfamiliar file.
- `find_symbol`: use overview when the exact name is unknown and retry
  ambiguity with a returned qualified name path.
- `find_referencing_symbols`: snippets remain opt-in.
- diagnostics tools: call explicitly after a meaningful edit group.
- `get_runtime_status`: debug/build/readiness only, not routine preflight.

This preserves discoverability while bounding the instruction prefix at 220
characters. The alternative of deleting initialization entirely was rejected
because cross-root binding is session-global context; retaining the current
long paragraph was rejected because the active client repeats it per tool.

### 2. Add only two closed recovery actions to deterministic errors

Recovery metadata will reuse the existing compact deterministic error envelope
and its `details` object:

```json
{"next_action":"get_symbols_overview"}
```

is added only when `find_symbol` misses inside one existing, authorized source
file. The existing `relative_path` identifies the overview input. Directory and
global misses do not receive this action because overview is file-scoped.

```json
{"next_action":"activate_workspace_if_other_root"}
```

is added to an `INVALID_PATH` produced by a bound semantic call. Its envelope
also retains the active physical workspace root. The action is conditional: it
does not claim that the invalid path exists elsewhere and does not name or
activate a guessed target root.

The action values form a closed internal enum. They are correction evidence,
not executable commands, and are never interpreted by the daemon. At the
512-character public floor, code, message, active workspace, and `next_action`
take priority over long echoed query/path values. Free-form prose, fuzzy
candidate lists, and automatic replay were rejected because they add tokens or
can silently select the wrong code.

### 3. Keep ambiguity strict and make its existing candidates easier to use

`AMBIGUOUS_SYMBOL` remains a typed failure with bounded deterministic
candidates. No new response field is required: `find_symbol` metadata will tell
the Agent to retry with one returned qualified name path. This preserves the
important distinction between expected disambiguation and a failed engine.

### 4. Treat orchestration truncation as a caller concern

The observed truncation came from aggregating several large MCP results inside
one orchestration output, not from Serena Light's final answer budget. The
server will not add a batch tool or change `max_answer_chars`. Acceptance
guidance will instead use one overview or body query at a time and narrow
subsequent reads.

### 5. Use deterministic acceptance and observational fresh-client receipts

Tests own the contract: exact instruction length/identity, tool-local guidance,
closed recovery fields, workspace retention, 512-character presentation, no
semantic dispatch for invalid inputs, and unchanged success shapes. Fresh
Codex and Claude/CC clients will record tool-description census and build
identity. One Luna/medium Light-only smoke will exercise unfamiliar-file and
cross-root recovery, but its call count is evidence rather than a release gate.

No new dependency or copied upstream mechanism is required. Production LOC
continues to be reported informationally; the obsolete 12k hard ceiling does
not return.

## Risks / Trade-offs

- **Shorter initialization may hide workflow detail** → Require every removed
  instruction to appear in its owning tool description and test the complete
  tool list.
- **Clients may change how they repeat server instructions** → Keep the
  source-level 220-character bound normative and record client-level totals as
  environment evidence, not a permanent compatibility constant.
- **Agents may ignore `next_action`** → Make it a closed, tool-named value and
  verify deterministic payloads; do not make model behavior a correctness gate.
- **Long paths can compete with recovery evidence at 512 characters** → Prune
  echoed path/name values before workspace and `next_action`.
- **Optional error fields change recorded fixtures** → Update only deterministic
  error fixtures; schema version remains 4 and consumers must tolerate optional
  correction details.
- **A source-only metadata change rolls the daemon build** → Rely on existing
  build identity slots and lease retirement; do not terminate older holders.

## Migration Plan

1. Capture the current 562/564-character instruction and fresh-client
   description census as the baseline.
2. Add red unit and real FastMCP tests for instruction ownership and both
   recovery actions.
3. Implement the source instruction, tool metadata, and deterministic error
   details without changing semantic dispatch.
4. Run focused and complete gates, then start fresh Codex and Claude/CC clients
   so they select the new source build identity.
5. Record the metadata census and one Light-only Luna/medium recovery smoke.
6. Update compatibility and acceptance evidence, sync specs, archive, and
   release through the existing local Git workflow.

Rollback restores the previous source revision. Existing leases remain on
their exact older build until ordinary release/expiry; no process is killed and
no official Serena asset or client registration is restored automatically.

## Open Questions

None. The instruction bound, recovery enum, non-goals, and observational status
of Agent call counts are fixed for implementation.
