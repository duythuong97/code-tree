from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree

from .comments import extract_comments
from .contract.graph_contract import normalize_repository_path
from .decoding import SourceDecodingError, decode_source, encoding_for
from .env_loader import load_dotenv
from .extractors import ExtractorSpec, extractor_spec
from .graph_package import (
    GraphPackage,
    SourceRecord,
    replace_directory,
    validate_output_directory,
)

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
    validated_sources = [
        _validate_source(source, index) for index, source in enumerate(sources)
    ]
    source_names = [str(source["name"]) for source in validated_sources]
    if len(source_names) != len(set(source_names)):
        raise ValueError("sources[].name must be unique")
    _validate_data_paths(config, validated_sources, output)
    validate_output_directory(output)
    default_encoding = str(config.get("defaultEncoding", "auto"))
    output_mode = str(config.get("outputMode", "flat")).strip().lower()
    if output_mode not in {"flat", "partitioned"}:
        raise ValueError("outputMode must be 'flat' or 'partitioned'")
    limits = _resolved_limits(config)
    graph = GraphPackage()
    for source in validated_sources:
        graph.register_source(
            str(source["name"]),
            str(source["type"]),
            str(source.get("system") or source["name"]),
            str(source.get("repository") or source["name"]),
        )
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
        for source in validated_sources:
            source_type = str(source["type"])
            spec = extractor_spec(source_type)
            source_name = str(source["name"])
            prepared_source_root = prepared_root / source_name
            source_records, valid_paths = _prepare_source(
                root,
                prepared_source_root,
                source,
                spec,
                default_encoding,
                graph,
                max_file_bytes=limits["maxFileBytes"],
            )
            if spec.config_type == "angular":
                _copy_angular_metadata(root, prepared_source_root, source)
            for record in source_records:
                graph.add_source(record)
            if not valid_paths:
                graph.add_issue(
                    "PARSE_ERROR",
                    f"No decodable supported files for source {source_name}",
                    severity="WARNING",
                    properties={"source_key": source_name},
                )
                continue
            package_output = packages_root / source_name
            semantic_project = spec.config_type in {
                "angular",
                "dotnet-api",
                "dotnet-batch",
            }
            has_legacy_encoding = any(
                record.actual_encoding.lower().replace("_", "-")
                not in {"utf-8", "utf-8-sig", "ascii"}
                for record in source_records
            )
            extractor_root = (
                root
                if semantic_project and not has_legacy_encoding
                else prepared_source_root
            )
            extractor_config = _extractor_config(
                config,
                source,
                spec,
                extractor_root,
                package_output,
                valid_paths,
            )
            extractor_config_path = temporary_root / f"{source_name}.json"
            extractor_config_path.write_text(
                json.dumps(extractor_config, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            api_operations_before = sum(
                node.get("node_type") == "API_OPERATION"
                for node in graph.nodes.values()
            )
            _run_extractor(spec, extractor_config_path, extractor_config, graph)
            if package_output.exists():
                graph.merge_directory(package_output)
            if (
                spec.config_type == "dotnet-api"
                and any(Path(path).suffix.lower() == ".cs" for path in valid_paths)
                and sum(
                    node.get("node_type") == "API_OPERATION"
                    for node in graph.nodes.values()
                )
                == api_operations_before
            ):
                graph.add_issue(
                    "NO_API_ENDPOINTS",
                    "C# parsed; no supported ASP.NET controller or minimal API endpoint recognized",
                    severity="WARNING",
                    source_path=next(
                        path
                        for path in valid_paths
                        if Path(path).suffix.lower() == ".cs"
                    ),
                )
            if spec.config_type in {"dotnet-api", "dotnet-batch"}:
                _run_dotnet_xml_companion(
                    extractor_config,
                    packages_root / f"{source_name}-xml",
                    valid_paths,
                    graph,
                )

        from .linker import run_linker

        run_linker(graph)
        graph.materialize_structure()
        final_staging = temporary_root / "final"
        final_staging.mkdir()
        graph.write_sqlite(
            final_staging,
            source_name=str(config.get("name") or root.name),
            config_path=str(config_path),
            output_mode=output_mode,
            combined_projection=bool(
                config.get("combinedProjection", output_mode == "flat")
            ),
            knowledge_chunking=(
                config.get("knowledgeChunking")
                if isinstance(config.get("knowledgeChunking"), dict)
                else {}
            ),
            max_tree_lines=limits["maxTreeLines"],
            max_evidence_snippet_chars=limits["maxEvidenceSnippetChars"],
            max_issues_per_type_per_file=limits["maxIssuesPerTypePerFile"],
        )
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
    owners = [
        value,
        *(item for item in value.get("sources", []) if isinstance(item, dict)),
    ]
    for owner in owners:
        for key in ("root", "output", "inputData"):
            configured = owner.get(key)
            if isinstance(configured, str) and configured:
                candidate = Path(configured).expanduser()
                owner[key] = str(
                    (
                        candidate
                        if candidate.is_absolute()
                        else path.parent / candidate
                    ).resolve()
                )
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


def _positive_int(config: dict, key: str, default: int, *, minimum: int = 1) -> int:
    value = config.get(key, default)
    if isinstance(value, bool):
        raise ValueError(f"{key} must be an integer >= {minimum}")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer >= {minimum}") from exc
    if result < minimum:
        raise ValueError(f"{key} must be an integer >= {minimum}")
    return result


def _resolved_limits(config: dict) -> dict[str, int]:
    configured = config.get("limits")
    limits = configured if isinstance(configured, dict) else {}

    def value(key: str, default: int, minimum: int = 1) -> int:
        owner = {key: limits.get(key, config.get(key, default))}
        return _positive_int(owner, key, default, minimum=minimum)

    return {
        "maxTreeLines": value("maxTreeLines", 20_000, 10),
        "maxFileBytes": value("maxFileBytes", 10 * 1024 * 1024, 1024),
        "maxEvidenceSnippetChars": value("maxEvidenceSnippetChars", 500),
        "maxIssuesPerTypePerFile": value("maxIssuesPerTypePerFile", 20),
        "extractorTimeoutSeconds": value("extractorTimeoutSeconds", 300),
        "projectTimeoutSeconds": value("projectTimeoutSeconds", 900),
        "maxWorkerProcesses": value("maxWorkerProcesses", 4),
    }


def _validate_data_paths(config: dict, sources: list[dict], output: Path) -> None:
    output = output.resolve()
    managed_paths = (output, output.with_name(output.name + ".previous"))
    owners = [
        (config, "inputData"),
        *(
            (source, f"sources[{index}].inputData")
            for index, source in enumerate(sources)
        ),
    ]
    for owner, label in owners:
        value = owner.get("inputData")
        if not isinstance(value, str) or not value:
            continue
        candidate = Path(value).resolve()
        if any(
            candidate == managed or managed in candidate.parents
            for managed in managed_paths
        ):
            raise ValueError(
                f"{label} must be outside output and output.previous: {candidate}"
            )


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
    reserved_names = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        name in {"", ".", ".."}
        or any(char in name for char in invalid_name_chars)
        or name.rstrip(". ").upper() in reserved_names
    ):
        raise ValueError(f"sources[{index}].name is unsafe: {name!r}")
    return value


def _prepare_source(
    root: Path,
    staging: Path,
    source: dict,
    spec: ExtractorSpec,
    default_encoding: str,
    graph: GraphPackage,
    *,
    max_file_bytes: int,
) -> tuple[list[SourceRecord], list[str]]:
    selected = _selected_files(root, source["folders"], spec.suffixes)
    if spec.config_type in {"dotnet-api", "dotnet-batch"}:
        selected = _expand_dotnet_selection(root, selected)
    records: list[SourceRecord] = []
    valid_paths: list[str] = []
    preserve_comments = source.get("preserveComments", True) is not False
    for path in selected:
        relative = path.relative_to(root).as_posix()
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            graph.add_issue(
                "PARSE_ERROR",
                f"Unable to inspect source file: {exc}",
                source_path=relative,
                properties={"source_key": str(source["name"])},
            )
            continue
        if file_size > max_file_bytes:
            graph.add_issue(
                "FILE_TOO_LARGE",
                f"Source file is {file_size} bytes; limit is {max_file_bytes}",
                source_path=relative,
                severity="WARNING",
                properties={
                    "source_key": str(source["name"]),
                    "bytes": file_size,
                    "max_file_bytes": max_file_bytes,
                },
            )
            continue
        try:
            decoded = decode_source(
                path, relative, encoding_for(relative, source, default_encoding)
            )
        except SourceDecodingError as exc:
            graph.add_issue(
                exc.issue_type,
                str(exc),
                source_path=exc.path,
                properties={
                    **exc.properties,
                    "source_key": str(source["name"]),
                },
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


def _expand_dotnet_selection(root: Path, selected: list[Path]) -> list[Path]:
    result = set(selected)
    for path in root.rglob("*"):
        is_msbuild_file = _safe_source_file(
            path, root, {".props", ".targets", ".proj", ".rsp"}
        )
        is_dotnet_config = (
            path.is_file()
            and path.name.lower() in {"global.json", "nuget.config"}
            and not (
                _BLOCKED_DIRECTORIES
                & {part.lower() for part in path.relative_to(root).parts}
            )
        )
        if is_msbuild_file or is_dotnet_config:
            result.add(path)
    projects: list[Path] = [
        path for path in selected if path.suffix.lower() == ".csproj"
    ]
    seen_projects: set[Path] = set()
    for solution in (path for path in selected if path.suffix.lower() == ".sln"):
        for match in re.finditer(
            r'Project\([^)]*\)\s*=\s*"[^"\r\n]+"\s*,\s*"(?P<path>[^"\r\n]+\.csproj)"',
            solution.read_text(encoding="utf-8"),
            re.IGNORECASE,
        ):
            projects.append(
                (solution.parent / match.group("path").replace("\\", "/")).resolve()
            )
    while projects:
        project = projects.pop()
        if project in seen_projects or not _safe_source_file(
            project, root, {".csproj"}
        ):
            continue
        seen_projects.add(project)
        result.add(project)
        for path in project.parent.rglob("*"):
            if _safe_source_file(
                path, root, {".cs", ".xml", ".config", ".json"}
            ):
                result.add(path)
        try:
            project_xml = ElementTree.fromstring(project.read_text(encoding="utf-8"))
        except (OSError, ElementTree.ParseError, UnicodeDecodeError):
            continue
        for element in project_xml.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            include = element.get("Include", "").strip()
            if not include or tag not in {"Compile", "ProjectReference"}:
                continue
            candidate = (project.parent / include.replace("\\", "/")).resolve()
            if tag == "ProjectReference":
                projects.append(candidate)
            elif _safe_source_file(candidate, root, {".cs"}):
                result.add(candidate)
    return sorted(result, key=lambda path: path.relative_to(root).as_posix())


def _safe_source_file(path: Path, root: Path, suffixes: set[str]) -> bool:
    resolved = path.resolve()
    return (
        resolved.is_file()
        and (resolved == root or root in resolved.parents)
        and resolved.suffix.lower() in suffixes
        and not (
            _BLOCKED_DIRECTORIES
            & {part.lower() for part in resolved.relative_to(root).parts}
        )
    )


def _copy_angular_metadata(root: Path, staging: Path, source: dict) -> None:
    root = root.resolve()
    pending: list[Path] = []
    for raw in source["folders"]:
        relative = raw.get("path", ".") if isinstance(raw, dict) else raw
        candidate = (root / str(relative)).resolve()
        current = candidate.parent if candidate.is_file() else candidate
        while current == root or root in current.parents:
            workspace_path = next(
                (
                    current / name
                    for name in (
                        "angular.json",
                        ".angular-cli.json",
                        "angular-cli.json",
                    )
                    if (current / name).is_file()
                ),
                None,
            )
            if workspace_path is not None:
                pending.extend(
                    (
                        workspace_path,
                        current / "package.json",
                        current / "tsconfig.json",
                        current / "tsconfig.app.json",
                    )
                )
                try:
                    workspace = json.loads(
                        workspace_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    workspace = {}
                projects = (
                    workspace.get("projects", {}) if isinstance(workspace, dict) else {}
                )
                for project in projects.values() if isinstance(projects, dict) else ():
                    if not isinstance(project, dict):
                        continue
                    targets = project.get("architect") or project.get("targets") or {}
                    for target in targets.values() if isinstance(targets, dict) else ():
                        options = (
                            target.get("options", {})
                            if isinstance(target, dict)
                            else {}
                        )
                        tsconfig = (
                            options.get("tsConfig")
                            if isinstance(options, dict)
                            else None
                        )
                        if isinstance(tsconfig, str):
                            pending.append((current / tsconfig).resolve())
                applications = (
                    workspace.get("apps", [])
                    if isinstance(workspace, dict)
                    else []
                )
                for application in (
                    applications if isinstance(applications, list) else ()
                ):
                    if not isinstance(application, dict):
                        continue
                    tsconfig = application.get("tsconfig")
                    if isinstance(tsconfig, str):
                        pending.append((current / tsconfig).resolve())
                break
            if current == root:
                break
            current = current.parent
    for key in ("appConfig", "tsconfig", "tsConfig"):
        value = source.get(key)
        if isinstance(value, str) and value:
            pending.append((root / value).resolve())

    copied: set[Path] = set()
    while pending:
        candidate = pending.pop().resolve()
        if (
            candidate in copied
            or not candidate.is_file()
            or not (candidate == root or root in candidate.parents)
        ):
            continue
        copied.add(candidate)
        destination = staging / candidate.relative_to(root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, destination)
        if (
            candidate.name.startswith("tsconfig")
            and candidate.suffix.lower() == ".json"
        ):
            try:
                metadata = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            extends = metadata.get("extends") if isinstance(metadata, dict) else None
            if isinstance(extends, str) and (
                extends.startswith(".") or Path(extends).is_absolute()
            ):
                parent = (candidate.parent / extends).resolve()
                pending.append(parent if parent.suffix else parent.with_suffix(".json"))


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
    result["limits"] = _resolved_limits(global_config)
    result["folders"] = _staged_folders(source["folders"])
    if spec.config_type == "angular":
        result.setdefault("appConfig", "")
        result["_typescriptPath"] = _typescript_path(Path(global_config["root"]))
    if spec.config_type in {"dotnet-api", "dotnet-batch"}:
        result["xmlMapperQueries"] = _xml_mapper_queries(staging_root, valid_paths)
    return result


def _staged_folders(folders: list) -> list:
    result = []
    for item in folders:
        if isinstance(item, dict):
            result.append({**item, "path": str(item.get("path", "."))})
        else:
            result.append(item)
    return result


def _run_dotnet_xml_companion(
    dotnet_config: dict,
    output: Path,
    valid_paths: list[str],
    graph: GraphPackage,
) -> None:
    root = Path(dotnet_config["root"])
    xml_paths = [
        path
        for path in valid_paths
        if Path(path).suffix.lower() == ".xml" and _is_sql_mapper(root / path)
    ]
    if not xml_paths:
        return
    from .extractors.xml_sql.runner import extract

    xml_config = {
        **dotnet_config,
        "type": "xml-sql",
        "output": str(output.resolve()),
        "folders": xml_paths,
        "files": xml_paths,
    }
    extract(xml_config)
    graph.merge_directory(output)


def _is_sql_mapper(path: Path) -> bool:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return False
    return (
        root.tag.rsplit("}", 1)[-1].lower() == "mapper"
        and bool(root.get("namespace", "").strip())
        and any(
            element.tag.rsplit("}", 1)[-1].lower()
            in {"select", "insert", "update", "delete", "merge", "statement"}
            for element in root.iter()
        )
    )


def _xml_mapper_queries(root: Path, paths: list[str]) -> list[str]:
    queries: set[str] = set()
    statement_tags = {"select", "insert", "update", "delete", "merge", "statement"}
    for relative in paths:
        path = root / relative
        if path.suffix.lower() != ".xml":
            continue
        try:
            mapper = ElementTree.parse(path).getroot()
        except (OSError, ElementTree.ParseError):
            continue
        if mapper.tag.rsplit("}", 1)[-1].lower() != "mapper":
            continue
        namespace = mapper.get("namespace", "").strip()
        if not namespace:
            continue
        for statement in mapper.iter():
            if statement.tag.rsplit("}", 1)[-1].lower() not in statement_tags:
                continue
            statement_id = statement.get("id", "").strip()
            if statement_id:
                queries.add(f"{namespace}.{statement_id}")
    return sorted(queries)


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
            command = [
                sys.executable,
                str(spec.script.with_name("main.py")),
                "--config",
                str(config_path),
            ]
            env["CODEMAP_USE_LEGACY_SCANNERS"] = "1"
    else:
        command = [sys.executable, str(spec.script), "--config", str(config_path)]
    limits = config.get("limits") if isinstance(config.get("limits"), dict) else {}
    timeout = int(limits.get("extractorTimeoutSeconds", 300))
    try:
        completed = subprocess.run(
            command,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        if spec.config_type != "angular":
            graph.add_issue(
                "PARSE_ERROR",
                f"{spec.config_type} extractor dependency missing: {exc}",
                properties={"source_key": str(config.get("source") or "")},
            )
            return
        completed = subprocess.CompletedProcess(command, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        graph.add_issue(
            "TIMEOUT",
            f"{spec.config_type} extractor exceeded {timeout} seconds",
            severity="WARNING",
            properties={
                "timeout_seconds": timeout,
                "source_key": str(config.get("source") or ""),
            },
        )
        return
    if completed.returncode and spec.config_type == "angular":
        primary_message = _bounded_subprocess_message(
            completed, "Angular TypeScript parser failed"
        )
        graph.add_issue(
            "SEMANTIC_TREE_UNAVAILABLE",
            f"Angular TypeScript parser unavailable; degraded regex fallback used: {primary_message}",
            severity="WARNING",
            properties={"source_key": str(config.get("source") or "")},
        )
        fallback = spec.script.with_name("main.py")
        if fallback.is_file():
            try:
                completed = subprocess.run(
                    [sys.executable, str(fallback), "--config", str(config_path)],
                    env={**env, "CODEMAP_USE_LEGACY_SCANNERS": "1"},
                    text=True,
                    capture_output=True,
                    timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                graph.add_issue(
                    "TIMEOUT",
                    f"Angular fallback extractor exceeded {timeout} seconds",
                    severity="WARNING",
                    properties={
                        "timeout_seconds": timeout,
                        "source_key": str(config.get("source") or ""),
                    },
                )
                return
    if completed.returncode:
        message = _bounded_subprocess_message(completed, "Extractor failed")
        graph.add_issue(
            "PARSE_ERROR",
            f"{spec.config_type} extractor failed: {message}",
            properties={"source_key": str(config.get("source") or "")},
        )


def _bounded_subprocess_message(
    completed: subprocess.CompletedProcess[str], fallback: str
) -> str:
    message = (completed.stderr or completed.stdout or fallback).strip()
    maximum = 16_000
    return message if len(message) <= maximum else message[: maximum - 1] + "…"


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
        path.parent for path in root.glob("**/node_modules/typescript/package.json")
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "typescript"
