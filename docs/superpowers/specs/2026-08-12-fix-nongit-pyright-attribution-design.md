# Fix non-Git Pyright attribution design

## Problem

An exact, read-only non-Git `site-packages` root can activate with the requested
Conda interpreter but still expose Python as `SCOPE_INCOMPATIBLE`. The live
`llm-framework-study` probe showed that Pyright and Serena Light own the same
19,068 Python paths, with no path outside trust and no trusted path omitted.
The failure occurs earlier: the Node attribution probe sorts absolute path
strings, while `_validate_owned_files_report` converts them to `Path` objects
and checks Python's component-wise `Path` order. Those orderings disagree for
names such as `wrapt-stubs` and `wrapt`, so valid evidence is rejected.

## Contract

The probe protocol owns a deterministic string ordering. The Python validator
shall validate uniqueness and order on the raw absolute strings exactly as
received, then convert them to `Path` objects. All existing validation remains:
schema, engine version, executable identity, absolute paths, NUL refusal,
count, digest, project/config consistency, and the later configured-program
versus trust comparison.

The change shall not:

- widen the trust inventory or synthesize a Pyright overlay;
- make a non-Git workspace editable;
- suppress a real configured-program path outside trust;
- change the public MCP tool/schema surface, dependency lock, or canonical
  Serena;
- modify backend-evaluation branches or artifacts.

This is an implementation repair for the existing
`semantic-navigation` requirement that trusted external Python definitions
remain navigable and read-only, and the `workspace-runtime` requirement that
an arbitrary non-Git directory binds as an exact read-only identity. It does
not need a new overlapping OpenSpec change.

## Verification

1. A unit report ordered as strings but not as `Path` objects is accepted.
2. Duplicate, unsorted-string, relative, digest-drift, and version-drift
   reports remain rejected.
3. The real `llm-framework-study` `site-packages` attribution returns a
   compatible projection with identical configured-program and trust sets.
4. A real runtime can activate that exact root with the matching interpreter,
   return a symbol overview/find result, and still reject edits as
   `READ_ONLY_ROOT`.
5. Focused, full test, Ruff, Ty, source/provenance, plugin validation, bootstrap,
   and fresh installed-client smoke gates pass before publication.

## Release

Because runtime source bytes change, the build identity must change naturally;
the public schema and dependency lock remain unchanged. Update the plugin's
Codex cachebuster with the supported helper, reinstall from the existing
`coordexp-local` marketplace, verify from a fresh client, commit the release
evidence, and push the repaired branch. Existing leased clients may finish on
their old build; new clients select the new build identity.
