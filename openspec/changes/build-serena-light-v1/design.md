## Context

Serena currently couples one agent to one mutable active project. Activating a
new project stops the previous project's language servers, while the non-stdio
server retains a singleton agent. That shape conflicts with the desired use
case: multiple Codex, Claude Code, and CC Agent sessions must share expensive
language runtimes without sharing mutable workspace selection.

The implementation reference is `/data/CoordExp/external/serena` at commit
`9a9d07e83d8c` (MIT). It remains a permanent reference checkout, not an
upstream. This repository will copy and reshape only mechanisms required for
LSP process management, symbol normalization, diagnostics, file freshness, and
process cleanup. Serena's agent, modes, memories, UI, project server, and broad
language registry are not part of the dependency closure.

The admission probe established the following constraints:

- The unpruned candidate closure is about 19.4k lines. Replacing generated LSP
  types with a pinned protocol library and deleting unused paths yielded a
  compact owned implementation. Production LOC remains reported for review but
  is not a fixed numeric stop condition; ownership, dependency, provenance, and
  excluded-subsystem checks remain hard gates.
- Five clean Pyright starts on the 2,219-file transformers package all found
  `Qwen2VLForConditionalGeneration`. `publishDiagnostics` arrived in roughly
  12.4–13.4 seconds, but the first `workspace/symbol` request required another
  11.3–12.1 seconds. A repeated query completed in about 5.1 seconds. Published
  diagnostics alone therefore cannot represent global-index readiness.
- With Git-derived exclusions, `/data/CoordExp` reached diagnostics readiness in
  7.6 seconds, used about 1.28 GB peak RSS, and returned `PipelinePlanner` in
  4.2 seconds.
- Responding to Pyright's `workspace/configuration` request with the conda `ms`
  interpreter allowed a symbol in `/data/ms-swift` to resolve into the exact
  conda transformers package.
- TypeScript navigation works on `cc-plugin-codex/runtime/*.mjs`, but the pinned
  TypeScript 5.9 LSP reported three errors in `runtime/args.mjs` while the
  repository's TypeScript 7 check passed. LSP diagnostics cannot be presented as
  repository-authoritative.
- Serena's Linux parent-death tests passed 2/2 in an isolated locked
  environment.
- Twenty clean readiness runs across transformers, `/data/CoordExp`,
  `/data/ms-swift`, and `cc-plugin-codex` all returned the expected symbol in
  under 30 seconds and cleaned up their owned processes.
- The scope probe disproved Git/program equivalence as a general contract:
  `cc-plugin-codex` has 57 Git-visible JS/TS files but its native tsconfig
  intentionally selects 22; `/data/CoordExp` has 1,116 Git-visible Python files
  while its native Pyright configuration selects a smaller program; and the
  ignored-subtree fixture demonstrated that a native tsconfig may also add an
  ignored generated source. The transformers count requires file-level
  attribution before any equivalence claim.

## Goals / Non-Goals

**Goals:**

- Provide one localhost daemon that safely shares language runtimes across
  sessions and roots.
- Preserve connection-local workspace selection and make cross-root switching
  explicit and deterministic.
- Provide the selected semantic tools for Python and JS/TS with typed JSON
  results and honest capability/readiness states.
- Support exact transformers navigation from `ms-swift` while keeping
  site-packages read-only.
- Make symbol-body editing conflict-safe, atomic, authorized, and non-replayed.
- Bound resource lifetime, clean up child processes, and isolate repeated
  adapter failures to one workspace.
- Keep copied code and provenance small enough to audit and own locally.
- Keep file authorization, native semantic-program membership, and path-scoped
  document availability distinct and observable.

**Non-Goals:**

- Public packaging or configuration compatibility with arbitrary Serena users.
- Cross-language reference edges between Python and JS/TS. Global symbol search
  may aggregate adapters, but each result remains language-owned.
- Serena UI, JetBrains integration, memories, telemetry, completion, rename,
  formatting, code actions, hierarchy tools, or line editing.
- Byte-compatible Serena response strings or an automated upstream sync path.
- Treating TypeScript LSP diagnostics as equivalent to a repository's native
  TypeScript compiler or CI configuration.

## Decisions

### 1. Owned module boundaries

The package will use a small set of one-way ownership layers:

```text
connector
  -> transport client and daemon bootstrap

daemon
  -> MCP tool registration
  -> connection/lease registry
  -> workspace runtime registry

workspace
  -> root identity and trust policy
  -> Git-aware file set and freshness
  -> per-root serialization and adapter ownership

adapters
  -> shared LSP process/protocol core
  -> pyright adapter
  -> typescript adapter
  -> runtime capability matrix and readiness gate

tools
  -> semantic navigation
  -> diagnostics/status
  -> guarded symbol editing
```

