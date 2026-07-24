#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from contract.graph_contract import api_operation_id, stable_node_id, table_id
from env_loader import load_dotenv
from extractors.package_support.csharp_scan import csharp_literals, endpoints, is_stored_procedure_literal, source_line
from extractors.package_support.package_writer import Catalog, PackageBuilder, configured_files, leaf_identifier, line_for_offset, line_text, load_config, slug, unresolved_id
from extractors.package_support.sql_analyzer import analyze_sql

_VERSION = "1.0.0"
_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_]\w*)")
_CALL_HINT_RE = re.compile(r"_([A-Za-z_]\w*)\.([A-Za-z_]\w*)\s*\(")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract .NET API routes and data access into a CSV graph package.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    config_path = Path(args.config).expanduser().resolve()
    load_dotenv(config_path.parent / ".env", Path.cwd() / ".env")
    if os.environ.get("CODEMAP_USE_LEGACY_SCANNERS") != "1":
        dotnet = os.environ.get("CODE_TREE_DOTNET") or shutil.which("dotnet")
        if not dotnet:
            parser.error(".NET SDK not found; set CODE_TREE_DOTNET or install dotnet")
        csproj = Path(__file__).with_name("DotNetApiExtractor.csproj")
        completed = subprocess.run([dotnet, "run", "--project", str(csproj), "--", "--config", str(config_path)])
        return completed.returncode
    extract(load_config(Path(args.config).expanduser()))
    return 0


