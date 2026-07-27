from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from .comments import extract_comments
from .contract.graph_contract import normalize_repository_path
from .decoding import SourceDecodingError, decode_source, encoding_for
from .env_loader import load_dotenv
from .extractors import ExtractorSpec, extractor_spec
from .graph_package import GraphPackage, SourceRecord, replace_directory
from .renderers import render_file_tree, render_system_tree

_BLOCKED_DIRECTORIES = frozenset(
    {
        ".git",
        ".angular",
        ".cache",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "bin",
        "build",
        "dist",
        "generated",
        "node_modules",
        "obj",
        "venv",
        "__pycache__",
    }
)


def run_pipeline(config_path: Path) -> Path:
    config_path = config_path.expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    config = _load_config(config_path)
    root = _absolute_directory(config, "root")
    output = _absolute_path(config, "output")
    if output == root or root in output.parents:
        raise ValueError("output must be outside root")
    sources = config.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources must be a non-empty array")
    default_encoding = str(config.get("defaultEncoding", "auto"))
    graph = GraphPackage()
    staging_parent = output.parent
    staging_parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(
        prefix=f".{output.name}-staging-", dir=staging_parent
    ) as temporary:
        temporary_root = Path(temporary)
        packages_root = temporary_root / "packages"
        prepared_root = temporary_root / "sources"
        packages_root.mkdir()
        prepared_root.mkdir()
        for index, raw_source in enumerate(sources):
            source = _validate_source(raw_source, index)
            source_type = str(source["type"])
            spec = extractor_spec(source_type)
            source_name = str(source["name"])
            source_records, valid_paths = _prepare_source(
                root, prepared_root / source_name, source, spec, default_encoding, graph
            )
            if spec.config_type == "angular":
                _copy_angular_metadata(root, prepared_root / source_name, source)
            for record in source_records:
                graph.add_source(record)
            if not valid_paths:
                graph.add_issue(
                    "PARSE_ERROR",
                    f"No decodable supported files for source {source_name}",
                    severity="WARNING",
                )
                continue
            package_output = packages_root / source_name
            extractor_config = _extractor_config(
                config,
                source,
                spec,
                prepared_root / source_name,
                package_output,
                valid_paths,
            )
            extractor_config_path = temporary_root / f"{source_name}.json"
            extractor_config_path.write_text(
                json.dumps(extractor_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            _run_extractor(spec, extractor_config_path, extractor_config, graph)
            if package_output.exists():
                graph.merge_directory(package_output)

        graph.resolve_routine_references()
        graph.materialize_structure()
        final_staging = output.with_name(output.name + ".new")
        if final_staging.exists():
            shutil.rmtree(final_staging)
        final_staging.mkdir(parents=True)
        graph.write(
            final_staging,
            source_name=str(config.get("name") or root.name),
            config_path=str(config_path),
            max_csv_rows=int(config.get("maxCsvRows", 1_000_000)),
        )
        render_file_tree(graph, final_staging / "file-trees")
        render_system_tree(graph, final_staging / "SYSTEM_TREE.md")
        replace_directory(final_staging, output)
    return output


def _load_config(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"Config does not exist: {path}")
    raw = os.path.expandvars(path.read_text(encoding="utf-8"))
    if "${" in raw:
        raise ValueError("Config contains unresolved environment variables")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Config root must be an object")
    owners = [value, *(item for item in value.get("sources", []) if isinstance(item, dict))]
    for owner in owners:
        for key in ("root", "output", "inputData"):
            configured = owner.get(key)
            if isinstance(configured, str) and configured:
                candidate = Path(configured).expanduser()
                owner[key] = str((candidate if candidate.is_absolute() else path.parent / candidate).resolve())
    return value


def _absolute_directory(config: dict, key: str) -> Path:
    path = _absolute_path(config, key)
    if not path.is_dir():
        raise ValueError(f"{key} must be an existing directory: {path}")
    return path


def _absolute_path(config: dict, key: str) -> Path:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{key} must be absolute: {path}")
    return path.resolve()


def _validate_source(value: object, index: int) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"sources[{index}] must be an object")
    for key in ("name", "type", "folders"):
        if key not in value:
            raise ValueError(f"sources[{index}].{key} is required")
    if not isinstance(value["folders"], list) or not value["folders"]:
        raise ValueError(f"sources[{index}].folders must be a non-empty array")
    name = str(value["name"])
    invalid_name_chars = '<>:"/\\|?*'
    reserved_names = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if name in {"", ".", ".."} or any(char in name for char in invalid_name_chars) or name.rstrip(". ").upper() in reserved_names:
        raise ValueError(f"sources[{index}].name is unsafe: {name!r}")
    return value


def _prepare_source(
    root: Path,
    staging: Path,
    source: dict,
    spec: ExtractorSpec,
    default_encoding: str,
    graph: GraphPackage,
) -> tuple[list[SourceRecord], list[str]]:
    selected = _selected_files(root, source["folders"], spec.suffixes)
    records: list[SourceRecord] = []
    valid_paths: list[str] = []
    preserve_comments = source.get("preserveComments", True) is not False
    for path in selected:
        relative = path.relative_to(root).as_posix()
        try:
            decoded = decode_source(
                path, relative, encoding_for(relative, source, default_encoding)
            )
        except SourceDecodingError as exc:
            graph.add_issue(
                exc.issue_type,
                str(exc),
                source_path=exc.path,
                properties=exc.properties,
            )
            continue
        destination = staging / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(decoded.text, encoding="utf-8", newline="")
        comments = (
            tuple(extract_comments(decoded.text, relative, str(source["type"])))
            if preserve_comments
            else ()
        )
        records.append(
            SourceRecord(
                source_key=str(source["name"]),
                source_type=str(source["type"]),
                system_key=str(source.get("system") or source["name"]),
                repository_key=str(source.get("repository") or source["name"]),
                relative_path=relative,
                declared_encoding=decoded.declared_encoding,
                actual_encoding=decoded.actual_encoding,
                raw_sha256=decoded.raw_sha256,
                text_sha256=decoded.text_sha256,
                newline_style=decoded.newline_style,
                bom=decoded.bom,
                comments=comments,
            )
        )
        valid_paths.append(relative)
    return records, valid_paths


def _selected_files(root: Path, folders: list, suffixes: tuple[str, ...]) -> list[Path]:
    suffix_set = {value.lower() for value in suffixes}
    result: set[Path] = set()
    for raw in folders:
        folder = raw.get("path", ".") if isinstance(raw, dict) else raw
        if not isinstance(folder, str) or not folder:
            raise ValueError("folder path must be a non-empty string")
        normalized = "." if folder == "." else normalize_repository_path(folder)
        candidate = root.joinpath(*PurePosixPath(normalized).parts).resolve()
        if candidate != root and root not in candidate.parents:
            raise ValueError(f"folder escapes root: {folder}")
        if candidate.is_symlink():
            raise ValueError(f"folder symlink is not allowed: {folder}")
        paths = (
            [candidate]
            if candidate.is_file()
            else candidate.rglob("*") if candidate.is_dir() else []
        )
        for path in paths:
            resolved = path.resolve()
            if resolved != root and root not in resolved.parents:
                raise ValueError(f"source file escapes root: {path}")
            if (
                path.is_file()
                and path.suffix.lower() in suffix_set
                and not (_BLOCKED_DIRECTORIES & {part.lower() for part in path.parts})
            ):
                result.add(path)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _copy_angular_metadata(root: Path, staging: Path, source: dict) -> None:
    configured = {"angular.json", "tsconfig.json", "tsconfig.app.json"}
    for key in ("appConfig", "tsconfig", "tsConfig"):
        value = source.get(key)
        if isinstance(value, str) and value:
            configured.add(value)
    for relative in configured:
        candidate = (root / relative).resolve()
        if candidate.is_file() and (candidate == root or root in candidate.parents):
            destination = staging / candidate.relative_to(root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, destination)


def _extractor_config(
    global_config: dict,
    source: dict,
    spec: ExtractorSpec,
    staging_root: Path,
    output: Path,
    valid_paths: list[str],
) -> dict:
    result = {
        key: value
        for key, value in source.items()
        if key not in {"name", "encoding", "encodingOverrides", "preserveComments"}
    }
    result.update(
        {
            "type": spec.config_type,
            "source": str(source["name"]),
            "root": str(staging_root.resolve()),
            "output": str(output.resolve()),
            "repository": str(source.get("repository") or source["name"]),
            "system": str(source.get("system") or source["name"]),
            "files": valid_paths,
            "inputData": str(
                Path(
                    source.get("inputData")
                    or global_config.get("inputData")
                    or staging_root
                ).resolve()
            ),
        }
    )
    result["folders"] = _staged_folders(source["folders"])
    if spec.config_type == "angular":
        result.setdefault("appConfig", "")
        result["_typescriptPath"] = _typescript_path(Path(global_config["root"]))
    return result


def _staged_folders(folders: list) -> list:
    result = []
    for item in folders:
        if isinstance(item, dict):
            result.append({**item, "path": str(item.get("path", "."))})
        else:
            result.append(item)
    return result


def _run_extractor(
    spec: ExtractorSpec, config_path: Path, config: dict, graph: GraphPackage
) -> None:
    if spec.config_type == "xml-sql":
        from .extractors.xml_sql.runner import extract

        extract(config)
        return
    env = os.environ.copy()
    command: list[str]
    if spec.config_type == "angular":
        node = env.get("CODE_TREE_NODE") or shutil.which("node")
        if node:
            command = [node, str(spec.script), "--config", str(config_path)]
            env.setdefault(
                "TYPESCRIPT_PATH", str(config.get("_typescriptPath") or "typescript")
            )
        else:
            command = [sys.executable, str(spec.script.with_name("main.py")), "--config", str(config_path)]
            env["CODEMAP_USE_LEGACY_SCANNERS"] = "1"
    else:
        command = [sys.executable, str(spec.script), "--config", str(config_path)]
    try:
        completed = subprocess.run(command, env=env, text=True, capture_output=True)
    except FileNotFoundError as exc:
        if spec.config_type != "angular":
            graph.add_issue("PARSE_ERROR", f"{spec.config_type} extractor dependency missing: {exc}")
            return
        completed = subprocess.CompletedProcess(command, 127, "", str(exc))
    if completed.returncode and spec.config_type == "angular":
        fallback = spec.script.with_name("main.py")
        completed = subprocess.run(
            [sys.executable, str(fallback), "--config", str(config_path)],
            env={**env, "CODEMAP_USE_LEGACY_SCANNERS": "1"},
            text=True,
            capture_output=True,
        )
    if completed.returncode:
        message = (completed.stderr or completed.stdout or "Extractor failed").strip()
        graph.add_issue(
            "PARSE_ERROR", f"{spec.config_type} extractor failed: {message}"
        )


def _typescript_path(root: Path) -> str:
    candidates = [
        root / "node_modules" / "typescript",
        Path(__file__).resolve().parent
        / "extractors"
        / "angular-extractor"
        / "node_modules"
        / "typescript",
    ]
    candidates.extend(
        path.parent for path in root.glob("*/node_modules/typescript/package.json")
    )
    candidates.extend(
        path.parent for path in root.glob("*/*/node_modules/typescript/package.json")
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "typescript"