The LSP wire model will depend on a pinned `lsprotocol` release rather than copy
Serena's approximately 5.9k generated `lsp_types.py`. Selected SolidLSP process,
symbol, diagnostics, and process-tree mechanisms may be copied and simplified.
Every copied file MUST identify the Serena commit and source path in
`THIRD_PARTY_NOTICES.md` and a machine-readable provenance manifest. Copied code
is thereafter maintained here; there is no merge or rebase workflow.

Python and Node dependencies are fixed by repository-owned lock files. A
bootstrap command materializes both environments below
`/data/CoordExp/.codex/runtime/serena-light/deps/<lock-digest>` and every daemon
status response reports the exact resolved executable paths and versions. The
observed baseline to freeze or deliberately supersede during the dependency
gate is MCP SDK 1.27.1, Pyright 1.1.403,
`typescript-language-server` 5.1.3, and TypeScript 5.9.3. The conda `ms`
interpreter is used for analyzed-program import resolution; it is not the
service dependency environment. No executable may resolve through global
`/root/.nvm`, user-level pip state, or an ambient `PATH` fallback.

Alternative rejected: importing Serena as a runtime dependency or placing a
thin wrapper around its project server. The shared mutable active-project
pointer and all-or-nothing language startup would remain correctness hazards.

### 2. Per-session stdio connector over a shared HTTP daemon

Codex, Claude Code, and CC Agent configurations continue to launch a command.
That command is a small stdio MCP connector. It inherits the client's startup
cwd, obtains a daemon-issued lease, binds the normalized workspace, and proxies
MCP messages to a Streamable HTTP daemon bound only to `127.0.0.1`.

The connector uses a process lock and health handshake to connect to or start
the daemon. The runtime directory is created without following symlinks and with
mode `0700`; discovery metadata and a randomly generated bearer secret are
atomically written with mode `0600` below
`/data/CoordExp/.codex/runtime/serena-light`. No service state depends on
`/root`. The MCP HTTP session identifier is transport correlation only; the
daemon-issued random lease UUID is the sole binding and resource-lifetime
authority.

The daemon is detached from the connector that wins startup and survives that
connector's normal exit. Heartbeats run as an event-loop task independent of
workspace locks and LSP executors. If daemon identity or the HTTP session is
lost, the connector may initialize a new daemon session, obtain a new lease, and
restore its last validated binding. It may retry an interrupted read-only
request at most once after rebinding. An in-flight edit is never resent at the
HTTP, MCP, connector, or daemon layer; the connector returns `UNCERTAIN` and
requires a hash/symbol reread.

Alternative rejected: `systemd --user` as the required lifecycle owner. It is
not reliably available in the target environment. The connector provides the
same connect-or-start behavior without making systemd a dependency.

### 3. Workspace identity is separate from connection binding

`activate_workspace` accepts an absolute path. For a path inside a Git
repository, the workspace identity is the resolved Git top-level. Activating
another directory inside the same Git root only updates the session's working
subdirectory metadata. A cross-root switch prepares a provisional registry
lease without publishing it, completes mandatory freshness on the candidate
runtime, then atomically commits only if the prior binding is still current.
Commit releases the old registry lease; abort releases only the provisional
candidate and immediately retires a runtime only when that prepare attempt
created it. A borrowed zero-holder runtime remains under its existing
warm-grace ownership. Same-root reactivation refreshes before changing only the
working-subdirectory metadata, retaining the exact registry lease. Any
validation, startup, acquisition, or refresh failure preserves the old binding.
For explicitly trusted non-Git roots, the exact normalized path is the identity.

The daemon registry maps workspace identities to `WorkspaceRuntime` objects.
There is no global active-workspace field. Multiple sessions on one root hold
independent leases but reuse the same adapters and caches.

The daemon event loop owns HTTP, lease, heartbeat, status, cancellation, and
readiness-condition work; it never runs the synchronous SolidLSP request stack.
Each workspace owns a bounded single-worker LSP executor and bounded queue, so
actual LSP dispatch and state mutation are ordered within that workspace while
different roots proceed concurrently. Readiness and diagnostics waits occur
outside the workspace operation lock. Status and lease work bypass the LSP
queue. Queue saturation returns typed `BUSY`. Cancellation removes work that has
not started; a started synchronous request is allowed to reach its bounded
terminal state, its result is discarded, and it cannot retain the workspace
lock after cancellation.

The connector automatically activates its startup cwd. A later shell `cd`
cannot be observed through MCP transport. Crossing a Git root therefore
requires an explicit `activate_workspace(new_absolute_path)` call.

### 4. Trust is fixed and path-based

Path operands for document and workspace tools must resolve inside the active
workspace inventory. A trusted path outside the active inventory returns
`OUT_OF_WORKSPACE` with the current identity and an absolute
`activate_workspace` hint; trust alone never makes it part of the binding.

