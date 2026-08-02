# Tighten Query Recovery Acceptance

## Implementation baseline

Recorded on 2026-08-02 before any runtime or test implementation edits.

| Evidence | Baseline |
| --- | --- |
| Git branch | `main` |
| Git commit | `2f5c302ab884fb9ef0aca2012515e44c146e0786` |
| Dirty ownership | Only the untracked `openspec/changes/tighten-query-recovery/` planning change; no pre-existing tracked edits |
| Public tool schema | `4` |
| Source build identity | `a92919af0add2dc6dbc40aa2f5cefd5dfa8db808ab58cb794d82ee615a571374` |
| Instruction characters / UTF-8 bytes | `562 / 562` |
| Instruction SHA-256 | `166a446a317c1d4ff86e792fba8e12026fe61abb629ba89299220bdb46aad5cc` |
| Live public tools | `11` |
| Aggregate live tool-description characters | `12,646` |
| Live common-prefix characters | `564` (`562` instruction characters plus two newlines) |
| Repeated common-prefix characters after the first copy | `5,640` |

The paired Luna benchmark reached equal final semantic facts. Official Serena
used 42 MCP calls and 15 shell calls; Serena Light used 43 MCP calls and 11
shell calls. The Serena Light trace contained four guessed-name misses, three
stale-root misses, one expected ambiguity, and one caller-orchestration
truncation. These counts are observational rather than release thresholds.

### Accepted baseline failure payloads

File-scoped guessed symbol:

```json
{"ok":false,"error":{"code":"SYMBOL_NOT_FOUND","message":"symbol was not found","details":{"relative_path":"src/serena_light/tools/diagnostics_adapter.py","name_path":"_present_diagnostic"}},"workspace":"/data/CoordExp/serena-light"}
```

Path queried while the lease remained bound to the prior workspace:

```json
{"ok":false,"error":{"code":"INVALID_PATH","message":"path is invalid","details":{"path":"/data/CoordExp/serena-light/scripts/research/collect_vllm_trajectory_panel.py"}}}
```

These payloads are correction baselines, not desired final fixtures. The first
must gain the file-overview action. The second must gain the active workspace
and conditional activation action without discovering or activating a root.

## Implementation receipts

### Contract implementation

- `AGENT_INSTRUCTIONS` is the approved 214-character/byte string with SHA-256
  `5ab9fc6c4350a99bf80398293fe968493e7ce84061c04ab28c8ed845175ff1d4`.
  Direct FastMCP initialization and the outer stdio connector both assert the
  same source-owned bytes. Public schema remains `4` and the live tool count
  remains `11`.
- Existing tool descriptions now own startup-cwd binding, absolute workspace
  switching, unfamiliar-file depth-0 overview, qualified ambiguity retry,
  opt-in reference snippets, explicit post-edit diagnostics, and debug-only
  runtime status. No hook, instructions tool, diagnostics injection, lexical
  tool, automatic activation, or input/success-shape change was introduced.
- File-scoped Python and TypeScript symbol misses carry only
  `next_action=get_symbols_overview`; directory and global misses do not.
  Bound semantic/diagnostic `INVALID_PATH` carries the active workspace and
  `next_action=activate_workspace_if_other_root` after exactly one runtime
  dispatch. Binding and activation counts remain unchanged, and editing and
  generic presentation paths remain action-free.
- `RecoveryAction` is a closed presentation-bound enum. Unknown values fail
  presentation, ambiguity remains candidate-owned and action-free, and a
  512-character deterministic result retains workspace/action before long
  correction echoes. The real FastMCP receipt keeps canonical text equal to
  `structuredContent` with no transport `isError` promotion.

The new metadata assertions distinguish the 562-byte baseline instruction and
old tool descriptions; the new recovery assertions distinguish both recorded
baseline payloads. The worker's final prose stream disconnected only after its
edits and `44 passed` receipt were delivered. That is a provider-surface event,
not implementation or model-quality evidence; the lead re-ran the integrated
gate independently.

### Focused lead gate

Command:

```text
uv run pytest -q tests/unit/test_schema4_interaction_contract.py tests/unit/test_daemon_server.py tests/unit/test_error_presentation.py tests/unit/test_document_navigation.py tests/unit/test_recovery_actions.py tests/unit/test_daemon_service.py tests/integration/test_daemon_compact_navigation.py tests/acceptance/test_connector_contract_acceptance.py tests/acceptance/test_stdio_connector_acceptance.py
```

The first integrated run produced `80 passed, 1 failed`: the pre-existing
schema-4 test still required the retired long-instruction wording. The owning
test was updated to the approved 220-byte global contract, direct daemon
initialization identity was added, and ambiguity was made explicitly
action-free. The fixed-point rerun produced `81 passed`.

Targeted Ruff over all implementation and owning test files passed, and
`git diff --check` passed.

### Post-implementation metadata census

| Evidence | Candidate |
| --- | --- |
| Source build identity | `78de9eaeac5c3b8522e568e364ee124aa6dca4b8bb0874eb1b63f51408c6d92c` |
| Instruction characters / UTF-8 bytes | `214 / 214` |
| Instruction SHA-256 | `5ab9fc6c4350a99bf80398293fe968493e7ce84061c04ab28c8ed845175ff1d4` |
| Live public tools | `11` |
| Aggregate live tool-description characters | `8,891` |
| Live common-prefix characters | `216` (`214` instruction characters plus two newlines) |
| Repeated common-prefix characters after the first copy | `2,160` |

The live description surface fell by 3,755 characters overall and by 3,480
repeated-prefix characters after the first copy. These are environment receipts,
not compatibility constants or semantic gates.

### Real two-workspace query recovery

The live Serena Light client selected pre-final build
`53bc00a1b68207cf86e0fc752b52c03fb05c33e69ef17efb4aec17a196c171ef`
and was explicitly bound to `/data/CoordExp/serena-light`. A later Ty-only
source adjustment changed the final source identity to `78de9eae...`; this
receipt therefore proves the behavior seam but is not relabelled as final-build
fresh-client evidence. The required fresh clients below must repeat the matrix
on `78de9eae...`.

1. A depth-0 overview of `src/serena_light/daemon/service.py` exposed
   `WorkspaceDaemonService`; an exact qualified lookup returned
   `WorkspaceDaemonService/_enrich_bound_query_error`.
2. An intentional `_no_such_symbol` file-scoped lookup returned typed
   `SYMBOL_NOT_FOUND`, the same file/name echo, active workspace, and exactly
   `next_action=get_symbols_overview`.
3. An intentional file-local `operation` ambiguity returned nine bounded
   qualified candidates and no `next_action`; retrying
   `RuntimeAdapter/submit_read/operation` succeeded.
4. A host shell command executed with cwd
   `/data/CoordExp/.worktrees/research-probes`; the MCP binding remained
   `/data/CoordExp/serena-light`. Querying
   `scripts/research/collect_vllm_trajectory_panel.py` then returned typed
   `INVALID_PATH`, active workspace `/data/CoordExp/serena-light`, and exactly
   `next_action=activate_workspace_if_other_root` without changing the lease.
5. Explicit absolute activation to `/data/CoordExp/.worktrees/research-probes`
   made a depth-0 overview and exact `collect_panel` lookup succeed. Explicit
   activation back to `/data/CoordExp/serena-light` reused the same lease.

No automatic lookup, root discovery, activation, retry, or batch call occurred.

### Complete mechanical and rollover gates

The first full-suite run was intentionally retained as evidence: it produced
`879 passed, 1 failed, 35 skipped`; the only failure was the stale long
instruction in `docs/compatibility.json`. README, compatibility,
client-registration, and roadmap owners were updated together. The fixed-point
command `uv run pytest -q tests` then completed with `880 passed, 35 skipped`,
no failures, and no warnings in approximately 183.6 seconds.

The fixed-point non-pytest gates all passed:

- `uv run ruff check src tests scripts`;
- `.venv/bin/ty check`;
- `uv run serena-light-bootstrap --check --json`, using dependency digest
  `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`
  and service-owned CPython 3.12.12;
- `uv run serena-light-source-budget --json`, with status `pass`, no LOC
  ceiling, no forbidden/undeclared imports, census/manifest agreement, and all
  nine copied-source hashes verified;
