import json
from pathlib import Path

import pytest


@pytest.mark.parametrize("engine", ["python", "typescript"])
def test_initialize_transcript_is_bounded_and_records_position_encoding(engine: str) -> None:
    path = Path(__file__).parent / "fixtures" / "initialize" / f"{engine}.json"
    payload = json.loads(path.read_text())
    assert path.stat().st_size < 64_000
    assert payload["request"]["capabilities"]["general"]["positionEncodings"] == [
        "utf-16",
        "utf-8",
        "utf-32",
    ]
    assert payload["selected_position_encoding"] in {"utf-8", "utf-16", "utf-32"}
    assert payload["position_encoding_source"]
    assert len(payload["lock_digest"]) == 64
    assert payload["engine_path"].startswith("/data/CoordExp/.codex/runtime/serena-light/deps/")
