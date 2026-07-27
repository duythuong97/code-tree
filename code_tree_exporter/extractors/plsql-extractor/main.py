#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

PACKAGE_IMPORT_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_IMPORT_ROOT))

from code_tree_exporter.contract.entities import ExtractionContext
from code_tree_exporter.contract.graph_contract import (
    column_id,
    database_id,
    normalize_http_route,
    stable_node_id,
    table_id,
)
from code_tree_exporter.extractors.oracle_plsql.lineage import OraclePlSqlLineageExtractor
from code_tree_exporter.extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    database_link_id,
    external_db_object_id,
    leaf_identifier,
    line_for_offset,
    line_text,
    load_config,
    plsql_package_id,
    procedure_id,
    function_id,
    owner_identifier,
    sequence_id,
    synonym_id,
    trigger_id,
    unresolved_id,
    configured_files,
)
from code_tree_exporter.extractors.package_support.oracle_parser import (
    OraclePlsqlParser,
    ParsedCallReference,
    ParsedRoutineDeclaration,
    mask_noncode,
)
from code_tree_exporter.extractors.package_support.semantic_tree import (
    attach_plsql_semantic_tree as _attach_semantic_tree,
)
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql, routine_signature

_VERSION = "1.0.0"
_IDENT = r'(?:"[^"]+"|[A-Za-z_][\w$#]*)'
_OBJECT = rf"{_IDENT}(?:\s*\.\s*{_IDENT}){{0,2}}(?:\s*@\s*[A-Za-z_][\w$#]*)?"
_VIEW_DECL_RE = re.compile(
    rf"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:EDITIONABLE\s+)?(?P<mview>MATERIALIZED\s+)?VIEW\s+(?P<name>{_OBJECT})\b.*?\bAS\b",
    re.IGNORECASE | re.DOTALL,
)
_EXTERNAL_API_RE = re.compile(
    r"external-api:([A-Za-z_][\w$#-]*):([A-Z]+):([^\s]+)", re.IGNORECASE
)
_DB_LINK_RE = re.compile(r"@([A-Za-z_][\w$#]*)")
_EXECUTE_IMMEDIATE_RE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)
_SKIP_CALLS = {
    "COUNT",
    "SUM",
    "TRUNC",
    "NVL",
    "TO_DATE",
    "TO_CHAR",
    "RAISE_APPLICATION_ERROR",
}


@dataclass(frozen=True)
class Routine:
    kind: str
    name: str
    signature: str
    node_id: str
    start: int
    end: int
    parameter_block: str | None = None
    is_local: bool = False
    parent_node_id: str | None = None


@dataclass(frozen=True)
class SynonymTarget:
    name: str
    node_id: str
    target_name: str
    target_node_id: str | None = None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract Oracle PL/SQL objects and SQL dependencies into a CSV graph package."
    )
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    extract(load_config(Path(args.config).expanduser()))
    return 0


