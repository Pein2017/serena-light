## Why

Serena Light now provides the required semantic quality, but several recoverable
conditions still make Agents spend extra calls or guess at the correct next
action: an omitted Conda environment can disagree with an activated
`site-packages` path, healthy runtime status is much larger than its routine
readiness use, coordinate conventions are not visible in initialization, and
unsupported implementation or oversized-body requests do not point to the
existing safe fallback workflow.

## What Changes

- Warn, without automatically switching, when an activated path is clearly
  inside one installed Conda environment but the binding selected another.
- Make healthy runtime status compact while retaining bounded, actionable detail
  only for affected language families and runtime issues.
- State the 0-based decoded-text coordinate convention once in initialization
  and at the tool decision point; do not introduce a 1-based response mode.
- Add deterministic recovery guidance to Pyright implementation-provider
  failures and exact symbol bodies that cannot fit the answer budget.
- Preserve the single-lease binding model, existing shared multi-root daemon,
  compact semantic success schema, explicit Agent-selected environment, and
  capability-gated LSP semantics.
- Do not add hierarchy inference, implementation-by-reference fallbacks,
  selective body slicing, workspace comparison, per-call workspace selection,
  new public tools, lexical discovery, diagnostics hooks, or automatic Conda
  environment inference.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Add non-authoritative activation environment mismatch
  warnings, expose the coordinate convention in bounded initialization
  guidance, and make healthy runtime status compact while retaining actionable
  issue evidence.
- `semantic-navigation`: Add bounded recovery actions for unsupported Python
  implementation queries and exact symbol bodies that exceed the answer budget,
  without changing the underlying LSP semantics or success schema.

## Impact

- Public MCP tool count and semantic operation names remain unchanged.
- `activate_workspace` success and `get_runtime_status` success presentation
  change additively or compactly; semantic navigation success remains compatible.
- Existing Agents continue to omit `python_environment` and receive `ms`; a
  mismatch warning requires an explicit reactivation to change the binding.
- Affected production owners are initialization text, workspace identity and
  activation presentation, runtime status projection, and semantic error
  presentation. Tests and compatibility documentation will be updated alongside
  the implementation.
- No dependency, upstream Serena synchronization, plugin-registration, daemon
  lifecycle, trust/edit boundary, or client migration is required.

Admission evidence: the current tool schema already supports explicit Conda
selection, exact `Class/method` symbol lookup, bounded overview depth, typed
`UNSUPPORTED`, and runtime status. Official Serena also gates implementation
lookup on the language-server backend rather than deriving implementations from
references. The remaining work is therefore a bounded interaction repair, not
a new semantic subsystem.