- the four focused dependency/source/provenance files (`22 passed`);
- `openspec validate --all --strict` (`7 passed, 0 failed`); and
- `git diff --check`.

`uv run pytest -q tests/unit/test_runtime_files.py
tests/acceptance/test_versioned_rollover_acceptance.py
tests/acceptance/test_real_versioned_rollover_acceptance.py` produced
`12 passed`. It proves old leased and new build slots coexist, the new source
identity does not disturb old holders, and retirement occurs only after holder
and grace conditions. On the live host, the accepted pre-change
`a92919af...` slot retained only its directory/log/start-lock after ordinary
zero-holder retirement, while predecessor candidate `53bc00a1...` published
its own daemon discovery and bearer. The final source identity after the Ty
repair is `78de9eae...` and is verified only by fresh clients below. No process
was killed or cleaned by name.

The final scoped census before client receipts contains the approved four docs,
five production modules, seven existing owning tests, one new recovery unit
test, and the OpenSpec change. The live public surface remains 11 tools with
schema 4; no hook, batch RPC, lexical tool, diagnostic adapter, or client config
was added. Caller-orchestration truncation remains separately labelled and did
not motivate a server retry or batching protocol.

## Fresh-client and observational smoke receipts

### Fresh Codex / Luna-medium receipt

A fresh Serena-Light-only Luna/medium client selected final source build
`78de9eaeac5c3b8522e568e364ee124aa6dca4b8bb0874eb1b63f51408c6d92c`,
schema 4, and the 11-tool public surface. It observed the compact initialize
instruction, explicitly activated `/data/CoordExp/serena-light`, and completed
the depth-0 overview, exact qualified lookup, intentional file miss, qualified
ambiguity retry, wrong-root query, explicit research-probes activation,
`collect_panel` lookup, return activation, and own-lease release.

The observational count was 15 MCP calls and one read-only shell call, with
zero accidental guessed-name misses, two intentional failure probes, one
stale-root miss, one expected ambiguity, and one caller/orchestration display
truncation. Both closed recovery actions were used correctly. There was no MCP
or server fault, wrong hint, automatic semantic retry, automatic workspace
rebind, or semantic regression. The display truncation occurred outside the
canonical tool result and did not cause a server retry or justify a batch RPC.

### Fresh Claude Code / CC Agent receipts

A fresh CC Agent on the same final build first ran with `write=false`. Its
connector intentionally projected the read-only 10-tool surface by withholding
`replace_symbol_body`; all query, activation, recovery, and release checks
passed. This is permission projection rather than a missing daemon tool.

A second fresh CC Agent ran with task-scoped `write=true` solely to observe the
complete surface and was explicitly forbidden to mutate files or configuration.
It reported schema 4 and all 11 tools, including `replace_symbol_body`, without
calling the edit tool. It repeated the Serena Light and research-probes
workflow on build `78de9eae...`, observed the file-scoped overview action,
performed only explicit workspace activations, returned to Serena Light, and
released its own lease with zero active holders. It reported no wrong hint,
fault, automatic retry/rebind, answer truncation, filesystem mutation, or
client/configuration change.

Both fresh-client families resolved the same source build and left canonical
`serena`, client registration, and external repositories untouched. Localhost
daemon traffic stayed on the existing explicit no-proxy boundary; these smokes
required no external-network bootstrap or proxy change.

## Final disposition

**PASS.** Deterministic recovery, final-build fresh clients, full regression,
static/provenance/bootstrap checks, rollover isolation, and strict OpenSpec all
pass. The new guidance reduces repeated metadata while retaining the complete
workflow; the two actions are bounded, error-only advice and never execute on
the Agent's behalf. The observational Luna smoke found no semantic regression,
wrong or non-deterministic hint, or automatic rebind/retry, so the declared stop
rule does not fire and no further ablation or sampled-model tuning is authorized
under this change.

The public schema remains 4, the daemon surface remains 11 tools, editing and
success envelopes are unchanged, and canonical Serena remains registered and
configured independently. The two delta specs were synchronized before the
completed change was archived.