def extract(config: dict) -> None:
    if config.get("type") != "dotnet-api":
        raise ValueError("Config type must be dotnet-api")
    source = config["source"]
    application = config.get("application", source)
    repository = config.get("repository", source)
    database = config["database"]
    system_key = config.get("system", application)
    output = Path(config["output"]).resolve()
    input_root = Path(config["inputData"]).resolve()
    catalog = Catalog.load(input_root, database)

    files = configured_files(config, [".cs"])
    builder = PackageBuilder(f"dotnet-api-{source}", f"extractor:dotnet-api/{source}", "dotnet-api-extractor", _VERSION, {"source": source, "technology": "Roslyn SemanticModel compatible scanner"})
    builder.files_scanned = len(files)

    app_id = stable_node_id("api-application", application)
    builder.add_node(app_id, "API_APPLICATION", application, application, application, system_key=system_key, database_key=database, repository_key=repository)

    sln_id = stable_node_id("dotnet-solution", repository, f"{application}.sln")
    project_id = stable_node_id("dotnet-project", repository, f"src/{application}/{application}.csproj")
    builder.add_node(sln_id, "DOTNET_SOLUTION", f"{application}.sln", f"{repository}/{application}.sln", f"{application} Solution", system_key=system_key, repository_key=repository, graph_role="TECHNICAL")
    builder.add_node(project_id, "DOTNET_PROJECT", f"{application}.csproj", f"{repository}/src/{application}/{application}.csproj", f"{application} Project", system_key=system_key, repository_key=repository, graph_role="TECHNICAL")
    builder.add_edge(sln_id, project_id, "CONTAINS", graph_layer="STRUCTURAL")

    controller_ids: dict[str, str] = {}
    service_ids: dict[str, str] = {}
    repository_ids: dict[str, str] = {}

    for file in files:
        for cls in _CLASS_RE.finditer(file.text):
            name = cls.group(1)
            if name.endswith("Controller"):
                controller_ids[name] = stable_node_id("controller", application, name)
            elif name.endswith("Service"):
                service_ids[name] = stable_node_id("service", application, name)
            elif name.endswith("Repository"):
                repository_ids[name] = stable_node_id("repository", application, name)

    for name, node_id in controller_ids.items():
        builder.add_node(node_id, "CONTROLLER", name, f"{application}.{name}", name, system_key=system_key, database_key=database, repository_key=repository, graph_role="TECHNICAL")
    for name, node_id in service_ids.items():
        builder.add_node(node_id, "SERVICE", name, f"{application}.{name}", name, system_key=system_key, database_key=database, repository_key=repository, graph_role="TECHNICAL")
    for name, node_id in repository_ids.items():
        builder.add_node(node_id, "REPOSITORY", name, f"{application}.{name}", name, system_key=system_key, database_key=database, repository_key=repository, graph_role="TECHNICAL")
        builder.add_edge(project_id, node_id, "PROJECT_REFERENCE", graph_layer="STRUCTURAL")

    for file in files:
        for endpoint in endpoints(file.text):
            operation_id = api_operation_id(application, endpoint.method, endpoint.route)
            builder.add_node(operation_id, "API_OPERATION", f"{endpoint.method} {endpoint.route}", f"{application}.{endpoint.method}.{endpoint.route}", f"{endpoint.action} API", system_key=system_key, database_key=database, repository_key=repository, properties={"method": endpoint.method, "route": endpoint.route})
            controller_id = controller_ids.setdefault(endpoint.controller, stable_node_id("controller", application, endpoint.controller))
            builder.add_edge(app_id, operation_id, "CONTAINS", graph_layer="STRUCTURAL")
            edge_id = builder.add_edge(operation_id, controller_id, "HANDLED_BY")
            line = line_for_offset(file.text, endpoint.offset)
            builder.add_evidence("EDGE", edge_id, file.relative, line, line, "API_ACTION", line_text(file.text, line))

    # Conservative reachable chain: endpoints -> controller -> reachable service -> data repository.
    for controller_id in controller_ids.values():
        for service_id in service_ids.values():
            builder.add_edge(controller_id, service_id, "CALLS")
    for service_id in service_ids.values():
        for repo_id in repository_ids.values():
            edge_id = builder.add_edge(service_id, repo_id, "CALLS")
            _add_first_evidence(builder, files, edge_id, "CALL", "repository")

    for file in files:
        for literal in csharp_literals(file.text):
            owner = literal.owner_class
            owner_id = repository_ids.get(owner) or service_ids.get(owner) or controller_ids.get(owner)
            if not owner_id:
                continue
            line, snippet = source_line(file.text, literal.start)
            if is_stored_procedure_literal(file.text, literal):
                raw = literal.value.strip().upper()
                proc_key = ".".join(raw.split(".")[-2:]) if "." in raw else raw
                parts = proc_key.split(".")
                target = unresolved_id(database, f"{application}:{raw}")
                builder.add_node(target, "UNRESOLVED_REFERENCE", raw, raw, raw, system_key=system_key, database_key=database, repository_key=repository, graph_role="TECHNICAL", properties={"database": database, "package": parts[-2] if len(parts) > 1 else "", "routine": parts[-1], "raw_reference": raw})
                edge_id = builder.add_edge(owner_id, target, "CALLS", raw_operation=parts[-1])
                builder.add_evidence("EDGE", edge_id, file.relative, line, line, "STORED_PROCEDURE", snippet)
                continue

            analysis = analyze_sql(literal.value)
            for ref in analysis.tables:
                table_name = leaf_identifier(ref.object_name)
                if not catalog.has_table(database, table_name):
                    builder.add_issue("TABLE_NOT_IMPORTED", "ERROR", "Table is absent from authoritative catalog", source_node_id=owner_id, raw_reference=table_name, database_key=database, source_path=file.relative, start_line=line)
                    continue
                target = table_id(database, table_name)
                edge_id = builder.add_edge(owner_id, target, ref.edge_type, raw_operation=ref.operation)
                builder.add_evidence("EDGE", edge_id, file.relative, line, line, "SQL", snippet)
                _column_issues(builder, catalog, database, table_name, literal.value, owner_id, file.relative, line)
            for offset in analysis.dynamic_offsets:
                builder.add_issue("DYNAMIC_SQL", "WARNING", "Runtime SQL target cannot be resolved", source_node_id=owner_id, raw_reference="dynamic SQL", database_key=database, source_path=file.relative, start_line=line)


    builder.write(output)


def _column_issues(builder: PackageBuilder, catalog: Catalog, database: str, table_name: str, sql: str, owner_id: str, source_path: str, line: int) -> None:
    pattern = re.compile(rf"\b{re.escape(table_name)}\.([A-Za-z_]\w*)\b", re.IGNORECASE)
    for match in pattern.finditer(sql):
        column = match.group(1).upper()
        if not catalog.has_column(database, table_name, column):
            builder.add_issue("COLUMN_NOT_IMPORTED", "WARNING", "Column is absent from authoritative catalog", source_node_id=owner_id, raw_reference=f"{table_name}.{column}", database_key=database, source_path=source_path, start_line=line)








def _add_first_evidence(builder: PackageBuilder, files, edge_id: str, kind: str, token: str) -> None:
    for file in files:
        for line_number, line in enumerate(file.text.splitlines(), 1):
            if token.lower() in line.lower():
                builder.add_evidence("EDGE", edge_id, file.relative, line_number, line_number, kind, line)
                return


if __name__ == "__main__":
    raise SystemExit(main())
