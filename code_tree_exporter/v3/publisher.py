from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path

from .catalog import CatalogImportResult


_READ_EDGES = frozenset({"READS", "REMOTE_READS", "READS_FROM", "READS_COLUMN"})
_WRITE_EDGES = frozenset(
    {
        "INSERTS",
        "UPDATES",
        "DELETES",
        "MERGES",
        "WRITES",
        "WRITES_TO",
        "WRITES_COLUMN",
        "LOADS_INTO",
        "POPULATES",
    }
)
_FLOW_EDGES = frozenset(
    {
        "CONTAINS",
        "CALLS_API",
        "STARTS",
        "CALLS",
        "TRIGGERS",
        "TRIGGERS_JOB",
        "HANDLED_BY",
        "HANDLES_API",
        "ENTRY_IN",
        "RESOLVES_TO",
        "EXECUTES_SQL",
        "DEFINES_STATEMENT",
        "INCLUDES_FRAGMENT",
        "READS",
        "REMOTE_READS",
        "READS_FROM",
        "READS_COLUMN",
        "INSERTS",
        "UPDATES",
        "DELETES",
        "MERGES",
        "WRITES",
        "WRITES_TO",
        "WRITES_COLUMN",
        "LOADS_INTO",
        "POPULATES",
        "USES_SEQUENCE",
    }
)
_DEFAULT_ENTRY_TYPES = frozenset(
    {
        "SCREEN",
        "ROUTE",
        "UI_ACTION",
        "API_OPERATION",
        "JOB",
        "COMMAND_MODE",
        "EXECUTABLE_ENTRY_POINT",
        "TRIGGER",
    }
)
_FLOW_TARGET_TYPES = frozenset(
    {
        "TABLE",
        "VIEW",
        "MATERIALIZED_VIEW",
        "COLUMN",
        "DATA_FILE",
        "EXTERNAL_API_OPERATION",
        "EXTERNAL_SYSTEM",
        "UNRESOLVED_REFERENCE",
    }
)
_SEMANTIC_IO_TYPES = frozenset(
    {
        "API_OPERATION",
        "JOB",
        "COMMAND_MODE",
        "EXECUTABLE_ENTRY_POINT",
        "PROCEDURE",
        "FUNCTION",
        "LOCAL_ROUTINE",
        "METHOD",
        "MAPPER_STATEMENT",
        "SQL_STATEMENT",
        "INLINE_SQL",
        "LOADER_CONTROL",
    }
)
_ROUTE_PARAMETER = re.compile(r"\{(?P<name>[^/{}:]+)(?::[^/{}]+)?\}")


def publish_v3(
    output: Path,
    *,
    catalog: CatalogImportResult | None,
    config: dict[str, object],
) -> None:
    options = config.get("enrichment")
    enrichment = options if isinstance(options, dict) else {}
    if enrichment.get("enabled", True) is False:
        return
    database_path = output / "graph.sqlite"
    manifest_path = output / "manifest.json"
    if not database_path.is_file() or not manifest_path.is_file():
        raise ValueError("V3 publisher requires graph.sqlite and manifest.json")
    max_depth = _positive_int(enrichment.get("flowMaxDepth"), 8, "flowMaxDepth")
    max_targets = _positive_int(
        enrichment.get("maxFlowTargetsPerEntry"), 100, "maxFlowTargetsPerEntry"
    )
    minimum_confidence = _confidence(
        enrichment.get("minimumResolutionConfidence", 0.85)
    )
    configured_entries = enrichment.get("flowEntryTypes")
    entry_types = (
        frozenset(str(item).upper() for item in configured_entries)
        if isinstance(configured_entries, list) and configured_entries
        else _DEFAULT_ENTRY_TYPES
    )

    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        _create_v3_schema(connection)
        _insert_catalog_files(connection, catalog)
        _materialize_io(connection)
        _materialize_resolution_candidates(connection, minimum_confidence)
        _materialize_flows(
            connection,
            entry_types=entry_types,
            max_depth=max_depth,
            max_targets=max_targets,
        )
        metrics = _materialize_quality(connection, catalog)
        connection.execute("PRAGMA user_version = 3")
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            ("contract_version", json.dumps("3.0")),
        )
        connection.execute(
            "INSERT OR REPLACE INTO metadata(key, value_json) VALUES (?, ?)",
            (
                "v3_capabilities",
                json.dumps(
                    [
                        "catalog-auto-import",
                        "input-output",
                        "resolution-candidates",
                        "materialized-flows",
                        "quality-metrics",
                    ]
                ),
            ),
        )
        violations = list(connection.execute("PRAGMA foreign_key_check"))
        if violations:
            raise ValueError(f"V3 SQLite foreign key validation failed: {violations[:5]}")
        connection.execute("ANALYZE")
        connection.commit()

    _write_quality_reports(output, metrics, catalog)
    _upgrade_manifest(manifest_path, database_path, metrics, catalog)


