## MODIFIED Requirements

### Requirement: Runtime status exposes operational truth
`get_runtime_status` SHALL report daemon identity, connector lease, bound
workspace, adapter phases, engine paths and versions, interpreter, selected
native config, full-file trust catalog, semantic trust inventory and
configured-program counts and digests, bounded file-level projection evidence,
scope differences and reasons, incompatible extras, capability matrix, current
trust/program/document/index generations, selected position encoding, semantic
executor queue state, lexical executor queue/running state, pinned ripgrep
path/version, last crash, and cooldown without exposing authentication secrets,
lexical patterns, subprocess arguments, matched text, or source.

Status SHALL also report the fixed zero-holder pool limits, daemon-local
candidate count, aggregate last-sampled known warm RSS and unknown-candidate
count, last bounded eviction reason, whether the bound runtime has active
holders, and that bound runtime's finite prewarm state (`idle`, `queued`,
`starting`, `abandoned`, or `complete`) plus current family when applicable.
Because `get_runtime_status` is lease-bound, it MUST NOT claim to address an
individual zero-holder runtime or expose candidate roots, per-candidate idle
age/LRU position, process descendant lists, or an unbounded eviction/prewarm
history.

Projection difference lists SHALL return at most 50 entries and include total,
digest, and omitted count. Adapter transition history SHALL contain at most 64
entries. Full-file catalog evidence SHALL be count/digest only unless an
existing bounded trust difference explicitly requires paths.

#### Scenario: Python workspace is warming
- **WHEN** Pyright has document readiness but has not completed its global sentinel
- **THEN** status reports `document_ready` or `global_warming` and does not report global readiness

#### Scenario: Capability planning is requested
- **WHEN** an agent inspects status before selecting a declaration or implementation tool
- **THEN** the response distinguishes raw LSP providers from derived availability of `find_declaration` and `find_implementations`

#### Scenario: TypeScript adapter is active
- **WHEN** a JS/TS workspace is bound
- **THEN** status reports the pinned TypeScript language-server path/version, selected tsconfig, configured-program projection, and its difference from the semantic Git trust inventory

#### Scenario: Native program includes an ignored source
- **WHEN** configured-program attribution finds a supported-language file outside the semantic trust inventory
- **THEN** status reports the incompatible path and reason while semantic calls return `SCOPE_INCOMPATIBLE`

#### Scenario: Native config omits trusted sources
- **WHEN** configured-program attribution finds trusted supported-language files excluded by native config
- **THEN** status reports them separately without claiming global readiness or global-search coverage for those files

#### Scenario: Full-file catalog contains documentation
- **WHEN** Git trust admits documentation or configuration that is not a supported semantic source
- **THEN** status reports a larger full-file count/digest without claiming that an adapter indexes those paths

#### Scenario: Lexical search is queued or running
- **WHEN** a `search_text` call occupies or waits for the lexical executor
- **THEN** status reports bounded queue/running state and the pinned ripgrep
  identity without revealing the pattern, result text, or raw process arguments

#### Scenario: Daemon retains zero-holder candidates
- **WHEN** a bound active lease requests status while the same daemon retains
  one or more other zero-holder runtimes
- **THEN** status reports fixed policy, candidate count, aggregate known RSS,
  unknown count, and last eviction reason without identifying candidate roots
  or pretending the active binding addresses one of them

#### Scenario: Bound runtime has an active holder
- **WHEN** runtime status is requested through a currently bound active lease
- **THEN** status reports active ownership and does not claim that warm-pool
  eviction is authorized

#### Scenario: Prewarm starts one family
- **WHEN** the finite prewarm scheduler is starting an attributed adapter
- **THEN** status reports `starting` and that family without claiming global
  readiness or exposing a queued request payload

#### Scenario: Prewarm is abandoned for foreground work
- **WHEN** remaining prewarm steps are dropped because user work appeared
- **THEN** status reports bounded `abandoned` state/reason and adapter readiness
  continues to reflect only phases actually reached
