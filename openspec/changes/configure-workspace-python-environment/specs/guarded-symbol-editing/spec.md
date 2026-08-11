## MODIFIED Requirements

### Requirement: Editing requires an authorized Git workspace
Before resolving or writing content, `replace_symbol_body` SHALL verify that the resolved target is inside the bound Git workspace below `/data`, is not a symlink escape, is present in the current Git trust inventory, and is not a read-only external root. Every non-Git workspace SHALL be read-only regardless of the selected Python environment. Edit authorization SHALL NOT depend on membership in the native configured semantic program; if the engine cannot safely resolve a trusted target path, the edit SHALL fail before writing. Authorization SHALL use lexical inventory membership and SHALL walk the target under the workspace lock with directory file descriptors, `lstat`, and `O_NOFOLLOW`; it MUST NOT authorize an inventory path through its later symlink resolution.

#### Scenario: Target is in the bound source repository
- **WHEN** the resolved file is inside the active editable Git workspace
- **THEN** edit processing may continue to symbol and hash validation

#### Scenario: Target is in a non-Git workspace
- **WHEN** the resolved file is inside any activated non-Git root
- **THEN** the tool returns `READ_ONLY_ROOT` before creating a temporary file

#### Scenario: Target is an external semantic location
- **WHEN** the resolved file was returned as a read-only external location
- **THEN** the tool returns `READ_ONLY_ROOT` without writing

#### Scenario: Target belongs to another bound workspace
- **WHEN** a session bound to workspace A submits a relative path that resolves only in workspace B
- **THEN** the edit is rejected as outside the active edit root

#### Scenario: Tracked path is substituted by an in-root symlink
- **WHEN** an inventoried source path is replaced after activation by a symlink to an ignored file under the same Git root
- **THEN** editing fails before opening a temporary file and the ignored target remains unchanged

