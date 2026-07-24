from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ExtractorSpec:
    config_type: str
    script: Path
    suffixes: tuple[str, ...]
    requires_typescript: bool = False


_ROOT = Path(__file__).resolve()
_SPECS = {
    "angular": ExtractorSpec(
        "angular",
        _ROOT.parent / "angular-extractor" / "main.mjs",
        (".ts", ".html"),
        True,
    ),
    "dotnet": ExtractorSpec(
        "dotnet-api",
        _ROOT.parent / "dotnet-api-extractor" / "main.py",
        (".cs", ".csproj", ".sln"),
    ),
    "dotnet-api": ExtractorSpec(
        "dotnet-api",
        _ROOT.parent / "dotnet-api-extractor" / "main.py",
        (".cs", ".csproj", ".sln"),
    ),
    "dotnet-batch": ExtractorSpec(
        "dotnet-batch",
        _ROOT.parent / "dotnet-batch-extractor" / "main.py",
        (".cs", ".csproj", ".sln"),
    ),
    "oracle-plsql": ExtractorSpec(
        "oracle-plsql",
        _ROOT.parent / "plsql-extractor" / "main.py",
        (".sql", ".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg"),
    ),
    "plsql": ExtractorSpec(
        "oracle-plsql",
        _ROOT.parent / "plsql-extractor" / "main.py",
        (".sql", ".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg"),
    ),
    "sql-files": ExtractorSpec(
        "sql-files", _ROOT.parent / "sql-file-extractor" / "main.py", (".sql", ".ctl")
    ),
    "sql-file": ExtractorSpec(
        "sql-files", _ROOT.parent / "sql-file-extractor" / "main.py", (".sql", ".ctl")
    ),
    "sql-loader": ExtractorSpec(
        "sql-files", _ROOT.parent / "sql-file-extractor" / "main.py", (".ctl",)
    ),
    "xml-sql": ExtractorSpec(
        "xml-sql", _ROOT.parent / "xml_sql" / "runner.py", (".xml",)
    ),
}


def extractor_spec(source_type: str) -> ExtractorSpec:
    try:
        return _SPECS[source_type]
    except KeyError as exc:
        raise ValueError(f"Unsupported source type: {source_type}") from exc


def supported_source_types() -> tuple[str, ...]:
    return tuple(sorted(_SPECS))
