from pathlib import Path

import serena_light.source_budget as source_budget
from serena_light.source_budget import inspect_census, inspect_import_boundary, repository_root


def test_source_census_passes_budget() -> None:
    report = inspect_census(repository_root())
    assert report["status"] == "pass"
    assert report["maximum_production_lines"] is None
    assert report["forbidden_imports"] == []
    assert report["dependency_boundary"]["undeclared_external_imports"] == []
    assert report["copied_source_hashes_verified"] == 9
    assert report["census_manifest_agreement"] is True


def test_production_lines_are_informational_not_a_gate(monkeypatch) -> None:
    monkeypatch.setattr(source_budget, "_production_lines", lambda _root: 1_000_000)

    report = inspect_census(repository_root())

    assert report["current_local_production_lines"] == 1_000_000
    assert report["maximum_production_lines"] is None
    assert report["status"] == "pass"


def test_import_boundary_reports_upstream_runtime_dependencies(tmp_path: Path) -> None:
    package = tmp_path / "src" / "serena_light"
    package.mkdir(parents=True)
    (package / "bad.py").write_text("import serena.agent\nfrom solidlsp import ls\n", encoding="utf-8")

    assert inspect_import_boundary(tmp_path) == [
        "src/serena_light/bad.py:1:serena.agent",
        "src/serena_light/bad.py:2:solidlsp",
    ]
