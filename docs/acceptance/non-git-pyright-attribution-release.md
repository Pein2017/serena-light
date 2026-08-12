# Non-Git Pyright Attribution Release Evidence

Date: 2026-08-12

Branch: `codex/fix-nongit-pyright-attribution`

Base: `main@5e7d8ba`

## Defect and correction

The Pyright owned-files probe emits absolute paths in canonical raw-string
order. Serena Light materialized those strings as `pathlib.Path` objects before
checking that order. Raw string ordering places `wrapt-stubs/__init__.pyi`
before `wrapt/__init__.py`, while component-wise `Path` ordering does the
opposite. A fully trusted 19,068-file configured program was therefore rejected
as `SCOPE_INCOMPATIBLE` even though it contained no outside path.

The validator now checks canonical ordering on the raw protocol strings, then
materializes `Path` objects. Uniqueness remains checked on materialized paths,
so normalized aliases are still rejected. Digest, projection, outside-trust,
workspace, and edit policy behavior are unchanged.

## Real product path

- Root: `/root/miniconda3/envs/llm-framework-study/lib/python3.12/site-packages`
- Environment: `llm-framework-study`
- Interpreter: `/root/miniconda3/envs/llm-framework-study/bin/python`
- Kind: `non_git_read_only`
- Trust inventory: 19,068 paths
- Configured program: 19,068 paths
- Outside paths: 0
- Semantic file: `torchtune/config/_parse.py`
- Overview evidence: top-level `parse` returned
- Symbol evidence: exact `parse` lookup returned
- Edit evidence: `READ_ONLY_ROOT`; source bytes unchanged
- Cleanup evidence: real-runtime descendants exited; shared-daemon final release
  reported zero holders and a stopped test-owned runtime

## Build and verification

- Prior stable build identity:
  `77e0ff6e7b74c3e100e75a3b81bb025a8e906642a089d0c81c755aaba6d183aa`
- Repaired runtime-source build identity:
  `6498a4eb68c62e23561aa6b04e167fe54dd55b9d90b80c12bbb6560f078b9c39`
- Dependency-lock digest:
  `eff6ebdf252faff7f77cb3a2f3894d17b9a0dfc89b46bd193fafdaa9e9ab4941`
- Public schema: 6
- Public tools: 11, unchanged

Verification results before publication:

- Focused unit regression: 1 passed
- Pyright adapter unit suite: 32 passed
- Exact real non-Git Python acceptance: 1 passed
- Real shared-daemon acceptance, including poisoned proxy: 1 passed
- Complete pytest suite: 943 passed, 35 skipped, 1 existing deprecation warning
- Ruff: passed
- Ty with the repository service environment: passed
- Bootstrap version and digest check: passed
- Source ownership, dependency, census, and copied-provenance checks: passed
- Strict OpenSpec validation: 4 passed, 0 failed
- Plugin validation: passed

No dependency lock, public schema, tool census, build-identity algorithm,
daemon protocol, marketplace source, or canonical Serena code changed. Existing
leased daemon slots are not terminated; fresh clients select the repaired
versioned build.

## Installed fresh-client smoke

After the feature branch was fast-forwarded into `main`, bootstrap was rerun
from `/data/CoordExp/serena-light`; the locked service environment imported
`serena_light` from `/data/CoordExp/serena-light/src`. The local plugin was then
installed as `0.1.0+codex.20260812101430`.

A new MCP stdio client started with the exact site-packages root as its process
cwd and no ambient proxy variables. It observed server `serena-light`, exactly
11 tools, `kind=non_git_read_only`, `python_environment=llm-framework-study`,
and an empty issue list. Overview returned
`torchtune/config/_parse.py`; exact lookup returned `parse`. Immediate release
reported `active_holders=0`, `runtime_stop_pending=false`, and
`runtime_stopped=true`.
