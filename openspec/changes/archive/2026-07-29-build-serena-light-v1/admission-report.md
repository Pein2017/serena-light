# serena-light Section-1 admission report

**Overall: FAIL — Section 2 is blocked.**

## Blocking gates

- TypeScript scope status is not pass: 'fail'
- coordexp engine source count disagrees with its declared inventory projection
- transformers engine source count disagrees with its declared inventory projection

## Locked runtime

- Lock digest: `f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865`
- Runtime: `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865`

### Exact versions

| Component | Version |
| --- | --- |
| node | `22.22.0` |
| npm | `11.13.0` |
| pyright | `1.1.403` |
| python | `Python 3.12.12` |
| typescript | `5.9.3` |
| typescript-language-server | `5.1.3` |

### Exact paths

| Component | Path |
| --- | --- |
| node | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node/bin/node` |
| npm | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/npm/bin/npm-cli.js` |
| pyright | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/pyright/index.js` |
| pyright-langserver | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/pyright/langserver.index.js` |
| python | `/root/.local/share/uv/python/cpython-3.12.12-linux-x86_64-gnu/bin/python3.12` |
| tsserver | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/typescript/lib/tsserver.js` |
| typescript | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/typescript/lib/tsc.js` |
| typescript-language-server | `/data/CoordExp/.codex/runtime/serena-light/deps/f466e21b2e6356b5623293ac2d60e7fba66eea0bf1c5d6e8aca28b34f8aea865/node-packages/node_modules/typescript-language-server/lib/cli.mjs` |

## Source census and budget

```json
{
  "action_counts": {
    "copy": 4,
    "delete": 17,
    "reference": 24,
    "reshape": 28
  },
  "current_local_production_lines": 323,
  "expected_production_lines": 9042,
  "maximum_production_lines": 12000,
  "owned_code_estimate_lines": 7600,
  "reference_commit": "9a9d07e83d8c1cba3458992707f440c624446c6d",
  "selected_upstream_lines": 1442,
  "status": "pass"
}
```

## Readiness runs

- Recorded runs: 20
- Probe status: `pass`
- Timeout: `30.0` seconds

| Profile | Run | Global ready (s) | Symbol | URI | Inventory | Server sources | Cleanup | Scope |
| --- | ---: | ---: | --- | --- | ---: | ---: | --- | --- |
| transformers | 1 | 20.475 | `Qwen2VLForConditionalGeneration` | `file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/models/qwen2_vl/modeling_qwen2_vl.py` | 2214 | 2219 | True | False |
| transformers | 2 | 21.21 | `Qwen2VLForConditionalGeneration` | `file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/models/qwen2_vl/modeling_qwen2_vl.py` | 2214 | 2219 | True | False |
| transformers | 3 | 21.777 | `Qwen2VLForConditionalGeneration` | `file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/models/qwen2_vl/modeling_qwen2_vl.py` | 2214 | 2219 | True | False |
| transformers | 4 | 20.73 | `Qwen2VLForConditionalGeneration` | `file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/models/qwen2_vl/modeling_qwen2_vl.py` | 2214 | 2219 | True | False |
| transformers | 5 | 20.733 | `Qwen2VLForConditionalGeneration` | `file:///root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers/models/qwen2_vl/modeling_qwen2_vl.py` | 2214 | 2219 | True | False |
| coordexp | 1 | 11.668 | `PipelinePlanner` | `file:///data/CoordExp/public_data/pipeline/planner.py` | 1116 | 718 | True | False |
| coordexp | 2 | 11.112 | `PipelinePlanner` | `file:///data/CoordExp/public_data/pipeline/planner.py` | 1116 | 718 | True | False |
| coordexp | 3 | 11.005 | `PipelinePlanner` | `file:///data/CoordExp/public_data/pipeline/planner.py` | 1116 | 718 | True | False |
| coordexp | 4 | 11.382 | `PipelinePlanner` | `file:///data/CoordExp/public_data/pipeline/planner.py` | 1116 | 718 | True | False |
| coordexp | 5 | 11.315 | `PipelinePlanner` | `file:///data/CoordExp/public_data/pipeline/planner.py` | 1116 | 718 | True | False |
| ms-swift | 1 | 3.423 | `SwiftPipeline` | `file:///data/ms-swift/swift/pipelines/base.py` | 617 | 617 | True | True |
| ms-swift | 2 | 3.648 | `SwiftPipeline` | `file:///data/ms-swift/swift/pipelines/base.py` | 617 | 617 | True | True |
| ms-swift | 3 | 3.701 | `SwiftPipeline` | `file:///data/ms-swift/swift/pipelines/base.py` | 617 | 617 | True | True |
| ms-swift | 4 | 3.603 | `SwiftPipeline` | `file:///data/ms-swift/swift/pipelines/base.py` | 617 | 617 | True | True |
| ms-swift | 5 | 3.317 | `SwiftPipeline` | `file:///data/ms-swift/swift/pipelines/base.py` | 617 | 617 | True | True |
| cc-plugin-codex | 1 | 1.411 | `createAgentStore` | `file:///data/CoordExp/cc-plugin-codex/runtime/agent-store.mjs` | 57 | None | True | None |
| cc-plugin-codex | 2 | 1.422 | `createAgentStore` | `file:///data/CoordExp/cc-plugin-codex/runtime/agent-store.mjs` | 57 | None | True | None |
| cc-plugin-codex | 3 | 1.42 | `createAgentStore` | `file:///data/CoordExp/cc-plugin-codex/runtime/agent-store.mjs` | 57 | None | True | None |
| cc-plugin-codex | 4 | 1.423 | `createAgentStore` | `file:///data/CoordExp/cc-plugin-codex/runtime/agent-store.mjs` | 57 | None | True | None |
| cc-plugin-codex | 5 | 1.436 | `createAgentStore` | `file:///data/CoordExp/cc-plugin-codex/runtime/agent-store.mjs` | 57 | None | True | None |

