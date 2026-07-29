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


def _worker_command(
    configured_worker: str, dotnet: str, config_path: Path
) -> list[str]:
    candidate = Path(configured_worker).expanduser()
    resolved = (
        str(candidate.resolve())
        if candidate.is_file()
        else shutil.which(configured_worker)
    )
    if not resolved:
        raise ValueError(
            "CODE_TREE_WINDOWS_WORKER must name an existing executable, "
            "Python script, or .NET DLL"
        )
    suffix = Path(resolved).suffix.lower()
    if suffix == ".dll":
        return [dotnet, resolved, "--config", str(config_path)]
    if suffix == ".py":
        return [sys.executable, resolved, "--config", str(config_path)]
    return [resolved, "--config", str(config_path)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract .NET API routes and data access into a CSV graph package.")
    parser.add_argument("--config", required=True)
    config_path = Path(parser.parse_args().config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
    configured_worker = os.environ.get("CODE_TREE_WINDOWS_WORKER", "").strip()
    if sys.platform == "win32" and configured_worker:
        try:
            command = _worker_command(configured_worker, dotnet, config_path)
        except ValueError as exc:
            parser.error(str(exc))
        print("[main.py] Running configured Windows MSBuild/Roslyn worker.")
    else:
        if sys.platform == "win32":
            print(
                "[main.py] Windows semantic worker is not configured; "
                "running packaged Roslyn syntax/config mode."
            )
        else:
            print("[main.py] Running packaged Roslyn extractor.")
        command = [
            dotnet,
            "run",
            "--project",
            str(Path(__file__).with_name("DotNetApiExtractor.csproj")),
            "--",
            "--config",
            str(config_path),
        ]
    return subprocess.run(
        command,
        env=os.environ | {"CODE_TREE_PYTHON": sys.executable},
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
