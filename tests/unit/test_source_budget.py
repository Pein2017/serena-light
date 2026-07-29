from pathlib import Path

from serena_light.source_budget import inspect_census, inspect_import_boundary, repository_root


def test_source_census_passes_budget() -> None:
    report = inspect_census(repository_root())
    assert report["status"] == "pass"
    assert report["expected_production_lines"] <= report["maximum_production_lines"]
    assert report["forbidden_imports"] == []


def test_import_boundary_reports_upstream_runtime_dependencies(tmp_path: Path) -> None:
    package = tmp_path / "src" / "serena_light"
    package.mkdir(parents=True)
    (package / "bad.py").write_text("import serena.agent\nfrom solidlsp import ls\n", encoding="utf-8")

    assert inspect_import_boundary(tmp_path) == [
        "src/serena_light/bad.py:1:serena.agent",
        "src/serena_light/bad.py:2:solidlsp",
    ]