def extract(config: dict) -> None:
    if config.get("type") != "oracle-plsql":
        raise ValueError("Config type must be oracle-plsql")
    source = config["source"]
    repository = config.get("repository", source)
    database = config["database"]
    schema = config.get("schema", "")
    system_key = config.get("system", database)
    input_root = Path(config["inputData"]).resolve()
    output = Path(config["output"]).resolve()
    catalog = Catalog.load(input_root, database)
    local_routines = {item.upper() for item in config.get("localRoutines", [])}

    files = configured_files(
        config, [".pks", ".pkb", ".pck", ".pls", ".plb", ".fnc", ".prc", ".trg", ".sql"]
    )
    builder = PackageBuilder(
        f"plsql-{source}",
        f"extractor:plsql/{source}",
        "plsql-extractor",
        _VERSION,
        {
            "source": source,
            "technology": "Python + ANTLR4 runtime Oracle PL/SQL parser",
            "parser": "extractors.package_support.oracle_parser.OraclePlsqlParser",
        },
    )
    builder.files_scanned = len(files)

    db_id = database_id(database)
    builder.add_node(
        db_id,
        "DATABASE",
        database,
        database,
        database,
        system_key=system_key,
        database_key=database,
    )

    for file in files:
        file_database = file.database or database
        _extract_file(
            builder,
            file.relative,
            file.text,
            file_database,
            schema,
            repository,
            system_key,
            catalog,
            local_routines,
        )
        _extract_column_lineage(
            builder,
            file.relative,
            file.text,
            file_database,
            schema,
            repository,
            system_key,
        )

    for node in config.get("supplementalNodes", []):
        builder.add_node(
            node["nodeId"],
            node["nodeType"],
            node["technicalName"],
            node.get("qualifiedName", node["technicalName"]),
            node.get("displayName"),
            system_key=node.get("system", system_key),
            database_key=node.get("database", database),
            repository_key=node.get("repository", repository),
            graph_role=node.get("graphRole", "MAIN"),
            confidence=node.get("confidence", 1.0),
            properties=node.get("properties", {}),
        )
    for edge in config.get("supplementalEdges", []):
        builder.add_edge(
            edge["source"],
            edge["target"],
            edge["edgeType"],
            graph_layer=edge.get("graphLayer", "TECHNICAL"),
            raw_operation=edge.get("rawOperation", ""),
            confidence=edge.get("confidence", 1.0),
            properties=edge.get("properties", {}),
        )

    builder.write(output)


def _extract_column_lineage(
    builder: PackageBuilder,
    source_path: str,
    text: str,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
) -> None:
    context = ExtractionContext(
        repository=repository,
        db_name=database,
        schema_name=schema,
        source_id=builder.source_id,
        relative_source_path=source_path,
    )
    result = OraclePlSqlLineageExtractor().extract(source_path, text, context)
    column_ids: dict[str, str] = {}
    column_parts: dict[str, tuple[str, str]] = {}
    for node in result.nodes:
        if node.label != "Column":
            continue
        table_name = str(node.properties.get("table_name") or "")
        column_name = str(node.properties.get("name") or "")
        if not table_name or not column_name:
            continue
        node_id = column_id(database, table_name, column_name)
        column_ids[node.key_value] = node_id
        column_parts[node.key_value] = (table_name, column_name)
        builder.add_node(
            node_id,
            "COLUMN",
            column_name,
            f"{database}.{table_name}.{column_name}",
            column_name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
            confidence=0.9,
            properties={
                "table_node_id": table_id(database, table_name),
                "table_code": table_name,
                "column_code": column_name,
            },
        )
    owner_ids: dict[str, str] = {}
    owner_keys = {
        edge.from_key_value
        for edge in result.edges
        if edge.rel_type in {"READS_COLUMN", "WRITES_COLUMN", "POPULATES"}
    }
    for owner_key in owner_keys:
        name = owner_key.rsplit(".", 1)[-1].upper()
        candidates = [
            row["node_id"]
            for row in builder.nodes.values()
            if row["technical_name"].upper() == name
            and row["node_type"]
            in {"PROCEDURE", "FUNCTION", "TRIGGER", "PLSQL_PACKAGE", "LOCAL_ROUTINE"}
        ]
        if len(candidates) == 1:
            owner_ids[owner_key] = candidates[0]
    for edge in result.edges:
        if edge.rel_type not in {
            "READS_COLUMN",
            "WRITES_COLUMN",
            "DERIVES_FROM",
            "POPULATES",
        }:
            continue
        source_id = column_ids.get(edge.from_key_value) or owner_ids.get(
            edge.from_key_value
        )
        target_id = column_ids.get(edge.to_key_value)
        if not source_id or not target_id:
            continue
        properties = dict(edge.properties)
        operation = str(properties.pop("operation", ""))
        confidence = float(properties.pop("confidence", 0.5))
        line = int(properties.pop("line", 0) or 0)
        properties.pop("source_file", None)
        edge_id = builder.add_edge(
            source_id,
            target_id,
            edge.rel_type,
            raw_operation=operation,
            confidence=confidence,
            properties=properties,
        )
        if line:
            builder.add_evidence(
                "EDGE",
                edge_id,
                source_path,
                line,
                line,
                "SQL_COLUMN_LINEAGE",
                line_text(text, line),
                confidence=confidence,
                properties=properties,
            )
    _attach_edge_semantics(
        builder,
        result.edges,
        owner_ids,
        column_ids,
        column_parts,
        source_path,
        database,
    )


