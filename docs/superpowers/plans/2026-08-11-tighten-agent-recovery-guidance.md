# Tighten Agent Recovery Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the accepted Serena Light activation warning, compact runtime status, coordinate guidance, and closed semantic recovery actions without adding a tool or changing semantic truth.

**Architecture:** The OpenSpec change at `openspec/changes/tighten-agent-recovery-guidance/` is the sole behavior and completion authority. This plan is execution-only: identity classifies an evident Conda path, daemon service attaches advisory activation presentation, one pure presenter compacts raw runtime status, and final error presentation selects closed recovery actions after the real public answer budget is known.

**Tech Stack:** Python 3.12, MCP Python SDK/FastMCP, Pyright, pinned TypeScript language server, pytest, Ruff, Ty, OpenSpec CLI.

## Global Constraints

- Work in an isolated linked worktree created from the commit containing this plan; use branch `codex/tighten-agent-recovery-guidance`.
- Run Python commands through `conda run -n ms` unless an acceptance case explicitly selects another environment.
- Treat `openspec/changes/tighten-agent-recovery-guidance/{proposal.md,design.md,specs/**,tasks.md}` as the only authority for public behavior.
- Do not add public tools, dependencies, environment inference, a 1-based mode, workspace comparison, hierarchy inference, body slicing, lexical discovery, or automatic fallback dispatch.
- Do not modify canonical Serena or `/data/CoordExp/external/serena`.
- Preserve the dirty-worktree boundary: stage only the exact Serena Light files named by each task.
- Stop if implementation requires copied upstream code, a new dependency, a different public tool surface, or a change to single-lease ownership.

---

### Task 1: Advisory Conda path mismatch

**Files:**
- Modify: `src/serena_light/workspace/identity.py`
- Modify: `src/serena_light/daemon/service.py`
- Test: `tests/unit/test_workspace_identity.py`
- Test: `tests/unit/test_daemon_service.py`
- Test: `tests/acceptance/test_connector_contract_acceptance.py`

**Interfaces:**
- Consumes: `CondaEnvironmentResolver.envs_root`, `WorkspacePolicy.resolve_activation(...)`, and `ResolvedWorkspace`.
- Produces: `CondaEnvironmentResolver.environment_for_path(path: Path) -> str | None`; internal service `_ResolvedActivation`; successful activation `warnings` containing only closed warning records.

- [ ] **Step 1: Write failing path-classification tests**

Add tests using the existing `_flexible_policy` fixture. Cover a matching direct
environment path, mismatch, ordinary path, invalid sibling, and a symlink whose
resolved target identifies the environment:

```python
def test_path_environment_is_reported_only_for_one_installed_prefix(tmp_path: Path) -> None:
    policy, envs_root = _flexible_policy(tmp_path)
    target = envs_root / "llm-framework-study" / "lib" / "python3.12" / "site-packages"
    target.mkdir(parents=True)

    assert policy.environment_for_path(target.resolve()) == "llm-framework-study"
    assert policy.environment_for_path((tmp_path / "external").resolve()) is None
```

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```bash
conda run -n ms pytest tests/unit/test_workspace_identity.py -q
```

Expected: FAIL because `WorkspacePolicy.environment_for_path` does not exist.

- [ ] **Step 3: Implement bounded path classification**

Add a resolver method that uses the already-resolved path, requires a direct
child below the fixed environments root, validates the existing name grammar,
and reuses `resolve(name)` to prove the interpreter is installed:

```python
def environment_for_path(self, path: Path) -> str | None:
    try:
        relative = path.relative_to(self._envs_root)
    except ValueError:
        return None
    if not relative.parts:
        return None
    name = relative.parts[0]
    if _CONDA_ENVIRONMENT_NAME.fullmatch(name) is None:
        return None
    try:
        return self.resolve(name).name
    except WorkspaceError:
        return None
```

Expose it through `WorkspacePolicy` without scanning directories or reading
ambient state.

- [ ] **Step 4: Add failing activation-warning tests**

In `tests/unit/test_daemon_service.py`, exercise the production
`WorkspacePolicy` path rather than a generic resolver and assert:

```python
assert activation["workspace"]["python_environment"] == "ms"
assert activation["warnings"] == [
    {
        "code": "PYTHON_ENVIRONMENT_PATH_MISMATCH",
        "selected_environment": "ms",
        "path_environment": "llm-framework-study",
        "next_action": "reactivate_with_path_environment",
    }
]
```

