#!/usr/bin/env python3
"""Print shell assignments for the current external acceptance snapshots."""

from __future__ import annotations

import shlex
from pathlib import Path

from external_snapshot import (
    CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE,
    DEFAULT_SNAPSHOT_PROFILE,
    snapshot_identity,
)


def main() -> None:
    profiles = {
        "SERENA_LIGHT_CC_PLUGIN_CODEX_SNAPSHOT": (
            Path("/data/CoordExp/cc-plugin-codex"),
            CC_PLUGIN_CODEX_TYPESCRIPT_AUTHORITY_PROFILE,
        ),
        "SERENA_LIGHT_COORDEXP_SNAPSHOT": (Path("/data/CoordExp"), DEFAULT_SNAPSHOT_PROFILE),
        "SERENA_LIGHT_MS_SWIFT_SNAPSHOT": (Path("/data/ms-swift"), DEFAULT_SNAPSHOT_PROFILE),
        "SERENA_LIGHT_TRANSFORMERS_SNAPSHOT": (
            Path("/root/miniconda3/envs/ms/lib/python3.12/site-packages/transformers"),
            DEFAULT_SNAPSHOT_PROFILE,
        ),
    }
    for name, (root, profile) in profiles.items():
        print(f"export {name}={shlex.quote(snapshot_identity(root, profile=profile))}")


if __name__ == "__main__":
    main()
