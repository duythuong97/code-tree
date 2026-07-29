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

from code_tree_exporter.contract.graph_contract import (
    database_id,
    normalize_http_route,
    stable_node_id,
    table_id,
)
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
)
from code_tree_exporter.extractors.package_support.semantic_tree import (
    attach_plsql_semantic_tree as _attach_semantic_tree,
)
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql

_VERSION = "1.0.0"
_EXTERNAL_API_RE = re.compile(
    r"external-api:([A-Za-z_][\w$#-]*):([A-Z]+):([^\s]+)", re.IGNORECASE
)
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
    semantic_detail = _semantic_detail(config.get("semanticDetail", "summary"))

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
            "semanticDetail": semantic_detail,
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
        chunks = re.split(r'(?m)^\s*/\s*$', file.text)
        base_line = 1
        for chunk in chunks:
            if not chunk.strip():
                base_line += chunk.count('\n') + 1
                continue
            # Prepend newlines so line_for_offset returns absolute line numbers
            padded = '\n' * (base_line - 1) + chunk
            _extract_file(
                builder,
                file.relative,
                padded,
                file_database,
                schema,
                repository,
                system_key,
                catalog,
                local_routines,
                semantic_detail,
            )
            base_line += chunk.count('\n') + 1

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
    semantic_detail: str,
) -> None:
    parser = OraclePlsqlParser(text)
    full_analysis = analyze_sql(text)
    if parser.syntax_errors:
        details = "; ".join(
            f"line {line}:{column} {message}"
            for line, column, message in parser.syntax_errors[:5]
        )
        builder.add_issue(
            "PARSE_ERROR",
            "ERROR",
            f"ANTLR PL/SQL parse failed for {source_path}: {details}",
            source_path=source_path,
        )
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
        parser,
        source_path,
        text,
        database,
        schema,
        repository,
        system_key,
        catalog,
        routine_by_name,
        synonyms,
        full_analysis,
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
            full_analysis,
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
            detail=semantic_detail,
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
            full_analysis,
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
            detail=semantic_detail,
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
            full_analysis,
            _calls_outside_ranges(all_calls, excluded_ranges),
        )


def _semantic_detail(value) -> str:
    detail = str(value or "summary").strip().lower()
    if detail not in {"summary", "full"}:
        raise ValueError("semanticDetail must be 'summary' or 'full'")
    return detail


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
        signature = parser.routine_signature(item)
        if parent:
            node_by_item[item] = stable_node_id(
                "local-routine", database, package, parent.name, item.name, signature
            )
        elif item.kind == "PROCEDURE":
            node_by_item[item] = procedure_id(database, package, item.name, signature)
        else:
            node_by_item[item] = function_id(database, package, item.name, signature)
    for item in parsed:
        signature = parser.routine_signature(item)
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
    parser: OraclePlsqlParser,
    source_path: str,
    text: str,
    database: str,
    schema: str,
    repository: str,
    system_key: str,
    catalog: Catalog,
    routine_by_name: dict[str, list[Routine]],
    synonyms: dict[str, SynonymTarget],
    full_analysis,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for declaration in parser.views():
        name = leaf_identifier(declaration.name)
        node = stable_node_id(
            "materialized-view" if declaration.kind == "MATERIALIZED_VIEW" else "view",
            database,
            name,
        )
        builder.add_node(
            node,
            declaration.kind,
            name,
            f"{database}.{name}",
            name,
            system_key=system_key,
            database_key=database,
            repository_key=repository,
        )
        start_line = line_for_offset(text, declaration.start)
        builder.add_evidence(
            "NODE",
            node,
            source_path,
            start_line,
            line_for_offset(text, declaration.end),
            "DECLARATION",
            line_text(text, start_line),
        )
        ranges.append((declaration.start, declaration.end))
        _extract_dependencies(
            builder,
            node,
            source_path,
            text[declaration.body_start : declaration.end],
            text,
            declaration.body_start,
            database,
            schema,
            repository,
            system_key,
            catalog,
            routine_by_name,
            synonyms,
            full_analysis,
        )
    return ranges






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
    analysis,
    calls: list[ParsedCallReference] | None = None,
) -> None:
    for ref in analysis.tables:
        if not _reference_visible(text, full_text, base_offset, ref.start):
            continue
        absolute_line = line_for_offset(full_text, ref.start)
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
            ref.db_link,
            database,
            schema,
            repository,
            system_key,
            catalog,
            synonyms,
        )

    for seq in analysis.sequences:
        if not _reference_visible(text, full_text, base_offset, seq.start):
            continue
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
            owner_id, seq_node, "USES_SEQUENCE", raw_operation=seq.operation
        )
        line = line_for_offset(full_text, seq.start)
        builder.add_evidence(
            "EDGE",
            edge_id,
            source_path,
            line,
            line,
            "SQL",
            line_text(full_text, line),
        )

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

    for offset in analysis.dynamic_offsets:
        if not _reference_visible(text, full_text, base_offset, offset):
            continue
        line = line_for_offset(full_text, offset)
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


def _reference_visible(
    segment: str, full_text: str, base_offset: int, absolute_offset: int
) -> bool:
    relative = absolute_offset - base_offset
    if relative < 0 or relative >= len(segment):
        return False
    original = full_text[absolute_offset : absolute_offset + 1]
    visible = segment[relative : relative + 1]
    return not (original and not original.isspace() and visible.isspace())


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
    db_link: str,
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
    if remote or db_link:
        _add_external_reference(
            builder,
            owner_id,
            source_path,
            line,
            snippet,
            object_name,
            operation,
            "READS_FROM" if edge_type == "READS_FROM" else edge_type,
            db_link,
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
        owner = owner_identifier(object_name, schema or "UNRESOLVED")
        target = unresolved_id(database, f"TABLE:{owner}:{table_name}")
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
                "raw_reference": raw_object_name,
            },
        )
        edge_id = builder.add_edge(
            owner_id,
            target,
            edge_type,
            raw_operation=operation,
            confidence=0.5,
            properties={"resolution": "unresolved_literal"},
        )
        builder.add_evidence(
            "EDGE",
            edge_id,
            source_path,
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
    db_link: str,
    database: str,
    repository: str,
    system_key: str,
) -> None:
    raw = object_name.strip().upper()
    link_name = db_link
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