## TypeScript scope equivalence

### cc_plugin_codex — FAIL

- Git inventory: 57
- tsserver workspace program: 22
- Missing from program: `['eslint.config.mjs', 'plugins/cc-for-pein/bootstrap/cc-runtime.mjs', 'scripts/local-plugin-install.mjs', 'scripts/probe-runtime-capacity.mjs', 'scripts/update-plugin-cachebuster.mjs', 'tests/runtime-integration/fixtures/real-claude-disconnect-wrapper.mjs', 'tests/runtime-integration/runtime-cli.test.mjs', 'tests/runtime/adapter.test.mjs', 'tests/runtime/agent-completion-projection.test.mjs', 'tests/runtime/agent-job-linkage.test.mjs', 'tests/runtime/agent-launch-boundary.test.mjs', 'tests/runtime/agent-message-idempotency.test.mjs', 'tests/runtime/agent-model-migration.test.mjs', 'tests/runtime/agent-progress-projection.test.mjs', 'tests/runtime/agent-reconciliation.test.mjs', 'tests/runtime/agent-root-isolation.test.mjs', 'tests/runtime/agent-session-conflict.test.mjs', 'tests/runtime/agent-store.test.mjs', 'tests/runtime/agent-wait-persistence.test.mjs', 'tests/runtime/args.test.mjs', 'tests/runtime/claude-session-history.test.mjs', 'tests/runtime/claude-version-compatibility.test.mjs', 'tests/runtime/completion-inbox.test.mjs', 'tests/runtime/detached-worker-handoff.test.mjs', 'tests/runtime/environment.test.mjs', 'tests/runtime/execution-profile.test.mjs', 'tests/runtime/fixtures/job-store-writer.mjs', 'tests/runtime/hardening.test.mjs', 'tests/runtime/job-runner.test.mjs', 'tests/runtime/job-store.test.mjs', 'tests/runtime/local-plugin-install.test.mjs', 'tests/runtime/plugin-contract.test.mjs', 'tests/runtime/process-control.test.mjs', 'tests/runtime/render.test.mjs', 'tests/runtime/supervisor.test.mjs']`
- Extra in program: `[]`
- Cleanup: `True`

### ignored_subtree_fixture — FAIL

- Git inventory: 2
- tsserver workspace program: 3
- Missing from program: `[]`
- Extra in program: `['ignored-generated/hidden.ts']`
- Cleanup: `True`

## Position encodings

All recorded initialize responses omitted `positionEncoding`; per the LSP specification the selected default is UTF-16.

## Stop decision

Do not start Section 2. Revise the OpenSpec source-scope contract before implementation continues.
