# Configurable Workspace Python Environment Acceptance

## Release decision

**PASS.** `activate_workspace` now
accepts an optional Conda environment name, defaults to `ms`, and permits any
existing absolute non-Git directory as an exact read-only workspace. Git
workspaces below `/data` retain guarded editing; non-Git roots and external LSP
locations remain read-only.

The accepted runtime source build identity is
`ffb53b23e3923fd6e422073316814262bf3fbae4534b6a8a54b6ff891a06a6a0`;
public tool schema is `5`; dependency lock digest is
`eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`.

## Live behavior evidence

- The real stdio connector/shared-daemon acceptance bound the exact
  `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages`
  directory first with `python_environment="llm-framework-study"`, then with
  the omitted/default `ms` environment. Runtime status reported the exact
  selected interpreter in each binding.
- A real MCP `replace_symbol_body` call against
  `torchtune/__init__.py` returned typed `READ_ONLY_ROOT`; before/after bytes
  were identical.
- Releasing the final lease retired the exact test-owned daemon and all
  observed LSP descendants. Discovery and bearer files were removed; no
  host-wide process cleanup or canonical Serena process was touched.
- Real Pyright navigation from a Git fixture using
  `llm-framework-study` resolved the environment-exclusive `torchtune`
  definition as `read_only_external`, and diagnostics/status disclosed the
  selected interpreter.
- A fresh stdio connector listed `python_environment` in the
  `activate_workspace` input schema and reported the source-computed build
  identity. Claude Code's project-level MCP registration connected to the
  same locked shared-runtime executable.

## Lead verification

| Gate | Result |
| --- | --- |
| Complete repository suite | `909 passed, 35 skipped, 1 warning` in the final fixed-point run |
| Real installed daemon/rollover/stdio suite | `6 passed` |
| Real non-Git environment/readonly/lifecycle case | `1 passed` |
| Fresh stdio schema/build cases | `4 passed` |
| Ruff | pass |
| Ty | pass |
| Bootstrap | pass; service-owned CPython 3.12.12 and locked engines |
| Source ownership/provenance | pass; 19,838 production LOC informational, no forbidden/undeclared imports, census/manifest agree, 9 copied hashes verified |
| Strict OpenSpec | `5 passed, 0 failed` before stable-spec sync/archive |

The 35 skips are explicit external-repository snapshot gates whose snapshot
environment variables were not injected. The one warning is the existing
Starlette `httpx` deprecation warning. Neither is silently promoted to runtime
coverage.

## Scope retained

There is no automatic environment inference, arbitrary interpreter path,
ambient shell/Conda capture, allowlist configuration service, new language,
lexical search, diagnostics hook, or editing expansion. Large non-Git roots are
allowed but remain an Agent-owned scope/latency choice.
