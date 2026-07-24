#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from contract.graph_contract import stable_node_id, table_id
from env_loader import load_dotenv
from extractors.package_support.csharp_scan import (
    csharp_literals,
    display_from_identifier,
    is_stored_procedure_literal,
    source_line,
)
from extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    configured_files,
    leaf_identifier,
    line_for_offset,
    line_text,
    load_config,
    slug,
    unresolved_id,
)
from extractors.package_support.sql_analyzer import analyze_sql

_VERSION = "1.0.0"
_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
_MODE_RE = re.compile(r"mode\s*==\s*\"([^\"]+)\"")
_MAIN_RE = re.compile(r"\bMain\s*\(")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract .NET batch executables, command modes, and data access into a CSV graph package."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
    if not dotnet:
        parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
    csproj = Path(__file__).with_name("DotNetBatchExtractor.csproj")
    completed = subprocess.run(
        [
            dotnet,
            "run",
            "--project",
            str(csproj),
            "--",
            "--config",
            str(config_path),
        ]
    )
    return completed.returncode


def extract(config: dict) -> None:
    """Deprecated compatibility entry point; use the Roslyn CLI for project discovery."""
    raise RuntimeError(
        "Legacy .NET scanning is unsupported; run main() with the .NET SDK"
    )


def _legacy_extract(config: dict) -> None:
    if config.get("type") != "dotnet-batch":
        raise ValueError("Config type must be dotnet-batch")
    source = config["source"]
    repository = config.get("repository", source)
    scope = config.get("executableScope", "batch-system")
    system_key = config.get("system", scope)
    database = config.get("database") or _first_folder_database(config) or ""
    input_root = Path(config["inputData"]).resolve()
    output = Path(config["output"]).resolve()
    catalog = (
        Catalog.load(input_root, database) if database else Catalog.load(input_root)
    )

    files = configured_files(config, [".cs", ".csproj"])
    builder = PackageBuilder(
        f"dotnet-batch-{source}",
        f"extractor:dotnet-batch/{source}",
        "dotnet-batch-extractor",
        _VERSION,
        {"source": source, "technology": "Roslyn Workspace compatible scanner"},
    )
    builder.files_scanned = len(files)

    executable_names = [f"{slug(source)}.exe"]
    executable_ids = {}
    for name in executable_names:
        exe_name = name if name.endswith(".exe") else f"{name}.exe"
        exe_id = stable_node_id("executable", scope, exe_name.lower())
        executable_ids[exe_name.lower()] = exe_id
        builder.add_node(
            exe_id,
            "EXECUTABLE",
            exe_name.lower(),
            f"{scope}.{exe_name.lower()}",
            display_from_identifier(exe_name),
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )

    primary_exe = next(iter(executable_ids.values()))
    entry_id = stable_node_id("executable-entry", slug(source), "main")
    builder.add_node(
        entry_id,
        "EXECUTABLE_ENTRY_POINT",
        "Main",
        f"{source}.Program.Main",
        f"{source} Entry",
        system_key=system_key,
        database_key=database,
        repository_key=repository,
        graph_role="TECHNICAL",
    )
    builder.add_edge(primary_exe, entry_id, "ENTRY_IN")

    mode_ids = {}
    for mode in _modes(files) | set(config.get("commandModes", [])):
        mode_id = stable_node_id("command-mode", slug(source), slug(mode))
        mode_ids[mode] = mode_id
        builder.add_node(
            mode_id,
            "COMMAND_MODE",
            mode,
            f"{slug(source)}.{mode}",
            f"{mode.capitalize()} Mode",
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )
        builder.add_edge(entry_id, mode_id, "CALLS")

    repository_ids: dict[str, str] = {}
    for file in files:
        if file.absolute.suffix.lower() != ".cs":
            continue
        for cls in _CLASS_RE.finditer(file.text):
            name = cls.group(1)
            if name.endswith("Repository"):
                repo_id = stable_node_id("repository", repository, name)
                repository_ids[name] = repo_id
                builder.add_node(
                    repo_id,
                    "REPOSITORY",
                    name,
                    f"{repository}.{name}",
                    name,
                    system_key=system_key,
                    database_key=database,
                    repository_key=repository,
                    graph_role="TECHNICAL",
                )
                builder.add_edge(entry_id, repo_id, "CALLS")

    _job_edges(builder, config, input_root, executable_ids, scope)

    for file in files:
        if file.absolute.suffix.lower() != ".cs":
            continue
        for literal in csharp_literals(file.text):
            owner_id = repository_ids.get(literal.owner_class, entry_id)
            line, snippet = source_line(file.text, literal.start)
            if is_stored_procedure_literal(file.text, literal):
                raw = literal.value.strip().upper()
                proc_key = ".".join(raw.split(".")[-2:]) if "." in raw else raw
                parts = proc_key.split(".")
                target = unresolved_id(database, f"{source}:{raw}")
                builder.add_node(
                    target,
                    "UNRESOLVED_REFERENCE",
                    raw,
                    raw,
                    raw,
                    system_key=system_key,
                    database_key=database,
                    repository_key=repository,
                    graph_role="TECHNICAL",
                    properties={
                        "database": database,
                        "package": parts[-2] if len(parts) > 1 else "",
                        "routine": parts[-1],
                        "raw_reference": raw,
                    },
                )
                edge_id = builder.add_edge(
                    owner_id, target, "CALLS", raw_operation=parts[-1]
                )
                builder.add_evidence(
                    "EDGE",
                    edge_id,
                    file.relative,
                    line,
                    line,
                    "STORED_PROCEDURE",
                    snippet,
                )
                continue
            analysis = analyze_sql(literal.value)
            for ref in analysis.tables:
                table_name = leaf_identifier(ref.object_name)
                if not catalog.has_table(database, table_name):
                    builder.add_issue(
                        "TABLE_NOT_IMPORTED",
                        "ERROR",
                        "Table is absent from authoritative catalog",
                        source_node_id=owner_id,
                        raw_reference=table_name,
                        database_key=database,
                        source_path=file.relative,
                        start_line=line,
                    )
                    continue
                edge_id = builder.add_edge(
                    owner_id,
                    table_id(database, table_name),
                    ref.edge_type,
                    raw_operation=ref.operation,
                )
                builder.add_evidence(
                    "EDGE", edge_id, file.relative, line, line, "SQL", snippet
                )
            for seq in analysis.sequences:
                # Sequence nodes are technical and can be internal to the package.
                sequence_node = stable_node_id(
                    "sequence", database, leaf_identifier(seq.object_name)
                )
                builder.add_node(
                    sequence_node,
                    "SEQUENCE",
                    leaf_identifier(seq.object_name),
                    f"{database}.{leaf_identifier(seq.object_name)}",
                    leaf_identifier(seq.object_name),
                    system_key=system_key,
                    database_key=database,
                    repository_key=repository,
                    graph_role="TECHNICAL",
                )
                builder.add_edge(
                    owner_id, sequence_node, "USES", raw_operation="NEXTVAL"
                )
            for offset in analysis.dynamic_offsets:
                builder.add_issue(
                    "DYNAMIC_SQL",
                    "WARNING",
                    "Runtime SQL target cannot be resolved",
                    source_node_id=owner_id,
                    raw_reference="dynamic SQL",
                    database_key=database,
                    source_path=file.relative,
                    start_line=line_for_offset(file.text, offset),
                )

    builder.write(output)