Add matching-environment, ordinary-path, failed-switch, and post-warning
semantic-binding assertions.

- [ ] **Step 5: Run the service tests and verify RED**

Run:

```bash
conda run -n ms pytest tests/unit/test_daemon_service.py -q
```

Expected: the mismatch success lacks `warnings`.

- [ ] **Step 6: Carry warning presentation outside registry identity**

Define an internal frozen record in `daemon/service.py`:

```python
@dataclass(frozen=True, slots=True)
class _ResolvedActivation[IdentityT]:
    workspace: ResolvedWorkspace[IdentityT]
    warnings: tuple[Mapping[str, JsonValue], ...] = ()
```

Make `_resolve_workspace` produce this record only after explicit environment
and activation-path validation. Pass `.workspace` to `prepare_activation` and
append `warnings` only after commit succeeds. Do not place warnings in
`WorkspaceIdentity.registry_key`, `ResolvedWorkspace`, or `WorkspaceBinding`.

- [ ] **Step 7: Run focused unit and connector acceptance tests**

Run:

```bash
conda run -n ms pytest \
  tests/unit/test_workspace_identity.py \
  tests/unit/test_daemon_service.py \
  tests/acceptance/test_connector_contract_acceptance.py -q
```

Expected: PASS, including failed-switch preservation and warning-only success.

- [ ] **Step 8: Commit Task 1**

```bash
git add \
  src/serena_light/workspace/identity.py \
  src/serena_light/daemon/service.py \
  tests/unit/test_workspace_identity.py \
  tests/unit/test_daemon_service.py \
  tests/acceptance/test_connector_contract_acceptance.py
git diff --cached --check
git commit -m "Warn on workspace environment path mismatch"
```

### Task 2: Compact public runtime status

**Files:**
- Create: `src/serena_light/tools/runtime_status.py`
- Modify: `src/serena_light/daemon/server.py`
- Test: `tests/unit/test_runtime_status.py`
- Modify: `tests/unit/test_compact_non_navigation_contract.py`
- Modify: `tests/unit/test_daemon_semantic_api.py`
- Modify: `tests/acceptance/test_connector_contract_acceptance.py`

**Interfaces:**
- Consumes: raw service status with `lease`, `binding`, and `runtime`; daemon build/server/protocol facts.
- Produces: `compact_runtime_status(raw: Mapping[str, object], *, build_identity: str, server_version: str, protocol_version: str) -> dict[str, object]`.

- [ ] **Step 1: Write failing pure-presenter tests**

Create `tests/unit/test_runtime_status.py` with explicit raw fixtures for:

- unbound lease;
- healthy Python cold plus TypeScript ready;
- Python scope incompatibility plus healthy TypeScript;
- cooldown with one current crash reason;
- active/queued executor;
- prohibited-field absence.

The healthy assertion must enumerate the exact public shape and explicitly
reject raw fields:

```python
assert result["issues"] == []
assert result["languages"] == [
    {"language": "python", "state": "cold"},
    {"language": "typescript", "state": "ready"},
]
serialized = json.dumps(result)
for forbidden in ("lease_id", "transitions", "generations", "executable", "sha256"):
    assert forbidden not in serialized
```

- [ ] **Step 2: Run the new tests and verify RED**

```bash
conda run -n ms pytest tests/unit/test_runtime_status.py -q
```

Expected: import failure because the presenter is absent.

- [ ] **Step 3: Implement the pure status presenter**

Create one focused module with stable state and issue enums, bounded fixed-family
iteration, and allowlist construction rather than copying then deleting raw
fields. The public entrypoint must accept only mappings and explicit build
facts. Use small helpers such as:

```python
def _language_state(adapter: Mapping[str, object]) -> str:
    if _cooldown_remaining(adapter) > 0:
        return "cooldown"
    phase = adapter.get("phase")
    return {
        "stopped": "cold",
        "starting": "warming",
        "warming": "warming",
        "ready": "ready",
        "failed": "failed",
    }.get(phase, "unavailable")
```

Build every `issues` record from an allowlist. Reuse the already-bounded scope
sample, never copy transitions or crash history, and cap issues to the two fixed
families plus executor.

