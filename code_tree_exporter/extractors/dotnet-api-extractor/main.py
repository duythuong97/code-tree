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
    project = Path(__file__).with_name("DotNetApiExtractor.csproj")
    assembly = (
        project.parent / "bin" / "Release" / "net9.0" / "DotNetApiExtractor.dll"
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
        command, build_status = _packaged_extractor_command(dotnet, config_path)
        if build_status:
            return build_status
    return subprocess.run(
        command,
        env=os.environ | {"CODE_TREE_PYTHON": sys.executable},
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
