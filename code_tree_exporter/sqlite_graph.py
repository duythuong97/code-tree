from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .graph_package import GraphPackage, SourceRecord

_GRAPH_TABLES = {
    "nodes": "node_id",
    "edges": "edge_id",
    "evidence": "evidence_id",
    "comments": "comment_id",
    "issues": "issue_id",
}


def resolve_database_path(value: Path) -> Path:
    path = value.expanduser().resolve()
    if path.is_dir():
        path = path / "graph.sqlite"
    if not path.is_file():
        raise ValueError(f"SQLite graph database not found: {path}")
    return path


def load_sqlite_graph(value: Path) -> tuple[GraphPackage, dict[str, object]]:
    """Load a published graph into the in-memory shape used by renderers."""
    database = resolve_database_path(value)
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        metadata = _read_metadata(connection)
        graph = GraphPackage()
        for table, identity in _GRAPH_TABLES.items():
            rows = {
                row[identity]: row
                for row in (
                    _string_row(item)
                    for item in connection.execute(f"SELECT * FROM {table}")
                )
            }
            setattr(graph, table, rows)

        graph.source_records = [
            SourceRecord(
                source_key=row["source_key"],
                source_type=row["source_type"],
                system_key=row["system_key"],
                repository_key=row["repository_key"],
                relative_path=row["relative_path"],
                declared_encoding=row["declared_encoding"],
                actual_encoding=row["actual_encoding"],
                raw_sha256=row["raw_sha256"],
                text_sha256=row["text_sha256"],
                newline_style=row["newline_style"],
                bom=row["bom"],
            )
            for row in (
                _string_row(item)
                for item in connection.execute(
                    "SELECT * FROM sources ORDER BY source_key, relative_path"
                )
            )
        ]
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Unable to read SQLite graph database: {database}") from exc
    finally:
        connection.close()

    configured_descriptors = metadata.get("source_descriptors")
    if isinstance(configured_descriptors, dict):
        graph.source_descriptors = {
            str(key): {
                str(field): str(field_value)
                for field, field_value in value.items()
            }
            for key, value in configured_descriptors.items()
            if isinstance(value, dict)
        }
    for record in graph.source_records:
        graph.source_descriptors.setdefault(
            record.source_key,
            {
                "source_key": record.source_key,
                "source_type": record.source_type,
                "system_key": record.system_key,
                "repository_key": record.repository_key,
            },
        )
    graph._identifiers_compacted = True
    return graph, metadata


def _read_metadata(connection: sqlite3.Connection) -> dict[str, object]:
    metadata: dict[str, object] = {}
    for row in connection.execute("SELECT key, value_json FROM metadata"):
        try:
            metadata[str(row["key"])] = json.loads(row["value_json"])
        except (json.JSONDecodeError, TypeError):
            metadata[str(row["key"])] = row["value_json"]
    return metadata


def _string_row(row: sqlite3.Row) -> dict[str, str]:
    return {
        key: "" if row[key] is None else str(row[key])
        for key in row.keys()
    }