def _first_folder_database(config: dict) -> str:
    for item in config.get("folders", []):
        if isinstance(item, dict) and item.get("database"):
            return item["database"]
    return ""


def _modes(files) -> set[str]:
    modes: set[str] = set()
    for file in files:
        for match in _MODE_RE.finditer(file.text):
            modes.add(match.group(1))
    return modes


def _job_edges(
    builder: PackageBuilder,
    config: dict,
    input_root: Path,
    executable_ids: dict[str, str],
    scope: str,
) -> None:
    jobnet = input_root / "jobnet.csv"
    mappings = _load_mappings(input_root / "executable-mappings.csv")
    if not jobnet.exists():
        return
    with jobnet.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            job_network = stable_node_id(
                "job-network", "batch-system", row["jobnet_id"]
            )
            job = stable_node_id("job", "batch-system", row["jobnet_id"], row["job_id"])
            # Source package references authoritative job nodes without declaring them.
            builder.add_edge(job_network, job, "CONTAINS", graph_layer="STRUCTURAL")
            if row.get("predecessor_job_id"):
                predecessor = stable_node_id(
                    "job", "batch-system", row["jobnet_id"], row["predecessor_job_id"]
                )
                builder.add_edge(
                    job, predecessor, "DEPENDS_ON", graph_layer="STRUCTURAL"
                )
            canonical = (
                mappings.get(("batch-system", scope, row["executable_name"]))
                or row["executable_name"].lower()
            )
            if not canonical.endswith(".exe"):
                canonical = f"{canonical}.exe"
            target = executable_ids.get(canonical.lower())
            if target:
                builder.add_edge(job, target, "STARTS")
            else:
                builder.add_issue(
                    "EXECUTABLE_NOT_MAPPED",
                    "WARNING",
                    "Executable not mapped",
                    source_node_id=job,
                    raw_reference=row["executable_name"],
                    database_key="",
                    source_path="input-data/jobnet.csv",
                    start_line=1,
                )


def _load_mappings(path: Path) -> dict[tuple[str, str, str], str]:
    result: dict[tuple[str, str, str], str] = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            result[
                (row["job_system"], row["executable_scope"], row["executable_name"])
            ] = row["canonical_executable_name"]
    return result


if __name__ == "__main__":
    raise SystemExit(main())