Semantic edges such as definitions may return locations outside the active
workspace when they resolve beneath `/data` or beneath the standard-library,
purelib, or platlib roots reported by the pinned conda `ms` interpreter. Such
locations are marked `read_only_external` and may not be used as edit targets.
An external location outside those roots fails with `UNTRUSTED_ROOT` rather
than being silently dropped.

V1 non-Git activation remains narrowly allowlisted to the exact resolved conda
transformers package directory. V1 edit roots are resolved Git workspaces
beneath `/data`; all conda environment and site-packages paths are read-only.
Authorization compares resolved paths, rejects symlink escapes, and runs before
a language server or filesystem mutation.

These are internal constants, not a public TOML/package override surface.

### 5. Trust inventory and native semantic program are separate

For Git workspaces, the source inventory begins with:

```text
git ls-files --cached --others --exclude-standard
```

The inventory retains only existing regular files; tracked-but-deleted paths and
symlink escapes are removed before extension routing.

The Git inventory is the trust and authorization boundary and a bounded
discovery upper bound; it is not a claim that every allowed source belongs to a
repository's configured semantic program. For each language, the runtime also
records the native configured program selected by the nearest applicable
`tsconfig.json`, `jsconfig.json`, or `pyrightconfig.json`, including the chosen
config path and a file-level projection when the engine can provide one.

The runtime MUST preserve native configuration semantics, including `extends`,
project references, path aliases, include/exclude rules, and Pyright execution
environments. It MUST NOT synthesize an overlay merely to make the configured
program equal the Git inventory. Git-visible sources omitted by native config
are reported as `trusted_not_in_configured_program`: they remain authorized for
explicit path-scoped document operations when the adapter can serve them
through an engine-owned inferred or transient project, but they are not covered
by configured-program global readiness or global search.

The opposite difference is a trust failure. If the native configured program
contains an ignored/generated supported-language file outside the Git
inventory, the adapter returns `SCOPE_INCOMPATIBLE` before global readiness and
does not index or serve that configured program. Tracked-but-deleted paths and
symlink escapes are removed before either comparison. Status reports the Git
inventory count/digest, configured program count/digest or bounded projection
evidence, selected config path, both difference sets with reasons, and any
incompatible extras. Non-Git trusted roots use a bounded no-symlink discovery
inventory; the language server's resulting program is recorded as its semantic
projection and requires file-level attribution before equivalence is claimed.

For pinned Pyright 1.1.403, file attribution uses a repository-owned Node probe
that runs Pyright's native CLI option/configuration path, captures
`AnalyzerService.getOwnedFiles()` immediately after `setOptions`, and reads the
selected config from the resulting native `ConfigOptions`. It does not wait for
type analysis, parse `pyrightconfig.json` itself, or generate an overlay. This
is an intentionally version-private adapter seam: the Pyright version,
`pyright.js` and `pyright-internal.js` hashes, webpack module shape, and required
methods are all checked before use, and any drift fails closed. The ordinary
LSP source count must still agree with this attributed path set before global
readiness. TypeScript attribution remains through tsserver `projectInfo`.

Git untracked files are included. Create/change/delete freshness increments the
trust-inventory generation and updates the applicable configured-program
projection before notifying an adapter. A change invalidates global readiness
only when it changes or may change that adapter's configured semantic program;
a trusted file outside the configured program invalidates only its path-scoped
document generation. The adapter cannot claim configured-program global
readiness for a new program generation until a bounded verification barrier
observes the changed state; until then global queries return `NOT_READY`, never
stale empty success. Read-only non-Git roots do not run a full per-call
freshness scan; targeted paths use stat checks and the language server's
watchers.

Alternatives rejected: hard-coded per-repository workspace folder lists, and
generated overlay configs that force Git/program equivalence. The former embeds
current repository layout in product code; the latter silently changes native
project semantics.

### 6. Adapters are fixed, lazy, and capability-driven

The daemon registers two adapters at startup but starts neither language server
until a request needs it:

- Python extensions start a pinned Pyright server. Pyright's
  `workspace/configuration` request receives
  `python.pythonPath=/root/miniconda3/envs/ms/bin/python`; this value is not put
  in `initializationOptions`.
