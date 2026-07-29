from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from code_tree_exporter.env_loader import load_dotenv


def run_dotnet_extractor(
    *,
    script_path: Path,
    project_name: str,
    description: str,
    argv: list[str] | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", required=True)
    config_path = Path(parser.parse_args(argv).config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    config = _load_config(config_path)
    source_root = _config_root(config)

    dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
    environment = _extractor_environment(dotnet, source_root)

    project = script_path.with_name(project_name)
    assembly = project.parent / "bin" / "Release" / "net9.0" / (
        project.stem + ".dll"
    )
    sources = [
        project,
        *project.parent.glob("*.cs"),
        *project.parent.parent.joinpath("_roslyn").glob("*.cs"),
    ]
    if not assembly.is_file() or any(
        source.stat().st_mtime_ns > assembly.stat().st_mtime_ns
        for source in sources
        if source.is_file()
    ):
        build = subprocess.run(
            [dotnet, "build", str(project), "-c", "Release", "--nologo"],
            env=environment,
        )
        if build.returncode:
            return build.returncode

    return subprocess.run(
        [dotnet, str(assembly), "--config", str(config_path)],
        env=environment,
    ).returncode


def _load_config(path: Path) -> dict[str, object]:
    try:
        value = json.loads(os.path.expandvars(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _config_root(config: dict[str, object]) -> Path | None:
    value = config.get("root")
    if not isinstance(value, str) or not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_dir() else None


def _extractor_environment(
    dotnet: str, source_root: Path | None
) -> dict[str, str]:
    resolved = Path(dotnet).expanduser().resolve()
    dotnet_root = resolved.parent if resolved.name.lower().startswith("dotnet") else None
    environment = os.environ | {"CODE_TREE_PYTHON": sys.executable}
    if dotnet_root is not None:
        environment.setdefault("DOTNET_ROOT", str(dotnet_root))
        environment.setdefault("DOTNET_HOST_PATH", str(resolved))
        environment.setdefault("DOTNET_MSBUILD_SDK_RESOLVER_CLI_DIR", str(dotnet_root))
        current_path = environment.get("PATH", "")
        environment["PATH"] = str(dotnet_root) + os.pathsep + current_path
    if source_root is not None:
        sdk_major = _selected_sdk_major(dotnet, source_root, environment)
        if sdk_major is not None and sdk_major > 9:
            # The packaged worker targets net9.0. Run it on the selected newer
            # runtime so MSBuildLocator can discover the source SDK.
            environment.setdefault("DOTNET_ROLL_FORWARD", "LatestMajor")
    return environment


def _selected_sdk_major(
    dotnet: str, source_root: Path, environment: dict[str, str]
) -> int | None:
    try:
        result = subprocess.run(
            [dotnet, "--version"],
            cwd=source_root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode:
        return None
    value = result.stdout.strip().split(".", 1)[0]
    try:
        return int(value)
    except ValueError:
        return None