- [ ] **Step 4: Wire only the public MCP tool through the presenter**

Keep `WorkspaceDaemonService.get_runtime_status` and
`WorkspaceRuntime.status()` as internal rich sources. In
`daemon/server.py:get_runtime_status`, pass successful `data` and the existing
`DaemonHealth` fields to `compact_runtime_status`; do not alter lease control or
direct service tests.

- [ ] **Step 5: Update failing public contract tests**

Change HTTP/connector-facing tests to assert the compact DTO while preserving
internal service tests that intentionally inspect raw runtime state. Add a
blocked-runtime concurrency case proving status stays off the semantic FIFO.

- [ ] **Step 6: Run status and concurrency tests**

```bash
conda run -n ms pytest \
  tests/unit/test_runtime_status.py \
  tests/unit/test_compact_non_navigation_contract.py \
  tests/unit/test_daemon_semantic_api.py \
  tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_daemon_fault_acceptance.py -q
```

Expected: PASS; healthy public status contains no UUID/history/digest leak and
blocked semantic work does not delay status.

- [ ] **Step 7: Commit Task 2**

```bash
git add \
  src/serena_light/tools/runtime_status.py \
  src/serena_light/daemon/server.py \
  tests/unit/test_runtime_status.py \
  tests/unit/test_compact_non_navigation_contract.py \
  tests/unit/test_daemon_semantic_api.py \
  tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_daemon_fault_acceptance.py
git diff --cached --check
git commit -m "Compact healthy runtime status"
```

### Task 3: Coordinate guidance and capability recovery

**Files:**
- Modify: `src/serena_light/instructions.py`
- Modify: `src/serena_light/daemon/server.py`
- Modify: `src/serena_light/tools/presentation.py`
- Modify: `src/serena_light/tools/declarations.py`
- Modify: `tests/unit/test_daemon_server.py`
- Modify: `tests/unit/test_schema4_interaction_contract.py`
- Modify: `tests/unit/test_recovery_actions.py`
- Modify: `tests/unit/test_declarations.py`
- Modify: `tests/acceptance/test_stdio_connector_acceptance.py`

**Interfaces:**
- Consumes: existing byte-identical `AGENT_INSTRUCTIONS`, `RecoveryAction`, and capability matrices.
- Produces: bounded coordinate guidance and closed `find_referencing_symbols`, `reactivate_with_path_environment`, and `activate_workspace` recovery actions.

- [ ] **Step 1: Write failing instruction/schema assertions**

Update the expected fixed string and assert byte length at most 220, inner/outer
identity, `Ranges are 0-based`, and owning tool descriptions containing both
`Unicode code-point` and editor-line `+1` guidance. Retain assertions excluding
hooks and public instruction tools.

- [ ] **Step 2: Run the instruction tests and verify RED**

```bash
conda run -n ms pytest \
  tests/unit/test_daemon_server.py::test_agent_instructions_are_the_approved_bounded_source_contract \
  tests/unit/test_schema4_interaction_contract.py \
  tests/acceptance/test_stdio_connector_acceptance.py -q
```

Expected: current instructions omit the coordinate statement.

- [ ] **Step 3: Implement static coordinate guidance**

Use the approved fixed string, verifying its encoded length in the test:

```python
AGENT_INSTRUCTIONS = (
    "Experimental Python/JS/TS. Shell cd won't rebind; activate_workspace needs an absolute "
    "path; Conda defaults to ms. Use rg/find for text, Light for symbols/references/diagnostics. "
    "Ranges are 0-based. Report friction."
)
```

Add the full normalized coordinate and editor-line conversion only to owning
range-returning tool descriptions. Do not add a success field or alter external
raw `position_basis`.

- [ ] **Step 4: Write failing capability-recovery tests**

In `tests/unit/test_declarations.py`, assert the exact Pyright error details:

```python
assert details["reason"] == "implementation_provider_unavailable"
assert details["next_action"] == "find_referencing_symbols"
```

Retain the TypeScript real-provider dispatch assertion and add absence checks
for fallback fields in success. Extend recovery enum validation tests so unknown
free-form actions still fail.

- [ ] **Step 5: Implement closed capability recovery**

Extend `RecoveryAction` with the OpenSpec-owned values. In `_unsupported`, add
the reason/action only when `operation == "find_implementations"` and the raw
implementation provider is false. Never call the reference service from this
path.

