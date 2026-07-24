from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from contract.graph_contract import sql_file_id, stable_node_id, table_id
from extractors.package_support.package_writer import (
    Catalog,
    PackageBuilder,
    configured_files,
    leaf_identifier,
    unresolved_id,
)
from extractors.package_support.sql_analyzer import analyze_sql


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
        namespace = root.get("namespace", "").strip()
        for statement in (
            item
            for item in root.iter()
            if _tag(item)
            in {"select", "insert", "update", "delete", "merge", "statement"}
        ):
            sql = " ".join("".join(statement.itertext()).split())
            line = _line(file.text, statement.get("id", ""), sql)
            analysis = analyze_sql(sql)
            query = ".".join(
                part for part in (namespace, statement.get("id", "").strip()) if part
            )
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
                        source_node_id=owner_id,
                        raw_reference=name,
                        database_key=database,
                        source_path=file.relative,
                        start_line=line,
                    )
                edge_id = builder.add_edge(
                    owner_id,
                    target,
                    ref.edge_type,
                    raw_operation=ref.operation,
                    properties={"query_id": query},
                )
                builder.add_evidence(
                    "EDGE", edge_id, file.relative, line, line, "XML_SQL", sql
                )
            for call in analysis.calls:
                name = str(getattr(call, "object_name", call)).strip()
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
                )
                edge_id = builder.add_edge(
                    owner_id,
                    target,
                    "CALLS",
                    raw_operation="CALL",
                    properties={"query_id": query},
                )
                builder.add_evidence(
                    "EDGE", edge_id, file.relative, line, line, "XML_SQL", sql
                )
    builder.write(Path(config["output"]))


def _tag(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


def _line(text: str, statement_id: str, sql: str) -> int:
    token = statement_id or (sql.split()[0] if sql else "")
    offset = text.find(token) if token else 0
    return text.count("\n", 0, max(0, offset)) + 1
