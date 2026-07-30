## Why

The first Serena-versus-Serena-Light ablation exposed correctness gaps that make otherwise valid semantic results hard to interpret: public positions are emitted with mixed bases, Python assignment symbols can return identifier-only bodies, and reference success does not disclose the semantic program it actually covered. These issues must be repaired before changing the success schema or using the ablation to compare token efficiency.

## What Changes

- **BREAKING**: Define every agent-facing source range as 0-based decoded-text line and Unicode code-point column coordinates. Existing text-offset and byte-offset compatibility fields remain in this repair change and must describe the same exact snapshot; their later removal belongs to the compact-response change.
- Reject or explicitly label any location that cannot be mapped through the exact verified snapshot; never mix a 1-based line with a 0-based LSP character.
- Recover complete Python assignment-statement ranges for module variables and constants when the language server reports only the identifier range, while preserving the identifier as the selection range.
- Verify TypeScript/JavaScript assignment bodies with real fixtures and add adapter-owned recovery when the pinned server returns identifier-only or identifier-start ranges that omit declaration syntax.
- Add explicit, bounded semantic-program coverage to successful reference results so an agent can distinguish “no references in the configured program” from “the requested repository files were not covered.”
- Keep references purely language-server semantic: do not merge lexical `rg` matches into semantic results or create a second residual semantic program.
- Update compatibility metadata, tool descriptions, and black-box MCP acceptance evidence for the corrected contracts.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `semantic-navigation`: Correct public coordinate semantics, complete assignment-body retrieval, and reference-coverage disclosure.
- `diagnostics-status`: Align public diagnostic ranges with the same 0-based decoded-text coordinate contract while retaining rich generation and authority metadata.

## Impact

This change affects navigation and diagnostic renderers, shared position mapping, Python and possibly TypeScript adapter range normalization, reference results, public tool documentation, compatibility metadata, and real-daemon/connector acceptance tests. Existing consumers that compensated for 1-based positions must remove that compensation. The success-envelope shape otherwise remains unchanged so compact-response work can be implemented and measured as a separate dependent change.

Admission evidence comes from the completed four-arm ablation and direct source/runtime inspection: navigation and diagnostic renderers add one to public lines and columns, raw reference fallback can mix coordinate bases, Pyright can expose identifier-only variable ranges, and reference results omit configured-program coverage. The repair remains inside the existing Python and JavaScript/TypeScript adapters and does not add a language, daemon architecture, package surface, lexical-reference fallback, raw LSP tunnel, UI, memories, telemetry, or new editing operation.
