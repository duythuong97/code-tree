#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.contract.graph_contract import sql_file_id, table_id
from code_tree_exporter.extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    configured_files,
    database_link_id,
    external_db_object_id,
    leaf_identifier,
    line_for_offset,
    line_text,
    load_config,
    owner_identifier,
    sequence_id,
    stable_node_id,
    unresolved_id,
)
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql
from code_tree_exporter.extractors.package_support.semantic_tree import attach_sql_semantic_tree
from code_tree_exporter.extractors.package_support.sql_loader import extract_sql_loader

_VERSION = "1.0.0"
_INVALID_CONFIG_RE = re.compile(
    r"INVALID_CONFIG|TODO_CONFIG|missing\s+database\s+mapping|\$\{[^}]+\}",
    re.IGNORECASE,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract standalone Oracle SQL files into a CSV graph package."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    extract(load_config(Path(args.config).expanduser()))
    return 0


def extract(config: dict) -> None:
    if config.get("type") != "sql-files":
        raise ValueError("Config type must be sql-files")
    source = config["source"]
    repository = config.get("repository", source)
    default_database = config.get("database", "")
    schema = config.get("schema", "")
    system_key = config.get("system", default_database or source)
    input_root = Path(config["inputData"]).resolve()
    output = Path(config["output"]).resolve()
    has_folder_database = any(
        isinstance(item, dict) and item.get("database")
        for item in config.get("folders", [])
    )
    catalog = Catalog.load(input_root, "" if has_folder_database else default_database)

    files = configured_files(config, [".sql", ".ctl"])
    builder = PackageBuilder(
        f"sql-files-{source}",
        f"extractor:sql-files/{source}",
        "sql-file-extractor",
        _VERSION,
        {
            "source": source,
            "technology": "Python Oracle SQL and SQL*Loader parser",
            "parser": "extractors.package_support.oracle_parser.OraclePlsqlParser",
        },
    )
    builder.files_scanned = len(files)

    for file in files:
        database = file.database or default_database
        if not database:
            temporary_id = sql_file_id(repository, file.relative)
            builder.add_node(
                temporary_id,
                "SQL_FILE",
                file.absolute.name,
                file.relative,
                file.absolute.name,
                system_key=system_key,
                database_key="",
                repository_key=repository,
                graph_role="MAIN",
                properties={"classification": "UNKNOWN_SQL"},
            )
            builder.add_evidence(
                "NODE",
                temporary_id,
                file.relative,
                1,
                len(file.text.splitlines()) or 1,
                "DECLARATION",
                line_text(file.text, 1),
            )
            builder.add_issue(
                "INVALID_CONFIG",
                "ERROR",
                "SQL file has no database context",
                source_node_id=temporary_id,
                raw_reference=file.relative,
                source_path=file.relative,
                start_line=1,
            )
            continue
        is_loader = file.absolute.suffix.lower() == ".ctl"
        sql_id = (
            stable_node_id("loader-control", repository, file.relative)
            if is_loader
            else sql_file_id(repository, file.relative)
        )
        analysis = None if is_loader else analyze_sql(file.text)
        classification = (
            "SQL_LOADER_CONTROL_FILE" if is_loader else analysis.classification
        )
        builder.add_node(
            sql_id,
            "LOADER_CONTROL" if is_loader else "SQL_FILE",
            file.absolute.name,
            file.relative,
            file.absolute.name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
            graph_role="MAIN",
            properties={"classification": classification},
        )
        builder.add_evidence(
            "NODE",
            sql_id,
            file.relative,
            1,
            len(file.text.splitlines()) or 1,
            "DECLARATION",
            line_text(file.text, 1),
        )
        if classification == "SQL_LOADER_CONTROL_FILE":
            extract_sql_loader(
                builder, catalog, sql_id, file.text, file.relative, database
            )
            continue
        assert analysis is not None
        for ref in analysis.tables:
            line = line_for_offset(file.text, ref.start)
            snippet = line_text(file.text, line)
            if ref.remote:
                raw = ref.object_name.strip().upper()
                link_name = ref.db_link
                if link_name:
                    link_node = database_link_id(database, link_name)
                    builder.add_node(
                        link_node,
                        "DATABASE_LINK",
                        link_name,
                        f"{database}.{link_name}",
                        link_name,
                        system_key=system_key,
                        database_key=database,
                        repository_key=repository,
                        graph_role="TECHNICAL",
                    )
                    builder.add_edge(sql_id, link_node, "USES", raw_operation="DB_LINK")
                external_node = external_db_object_id(database, raw)
                builder.add_node(
                    external_node,
                    "EXTERNAL_DATABASE_OBJECT",
                    raw,
                    raw,
                    raw,
                    system_key="external",
                    database_key=database,
                    repository_key=repository,
                    confidence=0.9,
                )
                edge_id = builder.add_edge(
                    sql_id,
                    external_node,
                    "REMOTE_READS",
                    raw_operation=ref.operation,
                    confidence=0.9,
                )
                builder.add_evidence(
                    "EDGE",
                    edge_id,
                    file.relative,
                    line,
                    line,
                    "SQL",
                    snippet,
                    confidence=0.9,
                )
                builder.add_issue(
                    "EXTERNAL_OBJECT",
                    "INFO",
                    "Reference is outside selected database scope",
                    source_node_id=sql_id,
                    raw_reference=raw,
                    database_key=database,
                    source_path=file.relative,
                    start_line=line,
                )
                continue
            if _is_external_schema_reference(ref.object_name, schema):
                raw = ref.object_name.strip().upper()
                external_node = external_db_object_id(database, raw)
                builder.add_node(
                    external_node,
                    "EXTERNAL_DATABASE_OBJECT",
                    raw,
                    raw,
                    raw,
                    system_key="external",
                    database_key=database,
                    repository_key=repository,
                    confidence=0.9,
                )
                edge_id = builder.add_edge(
                    sql_id,
                    external_node,
                    ref.edge_type,
                    raw_operation=ref.operation,
                    confidence=0.9,
                )
                builder.add_evidence(
                    "EDGE",
                    edge_id,
                    file.relative,
                    line,
                    line,
                    "SQL",
                    snippet,
                    confidence=0.9,
                )
                builder.add_issue(
                    "EXTERNAL_OBJECT",
                    "INFO",
                    "Reference is outside selected database scope",
                    source_node_id=sql_id,
                    raw_reference=raw,
                    database_key=database,
                    source_path=file.relative,
                    start_line=line,
                )
                continue
            table_name = leaf_identifier(ref.object_name)
            if not catalog.has_table(database, table_name):
                owner = owner_identifier(ref.object_name, schema or "UNRESOLVED")
                target = unresolved_id(
                    database, f"TABLE:{owner}:{table_name}"
                )
                builder.add_node(
                    target,
                    "UNRESOLVED_REFERENCE",
                    table_name,
                    f"{database}.{owner}.{table_name}",
                    table_name,
                    system_key=system_key,
                    database_key=database,
                    repository_key=repository,
                    graph_role="TECHNICAL",
                    confidence=0.2,
                    properties={
                        "database": database,
                        "schema": owner,
                        "table": table_name,
                        "raw_reference": ref.object_name,
                    },
                )
                edge_id = builder.add_edge(
                    sql_id,
                    target,
                    ref.edge_type,
                    raw_operation=ref.operation,
                    confidence=0.5,
                    properties={"resolution": "unresolved_literal"},
                )
                builder.add_evidence(
                    "EDGE",
                    edge_id,
                    file.relative,
                    line,
                    line,
                    "SQL",
                    snippet,
                    confidence=0.5,
                )
                builder.add_issue(
                    "TABLE_NOT_IMPORTED",
                    "ERROR",
                    "Table is absent from authoritative catalog",
                    source_node_id=sql_id,
                    raw_reference=table_name,
                    database_key=database,
                    source_path=file.relative,
                    start_line=line,
                )
                continue
            edge_id = builder.add_edge(
                sql_id,
                table_id(database, table_name),
                ref.edge_type,
                raw_operation=ref.operation,
            )
            builder.add_evidence(
                "EDGE", edge_id, file.relative, line, line, "SQL", snippet
            )

        for seq in analysis.sequences:
            name = leaf_identifier(seq.object_name)
            seq_node = sequence_id(database, name)
            builder.add_node(
                seq_node,
                "SEQUENCE",
                name,
                f"{database}.{name}",
                name,
                system_key=system_key,
                database_key=database,
                repository_key=repository,
                graph_role="TECHNICAL",
            )
            edge_id = builder.add_edge(
                sql_id, seq_node, "USES_SEQUENCE", raw_operation=seq.operation
            )
            line = line_for_offset(file.text, seq.start)
            builder.add_evidence(
                "EDGE",
                edge_id,
                file.relative,
                line,
                line,
                "SQL",
                line_text(file.text, line),
            )

        for call in analysis.calls:
            raw = call.object_name.strip().upper()
            parts = raw.split(".")
            package = parts[-2] if len(parts) > 1 else ""
            routine = parts[-1]
            target = unresolved_id(database, f"{source}:{file.relative}:{raw}")
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
                    "package": package,
                    "routine": routine,
                    "raw_reference": raw,
                },
            )
            edge_id = builder.add_edge(sql_id, target, "CALLS", raw_operation=routine)
            line = line_for_offset(file.text, call.start)
            builder.add_evidence(
                "EDGE",
                edge_id,
                file.relative,
                line,
                line,
                "STORED_PROCEDURE",
                line_text(file.text, line),
            )

        for offset in analysis.dynamic_offsets:
            line = line_for_offset(file.text, offset)
            builder.add_issue(
                "DYNAMIC_SQL",
                "WARNING",
                "Runtime SQL target cannot be resolved",
                source_node_id=sql_id,
                raw_reference="EXECUTE IMMEDIATE",
                database_key=database,
                source_path=file.relative,
                start_line=line,
            )
        for offset in analysis.parse_error_offsets:
            line = line_for_offset(file.text, offset)
            builder.add_issue(
                "PARSE_ERROR",
                "ERROR",
                "SQL parser rejected this statement",
                source_node_id=sql_id,
                raw_reference=line_text(file.text, line),
                database_key=database,
                source_path=file.relative,
                start_line=line,
            )
        for match in _INVALID_CONFIG_RE.finditer(file.text):
            line = line_for_offset(file.text, match.start())
            builder.add_issue(
                "INVALID_CONFIG",
                "ERROR",
                "SQL file contains unresolved or invalid configuration marker",
                source_node_id=sql_id,
                raw_reference=match.group(0),
                database_key=database,
                source_path=file.relative,
                start_line=line,
            )
        attach_sql_semantic_tree(
            builder, sql_id, file.absolute.name, file.text, file.relative
        )

    builder.write(output)






def _is_remote_reference(name: str) -> bool:
    return "@" in name


def _is_external_schema_reference(name: str, schema: str) -> bool:
    if not schema or _is_remote_reference(name) or "." not in name.split("@", 1)[0]:
        return False
    try:
        return owner_identifier(name, schema) != owner_identifier(
            "LOCAL_OBJECT", schema
        )
    except ValueError:
        return False


if __name__ == "__main__":
    raise SystemExit(main())
