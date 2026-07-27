from __future__ import annotations

import json
from pathlib import Path
from xml.etree import ElementTree

from code_tree_exporter.contract.graph_contract import sql_file_id, stable_node_id, table_id
from code_tree_exporter.extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    configured_files,
    leaf_identifier,
    line_text,
    unresolved_id,
)
from code_tree_exporter.extractors.package_support.semantic_tree_v3 import analysis_notes
from code_tree_exporter.extractors.package_support.sql_analyzer import analyze_sql
from code_tree_exporter.extractors.xml_sql.includes import expanded_sql
from code_tree_exporter.extractors.xml_sql.semantic_tree import element_lines, statement_semantic_tree


def extract(config: dict) -> None:
    if config.get("type") != "xml-sql":
        raise ValueError("Config type must be xml-sql")
    source = config["source"]
    repository = config.get("repository", source)
    database = config.get("database", "")
    system = config.get("system", database or source)
    input_data = Path(config["inputData"])
    catalog = (
        Catalog.load(input_data, database) if database else Catalog.load(input_data)
    )
    files = configured_files(config, [".xml"])
    builder = PackageBuilder(
        f"xml-sql-{source}",
        f"extractor:xml-sql/{source}",
        "xml-sql-extractor",
        "1.0.0",
        {
            "source": source,
            "repository": repository,
            "technology": "Python ElementTree and Oracle SQL analyzer",
        },
    )
    builder.files_scanned = len(files)
    for file in files:
        owner_id = sql_file_id(repository, file.relative)
        builder.add_node(
            owner_id,
            "SQL_FILE",
            file.absolute.name,
            file.relative,
            file.absolute.name,
            system_key=system,
            database_key=database,
            repository_key=repository,
            properties={"classification": "XML_SQL_MAPPER"},
        )
        try:
            root = ElementTree.fromstring(file.text)
        except ElementTree.ParseError as exc:
            builder.add_issue(
                "PARSE_ERROR",
                "ERROR",
                f"Invalid XML: {exc}",
                source_node_id=owner_id,
                source_path=file.relative,
                start_line=getattr(exc, "position", (1, 0))[0],
            )
            continue
        lines = element_lines(file.text, root)
        root_line = lines.get(id(root), 1)
        builder.add_evidence("NODE", owner_id, file.relative, root_line, root_line, "DECLARATION", line_text(file.text, root_line))
        namespace = root.get("namespace", "").strip()
        fragments: dict[str, tuple[str, ElementTree.Element]] = {}
        for fragment in (item for item in root.iter() if _tag(item) == "sql"):
            fragment_id = fragment.get("id", "").strip()
            if not fragment_id:
                continue
            canonical = ".".join(part for part in (namespace, fragment_id) if part)
            fragments.setdefault(fragment_id, (canonical, fragment))
            fragments.setdefault(canonical, (canonical, fragment))
        queries: set[str] = set()
        for statement in (
            item
            for item in root.iter()
            if _tag(item)
            in {"select", "insert", "update", "delete", "merge", "statement"}
        ):
            statement_id = statement.get("id", "").strip()
            line = lines.get(id(statement), 1)
            if not namespace or not statement_id:
                builder.add_issue(
                    "INVALID_CONFIG", "ERROR", "XML SQL statement requires mapper namespace and statement id",
                    source_node_id=owner_id, source_path=file.relative, start_line=line,
                )
                continue
            query = f"{namespace}.{statement_id}"
            if query in queries:
                builder.add_issue(
                    "INVALID_CONFIG", "ERROR", f"Duplicate XML SQL statement: {query}",
                    source_node_id=owner_id, raw_reference=query, source_path=file.relative, start_line=line,
                )
                continue
            queries.add(query)
            statement_node_id = stable_node_id("inline-sql", repository, query)
            builder.add_node(
                statement_node_id,
                "INLINE_SQL",
                statement_id,
                query,
                query,
                system_key=system,
                database_key=database,
                repository_key=repository,
                properties={
                    "classification": "XML_SQL_STATEMENT",
                    "namespace": namespace,
                    "statement_id": statement_id,
                    "mapper_tag": _tag(statement),
                },
            )
            builder.add_edge(owner_id, statement_node_id, "CONTAINS", graph_layer="STRUCTURAL")
            builder.add_evidence("NODE", statement_node_id, file.relative, line, line, "DECLARATION", line_text(file.text, line))
            expanded, include_issues = expanded_sql(statement, fragments)
            sql = " ".join(expanded.split())
            for issue_type, severity, message, raw_reference, chain in include_issues:
                builder.add_issue(
                    issue_type,
                    severity,
                    message,
                    source_node_id=statement_node_id,
                    raw_reference=raw_reference,
                    database_key=database,
                    source_path=file.relative,
                    start_line=line,
                    properties={"include_chain": list(chain)} if chain else None,
                )
            analysis = analyze_sql(sql)
            for ref in analysis.tables:
                name = leaf_identifier(ref.object_name)
                if database and catalog.has_table(database, name):
                    target = table_id(database, name)
                else:
                    target = unresolved_id(database or "UNKNOWN", f"{source}:{name}")
                    builder.add_node(
                        target,
                        "UNRESOLVED_REFERENCE",
                        name,
                        name,
                        name,
                        system_key=system,
                        database_key=database,
                        repository_key=repository,
                        graph_role="TECHNICAL",
                    )
                    builder.add_issue(
                        "TABLE_NOT_IMPORTED",
                        "WARNING",
                        "Table is absent from authoritative catalog",
                        source_node_id=statement_node_id,
                        raw_reference=name,
                        database_key=database,
                        source_path=file.relative,
                        start_line=line,
                    )
                edge_id = builder.add_edge(
                    statement_node_id,
                    target,
                    ref.edge_type,
                    raw_operation=ref.operation,
                    properties={"query_id": query},
                )
                builder.add_evidence(
                    "EDGE", edge_id, file.relative, line, line, "XML_SQL", line_text(file.text, line)
                )
            for call in analysis.calls:
                name = str(getattr(call, "object_name", call)).strip()
                parts = name.upper().split(".")
                target = stable_node_id(
                    "unresolved-reference", database or "UNKNOWN", f"{source}:{name}"
                )
                builder.add_node(
                    target,
                    "UNRESOLVED_REFERENCE",
                    name,
                    name,
                    name,
                    system_key=system,
                    database_key=database,
                    repository_key=repository,
                    graph_role="TECHNICAL",
                    properties={
                        "database": database,
                        "package": parts[-2] if len(parts) > 1 else "",
                        "routine": parts[-1],
                        "raw_reference": name,
                    },
                )
                edge_id = builder.add_edge(
                    statement_node_id,
                    target,
                    "CALLS",
                    raw_operation="CALL",
                    properties={"query_id": query},
                )
                builder.add_evidence(
                    "EDGE", edge_id, file.relative, line, line, "XML_SQL", line_text(file.text, line)
                )
            tree = statement_semantic_tree(
                statement,
                query_id=query,
                source_path=file.relative,
                line_by_element=lines,
                fragments=fragments,
                analysis_notes=analysis_notes(builder, statement_node_id, file.relative, line),
            )
            properties = json.loads(builder.nodes[statement_node_id]["properties_json"])
            properties["semantic_tree"] = tree
            builder.nodes[statement_node_id]["properties_json"] = json.dumps(
                properties, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
    builder.write(Path(config["output"]))


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()



