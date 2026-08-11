## MODIFIED Requirements

### Requirement: Python diagnostics disclose interpreter and engine
Successful Python diagnostics SHALL omit Pyright version, environment name, and interpreter path. `get_runtime_status` and rich Python diagnostic operational errors SHALL retain the pinned Pyright version plus the binding-selected Conda environment and interpreter used for import resolution.

#### Scenario: Python diagnostic uses the default environment
- **WHEN** a client omits `python_environment`
- **THEN** compact diagnostic success stays minimal while runtime status identifies `ms` and its interpreter

#### Scenario: Python diagnostic uses an explicit environment
- **WHEN** a client activates with `python_environment="llm-framework-study"`
- **THEN** compact diagnostic success stays minimal while runtime status identifies that selected environment and interpreter

#### Scenario: Python diagnostics fail operationally
- **WHEN** a Python diagnostic cannot run because its selected engine or interpreter setup is not ready
- **THEN** the rich typed error retains the binding-owned setup facts needed to recover

