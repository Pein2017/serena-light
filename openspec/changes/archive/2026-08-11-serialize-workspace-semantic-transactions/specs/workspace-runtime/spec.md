## MODIFIED Requirements

### Requirement: Calls are serialized within a workspace
The system SHALL admit every content-bearing semantic query, same-root activation refresh, and guarded symbol edit to one bounded FIFO transaction owner for the bound workspace identity. The transaction owner SHALL order the complete operation: freshness preflight, semantic work, generation mutation, response witnessing and postflight for reads, or commit-state resolution for edits. Same-workspace work admitted after the fixed bound is full SHALL return typed `BUSY` and SHALL NOT execute later. Readiness waits that participate in a semantic result SHALL retain their FIFO position, while lease operations, heartbeats, runtime status, and work for other workspace identities SHALL remain responsive.

#### Scenario: Parallel reads on one shared runtime
- **WHEN** multiple sessions issue semantic reads concurrently against the same workspace and no external source change occurs
- **THEN** the reads execute in FIFO transaction order and none fails because a sibling read advanced adapter or document generation

#### Scenario: Query overlaps an edit on the same root
- **WHEN** a query and `replace_symbol_body` arrive concurrently for the same workspace
- **THEN** one complete transaction finishes before the other performs its freshness preflight or observes workspace state

#### Scenario: Queued stale-hash edit reaches the front
- **WHEN** a guarded edit waits behind another same-workspace transaction and its expected hash is stale when it begins
- **THEN** it returns the existing stale-hash failure and does not overwrite the newer file

#### Scenario: Same-root activation overlaps a semantic read
- **WHEN** one lease activates the already-bound Git root while another lease is reading that root
- **THEN** the forced activation refresh and the complete read transaction execute in FIFO order without invalidating each other

#### Scenario: Queries target different roots
- **WHEN** operations target different workspace identities at the same time
- **THEN** neither workspace transaction owner blocks the other

#### Scenario: Cold global wait precedes a path query
- **WHEN** one same-workspace global query is admitted before a path query while global readiness is cold
- **THEN** the path query waits behind the global transaction and starts after that transaction has produced a result

#### Scenario: Runtime control remains responsive
- **WHEN** a same-workspace semantic transaction is blocked in readiness or LSP work
- **THEN** connector heartbeats, lease inspection, runtime status, and semantic calls for other roots remain responsive

#### Scenario: Blocking LSP call runs on one root
- **WHEN** a fake LSP request blocks one workspace executor for longer than a heartbeat interval
- **THEN** another root, runtime status, and connector heartbeats continue without event-loop delay

#### Scenario: Same-workspace transaction queue is saturated
- **WHEN** one transaction is running and the fixed number of ordinary queue entries are already waiting
- **THEN** the next semantic call returns typed `BUSY` and that rejected work never starts later

#### Scenario: Queued request is cancelled
- **WHEN** a client cancellation is accepted before its bounded transaction entry starts
- **THEN** the entry is removed without running freshness, mutating adapter state, or retaining a workspace lock

#### Scenario: Queued edit reaches its timeout
- **WHEN** an edit is proven not to have started and is cancelled in either owned queue
- **THEN** it returns `TIMED_OUT` and can never execute later

#### Scenario: Running edit reaches its timeout
- **WHEN** an edit has started or its commit state cannot be proven
- **THEN** it returns `UNCERTAIN`, is not replayed, and requires a fresh hash read

#### Scenario: Ordinary request queue is saturated during shutdown
- **WHEN** all ordinary LSP queued-work slots are occupied and both fixed language adapters require cleanup
- **THEN** their two cleanup obligations remain admissible without increasing ordinary request capacity, while a third cleanup submission fails explicitly

#### Scenario: Runtime stops with queued transactions
- **WHEN** a workspace runtime begins shutdown while semantic transactions are queued
- **THEN** queued reads and edits are cancelled before execution, the running transaction is settled, and all owned transaction and LSP workers reach a bounded terminal state