def _create_v3_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE catalog_files (
            catalog_file_id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL UNIQUE,
            path TEXT NOT NULL UNIQUE,
            catalog_type TEXT NOT NULL,
            database_key TEXT NOT NULL,
            profile_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            bytes INTEGER NOT NULL,
            rows_read INTEGER NOT NULL,
            rows_imported INTEGER NOT NULL,
            rows_rejected INTEGER NOT NULL,
            status TEXT NOT NULL,
            message TEXT NOT NULL
        );
        CREATE TABLE io_items (
            io_id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL UNIQUE,
            owner_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            direction TEXT NOT NULL CHECK (direction IN ('INPUT', 'OUTPUT', 'INOUT')),
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            data_type TEXT NOT NULL,
            ordinal INTEGER,
            required INTEGER NOT NULL CHECK (required IN (0, 1)),
            confidence REAL NOT NULL,
            properties_json TEXT NOT NULL
        );
        CREATE TABLE io_links (
            io_link_id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL UNIQUE,
            io_id INTEGER NOT NULL REFERENCES io_items(io_id) ON DELETE CASCADE,
            target_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            mapping_type TEXT NOT NULL,
            confidence REAL NOT NULL,
            properties_json TEXT NOT NULL
        );
        CREATE TABLE resolution_candidates (
            resolution_candidate_id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL UNIQUE,
            reference_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            candidate_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            rank INTEGER NOT NULL,
            score REAL NOT NULL,
            status TEXT NOT NULL,
            reasons_json TEXT NOT NULL
        );
        CREATE TABLE flows (
            flow_id INTEGER PRIMARY KEY,
            stable_id TEXT NOT NULL UNIQUE,
            flow_type TEXT NOT NULL,
            start_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            target_node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            confidence REAL NOT NULL,
            complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
            properties_json TEXT NOT NULL
        );
        CREATE TABLE flow_steps (
            flow_id INTEGER NOT NULL REFERENCES flows(flow_id) ON DELETE CASCADE,
            step_index INTEGER NOT NULL,
            node_id INTEGER NOT NULL REFERENCES nodes(node_id),
            incoming_edge_id INTEGER REFERENCES edges(edge_id),
            PRIMARY KEY (flow_id, step_index)
        ) WITHOUT ROWID;
        CREATE TABLE flow_io (
            flow_id INTEGER NOT NULL REFERENCES flows(flow_id) ON DELETE CASCADE,
            io_id INTEGER NOT NULL REFERENCES io_items(io_id) ON DELETE CASCADE,
            direction TEXT NOT NULL CHECK (direction IN ('INPUT', 'OUTPUT', 'INOUT')),
            PRIMARY KEY (flow_id, io_id)
        ) WITHOUT ROWID;
        CREATE TABLE quality_metrics (
            scope_type TEXT NOT NULL,
            scope_key TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            metric_value REAL NOT NULL,
            details_json TEXT NOT NULL,
            PRIMARY KEY (scope_type, scope_key, metric_name)
        ) WITHOUT ROWID;
        CREATE INDEX idx_catalog_files_type ON catalog_files(catalog_type, database_key);
        CREATE INDEX idx_io_owner ON io_items(owner_node_id, direction);
        CREATE INDEX idx_io_links_target ON io_links(target_node_id);
        CREATE INDEX idx_resolution_reference ON resolution_candidates(reference_node_id, rank);
        CREATE INDEX idx_resolution_candidate ON resolution_candidates(candidate_node_id);
        CREATE INDEX idx_flows_start ON flows(start_node_id);
        CREATE INDEX idx_flows_target ON flows(target_node_id);
        CREATE INDEX idx_flow_steps_node ON flow_steps(node_id);
        """
    )


def _insert_catalog_files(
    connection: sqlite3.Connection, catalog: CatalogImportResult | None
) -> None:
    if not catalog:
        return
    connection.executemany(
        """
        INSERT INTO catalog_files VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                _numeric_id("catalog-file", f"catalog-file:{report.sha256}:{report.path}"),
                f"catalog-file:{report.sha256}:{report.path}",
                report.path,
                report.catalog_type,
                report.database_key,
                report.profile_name,
                report.sha256,
                report.bytes,
                report.rows_read,
                report.rows_imported,
                report.rows_rejected,
                report.status,
                report.message,
            )
            for report in catalog.files
        ],
    )


