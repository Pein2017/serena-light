## Why

Serena Light advertises parallel tool calls and shares one warm runtime across sessions, but only individual LSP dispatches are serialized today. Concurrent same-workspace semantic calls can therefore interleave their freshness and document-generation phases, causing the service to reject its own otherwise stable batch with retryable `NOT_READY` responses.

The recorded admission case is a nine-call Python reference batch in which one call succeeded and eight failed as sibling calls advanced the same adapter identity. The existing fixed-contract ablation already established practical semantic-quality parity with canonical Serena, so this change is admitted as a narrow reliability repair rather than a new feature or a weaker freshness contract.

## What Changes

- Serialize each complete semantic transaction for one workspace, including freshness preflight, semantic work, response witnessing, and freshness postflight.
- Put same-workspace semantic transactions in one bounded FIFO admission queue so parallel MCP calls wait in issue order and saturation remains a typed `BUSY` failure.
- Admit activation refresh and guarded symbol replacement through the same workspace transaction owner so they cannot invalidate an in-flight read or escape read/edit ordering.
- Preserve concurrency for different workspace identities and preserve responsiveness for lease operations, heartbeats, and runtime status.
- Preserve `supports_parallel_tool_calls=true`; clients may continue batching independent requests while the server owns safe ordering.
- Preserve digest-based freshness, postflight validation, one bounded read replay, edit uncertainty rules, and every public tool/schema.
- Treat a same-workspace path query behind a cold global semantic transaction as queued work; it no longer bypasses that transaction.
- Add deterministic concurrency tests and a real connector burst acceptance covering the historical failure shape.

Explicit non-goals are changing freshness hashing, adding timing fields to success payloads, adding tools, changing language coverage, broadening editing, changing proxy behavior, or modifying canonical Serena.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `workspace-runtime`: Strengthen same-workspace serialization from individual LSP dispatches to the complete semantic transaction and define bounded FIFO, saturation, lifecycle, and cross-root behavior.

## Impact

The implementation is limited to workspace runtime admission/lifecycle code, focused unit and integration tests, and compatibility documentation if the observable concurrency wording changes. Public MCP names, arguments, compact success envelopes, error codes, dependency locks, and client registrations remain compatible. A source-identity change naturally rolls new clients onto a new versioned daemon build; older build leases are not killed or migrated in place.

Admission requires a red test reproducing sibling generation interference, a green same-root burst with no self-induced `NOT_READY`, a real external-write test that still fails closed or replays, a queue-saturation `BUSY` test, a cross-root concurrency test, and lifecycle cleanup evidence. Failure of any freshness or edit invariant stops release rather than weakening the contract.
