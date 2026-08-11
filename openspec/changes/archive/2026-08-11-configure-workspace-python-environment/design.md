## Context

See `proposal.md` for motivation. The current `WorkspacePolicy` owns a single `allowed_non_git_root`, `WorkspaceIdentity.registry_key` contains only kind plus root, and every Pyright adapter calls fixed `PyrightFacts.locked()` backed by `ms`. The lease service already has fail-closed cross-root switching and the runtime registry already isolates distinct physical keys; this change should deepen those existing owners instead of adding another configuration service.

The OpenSpec project context still mentions a 12k LOC stop gate and an exact transformers trust root. The user's later controlling decisions supersede both: production LOC remains informational, and non-Git activation is intentionally unrestricted but read-only.

## Goals / Non-Goals

**Goals:**

- Make one activation call the sole owner of root plus Python-environment selection.
- Preserve zero-argument compatibility through the `ms` default.
- Reuse the current lease, registry, bounded inventory, freshness, status, and guarded-edit seams.
- Keep an invalid environment or failed refresh from mutating the current binding.

**Non-Goals:**

- Automatic environment discovery, `VIRTUAL_ENV`, shell-state inheritance, or per-project config files.
- Supporting arbitrary interpreter paths or non-Conda environment layouts.
- Treating LSP diagnostics as runtime execution evidence.
- Optimizing activation of a caller-selected very large non-Git root; the Agent owns that scope choice.

## Decisions

### 1. `activate_workspace` accepts an optional environment name

The public schema adds `python_environment: string | null`; omission normalizes to `ms`. Names must be non-empty safe Conda environment basenames without separators or traversal. A small resolver maps the name beneath the service-owned Conda environments root, validates `bin/python`, and asks that interpreter for its sysconfig roots using the existing bounded subprocess pattern.

An absolute interpreter parameter was rejected because it makes routine calls verbose and lets callers repeat layout knowledge. Automatic inference was rejected at the user's direction because activation should be explicit and predictable.

### 2. Environment selection is physical runtime identity

`WorkspaceIdentity` carries normalized environment name and interpreter. Its registry key becomes `(kind, root, interpreter)`. Working-subdirectory remains lease-local metadata and does not enter the key. Thus same-root/same-environment leases reuse one runtime, while a different environment selects another warm runtime.

Changing environment on the same root follows the existing cross-key provisional acquisition path: resolve and validate first, acquire/refresh the candidate, then swap the lease. Any failure aborts only the candidate and leaves the old binding intact.

### 3. Pyright facts become interpreter-parameterized

The locked Pyright executable/version remain build-owned. Only the configured Python interpreter varies. Runtime construction passes the identity's interpreter into attribution, adapter configuration, engine metadata, and status. TypeScript construction remains unchanged. Helpers used only for Pyright's owned syntax recovery receive the same interpreter-aware facts rather than reconstructing the `ms` default.

### 4. Non-Git means exact-root, bounded, and read-only

If Git discovery finds no repository, the exact resolved directory becomes `non_git_read_only`. The existing bounded no-symlink inventory is generalized by name and reused unchanged; activating all of `site-packages` is allowed and may cost proportionally more because the user explicitly assigns scope control to the Agent.

Direct path operands remain inventory-bounded. Existing LSP-returned locations outside the workspace are surfaced as `read_only_external` after strict existence/resolution checks instead of being filtered through a second query-root allowlist. Only Git inventories below `/data` can pass edit authorization.

### 5. Success remains compact; status owns environment detail

Routine semantic and diagnostic successes do not repeat interpreter metadata. Activation returns the selected environment once, and runtime status/rich operational failures retain the environment plus interpreter for debugging. This preserves the compact-response contract.

## Risks / Trade-offs

- **Large non-Git roots can index slowly** → Keep traversal bounded/no-symlink and state in tool instructions that an exact package root is usually faster, but do not reject the Agent's chosen directory.
- **Static analysis still differs from runtime behavior** → Report the actual selected interpreter and retain the existing statement that diagnostics are advisory evidence.
- **Same root can own several warm runtimes** → Interpreter in the registry key prevents semantic corruption; existing lease/grace retirement bounds lifetime.
- **Conda environment disappears after binding** → Fresh activation validation fails closed; an already-running adapter follows existing crash/not-ready behavior and never silently switches to `ms`.
- **Public workspace kind changes** → Update compatibility fixtures/docs and accept the intentional internal schema change; no canonical Serena surface changes.

## Migration Plan

1. Ship the optional schema and environment-aware identity while preserving default `ms` calls.
2. Validate both existing Git/transformers acceptance and live `llm-framework-study` non-Git activation.
3. Restart fresh Codex/Claude clients so the optional field and new build identity are negotiated.
4. Roll back by returning clients to the prior build; no persisted workspace configuration requires migration.
