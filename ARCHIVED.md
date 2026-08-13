# Repository archived

Serena Light was an internal experiment in a smaller, agent-facing semantic
navigation service. Maintenance stopped on 2026-08-13 because the ongoing cost
of owning language-server lifecycle, freshness, trust, protocol evidence, and
multi-client compatibility exceeded the benefit relative to upstream Serena.

The repository is retained for its implementation history, OpenSpec changes,
acceptance evidence, and provenance records. It is not an install target and
will not receive dependency, security, compatibility, or language-server
updates.

New CoordExp sessions use [official Serena](https://github.com/oraios/serena).
The supported local deployment binds one shared upstream server to each exact
Git worktree and exposes only the minimal approved semantic/query/edit tools.