- [ ] **Step 6: Run capability and public MCP tests**

```bash
conda run -n ms pytest \
  tests/unit/test_recovery_actions.py \
  tests/unit/test_error_presentation.py \
  tests/unit/test_declarations.py \
  tests/integration/test_pyright_adapter_real.py \
  tests/integration/test_typescript_adapter_real.py \
  tests/acceptance/test_stdio_connector_acceptance.py -q
```

Expected: Pyright is honestly unsupported with one action; TypeScript uses the
real provider and no recovery metadata.

- [ ] **Step 7: Commit Task 3**

```bash
git add \
  src/serena_light/instructions.py \
  src/serena_light/daemon/server.py \
  src/serena_light/tools/presentation.py \
  src/serena_light/tools/declarations.py \
  tests/unit/test_daemon_server.py \
  tests/unit/test_schema4_interaction_contract.py \
  tests/unit/test_recovery_actions.py \
  tests/unit/test_declarations.py \
  tests/acceptance/test_stdio_connector_acceptance.py
git diff --cached --check
git commit -m "Guide coordinate and implementation recovery"
```

### Task 4: Oversized exact-body recovery

**Files:**
- Modify: `src/serena_light/tools/navigation.py`
- Modify: `src/serena_light/tools/global_symbols.py`
- Modify: `src/serena_light/tools/compact_adapter.py`
- Modify: `src/serena_light/tools/compact.py`
- Modify: `tests/unit/test_document_navigation.py`
- Modify: `tests/unit/test_compact_adapter.py`
- Modify: `tests/unit/test_compact_renderer.py`
- Modify: `tests/integration/test_daemon_compact_navigation.py`

**Interfaces:**
- Consumes: verified `NormalizedSymbol.children`, ordered compact symbol records, final canonical MCP text budget.
- Produces: internal-only `has_children`; minimum-budget errors with one closed body recovery action.

- [ ] **Step 1: Write failing semantic-core privacy tests**

Add a nested class fixture and assert rich internal symbol data can carry
`has_children=True` while ordinary compact success never contains that key.
Also assert a leaf supplies false.

- [ ] **Step 2: Run semantic/compact tests and verify RED**

```bash
conda run -n ms pytest \
  tests/unit/test_document_navigation.py \
  tests/unit/test_compact_adapter.py -q
```

Expected: no internal child fact or recovery behavior exists.

- [ ] **Step 3: Carry verified child evidence internally**

Add `has_children` to rich `_symbol_data` in document and global verified-symbol
paths. Parse it separately in `compact_adapter`; never add it to
`CompactSymbolMatch` serialization. Apply body recovery only when the result is
one exact body-bearing symbol.

- [ ] **Step 4: Write failing final-budget recovery tests**

Cover these exact outputs:

```python
assert container_error["error"]["details"]["next_action"] == "overview_then_find_child_symbol"
assert retry_error["error"]["details"]["next_action"] == "retry_with_minimum_answer_chars"
assert huge_leaf_error["error"]["details"]["next_action"] == "find_symbol_location_then_exact_file_read"
```

For all three, assert `minimum_required_chars` is truthful, `body` is absent,
canonical text parses as JSON and equals `structuredContent`, and text respects
the caller's error budget.

- [ ] **Step 5: Implement recovery selection at final minimum rendering**

Add an internal recovery descriptor passed by `compact_navigation_result` to
`render_bounded_records`. Let the renderer choose the closed action only after
it computes the exact first-record minimum. Extend
`minimum_required_chars_result` with an optional validated action; do not alter
non-symbol or non-body callers.

- [ ] **Step 6: Run compact and daemon integration tests**

```bash
conda run -n ms pytest \
  tests/unit/test_document_navigation.py \
  tests/unit/test_compact_adapter.py \
  tests/unit/test_compact_renderer.py \
  tests/integration/test_daemon_compact_navigation.py -q
```

Expected: all recovery branches pass; successful body responses retain the
existing compact schema.

- [ ] **Step 7: Commit Task 4**

