# Tighten Scope Error Readiness Acceptance

## Release decision

**PASS.** Serena Light now preserves the already-computed native-program
projection evidence on `SCOPE_INCOMPATIBLE` query failures and reports the
calling lease's own `working_subdirectory` when multiple leases share one Git
root. Successful compact envelopes, public tool inputs, schema version 4,
editing scope, initialization guidance, and canonical Serena are unchanged.

This is a source-only versioned-daemon rollover. The accepted build identity is
`7deb20c2cbff6b6fac622d012ff80a923987efd80b673e14c0bcc3fa9b6e0fcf`;
the dependency lock digest remains
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`.
Existing clients remain on their current build slot until restarted, while a
fresh connector resolves this build without stopping older leased daemons.

## Accepted behavior

- A projection-backed `SCOPE_INCOMPATIBLE` error carries `language`,
  `project_kind`, optional `selected_config_path`, and bounded
  `configured_program_outside_trust` evidence with path, reason, total, digest,
  and omitted count. It remains non-retryable and does not expose engine,
  executable, or interpreter identity.
- A scope failure without a backing projection retains the prior concise
  reason/path form and does not fabricate configuration facts.
- Bound operational failures source `workspace.working_subdirectory` from the
  failing caller's `WorkspaceBinding`; another lease on the same physical root
  retains its own independent subdirectory.
- A family previously recorded unavailable still performs the next call's
  freshness preflight, then fails before adapter construction, warming, or
  executor submission if it remains blocked.

The implementation was completed by CC Agent
`/root/scope_readiness_worker` using `claude-sonnet-5` at high effort. The
worker completed all 17 OpenSpec tasks and reported 834 unit/integration tests
passing with 14 skips. The lead independently reviewed the complete diff and
reran the release gates below.

## Lead verification

| Gate | Result |
| --- | --- |
| Focused scope/runtime/service/envelope suite | `180 passed` |
| Complete repository suite | `885 passed, 35 skipped` in 210.63s |
| Compact/public schema regression | `16 passed` |
| Connector, proxy, stdio, and versioned-rollover suite | `53 passed` |
| Ruff | pass |
| Ty | pass |
| Bootstrap check | pass; service-owned CPython 3.12.12 and locked engines |
| Source ownership/provenance | pass; no forbidden or undeclared imports, 9 copied hashes verified |
| Strict OpenSpec before sync | 5 items passed |
| `git diff --check` | pass |

The 35 skips are the repository's explicit external-snapshot gates; their
required snapshot environment variables were not injected in this release
run. The changed seam is nevertheless covered through the real stdio connector,
proxy boundary, daemon service, and two-build rollover tests. No fresh Codex or
Claude host-client receipt is claimed for the final source identity; the user
will obtain it by restarting the target session and checking
`get_runtime_status`.

One exploratory verification command named two nonexistent registration test
files and therefore collected no tests. It changed no state and was replaced by
the real connector/stdio/rollover suite listed above.

## Scope and residuals

No readiness tool, `inspect_symbol`, lexical discovery, diagnostics hook,
automatic retry, workspace guessing, new language, or editing expansion was
added. `get_runtime_status` remains the detailed readiness/build owner. Host
`rg`/`find` remains the lexical file/text discovery route. The retired
`add-lexical-discovery` and `improve-warm-runtime-reuse` plans remain retired;
future performance work requires new measured friction and a new decision.