def _attach_edge_semantics(
    builder: PackageBuilder,
    lineage_edges: list,
    owner_ids: dict[str, str],
    column_ids: dict[str, str],
    column_parts: dict[str, tuple[str, str]],
    source_path: str,
    database: str,
) -> None:
    derived: dict[tuple[str, int, str], list[str]] = {}
    for edge in lineage_edges:
        if edge.rel_type != "DERIVES_FROM" or edge.from_key_value not in column_ids:
            continue
        line = int(edge.properties.get("line", 0) or 0)
        operation = str(edge.properties.get("operation", ""))
        derived.setdefault((edge.to_key_value, line, operation), []).append(
            column_ids[edge.from_key_value]
        )

    statements: dict[tuple[str, str, str, int], list[dict]] = {}
    for edge in lineage_edges:
        if edge.rel_type != "WRITES_COLUMN":
            continue
        owner_id = owner_ids.get(edge.from_key_value)
        target = column_parts.get(edge.to_key_value)
        if not owner_id or not target:
            continue
        table_name, column_name = target
        operation = str(edge.properties.get("operation", ""))
        line = int(edge.properties.get("line", 0) or 0)
        field = {
            "name": column_name,
            "target_node_id": column_ids[edge.to_key_value],
            "expression": str(edge.properties.get("expression", "")),
            "sources": sorted(
                set(derived.get((edge.to_key_value, line, operation), []))
            ),
        }
        statements.setdefault((owner_id, table_name, operation, line), []).append(field)

    by_table_edge: dict[tuple[str, str, str], list[dict]] = {}
    operation_map = {
        "INSERT": ("INSERTS", "INSERT"),
        "UPDATE": ("UPDATES", "UPDATE"),
        "MERGE": ("MERGES", "MERGE"),
    }
    for (owner_id, table_name, lineage_operation, line), fields in statements.items():
        sql_operation = lineage_operation.split("_", 1)[0]
        mapped = operation_map.get(sql_operation)
        if not mapped:
            continue
        edge_type, raw_operation = mapped
        statement = {
            "operation": lineage_operation,
            "source": {"path": source_path, "line": line},
            "fields": sorted(fields, key=lambda item: item["name"]),
        }
        by_table_edge.setdefault((owner_id, table_name, edge_type), []).append(
            statement
        )
        edge_id = builder.add_edge(
            owner_id,
            table_id(database, table_name),
            edge_type,
            raw_operation=raw_operation,
        )
        statements_for_edge = sorted(
            by_table_edge[(owner_id, table_name, edge_type)],
            key=lambda item: (item["source"]["line"], item["operation"]),
        )
        builder.merge_edge_properties(
            edge_id,
            {
                "semantic": {
                    "version": 1,
                    "action": "WRITE",
                    "operation": raw_operation,
                    "target": {
                        "node_id": table_id(database, table_name),
                        "type": "TABLE",
                        "name": table_name,
                    },
                    "fields": _unique_semantic_fields(statements_for_edge),
                    "statements": statements_for_edge,
                }
            },
        )


def _unique_semantic_fields(statements: list[dict]) -> list[dict]:
    fields: dict[tuple[str, str, tuple[str, ...]], dict] = {}
    for statement in statements:
        for field in statement["fields"]:
            key = (
                field["target_node_id"],
                field["expression"],
                tuple(field["sources"]),
            )
            fields.setdefault(key, field)
    return sorted(fields.values(), key=lambda item: (item["name"], item["expression"]))


