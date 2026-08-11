## Context

See [proposal.md](proposal.md) for motivation. The observable contracts are the
two delta specs in [specs/workspace-runtime/spec.md](specs/workspace-runtime/spec.md)
and [specs/semantic-navigation/spec.md](specs/semantic-navigation/spec.md).

The current service already has the necessary semantic inputs but exposes them
at inconvenient boundaries:

- `WorkspacePolicy` validates one explicit Conda environment and resolves the
  activation path, while `WorkspaceDaemonService` owns the success envelope.
- `WorkspaceRuntime.status()` supplies a rich internal snapshot and
  `get_runtime_status` currently forwards almost all of it.
- compact navigation presentation owns the final answer budget, so an
  oversized exact body can be discovered after the rich semantic core succeeds.
- recovery actions are already a closed enum at the public error presentation
  boundary.

This change therefore deepens existing presentation seams. It does not create a
second readiness service, environment inference system, hierarchy engine, or
workspace owner.

## Goals / Non-Goals

**Goals:**

- keep activation selection explicit while making an obvious environment-path
  disagreement visible at the successful control response;
- reduce healthy status to the facts an Agent needs to confirm binding, build,
  readiness, and queue state;
- retain bounded causal evidence and remediation for unhealthy status;
- make coordinate and semantic recovery decisions available without extra
  exploratory calls;
- keep every new recovery action machine-readable and closed.

**Non-Goals:**

- no automatic environment selection, project-file inspection, PATH/ambient
  capture, or arbitrary interpreter parameter;
- no 1-based response schema, per-call workspace parameter, second lease per
  connector, workspace comparison, hierarchy inference, or synthetic
  implementation result;
- no body slicing, pagination, line editing, lexical MCP tool, diagnostics hook,
  new dependency, or copied upstream mechanism;
- no expansion of logs or a general-purpose debug endpoint.

## Decisions

### 1. Detect a path-indicated environment without making it authoritative

`CondaEnvironmentResolver` will expose a pure, bounded path classification that
accepts the already-resolved activation directory. It will recognize an
environment only when the path is below exactly one direct child of the fixed
Conda environments root, the child name satisfies the existing environment-name
grammar, and that child's configured `bin/python` is a usable installed
interpreter. It will not enumerate imports, inspect configuration, or consult
ambient state.

`WorkspaceDaemonService` will keep an internal resolved-activation record that
contains the existing `ResolvedWorkspace` plus zero or one advisory warning.
The registry and runtime key receive only `ResolvedWorkspace`; the warning is
attached only after activation commits successfully. Thus failed activation,
lease races, registry reuse, and runtime ownership remain unchanged.

The public warning is a fixed record:

```json
{
  "code": "PYTHON_ENVIRONMENT_PATH_MISMATCH",
  "selected_environment": "ms",
  "path_environment": "llm-framework-study",
  "next_action": "reactivate_with_path_environment"
}
```

The original absolute path is already known to the caller and will not be
echoed. Alternative rejected: automatically selecting the path environment.
That would contradict the accepted explicit binding contract and could silently
change diagnostics semantics.

### 2. Project rich internal status into one compact public DTO

`WorkspaceRuntime.status()` remains the internal source of runtime facts so
lifecycle and adapter code do not learn presentation policy. A focused pure
presenter under `serena_light.tools` will combine the service's lease-local
binding, raw runtime status, and daemon build/version facts into:

```json
{
  "workspace": {
    "root": "/abs/root",
    "working_subdirectory": "/abs/root/subdir",
    "kind": "git",
    "python_environment": "ms"
  },
  "build": {
    "identity": "...",
    "server_version": "...",
    "protocol_version": "..."
  },
  "languages": [
    {"language": "python", "state": "ready"}
  ],
  "executor": {"active": 0, "queued": 0, "capacity": 32},
  "issues": []
}
```

