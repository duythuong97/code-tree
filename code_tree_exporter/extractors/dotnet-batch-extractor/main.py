#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.env_loader import load_dotenv


def _packaged_extractor_command(
    dotnet: str, config_path: Path
) -> tuple[list[str], int]:
    project = Path(__file__).with_name("DotNetBatchExtractor.csproj")
    assembly = (
        project.parent
        / "bin"
        / "Release"
        / "net9.0"
        / "DotNetBatchExtractor.dll"
    )
    sources = [
        project,
        *project.parent.glob("*.cs"),
        *project.parent.parent.joinpath("_roslyn").glob("*.cs"),
    ]
    needs_build = not assembly.is_file() or any(
        source.stat().st_mtime_ns > assembly.stat().st_mtime_ns
        for source in sources
        if source.is_file()
    )
    if needs_build:
        result = subprocess.run(
            [dotnet, "build", str(project), "-c", "Release", "--nologo"],
            env=os.environ | {"CODE_TREE_PYTHON": sys.executable},
        )
        if result.returncode:
            return [], result.returncode
    return [dotnet, str(assembly), "--config", str(config_path)], 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract .NET batch executables, command modes, and data access into a CSV graph package."
    )
    parser.add_argument("--config", required=True)
    config_path = Path(parser.parse_args().config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
    command, build_status = _packaged_extractor_command(dotnet, config_path)
    if build_status:
        return build_status
    return subprocess.run(
        command,
        env=os.environ | {"CODE_TREE_PYTHON": sys.executable},
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
