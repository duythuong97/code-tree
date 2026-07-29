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
    return subprocess.run(
        [dotnet, "run", "--project", str(Path(__file__).with_name("DotNetBatchExtractor.csproj")), "--", "--config", str(config_path)],
        env=os.environ | {"CODE_TREE_PYTHON": sys.executable},
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