The presenter derives a stable state from the existing adapter phase, cooldown,
crash, and unavailable-family facts. It emits at most one issue per fixed
language family plus one executor issue. Existing bounded projection samples
are reused; transition arrays and historical crash counters are never copied.
An unbound lease uses `workspace=null`, empty languages, and a closed
`WORKSPACE_UNBOUND` issue.

Alternative rejected: `compact=true` or a second readiness tool. Both introduce
dual choreography and conflict with the established rule that healthy public
paths are compact by default while errors own recovery detail.

### 3. Keep coordinate guidance static

The source-owned initialize string will be replaced by a fixed string no longer
than 220 characters that includes `Ranges are 0-based`. Navigation tool
descriptions will state the full decoded-text/Unicode convention and the
editor-line `+1` conversion where a range is returned. Existing raw external
`position_basis` remains authoritative.

No position field is added to normal success. Alternative rejected: a 1-based
flag or repeated envelope-level basis, because either fragments the schema and
adds tokens to every successful call.

### 4. Extend the closed recovery vocabulary at the final presentation owner

The existing recovery enum will add these values:

- `find_referencing_symbols`
- `overview_then_find_child_symbol`
- `retry_with_minimum_answer_chars`
- `find_symbol_location_then_exact_file_read`
- `reactivate_with_path_environment`
- `activate_workspace`

The declaration service will add the implementation-provider reason and
reference action only to the existing `UNSUPPORTED` path. It will not dispatch a
fallback.

For exact body budgeting, the rich symbol record will carry one internal-only
`has_children` fact derived from the verified document-symbol tree. Compact
success strips it. When final rendering cannot retain the first complete body,
the minimum-error renderer selects one recovery action from `has_children` and
the measured size. The measured value remains in
`minimum_required_chars`; no source fragment enters the error.

Alternative rejected: selecting recovery in the semantic core. The public
budget can reject a body that fits the private semantic budget, so the final
presentation layer is the first truthful owner of this decision.

### 5. Treat the response change as a new public schema build

The public schema version will advance from 5 to 6. Because schema version
participates in build identity, fresh connectors will select a new build daemon
while existing leased clients can finish on the prior daemon. No old daemon is
killed while it has holders, and canonical Serena is untouched.

Compatibility documentation will record the new status DTO, warning, recovery
actions, unchanged compact success schema, and fresh-client requirement.

## Risks / Trade-offs

- **A path beneath an environment prefix may intentionally use another
  interpreter** → the warning is advisory, never changes the binding, and appears
  only on activation.
- **Compact status could hide evidence needed for repair** → affected-family
  issue records reuse current bounded configuration/scope evidence and explicit
  remediation; healthy history remains intentionally unavailable publicly.
- **Dynamic status shape could make Agents guess** → top-level keys are fixed;
  optional detail lives only inside bounded `issues` records with stable codes.
- **Internal `has_children` could leak into success** → compact-schema unit and
  exact-MCP acceptance tests assert it is absent.
- **Recovery advice could be stale or free-form** → actions are closed enum
  values validated at presentation, and capability success never contains them.
- **A long initialize string could regress repeated metadata cost** → retain the
  existing 220-character hard bound and byte-identity tests for inner/outer MCP.

## Migration Plan

1. Implement each behavior behind its existing owner with failing unit tests
   first, then focused daemon/connector integration tests.
2. Advance schema/build identity and update compatibility, client-registration,
   README, and OpenSpec evidence.
3. Run the deterministic suite, Ruff, Ty, bootstrap/provenance checks, strict
   OpenSpec validation, and fresh stdio/real-daemon acceptance in clean and
   poisoned-proxy environments.
4. Install the new Codex plugin/MCP build and update Claude Code only after the
   source checkout passes. Existing clients remain valid on their old build;
   fresh clients prove schema 6.
5. Roll back by reinstalling the preceding known-good plugin/build. Versioned
   daemon slots and leases make rollback non-destructive.

No copied Serena source or provenance manifest changes are expected. If
implementation requires a copied upstream mechanism, work stops for a new
scope decision rather than silently expanding this change.