```bash
git add \
  src/serena_light/tools/navigation.py \
  src/serena_light/tools/global_symbols.py \
  src/serena_light/tools/compact_adapter.py \
  src/serena_light/tools/compact.py \
  tests/unit/test_document_navigation.py \
  tests/unit/test_compact_adapter.py \
  tests/unit/test_compact_renderer.py \
  tests/integration/test_daemon_compact_navigation.py
git diff --cached --check
git commit -m "Guide oversized symbol body recovery"
```

### Task 5: Schema 6, documentation, and full acceptance

**Files:**
- Modify: `src/serena_light/build_identity.py`
- Modify: `docs/compatibility.json`
- Modify: `README.md`
- Modify: `docs/client-registration.md`
- Modify: `docs/roadmap.md`
- Modify: `tests/unit/test_public_contract_version.py`
- Modify: `tests/unit/test_schema4_interaction_contract.py`
- Modify: `tests/acceptance/test_real_shared_daemon_acceptance.py`
- Modify: `tests/acceptance/test_real_versioned_rollover_acceptance.py`
- Modify: `openspec/changes/tighten-agent-recovery-guidance/tasks.md`
- Create after verification: `openspec/changes/tighten-agent-recovery-guidance/acceptance.md`

**Interfaces:**
- Consumes: completed Tasks 1-4 and the source-computed build identity.
- Produces: schema 6 release evidence, fresh client installation, completed OpenSpec task state.

- [ ] **Step 1: Write and run the failing schema-version test**

Update tests to require:

```python
assert PUBLIC_TOOL_SCHEMA_VERSION == "6"
assert compatibility["schema_version"] == 6
assert compute_build_identity(REPOSITORY_ROOT) != compute_build_identity(
    REPOSITORY_ROOT,
    public_tool_schema_version="5",
)
```

Run `conda run -n ms pytest tests/unit/test_public_contract_version.py -q` and
verify it fails against schema 5.

- [ ] **Step 2: Advance schema and update compatibility documentation**

Change `PUBLIC_TOOL_SCHEMA_VERSION` to `"6"`. Record the exact public status DTO,
warning, recovery values, coordinate guidance, unchanged tool count, non-goals,
and fresh-client requirement. Keep production LOC informational with no hard
maximum.

- [ ] **Step 3: Run focused regression suites**

```bash
conda run -n ms pytest \
  tests/unit/test_public_contract_version.py \
  tests/unit/test_schema4_interaction_contract.py \
  tests/integration/test_daemon_compact_navigation.py \
  tests/acceptance/test_connector_contract_acceptance.py \
  tests/acceptance/test_stdio_connector_acceptance.py -q
```

- [ ] **Step 4: Run complete source verification**

Use the repository's documented commands and record exact outputs:

```bash
conda run -n ms pytest -q
conda run -n ms ruff check src tests
conda run -n ms ty check
conda run -n ms python -m serena_light.bootstrap --check --json
openspec validate tighten-agent-recovery-guidance --strict
```

Also run the existing source-ownership/provenance and copied-hash verification
commands from `README.md`; do not substitute a smaller helper test.

- [ ] **Step 5: Run production-shaped real daemon acceptance**

Exercise the installed daemon/stdio suites in clean and poisoned-proxy
environments, including:

```bash
conda run -n ms pytest \
  tests/acceptance/test_real_shared_daemon_acceptance.py \
  tests/acceptance/test_real_versioned_rollover_acceptance.py \
  tests/acceptance/test_stdio_connector_acceptance.py -q
```

Add live assertions for `/data/CoordExp`, `/data/CoordExp/ms-swift`, and the
`llm-framework-study` site-packages root, plus Pyright unsupported recovery and
TypeScript implementation success. Verify zero-holder retirement and no new
orphan process.

- [ ] **Step 6: Record acceptance and mark OpenSpec tasks truthfully**

Create `acceptance.md` containing commands, counts, build identity, environment,
proxy conditions, client freshness, external snapshot skips, and blocker
dispositions. Check a task only after its evidence exists. Re-run:

```bash
openspec status --change tighten-agent-recovery-guidance
openspec validate tighten-agent-recovery-guidance --strict
```

- [ ] **Step 7: Commit the accepted source change**

Stage only the files listed by `git status --short`, inspect the full staged
diff and whitespace, then commit:

```bash
git diff --check
git commit -m "Tighten Serena Light agent recovery"
```

Do not push, install into shared clients, merge, or archive until the lead has
reviewed the acceptance evidence and the user-owned publication step remains
authorized.
