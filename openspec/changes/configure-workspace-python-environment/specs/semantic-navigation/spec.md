## MODIFIED Requirements

### Requirement: External Python definitions remain navigable and read-only
The Pyright adapter SHALL use the Conda environment selected when the workspace was activated and SHALL return definitions resolved through that environment. Compact public identity SHALL be the authoritative absolute path plus `read_only=true`; an external location MUST NOT create a second public trust identifier or become editable.

#### Scenario: Default environment resolves an installed package
- **WHEN** a caller omits `python_environment` and requests the definition of an import available in `ms`
- **THEN** navigation uses the `ms` interpreter and marks the external file group read-only

#### Scenario: Explicit environment resolves its installed package
- **WHEN** a caller activates with `python_environment="llm-framework-study"` and requests a definition available in that environment
- **THEN** navigation uses the selected environment rather than an identically named or missing package in `ms`

#### Scenario: External definition becomes an edit input
- **WHEN** a caller passes any returned external path to an editing tool
- **THEN** navigation remains allowed but editing returns `READ_ONLY_ROOT`

#### Scenario: Environment selection remains binding-scoped
- **WHEN** two leases query the same root through different selected environments
- **THEN** each result is produced by its own environment's adapter without changing the other lease

