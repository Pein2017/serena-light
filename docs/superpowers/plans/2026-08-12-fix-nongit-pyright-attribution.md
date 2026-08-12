# Fix non-Git Pyright Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make exact read-only non-Git Python roots semantically navigable when Pyright's configured program exactly equals Serena Light trust.

**Architecture:** Preserve the existing fail-closed projection and change only the evidence-order validation boundary: validate raw absolute strings in the Node probe's canonical order, then materialize `Path` values. Lock the behavior with a synthetic protocol regression and a real `llm-framework-study` site-packages acceptance before updating and reinstalling the plugin.

**Tech Stack:** Python 3.12, pytest, Pyright 1.1.403 probe, Ruff, Ty, Codex plugin packaging.

## Global Constraints

- Non-Git roots remain exact and read-only.
- Real configured-program paths outside trust still return `SCOPE_INCOMPATIBLE`.
- Public tool schema stays at version 6 and the 11-tool surface is unchanged.
- `pyproject.toml`, `uv.lock`, `package-lock.json`, and canonical Serena remain unchanged.
- Work only in `/data/CoordExp/.worktrees/serena-light-nongit-pyright` on `codex/fix-nongit-pyright-attribution`.

---

### Task 1: Align attribution evidence ordering

**Files:**
- Modify: `tests/unit/test_pyright_adapter.py`
- Modify: `src/serena_light/lsp/pyright.py`

**Interfaces:**
- Consumes: schema-1 owned-files reports emitted by `pyright_owned_files_probe.mjs`.
- Produces: `_validate_owned_files_report(report, *, expected_cli) -> PyrightOwnedFilesEvidence` accepting unique raw strings in canonical string order.

- [x] **Step 1: Write the failing protocol regression**

Add a report with `owned_files` ordered as
`["/tmp/wrapt-stubs/__init__.pyi", "/tmp/wrapt/__init__.py"]`, compute its
existing `_path_digest`, and assert validation succeeds. Also assert reversing
the raw strings and duplicating one remain typed `PyrightAttributionError`s.

- [x] **Step 2: Run the exact regression and verify RED**

Run:

```bash
PYTHONPATH=src /data/CoordExp/serena-light/.venv/bin/python -m pytest \
  tests/unit/test_pyright_adapter.py::test_owned_files_report_uses_probe_string_order -q
```

Expected: FAIL with `Pyright owned-files attribution paths are not unique and sorted`.

- [x] **Step 3: Implement the minimum validator correction**

In `_validate_owned_files_report`, first validate every `raw` item as an
absolute NUL-free string, keep a `raw_paths: list[str]`, require
`raw_paths == sorted(raw_paths)` and uniqueness, then create `Path` instances.
Do not change `_path_digest` or projection logic.

- [x] **Step 4: Verify GREEN and focused guardrails**

Run the exact regression, then all `tests/unit/test_pyright_adapter.py` tests.
Expected: PASS, including existing version/digest and outside-trust failures.

- [x] **Step 5: Commit the implementation**

Stage only the two files, inspect the staged diff, run `git diff --cached --check`,
and commit `fix: validate Pyright paths in probe order`.

### Task 2: Prove the non-Git product path and publish

**Files:**
- Modify: `tests/acceptance/test_python_real_acceptance.py` or the smallest existing real-runtime acceptance owner.
- Modify: `tests/acceptance/test_real_shared_daemon_acceptance.py`
- Modify: `README.md`
- Modify: `docs/client-registration.md`
- Modify: `.codex-plugin/plugin.json` through the cachebuster helper.
- Create: `docs/acceptance/non-git-pyright-attribution-release.md`

**Interfaces:**
- Consumes: exact `WorkspaceIdentity(kind=non_git_read_only, python_environment="llm-framework-study")` and the repaired attribution validator.
- Produces: fresh-client evidence that `activate_workspace`, `get_symbols_overview`, and `find_symbol` work while `replace_symbol_body` remains read-only.

- [x] **Step 1: Replace the obsolete real-daemon expectation with a failing semantic assertion**

Activate `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages`
with `python_environment="llm-framework-study"`; assert status has a Python
adapter and no `SCOPE_INCOMPATIBLE`, then query a bounded known file such as
`torchtune/__init__.py`. Retain the existing byte-preserving edit rejection.

- [x] **Step 2: Prove the acceptance fails without Task 1 and passes with it**

Run the focused real shared-daemon and real Python acceptance selections. The
historical base must fail at attribution; the repaired tree must return semantic
success and `READ_ONLY_ROOT` for edit.

- [x] **Step 3: Run repository gates**

Run focused tests, complete `tests`, Ruff, Ty, source budget/ownership and
provenance checks, strict OpenSpec validation, `git diff --check`, and verify no
diff in `pyproject.toml`, `uv.lock`, `package-lock.json`, public schema/tool
census, or canonical Serena.

- [x] **Step 4: Update release documentation and cachebuster**

Record exact commands, counts, old/new build identity, fixed root/interpreter,
semantic outputs, edit refusal, and process cleanup. Run
`update_plugin_cachebuster.py` on this checkout; do not hand-edit marketplace
configuration.

- [x] **Step 5: Commit release evidence**

Stage only acceptance, documentation, and plugin manifest paths; inspect the
staged diff and commit `release: publish non-Git Pyright attribution fix`.

- [ ] **Step 6: Reinstall and run fresh-client smoke**

Verify `/data/CoordExp/.agents/plugins/marketplace.json` still points to this
repository's intended source, install `serena-light@coordexp-local`, start a
fresh strict client from the site-packages root, exercise activation/status/
overview/find/release, and verify no owned daemon or LSP process is orphaned.

- [ ] **Step 7: Synchronize and push**

Fetch `origin`, verify `main` has not advanced or rebase only if needed and
clean, rerun merge-sensitive gates, then push
`codex/fix-nongit-pyright-attribution`. Do not force-push or mutate other
branches/worktrees.