def _extract_file(
    builder: PackageBuilder,
    source_path: str,
    text: str,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
    local_routines: set[str],
) -> None:
    parser = OraclePlsqlParser(text)
    if parser.syntax_errors:
        details = "; ".join(
            f"line {line}:{column} {message}"
            for line, column, message in parser.syntax_errors[:5]
        )
        raise ValueError(f"ANTLR PL/SQL parse failed for {source_path}: {details}")
    package = parser.package_name() or "STANDALONE"
    package_node = plsql_package_id(database, package)
    if package != "STANDALONE":
        builder.add_node(
            package_node,
            "PLSQL_PACKAGE",
            package,
            f"{database}.{package}",
            package,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )
        builder.add_evidence(
            "NODE",
            package_node,
            source_path,
            1,
            len(text.splitlines()) or 1,
            "DECLARATION",
            line_text(text, 1),
        )

    synonyms = _synonyms_from_parser(
        builder, parser, database, schema, repository, system_key, catalog
    )

    routines = _routines_from_parser(parser, database, package, local_routines)
    all_calls = parser.calls()
    routine_by_name: dict[str, list[Routine]] = {}
    for routine in routines:
        routine_by_name.setdefault(routine.name.upper(), []).append(routine)
    for routine in routines:
        if routine.is_local:
            builder.add_node(
                routine.node_id,
                "LOCAL_ROUTINE",
                routine.name,
                f"{database}.{package}.{routine.name}",
                routine.name,
                system_key=system_key,
                database_key=database,
                repository_key=repository,
                graph_role="TECHNICAL",
            )
        else:
            node_type = "PROCEDURE" if routine.kind == "PROCEDURE" else "FUNCTION"
            qname = f"{database}.{package}.{routine.name}({routine.signature.replace('_', ',') if routine.signature != 'void' else ''})"
            builder.add_node(
                routine.node_id,
                node_type,
                routine.name,
                qname,
                routine.name,
                system_key=system_key,
                database_key=database,
                repository_key=repository,
            )
        if routine.parent_node_id:
            builder.add_edge(
                routine.parent_node_id,
                routine.node_id,
                "CONTAINS",
                graph_layer="STRUCTURAL",
            )
        elif package != "STANDALONE":
            builder.add_edge(
                package_node, routine.node_id, "CONTAINS", graph_layer="STRUCTURAL"
            )
        start_line = line_for_offset(text, routine.start)
        builder.add_evidence(
            "NODE",
            routine.node_id,
            source_path,
            start_line,
            line_for_offset(text, routine.end),
            "DECLARATION",
            line_text(text, start_line),
        )

    view_ranges = _extract_view_declarations(
        builder,
        source_path,
        text,
        database,
        schema,
        repository,
        system_key,
        catalog,
        routine_by_name,
        synonyms,
    )

    parsed_triggers = parser.triggers()
    for parsed_trigger in parsed_triggers:
        trigger_name = parsed_trigger.name.upper()
        trigger_node = trigger_id(database, trigger_name)
        builder.add_node(
            trigger_node,
            "TRIGGER",
            trigger_name,
            f"{database}.{trigger_name}",
            trigger_name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )
        start_line = line_for_offset(text, parsed_trigger.start)
        builder.add_evidence(
            "NODE",
            trigger_node,
            source_path,
            start_line,
            line_for_offset(text, parsed_trigger.end),
            "DECLARATION",
            line_text(text, start_line),
        )
        table_name = leaf_identifier(parsed_trigger.table_name)
        if catalog.has_table(database, table_name):
            builder.add_edge(
                table_id(database, table_name),
                trigger_node,
                "TRIGGERS",
                raw_operation="UPDATE_TRIGGER",
            )
        trigger_segment = text[parsed_trigger.start : parsed_trigger.end]
        _extract_dependencies(
            builder,
            trigger_node,
            source_path,
            trigger_segment,
            text,
            parsed_trigger.start,
            database,
            schema,
            repository,
            system_key,
            catalog,
            routine_by_name,
            synonyms,
            _calls_in_range(all_calls, parsed_trigger.start, parsed_trigger.end),
        )
        _attach_semantic_tree(
            builder,
            trigger_node,
            "TRIGGER",
            trigger_name,
            "void",
            text=trigger_segment,
            source_path=source_path,
            base_line=start_line,
        )

    for routine in routines:
        segment = _mask_nested_routines(text, routine, routines)
        _extract_dependencies(
            builder,
            routine.node_id,
            source_path,
            segment,
            text,
            routine.start,
            database,
            schema,
            repository,
            system_key,
            catalog,
            routine_by_name,
            synonyms,
            _calls_in_routine(all_calls, routine, routines),
        )
        _attach_semantic_tree(
            builder,
            routine.node_id,
            routine.kind,
            routine.name,
            routine.signature,
            parameter_block=routine.parameter_block,
            text=segment,
            source_path=source_path,
            base_line=line_for_offset(text, routine.start),
        )

    if package != "STANDALONE":
        excluded_ranges = [(routine.start, routine.end) for routine in routines]
        excluded_ranges.extend(
            (trigger.start, trigger.end) for trigger in parsed_triggers
        )
        excluded_ranges.extend(view_ranges)
        package_segment = _mask_ranges(text, excluded_ranges)
        _extract_dependencies(
            builder,
            package_node,
            source_path,
            package_segment,
            text,
            0,
            database,
            schema,
            repository,
            system_key,
            catalog,
            routine_by_name,
            synonyms,
            _calls_outside_ranges(all_calls, excluded_ranges),
        )


