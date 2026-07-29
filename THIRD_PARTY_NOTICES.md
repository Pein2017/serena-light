# Third-party notices

## Serena and SolidLSP reference

`serena-light` is designed with reference to the MIT-licensed Serena repository:

- Reference checkout: `/data/CoordExp/external/serena`
- Reference commit: `9a9d07e83d8c1cba3458992707f440c624446c6d`
- Copyright: Copyright (c) 2025 Oraios AI
- License: MIT

The full Serena MIT license text is reproduced in
[`third_party/licenses/serena-MIT.txt`](third_party/licenses/serena-MIT.txt).

`src/serena_light/processes.py` derives its
`LanguageServerSubprocessLauncher`, `_signal_process_tree`, and
`terminate_process_tree_with_kill_fallback` mechanisms from
`src/solidlsp/util/subprocess_util.py` at the reference commit above. The
launcher retains the dedicated Linux parent-death spawner thread, `exec`
handoff, and independent process-session invariants. Its public inputs were
simplified to repository-owned command/cwd/environment values. Process cleanup
was rewritten to signal the verified owned process group with TERM followed by
KILL instead of reproducing SolidLSP's recursive best-effort walk.

`src/serena_light/lsp/client.py` reshapes the synchronous framing and request
lifecycle from two SolidLSP sources at the same pinned commit. Its
`encode_message` and `read_message` mechanisms simplify
`src/solidlsp/lsp_protocol_handler/server.py` `create_message` and
`content_length` into bounded byte-stream framing. Its pending-request,
timeout, response-dispatch, and read-only `ContentModified` retry lifecycle
simplifies `src/solidlsp/ls_process.py` `Request`,
`LanguageServerInterface._send_request_once`,
`LanguageServerInterface.send_request`, and
`LanguageServerInterface._response_handler`. The local client removes Serena's
language registry and callback surface, owns stricter framing limits, and
permits retry only for an explicit read-method allowlist.

`third_party/copied_sources.json` is the authoritative symbol-level manifest.
Each `copied_sha256` is the SHA-256 of the exact UTF-8 upstream symbol span from
its AST `lineno` through `end_lineno`, including original line endings. Once
copied or rewritten, this code is maintained independently; the reference
checkout is not an upstream dependency.

The local source-budget command verifies the pinned checkout commit, recomputes
every copied-symbol hash, and requires the manifest to agree in both directions
with the `copy` classifications in `third_party/serena_source_census.json`.