def _materialize_io(connection: sqlite3.Connection) -> None:
    nodes = {
        int(row["node_id"]): row
        for row in connection.execute(
            "SELECT node_id, stable_id, node_type, technical_name, qualified_name, "
            "confidence, properties_json FROM nodes"
        )
    }
    edge_rows = list(
        connection.execute(
            "SELECT edge_id, stable_id, source_node_id, target_node_id, edge_type, "
            "confidence, properties_json FROM edges"
        )
    )
    for edge in edge_rows:
        edge_type = str(edge["edge_type"])
        direction = kind = mapping_type = ""
        if edge_type in _READ_EDGES:
            direction = "INPUT"
            kind = _node_io_kind(nodes.get(int(edge["target_node_id"])), edge_type)
            mapping_type = "READS_FROM"
        elif edge_type in _WRITE_EDGES:
            direction = "OUTPUT"
            kind = _node_io_kind(nodes.get(int(edge["target_node_id"])), edge_type)
            mapping_type = "WRITES_TO"
        elif edge_type == "CALLS_API":
            direction, kind, mapping_type = "OUTPUT", "API_REQUEST", "CALLS"
        elif edge_type in {"TRIGGERS", "TRIGGERS_JOB"}:
            direction, kind, mapping_type = "OUTPUT", "JOB_EVENT", "TRIGGERS"
        elif edge_type == "RETURNS":
            direction, kind, mapping_type = "OUTPUT", "RETURN_VALUE", "RETURNS_AS"
        if not direction:
            continue
        owner_id = int(edge["source_node_id"])
        target_id = int(edge["target_node_id"])
        target = nodes.get(target_id)
        if not target or owner_id not in nodes:
            continue
        name = str(target["qualified_name"] or target["technical_name"])
        stable = _io_stable(
            owner_id,
            direction,
            kind,
            name,
            str(edge["stable_id"]),
        )
        io_id = _numeric_id("io", stable)
        confidence = float(edge["confidence"])
        connection.execute(
            """
            INSERT OR IGNORE INTO io_items VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?)
            """,
            (
                io_id,
                stable,
                owner_id,
                direction,
                kind,
                name,
                "",
                confidence,
                json.dumps(
                    {
                        "edge_id": str(edge["edge_id"]),
                        "edge_type": edge_type,
                        "provenance": _edge_provenance(edge),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        link_stable = f"io-link:{stable}:{target_id}:{mapping_type}"
        connection.execute(
            """
            INSERT OR IGNORE INTO io_links VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _numeric_id("io-link", link_stable),
                link_stable,
                io_id,
                target_id,
                mapping_type,
                confidence,
                "{}",
            ),
        )

    for node_id, node in nodes.items():
        node_type = str(node["node_type"])
        if node_type not in _SEMANTIC_IO_TYPES:
            continue
        properties = _json_object(node["properties_json"])
        semantic = properties.get("semantic_tree")
        if not isinstance(semantic, dict):
            semantic = {}
        parameters = semantic.get("parameters")
        if isinstance(parameters, list):
            for ordinal, parameter in enumerate(parameters, 1):
                _insert_semantic_parameter(
                    connection, node_id, node_type, parameter, ordinal
                )
        route = str(properties.get("route") or properties.get("path") or "")
        for ordinal, match in enumerate(_ROUTE_PARAMETER.finditer(route), 1):
            _insert_io_item(
                connection,
                owner_node_id=node_id,
                direction="INPUT",
                kind="ROUTE_PARAM",
                name=match.group("name"),
                data_type="",
                ordinal=ordinal,
                required=True,
                confidence=1.0,
                discriminator=f"route:{route}:{ordinal}",
                properties={"route": route, "provenance": "EXTRACTED"},
            )
        return_type = str(
            semantic.get("return_type")
            or properties.get("return_type")
            or properties.get("response_type")
            or ""
        ).strip()
        if return_type:
            _insert_io_item(
                connection,
                owner_node_id=node_id,
                direction="OUTPUT",
                kind="RETURN_VALUE",
                name="return",
                data_type=return_type,
                ordinal=None,
                required=False,
                confidence=float(node["confidence"]),
                discriminator=f"return:{return_type}",
                properties={"provenance": "EXTRACTED"},
            )


def _insert_semantic_parameter(
    connection: sqlite3.Connection,
    owner_node_id: int,
    node_type: str,
    parameter: object,
    ordinal: int,
) -> None:
    if isinstance(parameter, str):
        name, data_type, raw_direction, required = parameter, "", "IN", False
        properties: dict[str, object] = {}
    elif isinstance(parameter, dict):
        name = str(parameter.get("name") or parameter.get("label") or f"arg{ordinal}")
        data_type = str(parameter.get("type") or parameter.get("data_type") or "")
        raw_direction = str(parameter.get("direction") or "IN").upper()
        required = not bool(parameter.get("default") or parameter.get("optional"))
        properties = dict(parameter)
    else:
        return
    direction = {
        "OUT": "OUTPUT",
        "REF": "INOUT",
        "INOUT": "INOUT",
        "IN OUT": "INOUT",
    }.get(raw_direction, "INPUT")
    kind = (
        "PLSQL_PARAMETER"
        if node_type in {"PROCEDURE", "FUNCTION", "LOCAL_ROUTINE"}
        else "ARGUMENT"
    )
    properties.setdefault("provenance", "EXTRACTED")
    _insert_io_item(
        connection,
        owner_node_id=owner_node_id,
        direction=direction,
        kind=kind,
        name=name,
        data_type=data_type,
        ordinal=ordinal,
        required=required,
        confidence=1.0,
        discriminator=f"parameter:{ordinal}:{name}:{raw_direction}",
        properties=properties,
    )


def _insert_io_item(
    connection: sqlite3.Connection,
    *,
    owner_node_id: int,
    direction: str,
    kind: str,
    name: str,
    data_type: str,
    ordinal: int | None,
    required: bool,
    confidence: float,
    discriminator: str,
    properties: dict[str, object],
) -> int:
    stable = _io_stable(
        owner_node_id, direction, kind, name, discriminator
    )
    io_id = _numeric_id("io", stable)
    connection.execute(
        """
        INSERT OR IGNORE INTO io_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            io_id,
            stable,
            owner_node_id,
            direction,
            kind,
            name,
            data_type,
            ordinal,
            int(required),
            confidence,
            json.dumps(properties, sort_keys=True, separators=(",", ":")),
        ),
    )
    return io_id


def _materialize_resolution_candidates(
    connection: sqlite3.Connection, minimum_confidence: float
) -> None:
    candidates = list(
        connection.execute(
            """
            SELECT node_id, node_type, technical_name, qualified_name, database_key,
                   properties_json
            FROM nodes
            WHERE node_type IN (
                'TABLE', 'VIEW', 'MATERIALIZED_VIEW', 'COLUMN',
                'PROCEDURE', 'FUNCTION', 'LOCAL_ROUTINE'
            )
            """
        )
    )
    references = list(
        connection.execute(
            """
            SELECT node_id, technical_name, qualified_name, database_key, properties_json
            FROM nodes
            WHERE node_type IN ('UNRESOLVED_REFERENCE', 'EXTERNAL_DATABASE_OBJECT')
            """
        )
    )
    for reference in references:
        ref_properties = _json_object(reference["properties_json"])
        ref_name = _reference_name(reference, ref_properties)
        ref_database = str(
            reference["database_key"] or ref_properties.get("database") or ""
        ).upper()
        ref_schema = str(
            ref_properties.get("schema") or ref_properties.get("owner") or ""
        ).upper()
        scored: list[tuple[float, sqlite3.Row, list[str]]] = []
        for candidate in candidates:
            candidate_properties = _json_object(candidate["properties_json"])
            candidate_name = _candidate_name(candidate, candidate_properties)
            if not ref_name or candidate_name != ref_name:
                continue
            score = 0.65
            reasons = ["same_name"]
            candidate_database = str(
                candidate["database_key"]
                or candidate_properties.get("database")
                or ""
            ).upper()
            candidate_schema = str(
                candidate_properties.get("schema")
                or candidate_properties.get("owner")
                or ""
            ).upper()
            if ref_database and candidate_database == ref_database:
                score += 0.2
                reasons.append("same_database")
            if ref_schema and candidate_schema == ref_schema:
                score += 0.1
                reasons.append("same_schema")
            if str(candidate["qualified_name"]).upper().endswith(
                str(reference["qualified_name"]).upper()
            ):
                score += 0.05
                reasons.append("qualified_suffix")
            scored.append((min(score, 1.0), candidate, reasons))
        scored.sort(key=lambda item: (-item[0], int(item[1]["node_id"])))
        for rank, (score, candidate, reasons) in enumerate(scored[:10], 1):
            stable = (
                f"resolution-candidate:{reference['node_id']}:"
                f"{candidate['node_id']}:{rank}"
            )
            connection.execute(
                """
                INSERT INTO resolution_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _numeric_id("resolution-candidate", stable),
                    stable,
                    int(reference["node_id"]),
                    int(candidate["node_id"]),
                    rank,
                    score,
                    "eligible" if score >= minimum_confidence else "observe",
                    json.dumps(reasons, separators=(",", ":")),
                ),
            )


def _materialize_flows(
    connection: sqlite3.Connection,
    *,
    entry_types: frozenset[str],
    max_depth: int,
    max_targets: int,
) -> None:
    nodes = {
        int(row["node_id"]): row
        for row in connection.execute(
            "SELECT node_id, stable_id, node_type, confidence FROM nodes"
        )
    }
    adjacency: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for edge in connection.execute(
        "SELECT edge_id, source_node_id, target_node_id, edge_type, confidence FROM edges"
    ):
        if str(edge["edge_type"]) in _FLOW_EDGES:
            adjacency[int(edge["source_node_id"])].append(edge)
    for values in adjacency.values():
        values.sort(key=lambda row: (str(row["edge_type"]), int(row["target_node_id"])))

    io_by_owner: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for row in connection.execute("SELECT io_id, owner_node_id, direction FROM io_items"):
        io_by_owner[int(row["owner_node_id"])].append(
            (int(row["io_id"]), str(row["direction"]))
        )

    for start_id, start in sorted(nodes.items()):
        if str(start["node_type"]) not in entry_types:
            continue
        predecessor: dict[int, tuple[int, sqlite3.Row]] = {}
        depths = {start_id: 0}
        queue = deque([start_id])
        targets: list[int] = []
        while queue and len(targets) < max_targets:
            current = queue.popleft()
            depth = depths[current]
            if depth >= max_depth:
                continue
            for edge in adjacency.get(current, ()):
                target_id = int(edge["target_node_id"])
                if target_id in depths:
                    continue
                depths[target_id] = depth + 1
                predecessor[target_id] = current, edge
                target = nodes.get(target_id)
                if not target:
                    continue
                if str(target["node_type"]) in _FLOW_TARGET_TYPES:
                    targets.append(target_id)
                    if len(targets) >= max_targets:
                        break
                else:
                    queue.append(target_id)
        for target_id in targets:
            node_path, edge_path = _reconstruct_path(
                start_id, target_id, predecessor
            )
            if not node_path:
                continue
            edge_stables = [str(edge["edge_id"]) for edge in edge_path]
            identity = "|".join(
                (str(start_id), str(target_id), *edge_stables)
            )
            stable = "flow:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
            flow_id = _numeric_id("flow", stable)
            confidence = min(
                [float(start["confidence"]), *(float(edge["confidence"]) for edge in edge_path)]
            )
            target_type = str(nodes[target_id]["node_type"])
            flow_type = _flow_type(str(start["node_type"]))
            complete = target_type != "UNRESOLVED_REFERENCE"
            connection.execute(
                """
                INSERT INTO flows VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    flow_id,
                    stable,
                    flow_type,
                    start_id,
                    target_id,
                    confidence,
                    int(complete),
                    json.dumps(
                        {"depth": len(edge_path), "target_type": target_type},
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                ),
            )
            for index, node_id in enumerate(node_path):
                incoming = int(edge_path[index - 1]["edge_id"]) if index else None
                connection.execute(
                    "INSERT INTO flow_steps VALUES (?, ?, ?, ?)",
                    (flow_id, index, node_id, incoming),
                )
                for io_id, direction in io_by_owner.get(node_id, ()):
                    connection.execute(
                        "INSERT OR IGNORE INTO flow_io VALUES (?, ?, ?)",
                        (flow_id, io_id, direction),
                    )


def _reconstruct_path(
    start_id: int,
    target_id: int,
    predecessor: dict[int, tuple[int, sqlite3.Row]],
) -> tuple[list[int], list[sqlite3.Row]]:
    nodes = [target_id]
    edges: list[sqlite3.Row] = []
    current = target_id
    while current != start_id:
        previous = predecessor.get(current)
        if previous is None:
            return [], []
        parent, edge = previous
        nodes.append(parent)
        edges.append(edge)
        current = parent
    nodes.reverse()
    edges.reverse()
    return nodes, edges


def _materialize_quality(
    connection: sqlite3.Connection, catalog: CatalogImportResult | None
) -> dict[str, object]:
    metrics: dict[str, object] = {}
    count_queries = {
        "nodes": "SELECT COUNT(*) FROM nodes",
        "edges": "SELECT COUNT(*) FROM edges",
        "evidence": "SELECT COUNT(*) FROM evidence",
        "issues": "SELECT COUNT(*) FROM issues",
        "ioItems": "SELECT COUNT(*) FROM io_items",
        "ioLinks": "SELECT COUNT(*) FROM io_links",
        "flows": "SELECT COUNT(*) FROM flows",
        "incompleteFlows": "SELECT COUNT(*) FROM flows WHERE complete = 0",
        "resolutionCandidates": "SELECT COUNT(*) FROM resolution_candidates",
        "unresolvedReferences": (
            "SELECT COUNT(*) FROM nodes WHERE node_type = 'UNRESOLVED_REFERENCE'"
        ),
        "ambiguousIssues": (
            "SELECT COUNT(*) FROM issues WHERE issue_type LIKE 'AMBIGUOUS_%'"
        ),
        "lowConfidenceEdges": "SELECT COUNT(*) FROM edges WHERE confidence < 0.8",
    }
    for name, statement in count_queries.items():
        value = int(connection.execute(statement).fetchone()[0])
        metrics[name] = value
        _put_metric(connection, "GLOBAL", "global", name, value)
    if catalog:
        catalog_metrics = {
            "catalogFiles": len(catalog.files),
            "catalogTables": len(catalog.tables),
            "catalogColumns": len(catalog.columns),
            "catalogRejectedFiles": sum(
                report.status == "rejected" for report in catalog.files
            ),
            "catalogRejectedRows": sum(
                report.rows_rejected for report in catalog.files
            ),
        }
        for name, value in catalog_metrics.items():
            metrics[name] = value
            _put_metric(connection, "GLOBAL", "global", name, value)
    for row in connection.execute(
        """
        SELECT package_key, COUNT(*) AS value
        FROM nodes
        GROUP BY package_key
        ORDER BY package_key
        """
    ):
        _put_metric(
            connection,
            "SOURCE",
            str(row["package_key"]),
            "nodes",
            int(row["value"]),
        )
    for row in connection.execute(
        """
        SELECT package_key, COUNT(*) AS value
        FROM issues
        GROUP BY package_key
        ORDER BY package_key
        """
    ):
        _put_metric(
            connection,
            "SOURCE",
            str(row["package_key"]),
            "issues",
            int(row["value"]),
        )
    return metrics


def _put_metric(
    connection: sqlite3.Connection,
    scope_type: str,
    scope_key: str,
    metric_name: str,
    value: int | float,
    details: dict[str, object] | None = None,
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO quality_metrics VALUES (?, ?, ?, ?, ?)",
        (
            scope_type,
            scope_key,
            metric_name,
            float(value),
            json.dumps(details or {}, sort_keys=True, separators=(",", ":")),
        ),
    )


def _write_quality_reports(
    output: Path,
    metrics: dict[str, object],
    catalog: CatalogImportResult | None,
) -> None:
    report = {
        "contractVersion": "3.0",
        "metrics": metrics,
        "catalogFiles": [report.__dict__ for report in catalog.files] if catalog else [],
        "catalogIssues": catalog.issues if catalog else [],
    }
    (output / "quality-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    lines = ["# Extraction Quality Report", ""]
    for name, value in sorted(metrics.items()):
        lines.append(f"- {name}: {value}")
    if catalog and catalog.issues:
        lines.extend(("", "## Catalog Issues", ""))
        for issue in catalog.issues[:100]:
            lines.append(
                f"- {issue.get('issue_type')}: {issue.get('message')} "
                f"({issue.get('source_path', '')})"
            )
    (output / "QUALITY_REPORT.md").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


def _upgrade_manifest(
    manifest_path: Path,
    database_path: Path,
    metrics: dict[str, object],
    catalog: CatalogImportResult | None,
) -> None:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["contractVersion"] = "3.0"
    extractor = manifest.setdefault("extractor", {})
    extractor["version"] = "3.0.0"
    files = manifest.setdefault("files", {})
    files["qualityReport"] = "quality-report.json"
    files["qualityReportMarkdown"] = "QUALITY_REPORT.md"
    statistics = manifest.setdefault("statistics", {})
    statistics.update(metrics)
    metadata = manifest.setdefault("metadata", {})
    metadata["capabilities"] = [
        "catalog-auto-import",
        "input-output",
        "resolution-candidates",
        "materialized-flows",
        "quality-metrics",
    ]
    metadata["catalogFileCount"] = len(catalog.files) if catalog else 0
    checksums = manifest.setdefault("checksums", {})
    for relative in ("graph.sqlite", "quality-report.json", "QUALITY_REPORT.md"):
        path = manifest_path.parent / relative
        checksums[relative] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _node_io_kind(node: sqlite3.Row | None, edge_type: str) -> str:
    node_type = str(node["node_type"]) if node else ""
    if node_type == "COLUMN" or edge_type.endswith("_COLUMN"):
        return "DATABASE_COLUMN"
    if node_type in {"TABLE", "VIEW", "MATERIALIZED_VIEW"}:
        return "DATABASE_TABLE"
    if node_type == "DATA_FILE":
        return "DATA_FILE"
    return "DATA_OBJECT"


def _edge_provenance(edge: sqlite3.Row) -> str:
    properties = _json_object(edge["properties_json"])
    value = properties.get("provenance") or properties.get("resolution")
    if value:
        return str(value).upper()
    return "EXTRACTED" if float(edge["confidence"]) >= 1.0 else "INFERRED"


def _reference_name(
    row: sqlite3.Row, properties: dict[str, object]
) -> str:
    value = (
        properties.get("column")
        or properties.get("table")
        or properties.get("object_name")
        or properties.get("object")
        or properties.get("routine")
        or row["technical_name"]
    )
    return str(value or "").split("@", 1)[0].split(".")[-1].strip('"').upper()


def _candidate_name(
    row: sqlite3.Row, properties: dict[str, object]
) -> str:
    value = (
        properties.get("column")
        if str(row["node_type"]) == "COLUMN"
        else properties.get("table")
        or properties.get("object_name")
        or properties.get("routine")
        or row["technical_name"]
    )
    return str(value or "").split(".")[-1].strip('"').upper()


def _flow_type(node_type: str) -> str:
    if node_type in {"SCREEN", "ROUTE", "UI_ACTION"}:
        return "UI"
    if node_type == "API_OPERATION":
        return "API"
    if node_type in {"JOB", "COMMAND_MODE", "EXECUTABLE_ENTRY_POINT"}:
        return "BATCH"
    return "DATABASE"


def _io_stable(
    owner_node_id: int,
    direction: str,
    kind: str,
    name: str,
    discriminator: str,
) -> str:
    identity = "|".join(
        (str(owner_node_id), direction, kind, name, discriminator)
    )
    return "io:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _numeric_id(kind: str, stable_id: str) -> int:
    payload = f"{kind}|{stable_id}|0".encode("utf-8")
    value = int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest(), "big")
    return (value & ((1 << 63) - 1)) or 1


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _positive_int(value: object, default: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(f"enrichment.{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"enrichment.{name} must be a positive integer") from exc
    if result < 1:
        raise ValueError(f"enrichment.{name} must be a positive integer")
    return result


def _confidence(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("minimumResolutionConfidence must be numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("minimumResolutionConfidence must be numeric") from exc
    if not 0.0 <= result <= 1.0:
        raise ValueError("minimumResolutionConfidence must be between 0 and 1")
    return result