def _routines_from_parser(
    parser: OraclePlsqlParser, database: str, package: str, local_routines: set[str]
) -> list[Routine]:
    routines: list[Routine] = []
    seen: set[tuple[str, str, str, int]] = set()
    parsed = parser.routines()
    parent_by_item = {item: _parent_routine(item, parsed) for item in parsed}
    node_by_item: dict[ParsedRoutineDeclaration, str] = {}
    for item in parsed:
        parent = parent_by_item[item]
        signature = routine_signature(item.parameter_block)
        if parent:
            node_by_item[item] = stable_node_id(
                "local-routine", database, package, parent.name, item.name, signature
            )
        elif item.kind == "PROCEDURE":
            node_by_item[item] = procedure_id(database, package, item.name, signature)
        else:
            node_by_item[item] = function_id(database, package, item.name, signature)
    for item in parsed:
        signature = routine_signature(item.parameter_block)
        parent = parent_by_item[item]
        is_local = item.name in local_routines or parent is not None
        node = node_by_item[item]
        key = (item.kind, item.name, signature, item.start)
        if key in seen:
            continue
        seen.add(key)
        routines.append(
            Routine(
                item.kind,
                item.name,
                signature,
                node,
                item.start,
                item.end,
                item.parameter_block,
                is_local,
                node_by_item.get(parent) if parent else None,
            )
        )
    return routines


def _parent_routine(
    item: ParsedRoutineDeclaration, candidates: list[ParsedRoutineDeclaration]
) -> ParsedRoutineDeclaration | None:
    parents = [
        candidate
        for candidate in candidates
        if candidate.start < item.start and item.end <= candidate.end
    ]
    return (
        min(parents, key=lambda candidate: candidate.end - candidate.start)
        if parents
        else None
    )


def _mask_nested_routines(
    full_text: str, routine: Routine, routines: list[Routine]
) -> str:
    segment = full_text[routine.start : routine.end]
    ranges = [
        (child.start - routine.start, child.end - routine.start)
        for child in routines
        if routine.start < child.start and child.end <= routine.end
    ]
    return _mask_ranges(segment, ranges)


def _mask_ranges(text: str, ranges: list[tuple[int, int]]) -> str:
    chars = list(text)
    for start, end in ranges:
        for index in range(max(0, start), min(len(chars), end)):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _calls_in_range(
    calls: list[ParsedCallReference], start: int, end: int
) -> list[ParsedCallReference]:
    return [call for call in calls if start <= call.start < end]


