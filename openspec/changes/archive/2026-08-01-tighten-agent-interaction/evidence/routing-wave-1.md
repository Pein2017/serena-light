# Routing wave 1 evidence

## Scope and controls

- The lead retained schema rollover, daemon integration, acceptance, documentation,
  final disposition, Git, and OpenSpec ownership.
- One Codex worker and one Claude worker received self-contained, non-overlapping
  write lanes. Neither worker was asked to implement the whole change, and neither
  touched `cc-plugin-codex`.
- The workers were allowed to finish naturally. No worker was stopped to equalize
  runtime.

## Codex worker

- Route: `gpt-5.6-terra`, `xhigh` reasoning.
- Task class: bounded cross-file semantic navigation implementation.
- Owned behavior: depth-0 overview defaults and filtering/omission semantics;
  reference declaration exclusion, opt-in snippets, and compact coverage evidence.
- Completion: completed within the parallel implementation wave.
- Worker receipt: 38 focused tests plus 5 runtime reference tests passed; Ruff
  passed on the owned surface.
- Lead verifier: 45 overview/reference tests passed, including a lead-added runtime
  assertion that both language-family reference dispatch paths set
  `includeDeclaration=false`.
- Lead correction: removed the daemon's legacy zero-snippet coercion so the worker's
  runtime default could reach the adapters unchanged. No correction to the worker's
  owned presenter/filter implementation was required before focused verification.
- Scope control and friction: stayed inside the assigned navigation/reference lane.
  The main friction was an integration seam outside that lane, not scope expansion.

The same completed Terra Agent was later continued for a non-overlapping
documentation/compatibility lane. It updated only README, compatibility/client
registration/roadmap, the public-contract test, and the planning-only
`add-lexical-discovery` change. Its receipt was 6 focused tests, Ruff, JSON
parsing, strict OpenSpec validation, and diff check all passing. Lead review found
no fabricated measurements or source-code edits; the historical schema-3
acceptance record correctly remained historical while the current top-level
schema advanced to 4.

## Claude worker

- Route: Claude Sonnet 5, `high` thinking, write-enabled leaf Agent.
- Task class: bounded diagnostics success-presentation adapter and unit tests.
- Owned behavior: compact file grouping, flat findings, TypeScript advisory
  authority, success-metadata removal, and whole-finding answer-budget trimming.
- Completion: completed within the same parallel implementation wave.
- Worker receipt: 22 focused tests passed; a broader unit run reported 720 passed
  and one expected cross-lane failure because source schema 4 preceded the still
  schema-3 compatibility document; Ruff passed on the owned surface.
- Lead verifier: the adapter, diagnostics tools/runtime metadata, and real FastMCP
  boundary now pass 37 focused tests.
- Lead corrections: wired the adapter at the daemon boundary, routed internal
  diagnostics with an effectively unbounded internal budget before enforcing the
  public final budget, and reused the shared canonical renderer. These were
  cross-layer integration responsibilities retained by the lead.
- Scope control and friction: the worker produced a small, isolated adapter and
  tests without editing runtime or docs. It did not identify the pre-presentation
  runtime pruning seam, which the lead caught during integration review.

## Interpretation

The tasks were different, so this wave does not support a matched model ranking or
an accuracy/speed comparison. Both routes were reliable for their bounded lanes.
The available spawn interfaces did not provide comparable start timestamps, so no
exact worker wall-time claim is recorded. The Terra result reinforces its use for
bounded cross-file contract work when the lead owns integration. The Sonnet result
supports its use for narrow, testable adapter work. These observations do not
materially change the existing shared routing priors.
