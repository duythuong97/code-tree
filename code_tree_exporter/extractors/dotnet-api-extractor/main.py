#!/usr/bin/env python3
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.extractors.dotnet_runner import run_dotnet_extractor


def main() -> int:
    return run_dotnet_extractor(
        script_path=Path(__file__),
        project_name="DotNetApiExtractor.csproj",
        description="Extract .NET API routes and data access into a graph package.",
    )


if __name__ == "__main__":
    raise SystemExit(main())
