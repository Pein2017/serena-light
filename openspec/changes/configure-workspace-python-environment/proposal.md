## Why

Serena Light currently couples workspace trust and Python analysis to one hard-coded `ms/transformers` non-Git root. Agents therefore cannot directly inspect another installed environment without copying source into a Git workspace, and a copied tree can silently lose the target environment's import semantics.

## What Changes

- Allow `activate_workspace` to bind any existing absolute directory. Git paths still normalize to their owning Git root; a non-Git directory uses its exact resolved path and is always read-only.
- Add optional `python_environment` to `activate_workspace`. Omitting it selects `ms`; a supplied Conda environment name selects that environment's verified `bin/python` for the bound Pyright runtime.
- Include the selected Python environment in physical runtime identity so the same root activated with different environments never reuses the wrong Pyright process.
- Make semantic locations outside the active workspace visible as read-only instead of enforcing the former `/data` plus pinned-`ms` query allowlist.
- Generalize the existing bounded no-symlink non-Git inventory and targeted freshness behavior from transformers to any non-Git root.
- Preserve the existing edit boundary: only inventoried files in Git workspaces below `/data` are editable; every non-Git workspace and external semantic location remains read-only.
- Remove the production `TRANSFORMERS_ROOT` trust special case. Retain `ms` only as the default Python analysis environment.

Non-goals: automatic environment inference, ambient shell/Conda environment capture, arbitrary interpreter paths, per-call environment changes after binding, package copying, new languages, lexical search, or broader editing.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Generalize non-Git activation and make the selected Conda environment part of session binding and runtime reuse.
- `semantic-navigation`: Bind Pyright to the selected environment and keep all external definitions visible but read-only.
- `diagnostics-status`: Report the selected environment/interpreter in status and rich operational failures while compact success stays unchanged.
- `guarded-symbol-editing`: Generalize the read-only non-Git rejection contract beyond the former transformers special case.

## Impact

- Public MCP schema: backward-compatible optional `python_environment` on `activate_workspace`; workspace kind changes from the misleading `allowlisted_non_git` value to `non_git_read_only`.
- Runtime identity and lease binding: the selected environment participates in reuse/isolation and must survive activation, refresh, status, and release.
- Python backend: Pyright facts/configuration/attribution become runtime-selected by the verified Conda environment; TypeScript behavior is unchanged.
- Trust/inventory: any non-Git directory may be queried through the existing bounded no-symlink inventory, but it cannot be edited.
- Compatibility: existing callers that omit `python_environment` continue using `ms`; canonical Serena and client registration names are unchanged.
- Admission evidence: reproduce the current `UNTRUSTED_ROOT` for `llm-framework-study/site-packages`, then require successful read-only activation with both default `ms` and explicit `llm-framework-study`, environment-isolated runtime reuse, invalid-environment rollback, and no write expansion.
