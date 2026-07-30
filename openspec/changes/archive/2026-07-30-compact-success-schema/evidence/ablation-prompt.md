# Locked Four-Arm Semantic MCP Ablation Prompt

Each arm receives this prompt with `{{ASSIGNED_MCP}}` replaced by either
`canonical serena` or `serena-light`. Model-family comparisons are paired; the
lead scores accuracy before efficiency.

---

You are a read-only benchmark participant. Work from the current shared
workspace rooted at `/data/CoordExp`. Do not edit, create, delete, rename,
install, fetch, commit, or otherwise mutate any file or repository state.

ASSIGNED SEMANTIC MCP: `{{ASSIGNED_MCP}}` only. You must not call the other
semantic MCP or any other MCP/app. You may use read-only shell commands. Prefer
the assigned MCP for code discovery, symbol lookup, reference traversal,
overviews, and reading symbol bodies. Shell source search/read (for example
`rg`/`sed`/`cat`) is fallback only after the assigned MCP has failed,
truncated, or lacks the needed capability; every such fallback must be
justified in the final tool ledger. Shell may be used directly for `pwd`, Git
metadata, hashes, and other non-source environment checks. Do not use web,
memory, subagents, or prior conversation context. If the assigned MCP is
unavailable, report it and do not substitute another MCP.

Use the assigned MCP's required initialization and absolute workspace/project
activation. Treat all MCP line numbers as 0-based; if shell evidence uses
1-based lines, label that explicitly. Do not run heavyweight model jobs. Keep
the answer at most 2,500 words. Both questions must be answered from the live
filesystem.

## Question 1 — Serena Light semantic-query contract

Target: `/data/CoordExp/serena-light`

Trace the current implementation contract of `WorkspaceRuntime.find_symbol`.

1. Show where freshness reconciliation occurs before the operation.
2. Compare the exact-file, inventory-bounded directory, and global-search
   branches: the service each branch delegates to, candidate/scope bounds, and
   behavior when an attributed language family is unavailable.
3. Explain how `FreshnessCoordinator.ensure_fresh` and `_scan_git` coordinate
   concurrent scans and distinguish content changes, membership/symlink
   changes, and native-config changes. State the observable consequences for
   inventory, adapter restart/reattribution, watched-file delivery, and typed
   failures. Do not infer behavior that the code does not establish.
4. Cite at least four current tests and state the distinct invariant each
   protects.
5. Give one concise prevented stale/scope failure and one remaining explicit
   limitation of this contract.

## Question 2 — Exact-history research evidence seam

Target: `/data/CoordExp/.worktrees/research-probes`

Trace the seam from `HFBackendSession.prepare_exact_history`,
`extend_exact_history`, and `teacher_forced_evidence` into
`scripts/research/run_continuation_locality_boundary_scoring.py`.

1. Explain the session-owned identity, immutable literal-token extension,
   digest validation, and forged/cross-session/closed-session behavior.
2. Explain exactly which logits positions are selected, why the tensors are
   converted to CPU FP32, and how `raw_model_logprob` and
   `candidate_vocab_rank` are computed, including tie behavior.
3. Trace how the boundary-scoring runner constructs the history, scores
   row-entry versus terminal tokens, preserves the subtraction semantics, and
   records provenance/claim-boundary evidence.
4. Cite at least four current tests and state what each proves.
5. Give separate YES/NO verdicts, with evidence, on whether the resulting
   receipt supports: (A) literal fixed-prefix identity, (B) fixed-prefix
   relative preference between the two selected next tokens, (C) free-rollout
   behavior, and (D) reconstruction of the full vocabulary distribution.

## Output format

- Snapshot: target Git HEADs and whether relevant files appeared modified.
- Question 1 answer, followed by an evidence table
  (`claim | file | symbol | line range | line convention`).
- Question 2 answer, followed by the same evidence table.
- Verdicts A-D.
- Tool ledger in execution order: `tool | purpose | outcome`; include every
  assigned-MCP call and every shell command. Mark each shell source read/search
  as `fallback` and explain the MCP limitation that triggered it.
- Final confidence and any unresolved ambiguity.

Completion means returning the analysis only. Stop without modifying anything.