- `.js`, `.jsx`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.mts`, and `.cts` start a
  server-owned pinned `typescript-language-server` with TypeScript 5.9. V1 does
  not select the workspace's TypeScript 7 package because it has no
  `tsserver.js` compatible with this adapter.

Each adapter records the raw capabilities advertised by its initialize
response. The observed v1 provider matrix is:

| Raw LSP provider | Pyright | TypeScript |
|---|---:|---:|
| definition | yes | yes |
| declaration | yes | no |
| implementation | no | yes |
| references, document symbols | yes | yes |
| workspace symbols | yes | yes |
| published diagnostics | yes | yes |

Serena-compatible tool names bind to protocol operations independently of raw
provider names:

| Tool | Protocol operation | Pyright | TypeScript |
|---|---|---:|---:|
| `find_declaration` | `textDocument/definition` | yes | yes |
| `find_implementations` | `textDocument/implementation` | no | yes |
| `find_referencing_symbols` | `textDocument/references` | yes | yes |

`find_declaration` deliberately retains upstream Serena's
declaration/definition meaning and does not dispatch
`textDocument/declaration`. A missing required provider returns typed
`UNSUPPORTED` with both raw and derived capability matrices. A supported
single-target request with no semantic location returns `SYMBOL_NOT_FOUND`, not
`UNSUPPORTED` or an ambiguous empty success.

The v1 occurrence locator also retains Serena's agent-tested shape:
`find_declaration(relative_path, regex, containing_symbol_name_path=null,
include_body=false, include_info=false)`. `regex` uses Python MULTILINE/DOTALL
semantics and MUST contain exactly one capture group. The capture start is the
definition-request position; zero matches return `SYMBOL_NOT_FOUND`, multiple
matches return `AMBIGUOUS_SYMBOL`, and an invalid capture contract returns
`INVALID_INPUT`. When a containing name path is supplied, matching is restricted
to that one resolved body. `find_implementations` accepts one `name_path` and one
source-file `relative_path`, with bounded optional info/kind filters. Neither
tool exposes raw LSP line/character offsets to agents.

Adapter startup and failure are independent, so a missing or crashing TS server
cannot tear down a healthy Pyright runtime.

### 7. Readiness has explicit phases and a configured-program sentinel

An adapter moves through `cold`, `starting`, `document_ready`, `global_warming`,
`ready`, `degraded`, `cooldown`, and `stopping`. Each phase is paired with trust
inventory, configured-program, document, and index generations.
`publishDiagnostics` after a controlled `didOpen` establishes document readiness
only. Before reporting initial global readiness for the native configured
program, the adapter completes a bounded sentinel `workspace/symbol` request.
After a configured-program change it completes the generation verification
barrier described above. The sentinel query and barrier must be validated
during admission; an empty initial sentinel result is acceptable only when the
request completes successfully and the fixture proves it distinguishes warming
from ready.

Connector auto-activation starts warming in the background. A global query may
wait outside the workspace lock for readiness for up to 30 seconds. If the
adapter does not reach the current configured-program generation, the tool
returns typed `NOT_READY` with phase, trust/program/index generations, elapsed
time, and a retry hint. It MUST NOT return an empty symbol list. Path-scoped
document operations may proceed once the relevant document generation is ready,
including for a trusted file served by an engine-owned inferred or transient
project, even if the configured global program is still warming.

The transformers probe implies an expected first-start global-ready time near
24 seconds and a repeated query near 5 seconds. The ten-minute warm grace is
therefore material to the intended multi-session experience.

### 8. Global symbol lookup uses candidates, not an O(files) tree walk

`find_symbol` with a global scope sends the last name-path segment to
`workspace/symbol`, applies exact-name filtering by default, and requests
`documentSymbol` only for the returned candidate files. It then rebuilds and
verifies full Serena-style name paths. `substring_matching=true` permits
substring candidates. All result sets are bounded and include truncation
metadata.

Path- or directory-scoped lookup may use document symbols for the explicitly
selected files. The implementation MUST NOT fall back to Serena's full
workspace `documentSymbol` walk. If an adapter's workspace-symbol recall fails
acceptance, global lookup for that adapter is removed from v1 rather than
silently using the O(files) path.

Global multi-language search fans out only to adapters required by the native
configured programs, then merges typed results. `workspace/symbol` covers only
the configured program reported in status; it does not search trusted files
omitted by that config, site-packages, standard-library, typeshed, or another
workspace. Cross-library navigation uses
`find_declaration` from a source occurrence. References, definitions, and
implementations stay within their owning adapter; no cross-language semantic
edge is inferred.

### 9. Diagnostics are push-based and authority-labelled

Neither v1 server advertises pull diagnostics. The implementation keeps only
published-diagnostics generations. A diagnostics request waits for the target
document generation up to a bounded timeout and returns one of `clean`,
`findings`, `not_ready`, or `timed_out`. A timeout is never represented as a
clean result.

Default output includes LSP severities Error and Warning. File diagnostics are
grouped by containing symbol when possible and otherwise under `<file>`;
symbol diagnostics filter the same generation to a selected symbol range.

All symbol, diagnostic, snippet, and edit ranges use one shared position mapper.
Initialization advertises the supported position encodings and records the
server-selected value; absence means the LSP default UTF-16. Conversion from LSP
code units to decoded-text and byte offsets uses the exact current file snapshot
and preserves BOM, CRLF/LF, and source encoding metadata.

TypeScript results include `authority: advisory`, engine path/version, project
scope, and a note that repository-native CI/typecheck is authoritative. Python
results include the Pyright version and selected interpreter.

### 10. Symbol editing is hash-guarded and atomic

`replace_symbol_body(name_path, relative_path, body, expected_hash)` requires one
unambiguous symbol previously retrievable with its body and a whole-file SHA-256
`expected_hash`. Under the workspace lock, the tool re-resolves the symbol,
re-reads the file, authorizes the resolved path, converts its LSP range through
the shared position mapper, and rejects a stale hash before writing. The hash
protects against an external change; the position mapper separately protects
against an internally miscomputed range.

The new content is written to a temporary file in the destination directory,
with the original mode and newline/encoding contract preserved, flushed, and
atomically installed with `os.replace`. The adapter is then notified of the
change. Diagnostics are not automatically run.

Editing calls are never replayed. If the file was replaced but the adapter
notification or transport response failed, the result is `UNCERTAIN` when a
response remains possible; after a broken connection, the caller re-reads the
file/hash. Connector recovery, HTTP retry middleware, and MCP session recovery
MUST NOT resend an edit. Repeating the old expected hash must fail, preventing a
duplicate edit.

Alternative rejected: Serena's direct `open(path, "w")` replacement and
best-effort retry. A process failure can otherwise leave a partial file or
repeat an edit.

### 11. Leases, crash recovery, and logs are bounded

The connector sends a heartbeat every 15 seconds. A lease expires after 60
seconds without renewal. The last released or expired lease starts a ten-minute
warm grace; after it elapses the runtime stops its language servers and leaves
only bounded metadata. `release_workspace(immediate=true)` bypasses the grace
only when the released binding is the last holder; otherwise it detaches that
binding without stopping the shared runtime. In both cases the daemon lease
remains active and unbound so the connector may activate another root.

A read-only request interrupted by a language-server crash may restart that
adapter and retry once. Editing calls never retry. Repeated crashes within one
workspace open a per-adapter circuit breaker and return `COOLDOWN`; other roots
and adapters continue.

Language servers run in owned process groups with Linux parent-death protection
and terminate/kill fallback. The copied launcher MUST preserve the Linux
thread-scoped parent-death invariants: every protected `Popen` is submitted to
one daemon-lifetime spawner thread, uses `start_new_session=True`, and prefixes
the shell command with `exec` so the registration reaches the actual language
server. The ordinary adapter executor is not the parent-death spawner. Tests
must prove both that a normally started server survives its caller's return and
that daemon SIGKILL leaves no descendants. The connector does not kill a shared
daemon when one client exits.

Logging is limited to concise stderr messages and bounded rotating debug files
under the shared runtime root. V1 has no dashboard, call audit, telemetry, or
memory log.

### 13. Repair containment and proxy ownership

Until reacceptance, new MCP negotiations omit `replace_symbol_body`. A stale
client that invokes the old declaration receives `UNSUPPORTED` with reason
`temporarily_disabled_pending_reacceptance`; the implementation remains covered
by tests and is not deleted.

After gates 15.1-15.6 pass, task 15.7 restores the guarded edit before the
three fresh-client hash-edit receipts and the independent dual audit. An audit
HOLD after that restoration blocks release and archive but does not by itself
re-enter containment or invalidate the already accepted edit fault matrix.

Ambient proxy variables are owned only by external-network callers. Bootstrap
may inherit them for Python/Node/npm downloads. Connector-to-loopback HTTP,
daemon health probes, and local acceptance/fault HTTP use clients configured not
to consult environment proxies. Daemon and LSP child environments remove every
case variant of `*_PROXY`; global proxy and `NO_PROXY` configuration are not
mutated. Every `DaemonProcess.start` failure path closes the child or driver
created by that attempt.

### 14. Synchronous freshness and lexical edit authorization

A workspace-owned `FreshnessCoordinator` runs before each semantic or edit
operation and when the same root is activated again. Concurrent callers share
one in-flight scan; there is no time cache that can authorize stale success.
Git workspaces rebuild the lexical trust inventory and compare create, change,
delete, symlink, and native-config state. Every trusted supported source and
native-config candidate is observed through guarded directory descriptors,
`O_NOFOLLOW`, a streaming SHA-256, and before/after descriptor and lexical-entry
identity checks. A same-size, same-stat in-place rewrite therefore changes the
freshness identity. A file or parent that changes during observation returns
retryable `NOT_READY` before inventory, generations, or events are committed;
stable source deletion, config absence, or a stably rejected source symlink remains an
ordinary committable membership/config change. Content changes advance
document/path generations; membership or config changes also reattribute the
affected language family and advance trust/program/index generations. Running
adapters receive `didChangeWatchedFiles`. If a changed URI is already open, the adapter
also sends full-text `didChange` from the exact observed snapshot; deleted or
unreadable open URIs are closed. Newly created files receive a bounded
open/close notification when required. Freshness retains the watcher/reconcile
future and settles it before dispatch; admission, timeout, or worker failure
keeps the exact batch pending and returns retryable `BUSY` or `NOT_READY`, so an
unchanged scan cannot silently authorize stale success. Git preflight is
`O(total trusted source bytes)` with streaming `O(1)` memory per file. The
allowlisted read-only transformers root uses the same byte identity only for
the caller-named path and is never fully walked per call.

One changed batch is applied in two phases across language families. First,
every affected tracker advances its path/program generation without touching
an adapter. Second, the coordinator publishes exact pending ownership for all
runnable families, admits every delivery, and only then awaits results. A
failure in the first family therefore cannot leave a later family at a stale
current generation. Failed deliveries retain their exact batch with a
retryable empty-future state; `notified` means admitted for asynchronous
delivery and settled for synchronous delivery.

A native-config restart removes the old adapter from publication before stop,
but retains one explicit pending-restart record that owns the exact stop future,
projection, tracker, and cleanup obligation. Timeout publishes a retryable
`TIMED_OUT` family state. Every later Git freshness preflight resolves pending
restarts before an unchanged scan may succeed. Runtime shutdown settles both
published adapters and pending restart futures before closing the executor and
cannot publish a replacement after the runtime enters stopped state.

Every other adapter removal, including a family that becomes scope-incompatible
during reattribution, likewise publishes an exact pending-retirement owner in
the same lifecycle critical section as removal. The workspace executor retains
exactly two cleanup queue slots, one per fixed language family, outside the
ordinary request capacity; a third cleanup obligation is rejected instead of
silently making the bound elastic. Runtime shutdown reuses already-published
cleanup futures, does not publish `stopped` until every obligation reaches a
bounded terminal state, and leaves a failed admission retryable on a later stop.
It also distinguishes a completed failed/cancelled cleanup future from an
in-flight or successful one: the owning restart, retirement, or runtime-shutdown
record retains the adapter and invokes its retryable `stop()` again on the next
preflight instead of re-awaiting the same terminal failure forever.
If lease retirement has already detached that runtime from the workspace
registry, the daemon service retains it in a pending-stop owner set, retries it
on later sweeps, and keeps the build daemon non-idle until cleanup succeeds.
Immediate release reports actual stop truth separately from the lifecycle
decision: `runtime_stopped` is true only after confirmed cleanup, while
`runtime_stop_pending` exposes unsettled ownership. A failed stop is best-effort
and cannot terminate the periodic sweep or poison another workspace operation.
Calling an adapter's stop operation synchronously seals every ordinary
submission path before cleanup is queued, and each queued ordinary worker
rechecks the seal before it can lazily start a provider. Cleanup-reserve
admission and a completed failed stop future remain retryable, but the adapter
never reopens ordinary admission and retains its runtime owner until provider
shutdown succeeds.

Edit membership is checked against the lexical inventory rather than a
dynamically resolved set. Under the workspace lock, the writer walks every path
component using directory file descriptors, `lstat`, and `O_NOFOLLOW` before
opening or replacing the file. Replacing an inventoried path with any symlink,
including an in-root link to an ignored file, fails closed.

Executor edits expose `queued`, `running`, `installed`, and `done` commit states.
A queued entry that is successfully cancelled returns `TIMED_OUT` and can never
write later. Once work starts, or when state cannot be proven, timeout or lost
response returns `UNCERTAIN` and prohibits replay. Any fsync, notification, or
transport failure after `os.replace` is `UNCERTAIN` and includes the current hash
when it can be read safely.

### 15. Semantic contract is enforced at one service boundary

The runtime/service boundary is the sole exception-to-envelope conversion
owner. It catches `TimeoutError` before `OSError` and preserves `BUSY`,
`COOLDOWN`, `TIMED_OUT`, `SCOPE_INCOMPATIBLE`, and `UNCERTAIN` without generic
rewriting. Ordinary `LspResponseError`, `LspProtocolError`, exhausted
`LspTransportClosed`, and `LspProcessLost` values are also translated there
without exposing server messages: semantic reads return bounded `UNSUPPORTED`,
while the guarded edit returns `UNCERTAIN` and is never replayed. Cold global
warm-up preserves those typed failures to this boundary instead of rewriting
them as readiness failures.

Each adapter opens a URI at most once, sends `didChange` for subsequent content
versions, and sends `didClose` on stop or least-recently-used eviction. The open
document set is capped at 128 per adapter. Global symbol candidates are verified
against one exact document snapshot and every returned range uses the shared
`PositionMapper` for source, decoded-text, and byte positions.

`find_symbol` supports an inventory-bounded directory scope. Global
`include_body` and `include_info` are populated only from verified candidate
documents. Unsupported parameter combinations return typed failure rather than
being ignored. Scope attribution and readiness are independent per language
family: an incompatible family remains visible as `SCOPE_INCOMPATIBLE` while a
healthy family continues to serve calls, and a workspace may bind even when no
family is currently ready so status and refresh remain available.

Status projection differences contain at most 50 path entries plus total,
digest, and omitted count. Adapter transitions use `deque(maxlen=64)`. The
existing `DebugLogger` records only build/daemon startup and takeover, adapter
crash/cooldown, lease/grace, and cleanup summaries; it never records tool
payloads, source text, bearer material, or other secrets.

Every public MCP tool has a concise agent-facing description. The
`find_declaration.regex` input schema explicitly requires one Python
MULTILINE/DOTALL capture group selecting the queried symbol; malformed regexes
return a field-specific capture-count or syntax reason before LSP dispatch.

### 16. Reproducible build identity and versioned daemon slots

The service build identity is:

```text
sha256(sorted runtime source path+bytes
       + dependency lock digest
       + public tool/schema version
       + build-identity algorithm version)