def _calls_in_routine(
    calls: list[ParsedCallReference], routine: Routine, routines: list[Routine]
) -> list[ParsedCallReference]:
    nested = [
        (item.start, item.end)
        for item in routines
        if routine.start < item.start and item.end <= routine.end
    ]
    return [
        call
        for call in _calls_in_range(calls, routine.start, routine.end)
        if not any(start <= call.start < end for start, end in nested)
    ]


def _calls_outside_ranges(
    calls: list[ParsedCallReference], ranges: list[tuple[int, int]]
) -> list[ParsedCallReference]:
    return [
        call
        for call in calls
        if not any(start <= call.start < end for start, end in ranges)
    ]


def _extract_view_declarations(
    builder: PackageBuilder,
    source_path: str,
    text: str,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
    routine_by_name: dict[str, list[Routine]],
    synonyms: dict[str, SynonymTarget],
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    scan = mask_noncode(text)
    for match in _VIEW_DECL_RE.finditer(scan):
        raw_name = _normalize_object_name(match.group("name"))
        name = leaf_identifier(raw_name)
        node_type = "MATERIALIZED_VIEW" if match.group("mview") else "VIEW"
        node = stable_node_id(
            "materialized-view" if node_type == "MATERIALIZED_VIEW" else "view",
            database,
            name,
        )
        builder.add_node(
            node,
            node_type,
            name,
            f"{database}.{name}",
            name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )
        end = _ddl_end(scan, match.end())
        start_line = line_for_offset(text, match.start())
        builder.add_evidence(
            "NODE",
            node,
            source_path,
            start_line,
            line_for_offset(text, end),
            "DECLARATION",
            line_text(text, start_line),
        )
        ranges.append((match.start(), end))
        _extract_dependencies(
            builder,
            node,
            source_path,
            text[match.end() : end],
            text,
            match.end(),
            database,
            schema,
            repository,
            system_key,
            catalog,
            routine_by_name,
            synonyms,
        )
    return ranges


def _ddl_end(scan: str, start: int) -> int:
    slash = re.search(r"^\s*/\s*$", scan[start:], re.MULTILINE)
    next_create = re.search(
        r"\bCREATE\s+(?:OR\s+REPLACE\s+)?", scan[start:], re.IGNORECASE
    )
    candidates = [start + match.start() for match in (slash, next_create) if match]
    return min(candidates) if candidates else len(scan)


def _normalize_object_name(name: str) -> str:
    parts = []
    for raw in re.split(r"\s*\.\s*", name.strip()):
        part = raw.strip().strip('"')
        if part:
            parts.append(
                part.upper()
                if not (raw.strip().startswith('"') and raw.strip().endswith('"'))
                else part
            )
    return ".".join(parts)


def _synonyms_from_parser(
    builder: PackageBuilder,
    parser: OraclePlsqlParser,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
) -> dict[str, SynonymTarget]:
    synonyms: dict[str, SynonymTarget] = {}
    for parsed in parser.synonyms():
        name = leaf_identifier(parsed.name)
        node = synonym_id(database, name)
        builder.add_node(
            node,
            "SYNONYM",
            name,
            f"{database}.{name}",
            name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
            graph_role="TECHNICAL",
        )
        target_node: str | None = None
        if _is_remote_reference(parsed.target_name) or _is_external_schema_reference(
            parsed.target_name, schema
        ):
            target_node = external_db_object_id(database, parsed.target_name.upper())
            builder.add_node(
                target_node,
                "EXTERNAL_DATABASE_OBJECT",
                parsed.target_name.upper(),
                parsed.target_name.upper(),
                parsed.target_name.upper(),
                system_key="external",
                database_key=database,
                repository_key=repository,
                confidence=0.9,
            )
        else:
            table_name = leaf_identifier(parsed.target_name)
            if catalog.has_table(database, table_name):
                target_node = table_id(database, table_name)
        if target_node:
            builder.add_edge(node, target_node, "RESOLVES_TO")
        synonyms[name] = SynonymTarget(name, node, parsed.target_name, target_node)
    return synonyms


def _extract_dependencies(
    builder: PackageBuilder,
    owner_id: str,
    source_path: str,
    text: str,
    full_text: str,
    base_offset: int,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
    routine_by_name: dict[str, list[Routine]],
    synonyms: dict[str, SynonymTarget],
    calls: list[ParsedCallReference] | None = None,
) -> None:
    analysis = analyze_sql(text)
    for ref in analysis.tables:
        absolute_line = line_for_offset(full_text, base_offset + ref.start)
        snippet = line_text(full_text, absolute_line)
        _add_table_reference(
            builder,
            owner_id,
            source_path,
            absolute_line,
            snippet,
            ref.object_name,
            ref.operation,
            ref.edge_type,
            ref.remote,
            database,
            schema,
            repository,
            system_key,
            catalog,
            synonyms,
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
        builder.add_edge(owner_id, seq_node, "USES", raw_operation="NEXTVAL")

    for call in calls or []:
        raw = call.object_name.upper()
        parts = raw.split(".")
        name = parts[-1]
        if name in _SKIP_CALLS:
            continue
        line = line_for_offset(full_text, call.start)
        candidates = routine_by_name.get(name, []) if len(parts) == 1 else []
        if len(candidates) == 1:
            target = candidates[0].node_id
        else:
            package = parts[-2] if len(parts) > 1 else ""
            target = unresolved_id(database, f"{source_path}:{raw}:{call.start}")
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
                confidence=0.6,
                properties={
                    "database": database,
                    "schema": parts[-3] if len(parts) > 2 else schema,
                    "package": package,
                    "routine": name,
                    "raw_reference": raw,
                },
            )
            if len(candidates) > 1:
                builder.add_issue(
                    "AMBIGUOUS_SYMBOL",
                    "WARNING",
                    "Routine call matches multiple overloads",
                    source_node_id=owner_id,
                    raw_reference=raw,
                    database_key=database,
                    source_path=source_path,
                    start_line=line,
                    properties={
                        "candidates": [candidate.node_id for candidate in candidates]
                    },
                )
        edge_id = builder.add_edge(owner_id, target, "CALLS", raw_operation=name)
        builder.add_evidence(
            "EDGE",
            edge_id,
            source_path,
            line,
            line,
            "ROUTINE_CALL",
            line_text(full_text, line),
        )

    for match in _EXTERNAL_API_RE.finditer(text):
        system = match.group(1).upper()
        method = match.group(2).upper()
        _, route = normalize_http_route(method, match.group(3))
        system_node = stable_node_id("external-system", system)
        api_node = stable_node_id("external-api", system, method, route)
        builder.add_node(
            system_node, "EXTERNAL_SYSTEM", system, system, system, system_key=system
        )
        builder.add_node(
            api_node,
            "EXTERNAL_API_OPERATION",
            f"{method} {route}",
            f"{system}.{method}.{route}",
            f"{method} {route}",
            system_key=system,
            confidence=0.9,
        )
        builder.add_edge(system_node, api_node, "CONTAINS", graph_layer="STRUCTURAL")
        builder.add_edge(
            owner_id, api_node, "CALLS_API", raw_operation=method, confidence=0.8
        )

    unresolved_dynamic = set(analysis.dynamic_offsets)
    scan = mask_noncode(text)
    for match in _EXECUTE_IMMEDIATE_RE.finditer(scan):
        if match.start() in unresolved_dynamic:
            continue
        line = line_for_offset(full_text, base_offset + match.start())
        builder.add_issue(
            "DYNAMIC_SQL",
            "INFO",
            "Dynamic SQL was analyzed with best-effort literal resolution",
            source_node_id=owner_id,
            raw_reference="EXECUTE IMMEDIATE",
            database_key=database,
            source_path=source_path,
            start_line=line,
        )

    for offset in analysis.dynamic_offsets:
        line = line_for_offset(full_text, base_offset + offset)
        dynamic_node = unresolved_id(database, "DYNAMIC_SQL")
        builder.add_node(
            dynamic_node,
            "UNRESOLVED_REFERENCE",
            "DYNAMIC_SQL",
            "DYNAMIC_SQL",
            "Dynamic SQL",
            system_key=system_key,
            database_key=database,
            repository_key=repository,
            confidence=0.2,
        )
        builder.add_edge(
            owner_id,
            dynamic_node,
            "CALLS",
            raw_operation="EXECUTE_IMMEDIATE",
            confidence=0.2,
        )
        builder.add_issue(
            "DYNAMIC_SQL",
            "WARNING",
            "Runtime SQL target cannot be resolved",
            source_node_id=owner_id,
            raw_reference="EXECUTE IMMEDIATE",
            database_key=database,
            source_path=source_path,
            start_line=line,
        )


def _add_table_reference(
    builder: PackageBuilder,
    owner_id: str,
    source_path: str,
    line: int,
    snippet: str,
    raw_object_name: str,
    operation: str,
    edge_type: str,
    remote: bool,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
    synonyms: dict[str, SynonymTarget],
) -> None:
    synonym = synonyms.get(leaf_identifier(raw_object_name))
    object_name = synonym.target_name if synonym else raw_object_name
    if synonym:
        builder.add_edge(owner_id, synonym.node_id, "USES", raw_operation="SYNONYM")
    if remote or _is_remote_reference(object_name):
        _add_external_reference(
            builder,
            owner_id,
            source_path,
            line,
            snippet,
            object_name,
            operation,
            "REMOTE_READS" if edge_type == "READS" else edge_type,
            database,
            repository,
            system_key,
        )
        return
    if _is_external_schema_reference(object_name, schema):
        _add_external_reference(
            builder,
            owner_id,
            source_path,
            line,
            snippet,
            object_name,
            operation,
            edge_type,
            database,
            repository,
            system_key,
        )
        return
    table_name = leaf_identifier(object_name)
    if not catalog.has_table(database, table_name):
        builder.add_issue(
            "TABLE_NOT_IMPORTED",
            "ERROR",
            "Table is absent from authoritative catalog",
            source_node_id=owner_id,
            raw_reference=table_name,
            database_key=database,
            source_path=source_path,
            start_line=line,
        )
        return
    properties = {"resolvedViaSynonym": synonym.name} if synonym else None
    edge_id = builder.add_edge(
        owner_id,
        table_id(database, table_name),
        edge_type,
        raw_operation=operation,
        properties=properties,
    )
    builder.add_evidence(
        "EDGE", edge_id, source_path, line, line, "SQL", snippet, properties=properties
    )


def _add_external_reference(
    builder: PackageBuilder,
    owner_id: str,
    source_path: str,
    line: int,
    snippet: str,
    object_name: str,
    operation: str,
    edge_type: str,
    database: str,
    repository: str,
    system_key: str,
) -> None:
    raw = object_name.strip().upper()
    for link in _DB_LINK_RE.finditer(raw):
        link_name = link.group(1).upper()
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
        builder.add_edge(owner_id, link_node, "USES", raw_operation="DB_LINK")
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
        owner_id, external_node, edge_type, raw_operation=operation, confidence=0.9
    )
    builder.add_evidence(
        "EDGE", edge_id, source_path, line, line, "SQL", snippet, confidence=0.9
    )
    builder.add_issue(
        "EXTERNAL_OBJECT",
        "INFO",
        "Reference is outside selected database scope",
        source_node_id=owner_id,
        raw_reference=raw,
        database_key=database,
        source_path=source_path,
        start_line=line,
    )


def _is_remote_reference(name: str) -> bool:
    return bool(_DB_LINK_RE.search(name))


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
