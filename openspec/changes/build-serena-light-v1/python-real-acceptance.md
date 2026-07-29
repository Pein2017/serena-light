# Python real acceptance: OpenSpec 9.2 and 9.4

Date: 2026-07-28 UTC

## Current snapshot-bound candidate rerun

**PASS for semantic/liveness at candidate build `f46812e239fb...`: 7 passed
with 1 intentional performance skip.** The 2026-07-29 rerun supplied exact
before/after identities for `/data/CoordExp`, `/data/ms-swift`, and transformers
4.57.1 and included the two real Pyright integration cases. It separately
permits typed retryable `NOT_READY` before transformers global success within
three production calls; no larger timeout was introduced.

```text
7 passed, 1 skipped in 140.54s
```

The opt-in first-attempt performance test used three fresh runtimes at the same
transformers snapshot. All three calls returned the exact symbol with phase
`ready`; isolated pytest runs completed in 29.42s, 33.74s, and 33.62s.
These observations prove first-call success for this bounded run, not a new
end-to-end wall-clock SLO or a statistical reliability claim.

## Post-audit current-build rerun

**PASS at build `f4ee8a248a8c...`: 4 passed.** This 2026-07-29 post-restoration
full-suite rerun used the
production workspace/runtime and real Pyright against `/data/CoordExp`,
`/data/ms-swift`, and the exact transformers root. It is real-repository plus
real-LSP evidence; it does not traverse the shared daemon/connector. Process
evidence attributes only newly observed locked language-server commands, so
delayed children from earlier fault harnesses cannot be misreported as this
runtime's descendants.

```text
uv run pytest -q tests/acceptance/test_python_real_acceptance.py
4 passed
```

## Superseding final rerun

**Overall: PASS.** The final production-API run completed all four scenarios:

```text
uv run pytest -q tests/acceptance/test_python_real_acceptance.py
4 passed
```

The rerun proves the `/data/CoordExp` configured-program projection and global
`PipelinePlanner` recall; current `program=index=1`; ignored-data pruning;
mixed Python/TypeScript readiness within the 30-second gate; ms-swift
cross-library definition and current clean diagnostics through the exact
`/root/miniconda3/envs/ms/bin/python`; transformers global
`Qwen2VLForConditionalGeneration` recall; read-only edit rejection; RSS below
8 GB; and owned-process cleanup. The earlier blocker record is retained below
as historical red evidence and is no longer the current decision.

---

## Historical pre-fix run

**Overall: BLOCKER**

The acceptance is implemented in
`tests/acceptance/test_python_real_acceptance.py`. It exercises the production
`WorkspaceRuntime`, native program attribution, locked Pyright process, real
LSP transport, push diagnostics, global-symbol path, and edit authorization.
No LSP or scope mock contributes to a decision claim.

The focused final run was:

```text
uv run pytest -vv --showlocals tests/acceptance/test_python_real_acceptance.py
1 passed, 3 failed in 57.85s
```

The red assertions are retained because product behavior does not yet satisfy
the OpenSpec contracts. Product source and `tasks.md` were not changed.

## Locked environment

- Service test interpreter: `/data/CoordExp/serena-light/.venv/bin/python`
  (Python 3.12.12)
- Pyright: 1.1.403 from lock digest
  `f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865`
- Conda analysis interpreter: `/root/miniconda3/envs/ms/bin/python`
- Exact transformers root resolved through that interpreter:
  `/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers`

## 9.2 `/data/CoordExp`: BLOCKER

### Passing Python scope and pruning evidence

The standalone production Python attribution and Git inventory test passes:

- Full supported-language Git inventory: 1,123 files.
- Python trust projection: 1,116 files.
- Native Python configured program: 718 files.
- `trusted_not_in_configured_program`: 398 files, all explained as
  `excluded_by_native_config`.
- `configured_program_outside_trust`: 0 files.
- Selected config: `pyrightconfig.json`.
- Scope compatible: true; overlay generated: false.
- Git confirms `processed_data`, `artifacts`, and `outputs` are ignored, and
  none has a prefix in the production trust inventory.

The exact read-only census command returned:

```json
{"compatible": true, "ignored_prefixes_absent": {"artifacts": true, "outputs": true, "processed_data": true}, "inventory_count": 1123, "overlay_generated": false, "python_omitted_count": 398, "python_outside_count": 0, "python_program_count": 718, "python_trust_count": 1116, "selected_config": "pyrightconfig.json"}
```

### Blocking production-runtime behavior

Constructing the production runtime for `/data/CoordExp` fails before a Python
global query can start. `WorkspaceRuntime` eagerly attributes the TypeScript
family in the mixed-language root, and the TypeScript attribution reports seven
locked-engine library files below `.codex/runtime/serena-light/deps/...` as
configured-program paths outside Git trust:

```text
SCOPE_INCOMPATIBLE: TypeScript configured program contains paths outside trust:
lib.d.ts, lib.decorators.d.ts, lib.decorators.legacy.d.ts, lib.dom.d.ts,
lib.es5.d.ts, lib.scripthost.d.ts, lib.webworker.importscripts.d.ts
```

Consequently configured-program `PipelinePlanner` recall, current-generation
global readiness, the 30-second readiness gate, and current production-runtime
RSS cannot be claimed for task 9.2. Historical admission resource figures are
not substituted for this failed current acceptance.

## 9.4 `/data/ms-swift` and transformers: BLOCKER

### Passing definition, projection, edit, resource, and cleanup evidence

- `/data/ms-swift` native Python program attribution is compatible at 617
  files.
- Real `find_declaration` on
  `swift/infer_engine/lmdeploy_engine.py` resolves `GenerationConfig` into
  `transformers/generation/configuration_utils.py` under the exact conda root.
  The returned location is `read_only_external`.
- The definition envelope advertises Pyright definition support and derived
  `find_declaration=true`.
- The ms-swift run reported the fixed Pyright 1.1.403 engine and exact conda
  interpreter. Its sampled pytest-process-tree peak was 2,956,247,040 bytes,
  and all observed LSP descendants exited after `WorkspaceRuntime.stop()`.
- The exact transformers non-Git root has a compatible `workspace_default`
  projection: trust 2,214, configured program 2,214, no omissions, no outside
  paths, no overlay, digest
  `269651c6afce5a9ee59acabfca5df9499f4f71de3e0536784bc8786b7db950f0`.
- The sole v1 edit operation, `replace_symbol_body`, rejects the transformers
  target before hash or symbol resolution with `READ_ONLY_ROOT`.
- The transformers run also cleaned every observed LSP descendant. Its sampled
  peak was 195,268,608 bytes, but this is not a full-warm resource claim because
  global warm-up returned prematurely.

### Blocking diagnostics behavior

After the successful real definition call, production
`get_diagnostics_for_file` opens the same ms-swift document at document
generation 2. No matching push publication arrives within 15 seconds, so the
tool returns the correct typed failure rather than false clean:

```json
{
  "ok": false,
  "error": {
    "code": "TIMED_OUT",
    "details": {
      "state": "timed_out",
      "target_generation": 2,
      "waited_seconds": 15.0
    }
  }
}
```

The engine metadata still reports Pyright 1.1.403, the exact `ms` interpreter,
and the transformers read-only external root. Task 9.4 requires a current
diagnostic result (`clean` or `findings`), so typed timeout remains a blocker.

### Blocking transformers global-readiness behavior

The first production global lookup for
`Qwen2VLForConditionalGeneration` returns in 0.213 seconds with:

```text
code=NOT_READY
phase=starting
trust/program/document/index generations=0/0/0/0
running=true
```

The runtime performs only one early `workspace/symbol` attempt while Pyright is
starting and then returns `NOT_READY`; it does not exercise the specified
lock-free wait for readiness for up to 30 seconds. Therefore neither the
required global symbol nor successful global readiness within 30 seconds is
established, despite the compatible 2,214-file projection.

## Static verification

```text
uv run ruff check tests/acceptance/test_python_real_acceptance.py
PASS

uv run ty check tests/acceptance/test_python_real_acceptance.py
All checks passed!
```

## Stop decision

Stop at acceptance evidence. Do not mark 9.2 or 9.4 complete. Production fixes
are outside this lane; rerun this same focused module after the runtime
composition, diagnostic-generation publication, and bounded global-readiness
wait are repaired.