```

The shared runtime layout is:

```text
/data/CoordExp/.codex/runtime/serena-light/
  python/
  deps/<lock_digest>/
  builds/<build_identity>/
    daemon.json
    bearer
    start.lock
    startup-nonce
    logs/
```

The connector attaches only to an exact build slot. Packaged Python modules and
the executed Pyright helper (`.py` and `.mjs` under `src/serena_light`), lock,
schema, or algorithm changes start a new daemon without killing older builds
that retain leases. The daemon recomputes and verifies identity before
publishing discovery.
Under the slot startup lock, the connector creates a one-use nonce; the daemon
must validate and consume it before discovery becomes visible. There is no
ordinary public daemon-start surface that bypasses this handshake.

A dedicated pytest acceptance may select a non-overlapping temporary runtime
root, a bounded short grace, and an alphanumeric build variant. The variant
identity is derived from the real source/lock/schema identity, so daemon-side
recomputation still detects source drift; arbitrary identities are not
accepted. This seam exists only to prove real connector/daemon slot coexistence
and natural retirement. It does not claim immutable source-snapshot packaging.

Service Python is installed at
`/data/CoordExp/.codex/runtime/serena-light/python`; daemon and service-venv
executables must not resolve through `/root/.local/share/uv`. Daemon and LSP
children use a service-owned HOME, locked executables, and a minimal environment
allowlist. Bootstrap itself may retain external proxy variables. V1's dependency
slot is an editable installation: the service-owned executable imports the live
repository source, while build identity detects and rolls over every covered
source-byte change. A build slot is therefore runtime isolation, not a frozen
source snapshot; rollback requires checking out the intended local commit.

When a build has no leases and every workspace warm grace has ended, its daemon
exits. If discovery already names a successor daemon for the same build, the
old process waits for zero holders and never deletes successor metadata. Legacy
v1 discovery, authenticated holder/status data, PID, and create time are
inspection evidence only. Because the v1 protocol cannot atomically freeze new
lease acquisition or issue a retirement token, automatic migration never
signals that daemon and returns `atomic_retirement_unsupported`; retirement
requires explicit operator coordination. Canonical Serena is never considered
a cleanup target.

### 17. Mutable external acceptance is snapshot-bound

Deterministic unit, integration, connector, and temporary-real-LSP tests remain
the default suite's contract owner. Tests that inspect `/data/CoordExp`,
`cc-plugin-codex`, `/data/ms-swift`, or the installed transformers source tree
are explicitly marked and skipped from marker metadata, without resolving or
hashing the root, unless the operator supplies the exact observed snapshot
identity. Git identities bind HEAD, the full tracked binary diff, and untracked
regular-file and symlink content. The `cc-plugin-codex` authority profile also
binds the ignored `.bin/tsc` launcher chain, TypeScript package/loader files,
platform-package metadata, and ultimate native `tsc` executable. The non-Git
transformers identity binds package version plus bounded source-tree content.
Setup and teardown use the same recorded profile. A mismatch, missing opted-in
root, or any before/after change fails rather than becoming `xfail` or clean.
Platform selection and repository-native TypeScript checks invoke the
service-owned locked Node and npm-cli; a controlled PATH puts that Node first
for the `.bin/tsc` shebang and retains only system shell lookup. Ambient
`node`, `npm`, `/root/.nvm`, and the model-client proxy are not command
authorities for this gate.

The clean/poisoned real-stdio acceptance uses a temporary runtime root, derived
test build variant, and two test-owned concurrent connector leases. Closing the
first lease must preserve the second holder and its daemon; cleanup targets only
the exact isolated PID/create-time owner. It never borrows or makes equality
claims about a production daemon that other clients may legitimately share.
The first holder enters teardown ownership before initialization/status awaits;
if partial startup fails, cleanup discovers only the unique test build slot and
reclaims an exact UUID/PID/create-time daemon identity.

The TypeScript 7 repository-native typecheck remains external, authoritative
acceptance rather than a default Serena Light correctness dependency. The
transformers semantic/liveness gate permits typed retryable `NOT_READY` before
exact success within three production calls. A separate opt-in performance
gate requires first-call success without increasing the production readiness
budget; observed wall time is evidence, not a newly invented SLO.

### 12. Result envelopes are stable and typed

Every tool returns JSON with a stable top-level `ok` boolean. Successful results
carry `data` plus workspace and adapter generation metadata. Failures carry an
`error` object with a stable code such as `INVALID_PATH`, `UNTRUSTED_ROOT`,
`INVALID_INPUT`, `OUT_OF_WORKSPACE`, `SCOPE_INCOMPATIBLE`, `NOT_READY`,
`UNSUPPORTED`, `SYMBOL_NOT_FOUND`, `AMBIGUOUS_SYMBOL`, `STALE_HASH`,
`READ_ONLY_ROOT`, `LEASE_EXPIRED`, `BUSY`, `TIMED_OUT`, `COOLDOWN`, or
`UNCERTAIN`.

`get_runtime_status` reports daemon identity, connector lease, bound workspace,
warm-grace state, adapter phases, engine paths/versions, interpreter, selected
native config, Git/trust inventory and configured-program projections and
digests, scope differences/reasons, incompatible extras, capability matrix,
generations, last crash, and cooldown. It does not expose secrets or absolute
paths outside the allowed operational set.

## Risks / Trade-offs

- **Cold global indexing can take about 24 seconds on transformers** → Start
  warming at connector activation, expose phases, use a sentinel gate, return
  `NOT_READY` rather than false empty, and retain a ten-minute grace.
- **The sentinel may not prove equivalent readiness on every server version** →
  Test it across five cold starts per adapter and remove global search for an
  adapter if a reliable completion condition cannot be found.
- **TypeScript 5.9 diagnostics diverge from TS 7 CI** → Label results advisory,
  report the engine, keep native typecheck authoritative, and include the known
  divergence as an acceptance fixture.
- **A copied LSP core creates local maintenance ownership** → Keep a provenance
  manifest, report production LOC, copy no unused languages, and
  treat future Serena features as new design inputs rather than sync work.
- **Native configuration can omit trusted sources or include ignored generated
  sources** → Report both projections and their reasons, retain path-scoped
  service for authorized omitted files when the engine supports it, refuse
  ignored extras with `SCOPE_INCOMPATIBLE`, and never force equivalence with an
  overlay config.
- **A shared daemon increases concurrency consequence** → Eliminate global
  project state, offload the synchronous LSP stack to bounded per-workspace
  executors, keep lease/status work on the event loop, and stress two sessions on
  one root plus two roots in parallel.
- **LSP positions are encoding-relative** → Negotiate and record position
  encoding, centralize conversion against the current raw file snapshot, and
  cover non-BMP text, BOM, and CRLF before enabling editing.
- **Loopback HTTP can be reached by another local process** → Use a random
  bearer secret in a mode-0600 runtime file and enforce query/edit trust policy
  independently of transport authentication.
- **Crash timing can make an edit outcome uncertain** → Atomic replacement,
  expected hashes, no replay, and explicit `UNCERTAIN` semantics make recovery
  observable and idempotent for callers.

## Migration Plan

1. Implement and test only inside this repository. Keep existing Serena
   configuration unchanged.
2. Expose the connector under a distinct `serena-light` MCP name and run it in
   parallel with `serena`.
3. Execute real-repository acceptance on `/data/CoordExp`,
   `/data/CoordExp/cc-plugin-codex`, `/data/ms-swift`, and the exact transformers
   package, including multi-session reuse, root isolation, stale hashes,
   freshness, diagnostics state, lease cleanup, and parent death.
4. Compare agent usability and resource behavior during normal Codex, Claude,
   and CC Agent sessions.
5. Produce a machine-readable old/new tool-name, required-argument, result-shape,
   hook, and instruction-consumer inventory. Classify each delta as supported,
   deliberately dropped, or separately shimmed.
6. Only after the inventory has no unresolved compatibility delta and the user
   gives explicit approval, change the canonical MCP registration from `serena`
   to `serena-light`. Preserve the previous command as the rollback value.

Rollback before the canonical switch is stopping the serena-light daemon and
removing its parallel MCP registration. Rollback after a switch restores the
previous Serena command and restarts the clients; the existing Serena repo and
runtime are never modified by this change.

## Open Questions

- Which server-specific sentinel query gives the strongest global-readiness
  signal without returning a large result set? This is an implementation probe
  owned by the warm-up task and does not change the public readiness contract.
- Whether a trimmed copied protocol model is eventually smaller than the
  pinned `lsprotocol` dependency may be revisited only after v1; the v1 decision
  is to depend on `lsprotocol`.
